from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from stat import S_ISREG
import time
from tempfile import TemporaryDirectory
import uuid

from .file_crypto import EncryptedReader, plain_size, seal
from .entry_ownership import backfill_entry_owners
from .file_metadata import (ACTIVE_VERSION, METADATA, collaboration, detach_versions, init_metadata,
                            path_scope, put_collaboration, restore_trash_metadata, revoke_shares, trash_metadata)
from .library_features import LibraryFeatures, within
from .store import DriveError, Store, clean_path, signature, valid_name
from .upload_integrity import fingerprint_matches, upload_digests, verified_fingerprint


class WorkspaceStore(LibraryFeatures, Store):
    def __init__(self, data_dir, state_dir=None):
        super().__init__(data_dir, state_dir)
        self.init_workspace()
        init_metadata(self)
        backfill_entry_owners(self)
        self.secure_staging = self.state / "staging"
        self.secure_staging.mkdir(mode=0o700, exist_ok=True)

    def staging_directory(self, lid):
        library = self.library(lid, include_deleted=True)
        return self.secure_staging if library["encryption"] else self.tmp

    def upload_path(self, gid, uid):
        return self.staging_directory(gid) / uid

    def create_group(self, name, description, creator=None):
        group = super().create_group(name, description)
        self.ensure_group_library(group, creator)
        creator = creator or self.one("SELECT * FROM users ORDER BY admin DESC,created LIMIT 1")
        if creator:
            with self.db() as db:
                db.execute("INSERT OR IGNORE INTO group_managers VALUES(?,?,?)", (group["id"], creator["id"], "owner"))
                db.execute("INSERT OR IGNORE INTO members VALUES(?,?,?)", (group["id"], creator["id"], "editor"))
        return group

    def groups_for(self, user):
        user = self.get_user(user["id"])
        groups = self.all("""
            SELECT g.*,
                   CASE WHEN :admin THEN 'editor' ELSE m.role END AS role,
                   CASE WHEN s.group_id IS NOT NULL THEN 'member'
                        WHEN :admin THEN 'owner'
                        ELSE COALESCE(manager.role, 'member') END AS management_role,
                   (SELECT COUNT(*) FROM members WHERE group_id=g.id) AS members,
                   s.group_id AS original_group
            FROM groups g
            LEFT JOIN members m ON m.group_id=g.id AND m.user_id=:user_id
            LEFT JOIN group_managers manager ON manager.group_id=g.id AND manager.user_id=:user_id
            LEFT JOIN seafile_groups s ON s.group_id=g.id
            WHERE :admin OR m.user_id IS NOT NULL
            ORDER BY g.created
        """, {"admin": bool(user["admin"]), "user_id": user["id"]})
        for group in groups:
            if group.pop('original_group') is not None:
                group['identity_source'] = 'seafile'
        return groups

    def entry(self, gid, path, *, library=None, tags=None, rel=None, stat=None):
        result = super().entry(gid, path, rel=rel, stat=stat)
        library = library if library is not None else self.library(gid)
        if (library.get("encryption") or library.get("encrypted")) and not result["is_dir"]:
            result["size"] = plain_size(path)
        if tags is None:
            tags = [row["tag"] for row in self.all("SELECT tag FROM file_tags WHERE library_id=? AND path=? ORDER BY tag", (gid, result["path"]))]
        result["tags"] = tags
        return result

    def listing(self, user, gid, rel="", query="", *, offset=0, limit=None, order="name", descending=False):
        rel = clean_path(rel)
        lib, _ = self.access(user, gid, rel=rel)
        grants = self.roles(user, lib)
        directory = self.path(gid, rel, True)
        if not directory.is_dir():
            raise DriveError(400, "该路径不是文件夹")
        prefix = rel + "/" if rel else ""
        tags = {}
        for row in self.all(
            "SELECT path,tag FROM file_tags WHERE library_id=? AND substr(path,1,?)=? "
            "AND instr(substr(path,?),'/')=0 ORDER BY tag", (gid, len(prefix), prefix, len(prefix)+1)
        ):
            tags.setdefault(row["path"], []).append(row["tag"])
        candidates = []
        owners = {row['path']: row['owner_id'] for row in self.all(
            'SELECT path,owner_id FROM entry_owners WHERE library_id=? AND substr(path,1,?)=?',
            (gid, len(prefix), prefix))}
        protected_folders = {p[len(prefix):].split('/', 1)[0] for p, owner in owners.items()
                             if owner != user['id'] and '/' in p[len(prefix):]}
        query = query.casefold()
        with os.scandir(directory) as children:
            for child in children:
                if query not in child.name.casefold() or child.is_symlink() or (os.name == 'nt' and child.is_junction()):
                    continue
                try:
                    valid_name(child.name)
                    directory_entry = child.is_dir(follow_symlinks=False)
                    stat = child.stat(follow_symlinks=False) if order != 'name' else None
                    candidates.append((child, directory_entry, stat))
                except (DriveError, FileNotFoundError):
                    continue
        # Keep directories first in both sort directions.
        def name_key(entry):
            name = entry[0].name.casefold()
            if limit is None:
                return name
            return tuple(int(part) if part.isdecimal() else part for part in re.split(r'(\d+)', name))

        if order == 'name':
            field = name_key
        elif order == 'size':
            field = lambda entry: 0 if entry[1] else entry[2].st_size
        else:
            field = lambda entry: entry[2].st_mtime
        candidates.sort(key=lambda entry: (field(entry), entry[0].name.casefold()), reverse=descending)
        candidates.sort(key=lambda entry: not entry[1])
        total = len(candidates)
        if limit is not None:
            candidates = candidates[offset:offset + limit]
        results = []
        for child, _, stat in candidates:
            try:
                stat = stat if stat is not None else child.stat(follow_symlinks=False)
                entry = super().entry(gid, child, rel=prefix + child.name, stat=stat)
                if lib["encryption"] and not entry["is_dir"]:
                    entry["size"] = plain_size(child.path)
            except (DriveError, FileNotFoundError):
                continue
            entry["tags"] = tags.get(entry["path"], [])
            entry['can_remove'] = (owners.get(entry['path']) == user['id'] and entry['name'] not in protected_folders
                                   and self.role_for_path(grants, entry['path']) == 'editor')
            results.append(entry)
        return results if limit is None else dict(items=results, total=total, offset=offset, limit=limit)

    def directory_summary(self, directory, encrypted, limit):
        size = count = examined = 0
        modified = directory.stat().st_mtime
        for parent, dirs, files in os.walk(directory, followlinks=False):
            safe_dirs = []
            for name, is_dir in [(name, True) for name in dirs] + [(name, False) for name in files]:
                if examined >= limit:
                    return dict(files=count, size=size, modified=modified, partial=True)
                examined += 1
                path = Path(parent) / name
                if path.is_symlink() or path.is_junction():
                    continue
                try:
                    stat = path.stat()
                    if is_dir:
                        safe_dirs.append(name)
                    elif S_ISREG(stat.st_mode):
                        size += plain_size(path) if encrypted else stat.st_size
                        count += 1
                    modified = max(modified, stat.st_mtime)
                except FileNotFoundError:
                    continue
            dirs[:] = safe_dirs
        return dict(files=count, size=size, modified=modified, partial=False)

    def mkdir(self, user, gid, rel):
        rel = clean_path(rel)
        with self.file_locks(gid):
            self.access(user, gid, True, rel=rel)
            return super().mkdir(user, gid, rel)

    def require_remove_entries(self, user, gid, rel, *, root=False):
        self.access(user, gid, True, rel=rel, require_key=False)
        path = self.path(gid, rel, True)
        scope = ("path=? OR (path>=? AND path<?)", (rel, rel + '/', rel + '0')) if rel else ('1=1', ())
        owners = {row['path']: row['owner_id'] for row in self.all(
            f'SELECT path,owner_id FROM entry_owners WHERE library_id=? AND ({scope[0]})', (gid, *scope[1]))}

        def require_owned(entry):
            entry_path = entry.relative_to(self.files / gid).as_posix()
            if owners.get(entry_path) != user['id']:
                raise DriveError(403, '只能删除、重命名或移走自己的文件和文件夹')

        if not rel and not root:
            raise DriveError(403, '不能删除或移走资料库根目录')
        if not root:
            require_owned(path)
        if path.is_dir():
            def walk_error(error):
                raise error

            for parent, dirs, files in os.walk(path, followlinks=False, onerror=walk_error):
                for name in dirs + files:
                    child = Path(parent) / name
                    if child.is_symlink() or child.is_junction():
                        raise DriveError(403, '文件夹包含无法确认归属的链接')
                    require_owned(child)

    def remove_library(self, user, lid):
        with self.file_locks(lid):
            self.owner(user, lid)
            self.require_remove_entries(user, lid, '', root=True)
            super().remove_library(user, lid)

    def rename(self, user, gid, rel, name):
        rel = clean_path(rel)
        destination = (Path(rel).parent / valid_name(name)).as_posix()
        with self.file_locks(gid):
            self.require_remove_entries(user, gid, rel)
            self.access(user, gid, True, rel=rel)
            self.access(user, gid, True, rel=destination)
            super().rename(user, gid, rel, name)
            try:
                self.remap_metadata(gid, rel, gid, destination)
            except Exception:
                os.replace(self.path(gid, destination, True), self.path(gid, rel))
                raise

    def delete(self, user, gid, rel, check_lock=True):
        rel = clean_path(rel)
        with self.file_locks(gid):
            self.require_remove_entries(user, gid, rel)
            self.access(user, gid, True, rel=rel)
            fp = self.path(gid, rel, True)
            if fp.is_file():
                self.record_version(user, gid, rel)
                self.access(user, gid, True, rel=rel)
                self.require_remove_entries(user, gid, rel)
            self._trash_entry(user, gid, rel, check_lock)

    def _trash_entry(self, user, gid, rel, check_lock=True):
        super().delete(user, gid, rel, check_lock)

    def on_trash(self, db, tid, gid, rel):
        trash_metadata(db, tid, gid, rel)

    def on_restore_trash(self, db, tid, gid, rel):
        restore_trash_metadata(db, tid, gid)

    def restore(self, user, tid):
        row = self.one("SELECT group_id FROM trash WHERE id=?", (tid,))
        if not row:
            raise DriveError(404, "回收站记录不存在")
        with self.file_locks(row["group_id"]):
            row = self.one("SELECT * FROM trash WHERE id=?", (tid,))
            if row:
                self.access(user, row["group_id"], True, rel=row["path"])
            return super().restore(user, tid)

    def list_trash(self, user, lid=None):
        if hasattr(self, "license") and not self.license.status()["can_write"]:
            return []
        _, catalog = self.library_grants(user, library_id=lid)
        if not catalog:
            return []
        sql = "SELECT * FROM trash WHERE group_id IN (SELECT value FROM json_each(?)) ORDER BY deleted DESC"
        result = []
        for row in self.all(sql, (json.dumps(list(catalog)),)):
            entry = catalog.get(row["group_id"])
            if entry is None:
                continue
            lib, grants = entry
            try:
                if self.role_for_path(grants, clean_path(row["path"])) == "editor":
                    result.append(dict(**row, group_name=lib["name"]))
            except DriveError:
                continue
        return result

    def events_for(self, user, lid=None):
        _, catalog = self.library_grants(user, library_id=lid)
        if not catalog:
            return []
        result = []
        sql = ("SELECT e.*,u.name actor,l.name group_name FROM events e "
               "LEFT JOIN users u ON u.id=e.user_id JOIN libraries l ON l.id=e.group_id WHERE l.deleted IS NULL "
               "AND e.group_id IN (SELECT value FROM json_each(?)) ORDER BY e.id DESC LIMIT 2000")
        rows = self.all(sql, (json.dumps(list(catalog)),))
        for row in rows:
            entry = catalog.get(row["group_id"])
            if entry is None or not self.role_for_path(entry[1], row["path"]):
                continue
            result.append(row)
            if len(result) >= 200:
                break
        return result

    def read_content(self, user, lid, rel, physical=None):
        lib, _ = self.access(user, lid, rel=rel)
        fp = Path(physical) if physical is not None else self.path(lid, rel, True)
        if not fp.is_file():
            raise DriveError(400, "请选择文件")
        if lib["encryption"]:
            return EncryptedReader(fp, self.library_key(user, lid))
        return open(fp, "rb")

    def content_size(self, lid, physical):
        return plain_size(physical) if self.library(lid)["encryption"] else Path(physical).stat().st_size

    def commit_file(self, user, lid, rel, source, expected=None, action="upload", check_lock=True, before_commit=None,
                    upload_id=None, *, entry_point="web_upload", device_id=None):
        rel = clean_path(rel)
        size = Path(source).stat().st_size
        if size > self.max_file:
            raise DriveError(413, "文件超过大小限制")
        with self.file_locks(lid):
            lib, _ = self.access(user, lid, True, rel=rel)
            target = self.path(lid, rel)
            if not rel or not target.parent.is_dir() or target.is_dir():
                raise DriveError(409, "目标路径不是可写文件位置")
            if check_lock:
                self.check_unlocked(lid, rel)
            original = signature(target)
            if expected is not None and original != expected:
                raise DriveError(409, "文件已被其他人修改，请重新打开或另存为")
            if original != 'missing' and action != 'version_restore':
                self.reject_empty_office_write(user, lid, rel, size, entry_point, device_id=device_id)
            previous_version = self.record_version(user, lid, rel) if original != "missing" else None
            staged = self.tmp / ("sealed-" + uuid.uuid4().hex) if lib["encryption"] else None
            replacement = staged or source
            try:
                if staged:
                    seal(source, staged, self.library_key(user, lid))
                if staged or original != "missing" or before_commit:
                    self.access(user, lid, True, rel=rel)
                    if check_lock:
                        self.check_unlocked(lid, rel)
                    if self.path(lid, rel) != target or signature(target) != original:
                        raise DriveError(409, "文件已被其他人修改，请重新打开或另存为")
                    if before_commit:
                        before_commit()
                with TemporaryDirectory(dir=self.tmp, prefix='commit-') as scratch, ExitStack() as rollback:
                    # Retain the prior inode, so a storage transaction failure can
                    # restore both the live file and the original upload offset.
                    backup = Path(scratch) / 'original'
                    if previous_version:
                        try:
                            os.link(target, backup)
                        except OSError:
                            shutil.copy2(target, backup)
                    with self.db() as db:
                        public_upload = upload_id is not None and db.execute(
                            'SELECT 1 FROM public_uploads WHERE upload_id=?', (upload_id,)).fetchone() is not None
                        self.event(user, lid, action, rel, created_entry=original == 'missing' and action != 'version_restore',
                                   anonymous=public_upload, db=db)
                        if upload_id is not None:
                            db.execute('DELETE FROM uploads WHERE id=?', (upload_id,))
                        os.replace(replacement, target)
                        if previous_version:
                            rollback.callback(os.replace, backup, target)
                        rollback.callback(os.replace, target, replacement)
                        result = self.entry(lid, target)
                    rollback.pop_all()
            finally:
                if staged:
                    staged.unlink(missing_ok=True)
            return result

    def init_upload(self, user, gid, rel, size, fingerprint, overwrite=False):
        with self.file_locks(gid):
            self.access(user, gid, True, rel=rel)
            if not overwrite:
                return super().init_upload(user, gid, rel, size, fingerprint)
            rel = clean_path(rel)
            target = self.path(gid, rel)
            if not rel or size < 0 or size > self.max_file or target.is_dir() or not target.parent.is_dir():
                raise DriveError(400, "目标名称、位置或文件大小无效")
            self.check_unlocked(gid, rel)
            expected = signature(target)
            if expected != 'missing':
                self.reject_empty_office_write(user, gid, rel, size, 'web_upload')
            row = self.one("SELECT * FROM uploads WHERE user_id=? AND group_id=? AND path=? AND size=? AND fingerprint=? AND target_signature=?",
                           (user["id"], gid, rel, size, fingerprint, expected))
            if row and verified_fingerprint(fingerprint):
                try:
                    return dict(id=row["id"], offset=self.upload_path(gid, row["id"]).stat().st_size)
                except FileNotFoundError:
                    pass
            uid = uuid.uuid4().hex
            self.upload_path(gid, uid).touch()
            with self.db() as db:
                db.execute("INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?)", (uid, user["id"], gid, rel, size, fingerprint, expected, time.time()))
            return dict(id=uid, offset=0)

    def upload(self, user, uid):
        row = super().upload(user, uid)
        self.access(user, row["group_id"], True, rel=row["path"])
        return row

    def finish_upload(self, user, uid, before_commit=None):
        with self.file_locks("upload:" + uid):
            row = self.upload(user, uid)
            source = self.upload_path(row["group_id"], uid)
            if source.stat().st_size != row["size"]:
                raise DriveError(409, "文件尚未完整上传")
            with open(source, "rb") as stream:
                digest, fingerprint = upload_digests(stream)
            if not fingerprint_matches(row["fingerprint"], fingerprint):
                source.unlink(missing_ok=True)
                with self.db() as db:
                    db.execute("DELETE FROM public_uploads WHERE upload_id=?", (uid,))
                    db.execute("DELETE FROM uploads WHERE id=?", (uid,))
                raise DriveError(409, "上传内容校验失败，请重新选择文件")
            result = self.commit_file(user, row["group_id"], row["path"], source,
                                      row["target_signature"], before_commit=before_commit, upload_id=uid)
            source.unlink(missing_ok=True)
            return dict(**result, sha256=digest)

    def record_version(self, user, lid, rel):
        fp = self.path(lid, rel, True)
        if not fp.is_file():
            return None
        current = signature(fp)
        row = self.one("SELECT id FROM file_versions WHERE library_id=? AND path=? AND signature=? AND " + ACTIVE_VERSION,
                       (lid, rel, current))
        if row:
            return row["id"]
        vid = uuid.uuid4().hex
        archived = self.history / vid
        # Completed files are immutable inodes: later writes always use replace.
        try:
            os.link(fp, archived)
        except OSError:
            shutil.copy2(fp, archived)
        try:
            with self.read_content(user, lid, rel) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            with self.db() as db:
                db.execute("INSERT INTO file_versions VALUES(?,?,?,?,?,?,?,?)",
                           (vid, lid, rel, current, self.content_size(lid, fp), digest, user["id"], time.time()))
        except Exception:
            archived.unlink(missing_ok=True)
            raise
        return vid

    def versions_for(self, user, lid, rel):
        rel = clean_path(rel)
        with self.file_locks(lid):
            self.access(user, lid, rel=rel)
            return self.all("SELECT file_versions.*,u.name actor FROM file_versions LEFT JOIN users u ON u.id=file_versions.user_id "
                            "WHERE library_id=? AND path=? AND " + ACTIVE_VERSION + " ORDER BY file_versions.created DESC", (lid, rel))

    def version(self, vid):
        row = self.one("SELECT * FROM file_versions WHERE id=? AND " + ACTIVE_VERSION, (vid,))
        if not row:
            raise DriveError(404, "历史版本不存在")
        return row

    def version_file(self, vid):
        blob = self.one("SELECT blob_id FROM version_blobs WHERE version_id=?", (vid,))
        return self.history / (blob['blob_id'] if blob else vid)

    def restore_version(self, user, vid):
        row = self.version(vid)
        with self.file_locks(row["library_id"]):
            row = self.version(vid)
            self.access(user, row["library_id"], True, rel=row["path"])
            target = self.path(row["library_id"], row["path"], True)
            if not target.parent.is_dir():
                raise DriveError(409, "请先恢复或创建上级目录")
            tmp = self.staging_directory(row["library_id"]) / ("restore-" + uuid.uuid4().hex)
            try:
                with self.read_content(user, row["library_id"], row["path"], self.version_file(vid)) as src, open(tmp, "xb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                self.commit_file(user, row["library_id"], row["path"], tmp, signature(target), "version_restore")
            finally:
                tmp.unlink(missing_ok=True)

    def save_text(self, user, lid, rel, content, expected):
        with self.file_locks(lid):
            self.access(user, lid, True, rel=rel)
            target = self.path(lid, rel, True)
            self.check_unlocked(lid, clean_path(rel))
            if signature(target) != expected:
                raise DriveError(409, "文件已被其他人修改，请重新打开或另存为")
            original, encoding = self.read_text(user, lid, rel)
            if encoding is None:
                raise DriveError(400, "文件编码无法安全识别，请通过电脑盘编辑")
            normalize = lambda value: re.sub(r'\r\n?', '\n', value)
            edited = normalize(content)
            if edited == normalize(original):
                return self.entry(lid, target)
            breaks = re.findall(r'\r\n|\r|\n', original)
            if breaks:
                separator = max(dict.fromkeys(breaks), key=breaks.count)
                edited = edited.replace('\n', separator)
            try:
                data = edited.encode(encoding)
            except UnicodeEncodeError as exc:
                raise DriveError(400, "修改包含原文件编码不支持的字符，请通过电脑盘另存为 UTF-8") from exc
            if encoding == 'utf-16-le':
                data = b'\xff\xfe' + data
            elif encoding == 'utf-16-be':
                data = b'\xfe\xff' + data
            if len(data) > 4 * 1024**2:
                raise DriveError(413, "在线文本编辑最多 4MB")
            tmp = self.staging_directory(lid) / ("text-" + uuid.uuid4().hex)
            try:
                tmp.write_bytes(data)
                return self.commit_file(user, lid, rel, tmp, expected, "edit", entry_point='text_editor')
            finally:
                tmp.unlink(missing_ok=True)

    def read_text(self, user, lid, rel):
        with self.read_content(user, lid, rel) as reader:
            raw = reader.read(4 * 1024**2 + 1)
        if len(raw) > 4 * 1024**2:
            raise DriveError(413, "文本预览最多 4MB，请下载或使用电脑盘")
        encodings = ('utf-8', 'gb18030')
        if raw.startswith(b'\xef\xbb\xbf'):
            encodings = ('utf-8-sig',)
        elif raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            # Decode without the marker, and prepend the original marker on save.
            encoding = 'utf-16-le' if raw.startswith(b'\xff\xfe') else 'utf-16-be'
            try:
                return raw[2:].decode(encoding), encoding
            except UnicodeDecodeError:
                return raw[2:].decode(encoding, errors='replace'), None
        for encoding in encodings:
            try:
                content = raw.decode(encoding)
                if '\0' not in content:
                    return content, encoding
            except UnicodeDecodeError:
                continue
        return raw.decode('gb18030', errors='replace'), None

    def create_document(self, user, lid, rel, kind):
        expected_suffix = {"markdown": ".md", "text": ".txt", "word": ".docx", "excel": ".xlsx", "ppt": ".pptx"}.get(kind)
        if not expected_suffix:
            raise DriveError(400, "文档类型无效")
        if not rel.lower().endswith(expected_suffix):
            rel += expected_suffix
        rel = clean_path(rel)
        self.access(user, lid, True, rel=rel)
        temp = self.staging_directory(lid) / ("document-" + uuid.uuid4().hex)
        try:
            if kind in ("markdown", "text"):
                temp.write_text("# " + Path(rel).stem + "\n" if kind == "markdown" else "", encoding="utf-8")
            elif kind == "word":
                from docx import Document
                document = Document()
                document.add_paragraph("")
                document.save(temp)
            elif kind == "excel":
                from openpyxl import Workbook
                workbook = Workbook()
                workbook.active.title = "工作表1"
                workbook.save(temp)
            else:
                from pptx import Presentation
                presentation = Presentation()
                presentation.slides.add_slide(presentation.slide_layouts[6])
                presentation.save(temp)
            return self.commit_file(user, lid, rel, temp, "missing", "create")
        finally:
            temp.unlink(missing_ok=True)

    def remap_metadata(self, source_lid, source, dest_lid, dest):
        with self.db() as db:
            self._remap_metadata(db, source_lid, source, dest_lid, dest)

    def _remap_metadata(self, db, lid, source, dest_lid, dest, versions=()):
        scope, args = path_scope(lid, source)
        tables = (*METADATA, 'entry_owners')
        if lid == dest_lid:
            tables += ('library_shares', 'share_links', 'file_versions')
        else:
            revoke_shares(db, lid, source)
            detach_versions(db, lid, source)
            for row, vid, blob in versions:
                self._attach_version(db, dest_lid, dest + row['path'][len(source):], row, vid, blob)
        for table in tables:
            active = ' AND ' + ACTIVE_VERSION if table == 'file_versions' else ''
            db.execute(f"UPDATE {table} SET library_id=?,path=?||substr(path,?) WHERE {scope}{active}",
                       (dest_lid, dest, len(source) + 1, *args))

    def _attach_version(self, db, lid, rel, row, vid, blob):
        db.execute("INSERT INTO file_versions VALUES(?,?,?,?,?,?,?,?)",
                   (vid, lid, rel, row['signature'], row['size'], row['sha256'], row['user_id'], row['created']))
        if vid != blob:
            db.execute("INSERT INTO version_blobs VALUES(?,?)", (vid, blob))

    def _stage_copy(self, source, target, source_key, dest_key, scratch):
        if not source_key and not dest_key:
            shutil.copy2(source, target)
        else:
            plaintext = scratch / uuid.uuid4().hex
            try:
                if source_key:
                    with EncryptedReader(source, source_key) as reader, (plaintext if dest_key else target).open('xb') as out:
                        shutil.copyfileobj(reader, out, 1024 * 1024)
                if dest_key:
                    seal(plaintext if source_key else source, target, dest_key)
            finally:
                plaintext.unlink(missing_ok=True)

    def _transfer_tree(self, source, target, source_key, dest_key, scratch):
        if source.is_symlink() or source.is_junction():
            raise DriveError(403, "不能复制符号链接或目录联接")
        if source.is_dir():
            target.mkdir()
            for child in source.iterdir():
                self._transfer_tree(child, target / valid_name(child.name), source_key, dest_key, scratch)
        elif source.is_file():
            self._stage_copy(source, target, source_key, dest_key, scratch)
        else:
            raise DriveError(400, "不能复制此文件类型")

    def transfer_entries(self, user, lid, paths, destination_lid, destination, move=False):
        if not paths or len(paths) > 200:
            raise DriveError(400, "每次选择 1–200 个项目")
        destination = clean_path(destination)
        with self.file_locks(lid, destination_lid):
            self.access(user, destination_lid, True, rel=destination)
            if not self.path(destination_lid, destination, True).is_dir():
                raise DriveError(400, "目标必须是文件夹")
            paths = list(dict.fromkeys(clean_path(path) for path in paths))
            self._transfer(user, lid, [(path, '/'.join(filter(None, (destination, Path(path).name)))) for path in paths],
                           destination_lid, move)

    def _transfer(self, user, lid, entries, dest_lid, move, *, check_lock=True):
        plan, targets = [], set()
        for rel, dest in entries:
            dest = clean_path(dest)
            if not rel or not dest:
                raise DriveError(400, "不能移动或复制资料库根目录")
            if any(rel != other and within(rel, other) for other, _ in entries):
                raise DriveError(400, "请勿同时选择文件夹和其中的项目")
            source_lib, _ = self.access(user, lid, move, rel=rel)
            dest_lib, _ = self.access(user, dest_lid, True, rel=dest)
            if move:
                self.require_remove_entries(user, lid, rel)
            source, target = self.path(lid, rel, True), self.path(dest_lid, dest)
            if not target.parent.is_dir():
                raise DriveError(409, "目标文件夹不存在")
            if target.exists() or target in targets or (lid == dest_lid and within(dest, rel)):
                raise DriveError(409, "目标有同名项目，或目标位于源目录内部")
            if check_lock:
                self.check_unlocked(lid, rel)
                self.check_unlocked(dest_lid, dest)
            targets.add(target)
            plan.append((rel, dest, source, target))

        source_key = self.library_key(user, lid) if source_lib['encryption'] else None
        dest_key = self.library_key(user, dest_lid) if dest_lib['encryption'] else None
        direct_move = move and (lid == dest_lid or not (source_key or dest_key))
        converted = {}
        with TemporaryDirectory(dir=self.tmp) as temp, TemporaryDirectory(dir=self.secure_staging) as plain, ExitStack() as cleanup:
            scratch = Path(plain)
            stage = Path(temp)
            for index, (rel, _, source, _) in enumerate(plan):
                if not direct_move:
                    self._transfer_tree(source, stage / str(index), source_key, dest_key, scratch)
                if move and lid != dest_lid:
                    scope, args = path_scope(lid, rel)
                    converted[rel] = []
                    for row in self.all(f"SELECT * FROM file_versions WHERE {scope} AND {ACTIVE_VERSION}", args):
                        vid = uuid.uuid4().hex
                        physical = self.version_file(row['id'])
                        blob = physical.name
                        if source_key or dest_key:
                            dest_version = self.history / vid
                            cleanup.callback(dest_version.unlink, missing_ok=True)
                            self._stage_copy(physical, dest_version, source_key, dest_key, scratch)
                            blob = vid
                        converted[rel].append((row, vid, blob))
            for rel, dest, _, target in plan:
                self.access(user, lid, move, rel=rel)
                self.access(user, dest_lid, True, rel=dest)
                if move:
                    self.require_remove_entries(user, lid, rel)
                if target.exists():
                    raise DriveError(409, "目标已出现同名项目")
                if check_lock:
                    self.check_unlocked(lid, rel)
                    self.check_unlocked(dest_lid, dest)
            with ExitStack() as rollback:
                with self.db() as db:
                    for index, (rel, dest, source, target) in enumerate(plan):
                        ready = source if direct_move else stage / str(index)
                        os.replace(ready, target)
                        rollback.callback(os.replace, target, ready)
                        if move and not direct_move:
                            backup = stage / ('original-' + str(index))
                            os.replace(source, backup)
                            rollback.callback(os.replace, backup, source)
                        if move:
                            self._remap_metadata(db, lid, rel, dest_lid, dest, converted.get(rel, ()))
                        else:
                            copies = [target, *target.rglob('*')] if target.is_dir() else [target]
                            db.executemany("INSERT INTO entry_owners VALUES(?,?,?)",
                                           [(dest_lid, item.relative_to(self.files / dest_lid).as_posix(), user['id']) for item in copies])
                        self.event(user, dest_lid, 'move' if move else 'copy', dest,
                                   previous_path=rel if move and lid == dest_lid else None, db=db)
                        if move and lid != dest_lid:
                            self.event(user, lid, 'move', rel, db=db)
                rollback.pop_all()
            cleanup.pop_all()

    def copy_file(self, user, lid, rel, dest_lid, dest_rel, expected="missing", check_lock=True):
        temp = self.staging_directory(dest_lid) / ("copy-" + uuid.uuid4().hex)
        try:
            with self.read_content(user, lid, rel) as src, open(temp, "xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            return self.commit_file(user, dest_lid, dest_rel, temp, expected, "copy", check_lock)
        finally:
            temp.unlink(missing_ok=True)

    def create_snapshot(self, user, lid, label):
        self.owner(user, lid)
        self.access(user, lid)
        if not label.strip() or len(label) > 100:
            raise DriveError(400, "快照名称需要 1–100 字")
        manifest = {"directories": [], "files": {}}
        root = self.path(lid)
        with self.file_locks(lid):
            self.owner(user, lid)
            self.access(user, lid)
            manifest['owners'] = {row['path']: row['owner_id'] for row in self.all(
                'SELECT path,owner_id FROM entry_owners WHERE library_id=?', (lid,))}
            for parent, dirs, files in os.walk(root, followlinks=False):
                for name in list(dirs):
                    fp = Path(parent) / name
                    if fp.is_symlink() or fp.is_junction():
                        dirs.remove(name)
                        continue
                    manifest["directories"].append(fp.relative_to(root).as_posix())
                for name in files:
                    fp = Path(parent) / name
                    if fp.is_symlink() or not fp.is_file():
                        continue
                    rel = fp.relative_to(root).as_posix()
                    manifest["files"][rel] = self.record_version(user, lid, rel)
            sid = uuid.uuid4().hex
            with self.db() as db:
                included = set(manifest['directories']) | manifest['files'].keys() | {''}
                manifest['metadata'] = {table: [row for row in rows if row['path'] in included]
                                        for table, rows in collaboration(db, lid).items()}
                manifest['versions'] = {rel: [] for rel in manifest['files']}
                for version in db.execute("SELECT id,path FROM file_versions WHERE library_id=? AND " + ACTIVE_VERSION, (lid,)):
                    if version['path'] in manifest['versions']:
                        manifest['versions'][version['path']].append(version['id'])
                db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?)", (sid, lid, label.strip(), json.dumps(manifest, ensure_ascii=False), user["id"], time.time()))
        self.event(user, lid, "snapshot", label.strip())
        return sid

    def restore_snapshot(self, user, sid):
        row = self.one("SELECT * FROM snapshots WHERE id=?", (sid,))
        if not row:
            raise DriveError(404, "快照不存在")
        lid = row["library_id"]
        self.owner(user, lid)
        self.access(user, lid, True, rel="")
        self.check_unlocked(lid, "")
        manifest = json.loads(row["manifest"])
        staged = self.tmp / ("snapshot-stage-" + uuid.uuid4().hex)
        previous = self.tmp / ("snapshot-original-" + uuid.uuid4().hex)
        with self.file_locks(lid):
            self.require_remove_entries(user, lid, '', root=True)
            self.check_unlocked(lid, "")
            self.create_snapshot(user, lid, "恢复前自动备份 " + time.strftime("%Y-%m-%d %H:%M"))
            staged.mkdir()
            root = self.path(lid, must_exist=True)
            try:
                for directory in manifest["directories"]:
                    (staged / clean_path(directory)).mkdir(parents=True, exist_ok=True)
                versions, renamed = {}, {}
                for rel, vid in manifest["files"].items():
                    version = self.one("SELECT * FROM file_versions WHERE id=? AND library_id=?", (vid, lid))
                    if not version:
                        raise DriveError(409, "快照中的文件版本已缺失")
                    if 'versions' in manifest:
                        ids = manifest['versions'][rel]
                        versions[rel] = [self.one("SELECT v.*,coalesce(b.blob_id,v.id) blob_id FROM file_versions v "
                                                  "LEFT JOIN version_blobs b ON b.version_id=v.id WHERE v.id=? AND v.library_id=?", (item, lid))
                                         for item in ids]
                    else:
                        renamed[version['path']] = rel
                        versions[rel] = self.all("SELECT v.*,coalesce(b.blob_id,v.id) blob_id FROM file_versions v "
                                                 "LEFT JOIN version_blobs b ON b.version_id=v.id WHERE v.library_id=? AND v.path=? AND v.created<=?",
                                                 (lid, version['path'], row['created']))
                    if any(item is None for item in versions[rel]):
                        raise DriveError(409, "快照中的文件版本已缺失")
                    target = staged / clean_path(rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    archived = self.version_file(vid)
                    try:
                        os.link(archived, target)
                    except OSError:
                        shutil.copy2(archived, target)
                self.access(user, lid, True, rel="")
                self.check_unlocked(lid, "")
                os.replace(root, previous)
                try:
                    os.replace(staged, root)
                    with self.db() as db:
                        db.execute('DELETE FROM entry_owners WHERE library_id=?', (lid,))
                        included = set(manifest['directories']) | manifest['files'].keys()
                        db.executemany('INSERT INTO entry_owners VALUES(?,?,?)',
                                       [(lid, path, owner) for path, owner in manifest.get('owners', {}).items() if path in included])
                        if 'metadata' in manifest:
                            metadata = manifest['metadata']
                        else:
                            metadata = {table: [dict(entry, path=renamed.get(entry['path'], entry['path'])) for entry in rows
                                                if renamed.get(entry['path'], entry['path']) in included or not entry['path']]
                                        for table, rows in collaboration(db, lid).items()}
                        for table in METADATA:
                            db.execute(f"DELETE FROM {table} WHERE library_id=?", (lid,))
                        put_collaboration(db, lid, metadata, restore_snapshot=True)
                        detach_versions(db, lid)
                        for rel, history in versions.items():
                            for item in history:
                                self._attach_version(db, lid, rel, item, uuid.uuid4().hex, item['blob_id'])
                        revoke_shares(db, lid, '')
                except Exception:
                    if root.exists():
                        os.replace(root, staged)
                    os.replace(previous, root)
                    raise
                self.event(user, lid, "snapshot_restore", row["label"])
            finally:
                if staged.is_dir() and staged.resolve().parent == self.tmp.resolve():
                    shutil.rmtree(staged)
            if previous.is_dir() and previous.resolve().parent == self.tmp.resolve():
                shutil.rmtree(previous)

    def touch_device(self, token_id, ip):
        now = time.time()
        with self.db() as db:
            updated = db.execute("UPDATE tokens SET last_ip=?,last_seen=? WHERE id=? AND "
                                 "(last_seen IS NULL OR last_seen<? OR last_ip IS NOT ?) RETURNING user_id",
                                 (ip, now, token_id, now-60, ip)).fetchone()
            if updated and getattr(self, 'usage', None):
                self.usage.touch_account(db, updated['user_id'], now)
