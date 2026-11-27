from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
import math
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .store import DriveError, clean_path, password_hash, password_ok, valid_name


def within(path, prefix):
    return path == prefix or not prefix or path.startswith(prefix + "/")


def b64(value):
    return base64.urlsafe_b64encode(value).decode()


class LibraryFeatures:
    def init_workspace(self):
        self.history = self.data / "versions"
        self.history.mkdir(exist_ok=True)
        self.key_cache = {}
        self.link_attempts = {}
        self.library_attempts = {}
        key_path = self.state / "server.key"
        if not key_path.exists():
            try:
                with open(key_path, "xb") as stream:
                    stream.write(os.urandom(32))
                os.chmod(key_path, 0o600)
            except FileExistsError:
                pass
        self.server_key = key_path.read_bytes()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS libraries(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, mount_name TEXT UNIQUE NOT NULL,
                    description TEXT NOT NULL DEFAULT '', owner_id TEXT NOT NULL REFERENCES users(id),
                    visibility TEXT NOT NULL DEFAULT 'private', created REAL NOT NULL,
                    deleted REAL, legacy_group TEXT, encryption TEXT);
                CREATE TABLE IF NOT EXISTS library_shares(
                    id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES libraries(id),
                    path TEXT NOT NULL DEFAULT '', subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL, role TEXT NOT NULL,
                    UNIQUE(library_id,path,subject_type,subject_id));
                CREATE TABLE IF NOT EXISTS group_managers(
                    group_id TEXT NOT NULL REFERENCES groups(id), user_id TEXT NOT NULL REFERENCES users(id),
                    role TEXT NOT NULL DEFAULT 'manager', PRIMARY KEY(group_id,user_id));
                CREATE TABLE IF NOT EXISTS favorites(
                    user_id TEXT NOT NULL, library_id TEXT NOT NULL, path TEXT NOT NULL,
                    created REAL NOT NULL, PRIMARY KEY(user_id,library_id,path));
                CREATE TABLE IF NOT EXISTS file_tags(
                    library_id TEXT NOT NULL, path TEXT NOT NULL, tag TEXT NOT NULL,
                    color TEXT NOT NULL DEFAULT '#8ea67a', PRIMARY KEY(library_id,path,tag));
                CREATE INDEX IF NOT EXISTS file_tags_by_tag ON file_tags(tag,library_id,path);
                CREATE TABLE IF NOT EXISTS comments(
                    id TEXT PRIMARY KEY, library_id TEXT NOT NULL, path TEXT NOT NULL,
                    user_id TEXT NOT NULL REFERENCES users(id), body TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS file_versions(
                    id TEXT PRIMARY KEY, library_id TEXT NOT NULL, path TEXT NOT NULL,
                    signature TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    user_id TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS version_path ON file_versions(library_id,path,created);
                CREATE TABLE IF NOT EXISTS snapshots(
                    id TEXT PRIMARY KEY, library_id TEXT NOT NULL, label TEXT NOT NULL,
                    manifest TEXT NOT NULL, user_id TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS share_links(
                    id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, library_id TEXT NOT NULL,
                    path TEXT NOT NULL, kind TEXT NOT NULL, creator TEXT NOT NULL,
                    password TEXT, expires REAL, created REAL NOT NULL,
                    visits INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                    allow_download INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS public_uploads(upload_id TEXT PRIMARY KEY, link_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notifications(
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, text TEXT NOT NULL,
                    created REAL NOT NULL, read_at REAL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS office_sessions(
                    id TEXT PRIMARY KEY, library_id TEXT NOT NULL, path TEXT NOT NULL,
                    user_id TEXT NOT NULL, file_signature TEXT NOT NULL, document_key TEXT NOT NULL,
                    expires REAL NOT NULL, mode TEXT NOT NULL, completed REAL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(tokens)")}
            for name, sql_type in (("platform", "TEXT NOT NULL DEFAULT 'windows'"), ("last_ip", "TEXT"), ("last_seen", "REAL")):
                if name not in columns:
                    db.execute(f"ALTER TABLE tokens ADD COLUMN {name} {sql_type}")
            office_columns = {row[1] for row in db.execute("PRAGMA table_info(office_sessions)")}
            if "completed" not in office_columns:
                db.execute("ALTER TABLE office_sessions ADD COLUMN completed REAL")
            for column in ("legacy_session", "legacy_account"):
                if column not in office_columns:
                    db.execute(f"ALTER TABLE office_sessions ADD COLUMN {column} TEXT")
        # Migration does not move or rename any old file or directory.
        for group in self.all("SELECT * FROM groups"):
            if not self.one('SELECT group_id FROM seafile_groups WHERE group_id=?', (group['id'],)):
                self.ensure_group_library(group)

    def ensure_group_library(self, group, owner=None):
        if self.one("SELECT id FROM libraries WHERE id=?", (group["id"],)):
            return
        owner = owner or self.one("SELECT id FROM users ORDER BY admin DESC,created LIMIT 1")
        if not owner:
            return
        with self.mutex, self.db() as db:
            mount = group["name"]
            if db.execute("SELECT 1 FROM libraries WHERE mount_name=? COLLATE NOCASE", (mount,)).fetchone():
                mount = valid_name(f"{mount} ({group['id'][:8]})")
            db.execute("INSERT OR IGNORE INTO libraries(id,name,mount_name,description,owner_id,created,legacy_group) VALUES(?,?,?,?,?,?,?)",
                       (group["id"], group["name"], mount, group["description"], owner["id"], group["created"], group["id"]))

    def library(self, lid, include_deleted=False):
        row = self.one("SELECT l.*,u.name owner_name,u.username owner_username FROM libraries l JOIN users u ON u.id=l.owner_id WHERE l.id=?", (lid,))
        if not row or (row["deleted"] is not None and not include_deleted):
            raise DriveError(404, "资料库不存在或已删除")
        return row

    def group_role(self, user, gid):
        if self.one('SELECT group_id FROM seafile_groups WHERE group_id=?', (gid,)):
            return 'member'
        if user["admin"]:
            return "owner"
        row = self.one("SELECT role FROM group_managers WHERE group_id=? AND user_id=?", (gid, user["id"]))
        return row["role"] if row else "member"

    def manage_group(self, user, gid):
        self.group(gid)
        if self.one('SELECT group_id FROM seafile_groups WHERE group_id=?', (gid,)):
            raise DriveError(403, '此群组沿用 Seafile 成员关系，请在原系统维护')
        if self.group_role(self.get_user(user["id"]), gid) not in ("owner", "manager"):
            raise DriveError(403, "需要群组管理员权限")

    def owner(self, user, lid, deleted=False):
        user = self.get_user(user["id"])
        lib = self.library(lid, deleted)
        if not user["admin"] and lib["owner_id"] != user["id"]:
            raise DriveError(403, "此操作需要资料库所有者权限")
        return lib

    def roles(self, user, lib):
        user = self.get_user(user["id"])
        if user["admin"] or lib["owner_id"] == user["id"]:
            return [("", "editor")]
        grants = []
        if lib["visibility"] == "public":
            grants.append(("", "reader"))
        if lib["legacy_group"]:
            row = self.one("SELECT role FROM members WHERE group_id=? AND user_id=?", (lib["legacy_group"], user["id"]))
            if row:
                grants.append(("", row["role"]))
        rows = self.all("SELECT s.path,s.role FROM library_shares s WHERE s.library_id=? AND "
                        "((s.subject_type='user' AND s.subject_id=?) OR (s.subject_type='group' AND s.subject_id IN "
                        "(SELECT group_id FROM members WHERE user_id=?)))", (lib["id"], user["id"], user["id"]))
        grants.extend((r["path"], r["role"]) for r in rows)
        return grants

    def access(self, user, gid, write=False, rel=None, require_key=True):
        if write and hasattr(self, "license"):
            self.license.require_write()
        lib = self.library(gid)
        grants = self.roles(user, lib)
        role = self.role_for_path(grants, clean_path(rel) if rel is not None else None)
        if not role or (write and role != "editor"):
            raise DriveError(403, "没有该位置的写入权限" if write else "没有该位置的访问权限")
        if require_key and lib["encryption"]:
            self.library_key(user, gid)
        return lib, role

    @staticmethod
    def role_for_path(grants, rel):
        roles = {role for prefix, role in grants if rel is None or within(rel, prefix)}
        return "editor" if "editor" in roles else "reader" if roles else None

    def library_key(self, user, lid):
        row = self.key_cache.get((user["id"], lid))
        if not row or row[1] < time.time():
            self.key_cache.pop((user["id"], lid), None)
            raise DriveError(423, "加密资料库未解锁，请输入资料库密码")
        return row[0]

    def unlock_library(self, user, lid, password):
        lib, _ = self.access(user, lid, require_key=False)
        if not lib["encryption"]:
            return
        attempt_key = (user["id"], lid)
        now = time.time()
        attempts = [attempt for attempt in self.library_attempts.get(attempt_key, []) if attempt > now - 300]
        if len(attempts) >= 12:
            raise DriveError(429, "尝试次数过多，请 5 分钟后再试")
        try:
            config = json.loads(lib["encryption"])
            key = hashlib.scrypt(password.encode(), salt=base64.urlsafe_b64decode(config["salt"]), n=16384, r=8, p=1, dklen=32)
            raw = AESGCM(key).decrypt(base64.urlsafe_b64decode(config["nonce"]), base64.urlsafe_b64decode(config["wrapped"]), lid.encode())
        except (InvalidTag, ValueError, KeyError):
            self.library_attempts[attempt_key] = attempts + [now]
            raise DriveError(403, "资料库密码错误")
        self.library_attempts.pop(attempt_key, None)
        self.key_cache[attempt_key] = (raw, now + 3600)

    def create_library(self, user, name, description="", visibility="private", password=""):
        user = self.get_user(user["id"])
        name = valid_name(name.strip())
        if len(name) > 80 or len(description) > 500 or visibility not in ("private", "public"):
            raise DriveError(400, "资料库名称、说明或类型无效")
        if visibility == "public" and not user["admin"]:
            raise DriveError(403, "公共资料库由管理员创建")
        if password and not 10 <= len(password) <= 200:
            raise DriveError(400, "加密资料库密码需要 10–200 位")
        if password and visibility == "public":
            raise DriveError(400, "加密资料库不能设为公共资料库")
        lid = uuid.uuid4().hex
        encryption = None
        if password:
            salt, nonce, raw = os.urandom(16), os.urandom(12), os.urandom(32)
            wrapping = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
            encryption = json.dumps(dict(salt=b64(salt), nonce=b64(nonce), wrapped=b64(AESGCM(wrapping).encrypt(nonce, raw, lid.encode()))))
        with self.mutex, self.db() as db:
            mount = name
            if db.execute("SELECT id FROM libraries WHERE mount_name=? COLLATE NOCASE", (mount,)).fetchone():
                mount = valid_name(f"{name} ({user['username']}-{lid[:6]})")
            db.execute("INSERT INTO libraries(id,name,mount_name,description,owner_id,visibility,created,encryption) VALUES(?,?,?,?,?,?,?,?)",
                       (lid, name, mount, description, user["id"], visibility, time.time(), encryption))
            (self.files / lid).mkdir()
        if encryption:
            self.key_cache[(user["id"], lid)] = (raw, time.time() + 3600)
        self.event(user, lid, "library_create", name)
        return self.library_public(user, self.library(lid), "")

    def library_public(self, user, lib, prefix, grants=None):
        if grants is None:
            role = self.access(user, lib["id"], rel=prefix, require_key=False)[1]
            grants = self.roles(user, lib)
        else:
            role = self.role_for_path(grants, prefix)
            if role is None:
                raise DriveError(403, "没有该位置的访问权限")
        result = {key: lib[key] for key in ("id", "name", "mount_name", "description", "owner_id", "owner_name", "visibility", "created", "deleted")}
        result.update(role=role, root_path=prefix, encrypted=bool(lib["encryption"]),
                      locked=bool(lib["encryption"]) and (self.key_cache.get((user["id"], lib["id"]), (None, 0))[1] < time.time()),
                      is_owner=bool(user["admin"] or lib["owner_id"] == user["id"]))
        result["write_paths"] = [path for path, grant in grants if grant == "editor"]
        return result

    def libraries_for(self, user, view="all", group_id=None):
        user, catalog = self.library_grants(user, view, group_id)
        result = []
        for lib, grants in catalog.values():
            roots = [""] if any(not p for p, _ in grants) else sorted({p for p, _ in grants})
            for prefix in roots:
                if self.path(lib["id"], prefix).is_dir():
                    result.append(self.library_public(user, lib, prefix, grants=grants))
        return result

    def group_library_catalog(self, user):
        """Return visible library roots grouped by the current user's groups."""
        with self.reuse_read_connection():
            groups = self.groups_for(user)
            grouped = {group['id']: [] for group in groups}
            if not grouped:
                return {'groups': groups, 'libraries': grouped}
            shared = {}
            for row in self.all("SELECT DISTINCT library_id,subject_id FROM library_shares WHERE subject_type='group'"):
                if row['subject_id'] in grouped:
                    shared.setdefault(row['library_id'], set()).add(row['subject_id'])
            user, catalog = self.library_grants(user)
            for lib, grants in catalog.values():
                group_ids = shared.get(lib['id'], set())
                if lib['legacy_group'] in grouped:
                    group_ids = group_ids | {lib['legacy_group']}
                if not group_ids:
                    continue
                roots = [''] if any(not prefix for prefix, _ in grants) else sorted({prefix for prefix, _ in grants})
                visible = [self.library_public(user, lib, prefix, grants=grants) for prefix in roots
                           if self.path(lib['id'], prefix).is_dir()]
                for gid in group_ids:
                    grouped[gid].extend(visible)
        return {'groups': groups, 'libraries': grouped}

    def library_grants(self, user, view="all", group_id=None, library_id=None):
        user = self.get_user(user["id"])
        # These grants live only for this call. Subsequent requests and file
        # operations still read current permissions, including revoked shares.
        with self.db() as db:
            db.execute("BEGIN")
            memberships = {} if user["admin"] else {
                row["group_id"]: row["role"] for row in
                db.execute("SELECT group_id,role FROM members WHERE user_id=?", (user["id"],))
            }
            if group_id and not user["admin"] and group_id not in memberships:
                raise DriveError(403, "你不是此群组成员")
            conditions, params = ["l.deleted IS NULL"], []
            if library_id is not None:
                conditions.append("l.id=?")
                params.append(library_id)
            if view == "mine":
                conditions.append("l.owner_id=?")
                params.append(user["id"])
            elif view == "public":
                conditions.append("l.visibility='public'")
            elif view == "shared":
                conditions.append("l.owner_id!=?")
                params.append(user["id"])
            if group_id:
                conditions.append("(l.legacy_group=? OR EXISTS (SELECT 1 FROM library_shares s "
                                  "WHERE s.library_id=l.id AND s.subject_type='group' AND s.subject_id=?))")
                params.extend((group_id, group_id))
            libraries = [dict(row) for row in db.execute(
                "SELECT l.*,u.name owner_name,u.username owner_username FROM libraries l "
                "JOIN users u ON u.id=l.owner_id WHERE " + " AND ".join(conditions) + " ORDER BY l.created DESC", params)]
            shares = {}
            if not user["admin"]:
                for row in db.execute(
                    "SELECT library_id,path,role FROM library_shares WHERE "
                    "(subject_type='user' AND subject_id=?) OR (subject_type='group' AND subject_id IN "
                    "(SELECT group_id FROM members WHERE user_id=?))", (user["id"], user["id"])):
                    shares.setdefault(row["library_id"], []).append((row["path"], row["role"]))
        result = {}
        for lib in libraries:
            if user["admin"] or lib["owner_id"] == user["id"]:
                grants = [("", "editor")]
            else:
                grants = [("", "reader")] if lib["visibility"] == "public" else []
                if lib["legacy_group"] in memberships:
                    grants.append(("", memberships[lib["legacy_group"]]))
                grants.extend(shares.get(lib["id"], []))
            if grants:
                result[lib["id"]] = lib, grants
        return user, result

    def dav_roots(self, user):
        roots = []
        for lib in self.libraries_for(user):
            if lib["locked"]:
                continue
            folder_name = Path(lib["root_path"]).name[:150]
            mount = lib["mount_name"] if not lib["root_path"] else valid_name(f"{folder_name} ({lib['id'][:10]}-{hashlib.sha256(lib['root_path'].encode()).hexdigest()[:10]})")
            roots.append(dict(name=mount, id=lib["id"], prefix=lib["root_path"], role=lib["role"]))
        return roots

    def update_library(self, user, lid, changes):
        lib = self.owner(user, lid)
        name = valid_name(str(changes.get("name", lib["name"])).strip())
        desc = str(changes.get("description", lib["description"]))
        visibility = changes.get("visibility", lib["visibility"])
        if len(name) > 80 or len(desc) > 500 or visibility not in ("private", "public"):
            raise DriveError(400, "资料库设置无效")
        if visibility == "public" and (not user["admin"] or lib["encryption"]):
            raise DriveError(403, "加密资料库不能公开，公共资料库由管理员管理")
        owner_id = changes.get("owner_id", lib["owner_id"])
        if not isinstance(owner_id, str):
            raise DriveError(400, "所有者无效")
        self.get_user(owner_id)
        mount = name
        if self.one("SELECT id FROM libraries WHERE mount_name=? AND id!=?", (mount, lid)):
            mount = valid_name(f"{name} ({lid[:6]})")
        with self.file_locks(lid), self.mutex, self.db() as db:
            self.owner(user, lid)
            db.execute("UPDATE libraries SET name=?,mount_name=?,description=?,visibility=?,owner_id=? WHERE id=?", (name, mount, desc, visibility, owner_id, lid))
            if owner_id != lib["owner_id"]:
                db.execute("UPDATE share_links SET active=0 WHERE library_id=?", (lid,))
        self.event(user, lid, "library_update", name)
        return {"ok": True, "id": lid}

    def remove_library(self, user, lid):
        with self.file_locks(lid):
            self.owner(user, lid)
            self.check_unlocked(lid, "")
            with self.db() as db:
                db.execute("UPDATE libraries SET deleted=? WHERE id=?", (time.time(), lid))
                db.execute("UPDATE share_links SET active=0 WHERE library_id=?", (lid,))
            for key in list(self.key_cache):
                if key[1] == lid:
                    self.key_cache.pop(key, None)
        self.event(user, lid, "library_delete", "")

    def restore_library(self, user, lid):
        with self.file_locks(lid), self.db() as db:
            self.owner(user, lid, True)
            db.execute("UPDATE libraries SET deleted=NULL WHERE id=?", (lid,))
        self.event(user, lid, "library_restore", "")

    def share(self, user, lid, path, subject_type, subject_id, role):
        lib = self.owner(user, lid)
        path = clean_path(path)
        self.access(user, lid, rel=path)
        self.path(lid, path, True)
        if path and not self.path(lid, path).is_dir():
            raise DriveError(400, "内部授权请选择资料库或文件夹")
        if role not in ("reader", "editor") or subject_type not in ("user", "group"):
            raise DriveError(400, "共享对象或权限无效")
        if subject_type == "user":
            self.get_user(subject_id)
        else:
            self.group(subject_id)
            if not user["admin"] and not self.one("SELECT 1 FROM members WHERE group_id=? AND user_id=?", (subject_id, user["id"])):
                raise DriveError(403, "只能向自己加入的群组共享")
        sid = uuid.uuid4().hex
        with self.db() as db:
            db.execute("INSERT INTO library_shares VALUES(?,?,?,?,?,?) ON CONFLICT(library_id,path,subject_type,subject_id) DO UPDATE SET role=excluded.role",
                       (sid, lid, path, subject_type, subject_id, role))
        if subject_type == "user":
            self.notify(subject_id, f"{user['name']} 向你共享了资料库“{lib['name']}”" + (f"中的文件夹“{path}”" if path else ""))
        self.event(user, lid, "share", path)

    def unshare(self, user, sid):
        row = self.one("SELECT * FROM library_shares WHERE id=?", (sid,))
        if not row:
            raise DriveError(404, "共享记录不存在")
        self.owner(user, row["library_id"])
        with self.mutex, self.db() as db:
            db.execute("DELETE FROM library_shares WHERE id=?", (sid,))
        self.event(user, row["library_id"], "unshare", row["path"])

    def shares_for(self, user, lid=None):
        return self.all("SELECT s.*,l.name library_name,l.owner_id,u.name user_name,g.name group_name "
                        "FROM library_shares s JOIN libraries l ON l.id=s.library_id "
                        "LEFT JOIN users u ON s.subject_type='user' AND s.subject_id=u.id "
                        "LEFT JOIN groups g ON s.subject_type='group' AND s.subject_id=g.id "
                        "WHERE l.deleted IS NULL AND (? OR l.owner_id=?) AND (? IS NULL OR s.library_id=?) "
                        "ORDER BY l.created DESC", (bool(user["admin"]), user["id"], lid, lid))

    def notify(self, uid, text):
        with self.db() as db:
            db.execute("INSERT INTO notifications(id,user_id,text,created) VALUES(?,?,?,?)", (uuid.uuid4().hex, uid, text, time.time()))

    def favorite(self, user, lid, path, enabled):
        path = clean_path(path)
        with self.file_locks(lid), self.db() as db:
            self.access(user, lid, rel=path, require_key=False)
            self.path(lid, path, True)
            if enabled:
                db.execute("INSERT OR IGNORE INTO favorites VALUES(?,?,?,?)", (user["id"], lid, path, time.time()))
            else:
                db.execute("DELETE FROM favorites WHERE user_id=? AND library_id=? AND path=?", (user["id"], lid, path))

    def favorites_for(self, user):
        _, catalog = self.library_grants(user)
        rows = self.all("SELECT * FROM favorites WHERE user_id=? ORDER BY created DESC", (user["id"],))
        result = []
        for row in rows:
            entry = catalog.get(row["library_id"])
            if entry is None:
                continue
            lib, grants = entry
            try:
                if not self.role_for_path(grants, clean_path(row["path"])):
                    continue
                fp = self.path(lib["id"], row["path"], True)
                result.append(dict(**row, name=fp.name if row["path"] else lib["name"], library_name=lib["name"], is_dir=fp.is_dir()))
            except DriveError:
                pass
        return result

    def set_tags(self, user, lid, path, tags):
        path = clean_path(path)
        if len(tags) > 20 or any(not str(t).strip() or len(str(t)) > 32 for t in tags):
            raise DriveError(400, "最多 20 个标签，每个 1–32 字")
        with self.file_locks(lid), self.db() as db:
            self.access(user, lid, True, rel=path)
            self.path(lid, path, True)
            db.execute("DELETE FROM file_tags WHERE library_id=? AND path=?", (lid, path))
            for tag in sorted({str(t).strip() for t in tags}):
                db.execute("INSERT INTO file_tags(library_id,path,tag) VALUES(?,?,?)", (lid, path, tag))
        self.event(user, lid, "tags", path)

    def create_link(self, user, lid, path, kind="download", password="", expires=None):
        lib = self.owner(user, lid)
        if lib["encryption"]:
            raise DriveError(400, "加密资料库不能创建外链或公开发布")
        if kind not in ("download", "upload", "published") or len(password) > 200:
            raise DriveError(400, "链接类型或密码无效")
        path = clean_path(path)
        fp = self.path(lid, path, True)
        if kind in ("upload", "published") and not fp.is_dir():
            raise DriveError(400, "上传链接或发布请选择资料库／文件夹")
        if expires is not None and (not math.isfinite(expires) or expires < time.time() or expires > time.time() + 366 * 86400 * 10):
            raise DriveError(400, "过期时间应在未来 10 年内")
        link_id, token = uuid.uuid4().hex, secrets.token_urlsafe(24)
        with self.db() as db:
            db.execute("INSERT INTO share_links(id,token,library_id,path,kind,creator,password,expires,created) VALUES(?,?,?,?,?,?,?,?,?)",
                       (link_id, token, lid, path, kind, user["id"], password_hash(password) if password else None, expires, time.time()))
        self.event(user, lid, "publish" if kind == "published" else "link_create", path)
        return self.link_public(self.one("SELECT * FROM share_links WHERE id=?", (link_id,)))

    @staticmethod
    def link_public(row):
        return {**{k: row[k] for k in ("id", "token", "library_id", "path", "kind", "expires", "created", "visits", "active")}, "has_password": bool(row["password"])}

    def links_for(self, user):
        rows = self.all("SELECT s.*,l.name library_name,l.owner_id,l.deleted FROM share_links s JOIN libraries l ON l.id=s.library_id WHERE s.creator=? OR l.owner_id=? ORDER BY s.created DESC", (user["id"], user["id"]))
        return [dict(**self.link_public(row), library_name=row["library_name"], expired=bool(row["expires"] and row["expires"] < time.time())) for row in rows if row["deleted"] is None]

    def resolve_link(self, token, proof=None):
        row = self.one("SELECT * FROM share_links WHERE token=? AND active=1", (token,))
        if not row or (row["expires"] and row["expires"] < time.time()):
            raise DriveError(404, "链接不存在、已撤销或已过期")
        try:
            owner = self.get_user(row["creator"])
            self.owner(owner, row["library_id"])
            self.path(row["library_id"], row["path"], True)
        except DriveError:
            raise DriveError(404, "链接已失效")
        if row["password"]:
            try:
                expires, mac = (proof or "").split(".", 1)
                payload = f"{row['id']}:{expires}:{row['password']}".encode()
                ok = int(expires) > time.time() and hmac.compare_digest(mac, hmac.new(self.server_key, payload, hashlib.sha256).hexdigest())
            except (ValueError, TypeError):
                ok = False
            if not ok:
                raise DriveError(401, "此链接需要访问密码")
        return row, owner

    def unlock_link(self, token, password, ip):
        row = self.one("SELECT * FROM share_links WHERE token=? AND active=1", (token,))
        if not row or (row["expires"] and row["expires"] < time.time()):
            raise DriveError(404, "链接已失效")
        now = time.time()
        key = (ip, token)
        attempts = [n for n in self.link_attempts.get(key, []) if n > now - 300]
        if len(attempts) >= 15:
            raise DriveError(429, "尝试次数过多，请 5 分钟后再试")
        if row["password"] and not password_ok(password, row["password"]):
            self.link_attempts[key] = attempts + [now]
            raise DriveError(403, "访问密码错误")
        self.link_attempts.pop(key, None)
        expires = str(int(now + 3600))
        payload = f"{row['id']}:{expires}:{row['password']}".encode()
        return expires + "." + hmac.new(self.server_key, payload, hashlib.sha256).hexdigest()
