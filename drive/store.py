from __future__ import annotations

import hashlib
import hmac
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
from stat import S_ISDIR
import threading
import time
import unicodedata
import uuid
from .upload_integrity import verified_fingerprint
from contextlib import ExitStack, contextmanager
from weakref import WeakValueDictionary


OFFICE_FILE = re.compile(r'.+\.(?:docx?|docm|xlsx?|xlsm|xlsb|pptx?|pptm|odt|ods|odp|rtf|wps|et|dps)$', re.IGNORECASE)


class DriveError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


def password_hash(value: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(value.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


def password_ok(value: str, encoded: str) -> bool:
    try:
        _, salt, digest = encoded.split("$")
        actual = hashlib.scrypt(value.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, digest)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def valid_name(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    if (not value or len(value) > 180 or value in (".", "..")
            or value[-1] in " ." or any(ord(c) < 32 or c in '/\\:*?"<>|' for c in value)
            or re.match(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)", value, re.I)):
        raise DriveError(400, "名称无效：请避免特殊符号、保留名称及结尾空格")
    return value


def clean_path(value: str) -> str:
    if len(value) > 1800 or value.startswith(("/", "\\")) or "\\" in value:
        raise DriveError(400, "文件路径无效")
    parts = value.split("/") if value else []
    return "/".join(valid_name(part) for part in parts)


def signature(path: Path) -> str:
    if not path.exists():
        return "missing"
    s = path.stat()
    return f"{s.st_ino}:{s.st_size}:{s.st_mtime_ns}"


class Store:
    def __init__(self, data_dir: str | Path, state_dir: str | Path | None = None):
        self.data = Path(data_dir).resolve()
        self.state = Path(state_dir).resolve() if state_dir else self.data / "state"
        self.files, self.tmp, self.trash = (self.data / p for p in ("files", "uploads", "trash"))
        for p in (self.data, self.state, self.files, self.tmp, self.trash):
            p.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state / "drive.sqlite3"
        self.mutex = threading.RLock()
        self._read_connections = threading.local()
        self._file_locks = WeakValueDictionary()
        self._file_locks_guard = threading.Lock()
        self.max_file = int(os.environ.get("DRIVE_MAX_FILE_GB", "100")) * 1024**3
        self.lock_checker = None
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    name TEXT NOT NULL, password TEXT NOT NULL,
                    admin INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS member_managers(
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS groups(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    description TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS members(
                    group_id TEXT REFERENCES groups(id), user_id TEXT REFERENCES users(id),
                    role TEXT CHECK(role IN ('reader','editor')) NOT NULL,
                    PRIMARY KEY(group_id,user_id));
                CREATE TABLE IF NOT EXISTS sessions(
                    hash TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id),
                    csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tokens(
                    id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id),
                    name TEXT NOT NULL, hash TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS token_user ON tokens(user_id);
                CREATE TABLE IF NOT EXISTS uploads(
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    path TEXT NOT NULL, size INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                    target_signature TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS trash(
                    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, path TEXT NOT NULL,
                    deleted REAL NOT NULL, actor TEXT NOT NULL, is_dir INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT,
                    group_id TEXT, action TEXT NOT NULL, path TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS events_by_library ON events(group_id,id DESC);
                CREATE TABLE IF NOT EXISTS blocked_office_writes(
                    id INTEGER PRIMARY KEY, created REAL NOT NULL, account TEXT NOT NULL,
                    device_name TEXT, storage TEXT NOT NULL, library_id TEXT NOT NULL,
                    path TEXT NOT NULL, entry_point TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS trash_by_library ON trash(group_id,deleted DESC);
                CREATE TABLE IF NOT EXISTS entry_owners(
                    library_id TEXT NOT NULL, path TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES users(id),
                    PRIMARY KEY(library_id,path));
                CREATE TABLE IF NOT EXISTS trash_entry_owners(
                    trash_id TEXT NOT NULL REFERENCES trash(id) ON DELETE CASCADE,
                    path TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES users(id),
                    PRIMARY KEY(trash_id,path));
            """)
            event_columns = {row[1] for row in db.execute('PRAGMA table_info(events)')}
            if 'entry_created' not in event_columns:
                db.execute('ALTER TABLE events ADD COLUMN entry_created INTEGER')
            if 'previous_path' not in event_columns:
                db.execute('ALTER TABLE events ADD COLUMN previous_path TEXT')
            columns = {row[1] for row in db.execute('PRAGMA table_info(users)')}
            if 'seafile_identity' not in columns:
                db.execute('ALTER TABLE users ADD COLUMN seafile_identity TEXT')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_seafile_identity ON users(seafile_identity)')
            db.execute('CREATE TABLE IF NOT EXISTS seafile_groups('
                       'group_id TEXT PRIMARY KEY REFERENCES groups(id), external_id INTEGER NOT NULL UNIQUE)')
            db.execute('CREATE TABLE IF NOT EXISTS identity_migrations('
                       'user_id TEXT PRIMARY KEY REFERENCES users(id), contact_email TEXT NOT NULL UNIQUE)')
        key = self.state / "setup.key"
        if not self.initialized() and not key.exists():
            try:
                with open(key, "x", encoding="utf-8") as f:
                    f.write(secrets.token_urlsafe(24))
                os.chmod(key, 0o600)
            except FileExistsError:
                pass
        self.dummy_hash = password_hash(secrets.token_urlsafe(20))

    @contextmanager
    def file_locks(self, *library_ids):
        # Acquire cross-library locks in one order, including WebDAV moves.
        with self._file_locks_guard:
            locks = []
            for lid in sorted(set(library_ids)):
                lock = self._file_locks.get(lid)
                if lock is None:
                    lock = threading.RLock()
                    self._file_locks[lid] = lock
                locks.append(lock)
        with ExitStack() as stack:
            for lock in locks:
                stack.enter_context(lock)
            yield

    @contextmanager
    def db(self):
        shared = getattr(getattr(self, '_read_connections', None), 'connection', None)
        if shared is not None:
            with shared:
                yield shared
            return
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def reuse_read_connection(self):
        """Reuse a connection within one synchronous read operation on this thread."""
        if getattr(self._read_connections, 'connection', None) is not None:
            yield
            return
        with self.db() as db:
            self._read_connections.connection = db
            try:
                yield
            finally:
                del self._read_connections.connection

    def one(self, sql, params=()):
        with self.db() as db:
            row = db.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql, params=()):
        with self.db() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    def initialized(self):
        return self.one("SELECT id FROM users LIMIT 1") is not None

    def create_user(self, username, name, password, admin=False):
        username = unicodedata.normalize("NFC", username).strip()
        if not re.fullmatch(r"[\w.-]{2,40}", username):
            raise DriveError(400, "账号使用 2–40 位中文、字母、数字、点、横线或下划线")
        if not 6 <= len(password) <= 200:
            raise DriveError(400, "密码至少 6 位即可")
        if not name.strip() or len(name) > 60:
            raise DriveError(400, "请填写 1–60 字的姓名")
        uid = uuid.uuid4().hex
        try:
            with self.db() as db:
                db.execute("INSERT INTO users(id,username,name,password,admin,active,created) VALUES(?,?,?,?,?,?,?)",
                           (uid, username, name.strip(), password_hash(password), int(admin), 1, time.time()))
        except sqlite3.IntegrityError:
            raise DriveError(409, "账号已经存在")
        return self.public_user(self.one("SELECT * FROM users WHERE id=?", (uid,)))

    @staticmethod
    def public_user(user):
        result = {k: user[k] for k in ("id", "username", "name", "admin", "active")}
        if user.get('seafile_identity'):
            result['identity_source'] = 'seafile'
        return result

    def authenticate(self, username, password):
        username = unicodedata.normalize("NFC", username).strip()
        user = self.one("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,))
        ok = password_ok(password, user["password"] if user else self.dummy_hash)
        return self.public_user(user) if ok and user and user["active"] and not user.get('seafile_identity') else None

    def get_user(self, uid):
        user = self.one("SELECT * FROM users WHERE id=? AND active=1", (uid,))
        if not user:
            raise DriveError(401, "请重新登录")
        return self.public_user(user)

    def group(self, gid):
        group = self.one("SELECT * FROM groups WHERE id=?", (gid,))
        if not group:
            raise DriveError(404, "分组不存在")
        return group

    def access(self, user, gid, write=False):
        group = self.group(gid)
        # Fetch current status and memberships for every operation, including DAV.
        user = self.get_user(user["id"])
        role = "editor" if user["admin"] else (self.one(
            "SELECT role FROM members WHERE group_id=? AND user_id=?", (gid, user["id"])) or {}).get("role")
        if not role or (write and role != "editor"):
            raise DriveError(403, "没有该分组的写入权限" if write else "没有该分组的访问权限")
        return group, role

    def groups_for(self, user):
        groups = self.all("SELECT * FROM groups ORDER BY created") if user["admin"] else self.all(
            "SELECT g.* FROM groups g JOIN members m ON g.id=m.group_id WHERE m.user_id=? ORDER BY g.created",
            (user["id"],))
        for g in groups:
            _, g["role"] = self.access(user, g["id"])
            g["members"] = self.one("SELECT COUNT(*) n FROM members WHERE group_id=?", (g["id"],))["n"]
        return groups

    def create_group(self, name, description):
        name = valid_name(name.strip())
        if len(name) > 50 or len(description) > 240:
            raise DriveError(400, "分组名最多 50 字，说明最多 240 字")
        gid = uuid.uuid4().hex
        with self.mutex:
            try:
                with self.db() as db:
                    db.execute("INSERT INTO groups VALUES(?,?,?,?)", (gid, name, description, time.time()))
                    (self.files / gid).mkdir()
            except sqlite3.IntegrityError:
                raise DriveError(409, "分组名称已存在")
        return self.group(gid)

    def path(self, gid, rel="", must_exist=False):
        rel = clean_path(rel)
        root = self.files / gid
        p = root
        for name in rel.split("/") if rel else []:
            p /= name
            if p.is_symlink() or p.is_junction():
                raise DriveError(403, "不允许访问符号链接或目录联接")
        if root.is_symlink() or root.is_junction() or not p.resolve().is_relative_to(root.resolve()):
            raise DriveError(403, "文件路径超出共享范围")
        if must_exist and not p.exists():
            raise DriveError(404, "文件不存在或已被移动")
        return p

    def check_unlocked(self, gid, rel):
        if self.lock_checker:
            self.lock_checker(gid, rel)

    def entry(self, gid, p, *, rel=None, stat=None):
        s = stat if stat is not None else p.stat()
        is_dir = S_ISDIR(s.st_mode)
        return dict(name=p.name, path=rel if rel is not None else p.relative_to(self.files / gid).as_posix(),
                    is_dir=is_dir, size=0 if is_dir else s.st_size,
                    modified=s.st_mtime, etag=f"{s.st_ino}:{s.st_size}:{s.st_mtime_ns}")

    def listing(self, user, gid, rel="", query=""):
        self.access(user, gid)
        p = self.path(gid, rel, True)
        if not p.is_dir():
            raise DriveError(400, "该路径不是文件夹")
        entries = []
        for child in p.iterdir():
            if child.is_symlink() or child.is_junction():
                continue
            try:
                valid_name(child.name)
                if query.casefold() in child.name.casefold():
                    entries.append(self.entry(gid, child))
            except (FileNotFoundError, DriveError):
                continue
        return sorted(entries, key=lambda x: (not x["is_dir"], x["name"].casefold()))

    def event(self, user, gid, action, path, *, created_entry=False, anonymous=False, previous_path=None, db=None):
        if db is None:
            with self.db() as connection:
                self.event(user, gid, action, path, created_entry=created_entry, anonymous=anonymous,
                           previous_path=previous_path, db=connection)
            return
        if created_entry:
            db.execute("INSERT OR REPLACE INTO entry_owners VALUES(?,?,?)", (gid, path, user["id"]))
        db.execute("INSERT INTO events(user_id,group_id,action,path,created,entry_created,previous_path) VALUES(?,?,?,?,?,?,?)",
                   (None if anonymous else user["id"], gid, action, path, time.time(), created_entry, previous_path))
        if getattr(self, 'usage', None):
            self.usage.native_event(db, user, gid, action, path, anonymous)

    def reject_empty_office_write(self, user, lid, path, size, entry_point, *, legacy=False, device_id=None):
        if size != 0 or not OFFICE_FILE.fullmatch(Path(path).name):
            return
        storage = 'seafile' if legacy else 'native'
        try:
            with self.db() as db:
                device = db.execute('SELECT name FROM tokens WHERE id=? AND user_id=?', (device_id, user['id'])).fetchone() if device_id else None
                db.execute('INSERT INTO blocked_office_writes(created,account,device_name,storage,library_id,path,entry_point) '
                           'VALUES(?,?,?,?,?,?,?)',
                           (time.time(), user['username'], device['name'] if device else None, storage, lid, path, entry_point))
        except sqlite3.Error:
            logging.getLogger('uvicorn.error').exception('Blocked empty Office write, but audit storage was unavailable (%s %s)', storage, lid)
        raise DriveError(422, '未收到 Office 文件内容，已保留原文件，请重新保存')

    def mkdir(self, user, gid, rel):
        self.access(user, gid, True)
        p = self.path(gid, rel)
        self.check_unlocked(gid, rel)
        with self.file_locks(gid):
            if p.exists():
                raise DriveError(409, "同名文件或文件夹已经存在")
            if not p.parent.is_dir():
                raise DriveError(409, "上级文件夹不存在")
            p.mkdir()
            self.event(user, gid, "mkdir", rel, created_entry=True)

    def rename(self, user, gid, rel, name):
        self.access(user, gid, True)
        if not rel:
            raise DriveError(403, "不能重命名分组根目录")
        name = valid_name(name)
        with self.file_locks(gid):
            source = self.path(gid, rel, True)
            target = source.with_name(name)
            self.check_unlocked(gid, rel)
            self.check_unlocked(gid, target.relative_to(self.files / gid).as_posix())
            if target.exists():
                raise DriveError(409, "同名文件或文件夹已经存在")
            source.rename(target)
            self.event(user, gid, "rename", target.relative_to(self.files / gid).as_posix(), previous_path=rel)

    def delete(self, user, gid, rel, check_lock=True):
        self.access(user, gid, True)
        if not rel:
            raise DriveError(403, "不能删除分组根目录")
        if check_lock:
            self.check_unlocked(gid, rel)
        with self.file_locks(gid):
            p = self.path(gid, rel, True)
            tid = uuid.uuid4().hex
            is_dir = p.is_dir()
            os.replace(p, self.trash / tid)
            try:
                with self.db() as db:
                    db.execute("INSERT INTO trash VALUES(?,?,?,?,?,?)",
                               (tid, gid, rel, time.time(), user["id"], int(is_dir)))
                    descendants = "library_id=? AND (path=? OR (path>=? AND path<?))"
                    params = (gid, rel, rel + "/", rel + "0")
                    db.execute("INSERT INTO trash_entry_owners SELECT ?,path,owner_id FROM entry_owners WHERE " + descendants,
                               (tid, *params))
                    db.execute("DELETE FROM entry_owners WHERE " + descendants, params)
                    self.on_trash(db, tid, gid, rel)
            except Exception:
                os.replace(self.trash / tid, p)
                raise
            self.event(user, gid, "delete", rel)

    def on_trash(self, db, tid, gid, rel):
        pass

    def on_restore_trash(self, db, tid, gid, rel):
        pass

    def restore(self, user, tid):
        row = self.one("SELECT * FROM trash WHERE id=?", (tid,))
        if not row:
            raise DriveError(404, "回收站记录不存在")
        self.access(user, row["group_id"], True)
        self.check_unlocked(row["group_id"], row["path"])
        with self.file_locks(row["group_id"]):
            p = self.path(row["group_id"], row["path"])
            if p.exists():
                raise DriveError(409, "原位置已有同名文件，请先重命名该文件后再恢复")
            if not p.parent.is_dir():
                raise DriveError(409, "原文件夹不存在，请先恢复或创建上级文件夹")
            os.replace(self.trash / tid, p)
            try:
                with self.db() as db:
                    db.execute("INSERT OR REPLACE INTO entry_owners SELECT ?,path,owner_id FROM trash_entry_owners WHERE trash_id=?",
                               (row["group_id"], tid))
                    self.on_restore_trash(db, tid, row["group_id"], row["path"])
                    db.execute("DELETE FROM trash WHERE id=?", (tid,))
            except Exception:
                os.replace(p, self.trash / tid)
                raise
            self.event(user, row["group_id"], "restore", row["path"])

    def upload_path(self, gid, uid):
        return self.tmp / uid

    def init_upload(self, user, gid, rel, size, fingerprint):
        self.access(user, gid, True)
        rel = clean_path(rel)
        if not rel or size < 0 or size > self.max_file or len(fingerprint) > 240:
            raise DriveError(400, "文件大小或名称不符合要求")
        self.check_unlocked(gid, rel)
        with self.file_locks(gid):
            p = self.path(gid, rel)
            if not p.parent.is_dir():
                raise DriveError(409, "目标文件夹不存在")
            if p.exists():
                raise DriveError(409, "已有同名文件，请重命名后上传；电脑盘写入可更新文件")
            row = self.one("SELECT * FROM uploads WHERE user_id=? AND group_id=? AND path=? AND size=? AND fingerprint=?",
                           (user["id"], gid, rel, size, fingerprint))
            if row and verified_fingerprint(fingerprint):
                try:
                    return dict(id=row["id"], offset=self.upload_path(gid, row["id"]).stat().st_size)
                except FileNotFoundError:
                    pass
            uid = uuid.uuid4().hex
            self.upload_path(gid, uid).touch()
            with self.db() as db:
                db.execute("INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?)",
                           (uid, user["id"], gid, rel, size, fingerprint, signature(p), time.time()))
            return dict(id=uid, offset=0)

    def upload(self, user, uid):
        row = self.one("SELECT * FROM uploads WHERE id=? AND user_id=?", (uid, user["id"]))
        if not row:
            raise DriveError(404, "上传任务不存在")
        self.access(user, row["group_id"], True)
        return row

    def append_upload(self, user, uid, offset, content):
        with self.file_locks("upload:" + uid):
            row = self.upload(user, uid)
            p = self.upload_path(row["group_id"], uid)
            actual = p.stat().st_size
            if offset != actual:
                raise DriveError(409, "上传进度已经变化，请重试")
            if actual + len(content) > row["size"]:
                raise DriveError(400, "上传数据超出声明的文件大小")
            with open(p, "ab") as f:
                try:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                except Exception:
                    f.truncate(actual)
                    raise
            with self.db() as db:
                db.execute("UPDATE uploads SET updated=? WHERE id=?", (time.time(), uid))
            return actual + len(content)

    def finish_upload(self, user, uid):
        row = self.upload(user, uid)
        with self.file_locks("upload:" + uid), self.file_locks(row["group_id"]):
            row = self.upload(user, uid)
            self.check_unlocked(row["group_id"], row["path"])
            source = self.upload_path(row["group_id"], uid)
            target = self.path(row["group_id"], row["path"])
            if source.stat().st_size != row["size"]:
                raise DriveError(409, "文件尚未完整上传")
            if signature(target) != row["target_signature"]:
                raise DriveError(409, "目标文件已经变化，已保留上传数据，请更换名称")
            if not target.parent.is_dir():
                raise DriveError(409, "上级文件夹已被移动")
            with open(source, "rb") as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            os.replace(source, target)
            with self.db() as db:
                db.execute("DELETE FROM uploads WHERE id=?", (uid,))
            self.event(user, row["group_id"], "upload", row["path"], created_entry=row["target_signature"] == "missing")
            return dict(**self.entry(row["group_id"], target), sha256=digest)

    def dav_auth(self, username, password):
        if len(password) > 200 or "." not in password:
            return None
        tid, _ = password.split(".", 1)
        row = self.one("SELECT t.hash,t.user_id FROM tokens t JOIN users u ON t.user_id=u.id "
                       "WHERE t.id=? AND u.username=? COLLATE NOCASE AND u.active=1", (tid, username))
        if row and hmac.compare_digest(row["hash"], token_hash(password)):
            return self.get_user(row["user_id"])
        return None
