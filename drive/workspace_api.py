from __future__ import annotations

import json
import mimetypes
import os
from collections import OrderedDict
from pathlib import Path
from typing import Literal
import re
from threading import BoundedSemaphore, Lock
import time
import uuid
from urllib.parse import quote

from fastapi import Body, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .store import DriveError, clean_path, signature, valid_name
from .file_crypto import plain_size
from .file_search import search_files
from .archives import Archives
from .usage import note_download

TEXT_TYPES = {".txt", ".md", ".markdown", ".csv", ".json", ".xml", ".log", ".yaml", ".yml", ".ini", ".py", ".js", ".ts", ".css", ".html", ".sql"}
INLINE_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".pdf", ".mp4", ".webm", ".mp3", ".wav", ".ogg"}


def content_response(store, user, lid, rel, request, inline=False, physical=None, filename=None, *, audit=False):
    lib, _ = store.access(user, lid, rel=rel)
    fp = Path(physical) if physical else store.path(lid, rel, True)
    if not fp.is_file():
        raise DriveError(400, "请选择文件")
    filename = filename or Path(rel).name
    suffix = Path(filename).suffix.lower()
    media_type = (mimetypes.guess_type(filename)[0] or "application/octet-stream") if inline and suffix in INLINE_TYPES else "text/plain; charset=utf-8" if inline and suffix in TEXT_TYPES else "application/octet-stream"
    disposition = "inline" if inline else "attachment"
    if audit and not inline:
        note_download(request, user, 'native', lid, rel)
    if not lib["encryption"]:
        return FileResponse(fp, filename=filename, media_type=media_type, content_disposition_type=disposition)
    reader = store.read_content(user, lid, rel, fp)
    size = reader.size
    # The encrypted file's random salt is tied to the open file handle.
    etag = '"' + reader.header[8:40].hex() + '"'
    start, end, status = 0, size - 1, 200
    range_header = request.headers.get("range")
    if range_header and request.headers.get("if-range", etag) == etag:
        match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", range_header)
        if not match or not any(match.groups()) or not size:
            reader.close()
            return JSONResponse({"detail": "无效的文件范围"}, status_code=416, headers={"Content-Range": f"bytes */{size}"})
        a, b = match.groups()
        if a:
            start, end = int(a), min(int(b), end) if b else end
        else:
            start = max(0, size - int(b))
        if start > end or start >= size:
            reader.close()
            return JSONResponse({"detail": "文件范围超出大小"}, status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
    reader.seek(start)
    headers = {"Accept-Ranges": "bytes", "ETag": etag, "Content-Length": str(max(0, end - start + 1)),
               "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}"}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    def chunks():
        try:
            remaining = max(0, end - start + 1)
            while remaining:
                data = reader.read(min(1024 * 1024, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data
        finally:
            reader.close()
    return StreamingResponse(chunks(), status_code=status, media_type=media_type, headers=headers)


def register_workspace_routes(app, store, user, admin):
    archives = Archives(store)
    app.state.archives = archives
    summary_cache = OrderedDict()
    summary_lock = Lock()
    summary_slots = BoundedSemaphore(2)
    @app.get("/api/workspace")
    def workspace(include_favorites: bool = False, current=Depends(user)):
        # License status may persist a clock check. Resolve it before read aggregation.
        license = store.license.status() if current["admin"] else store.license.access_mode()
        with store.reuse_read_connection():
            return workspace_data(include_favorites, current, license)

    def workspace_data(include_favorites, current, license):
        return {"libraries": store.libraries_for(current), "groups": store.groups_for(current),
                "license": license,
                "banner": (store.one("SELECT value FROM settings WHERE key='banner'") or {}).get("value", ""),
                "office_enabled": app.state.office_enabled,
                "legacy_enabled": app.state.legacy_enabled,
                "unread": store.one("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND read_at IS NULL", (current["id"],))["n"],
                **({"favorites": store.favorites_for(current)} if include_favorites else {})}

    @app.get("/api/libraries")
    def libraries(view: str = "all", group_id: str | None = None, current=Depends(user)):
        return store.libraries_for(current, view, group_id)

    @app.get('/api/group-libraries', summary='List authorized native libraries grouped by visible groups')
    def group_libraries(current=Depends(user)):
        return store.group_library_catalog(current)

    @app.post("/api/libraries", status_code=201)
    def create_library(name: str = Body(max_length=80), description: str = Body(default="", max_length=500),
                       visibility: str = Body(default="private"), password: str = Body(default="", max_length=200), current=Depends(user)):
        return store.create_library(current, name, description, visibility, password)

    @app.patch("/api/libraries/{lid}")
    def edit_library(lid: str, changes: dict = Body(), current=Depends(user)):
        return store.update_library(current, lid, changes)

    @app.delete("/api/libraries/{lid}")
    def delete_library(lid: str, current=Depends(user)):
        store.remove_library(current, lid)
        return {"ok": True}

    @app.get("/api/deleted-libraries")
    def deleted_libraries(current=Depends(admin)):
        return store.all("SELECT id,name,description,deleted FROM libraries WHERE deleted IS NOT NULL ORDER BY deleted DESC")

    @app.post("/api/libraries/{lid}/restore")
    def restore_library(lid: str, current=Depends(admin)):
        store.restore_library(current, lid)
        return {"ok": True}

    @app.post("/api/libraries/{lid}/unlock")
    def unlock_library(lid: str, password: str = Body(embed=True, max_length=200), current=Depends(user)):
        store.unlock_library(current, lid, password)
        return {"ok": True, "expires_in": 3600}

    @app.post("/api/libraries/{lid}/lock")
    def lock_library(lid: str, current=Depends(user)):
        store.access(current, lid, require_key=False)
        store.key_cache.pop((current["id"], lid), None)
        return {"ok": True}

    @app.get("/api/libraries/{lid}/files")
    def library_files(lid: str, path: str = "", q: str = "", offset: int = Query(0, ge=0),
                      limit: int | None = Query(None, ge=1, le=500),
                      order: Literal['name','modified','size'] = 'name', descending: bool = False, current=Depends(user)):
        with store.reuse_read_connection():
            return store.listing(current, lid, path, q, offset=offset, limit=limit, order=order, descending=descending)

    @app.get("/api/libraries/{lid}/properties")
    def library_properties(lid: str, path: str = "", current=Depends(user)):
        lib, role = store.access(current, lid, rel=path)
        directory = store.path(lid, path, True)
        if not directory.is_dir():
            raise DriveError(400, "请选择资料库或目录")
        return dict(store.directory_summary(directory, bool(lib["encryption"]), 100000), role=role)

    @app.get("/api/library-summaries")
    def library_summaries(library_id: list[str] = Query(max_length=16), path: list[str] = Query(max_length=16), current=Depends(user)):
        if len(library_id) != len(path):
            raise DriveError(400, "资料库和目录数量不一致")
        _, catalog = store.library_grants(current)
        revisions = {row['group_id']: row['id'] for row in store.all(
            "SELECT group_id,MAX(id) AS id FROM events WHERE group_id IN (SELECT value FROM json_each(?)) AND action IN "
            "('upload','mkdir','rename','delete','restore','move','copy','edit','create','version_restore','snapshot_restore') GROUP BY group_id",
            (json.dumps(list(set(library_id) & catalog.keys())),))}
        result = []
        for lid, raw in zip(library_id, path):
            rel = clean_path(raw)
            entry = catalog.get(lid)
            if not entry or not store.role_for_path(entry[1], rel):
                continue
            lib = entry[0]
            try:
                if lib["encryption"]:
                    store.library_key(current, lid)
                directory = store.path(lid, rel, True)
                if not directory.is_dir():
                    continue
                # A change from the app invalidates the cached size immediately.
                key = lid, rel
                stamp = (revisions.get(lid), directory.stat().st_mtime_ns)
                with summary_lock:
                    cached = summary_cache.get(key)
                    if cached and cached[0] == stamp and cached[1] > time.monotonic():
                        summary_cache.move_to_end(key)
                        result.append(dict(cached[2], library_id=lid, path=rel))
                        continue
                if not summary_slots.acquire(blocking=False):
                    continue
                try:
                    stats = store.directory_summary(directory, bool(lib["encryption"]), 5000)
                finally:
                    summary_slots.release()
                with summary_lock:
                    summary_cache[key] = stamp, time.monotonic() + 60, stats
                    summary_cache.move_to_end(key)
                    if len(summary_cache) > 256:
                        summary_cache.popitem(last=False)
                result.append(dict(stats, library_id=lid, path=rel))
            except (DriveError, FileNotFoundError):
                continue
        return result

    @app.post("/api/libraries/{lid}/ensure-folder")
    def ensure_folder(lid: str, path: str = Body(embed=True), current=Depends(user)):
        store.access(current, lid, True, rel=path)
        with store.file_locks(lid):
            store.access(current, lid, True, rel=path)
            if not store.path(lid, path).is_dir():
                store.mkdir(current, lid, path)
        return {"ok": True}

    @app.get("/api/libraries/{lid}/content")
    def file_content(lid: str, request: Request, path: str, inline: bool = False, current=Depends(user)):
        return content_response(store, current, lid, path, request, inline, audit=True)

    @app.get("/api/libraries/{lid}/text")
    def text_content(lid: str, path: str, current=Depends(user)):
        with store.file_locks(lid):
            _, role = store.access(current, lid, rel=path)
            fp = store.path(lid, path, True)
            if fp.suffix.lower() not in TEXT_TYPES or not fp.is_file():
                raise DriveError(400, "这个格式不能在文本编辑器中打开")
            content, encoding = store.read_text(current, lid, path)
            return {"content": content, "etag": signature(fp),
                    "editable": encoding is not None and role == "editor" and store.license.status()["can_write"]}

    @app.put("/api/libraries/{lid}/text")
    def save_text(lid: str, path: str = Body(), content: str = Body(max_length=4 * 1024**2), etag: str = Body(), current=Depends(user)):
        if Path(path).suffix.lower() not in TEXT_TYPES:
            raise DriveError(400, "只支持编辑文本格式")
        return store.save_text(current, lid, path, content, etag)

    @app.post("/api/libraries/{lid}/documents", status_code=201)
    def new_document(lid: str, path: str = Body(), kind: str = Body(), current=Depends(user)):
        return store.create_document(current, lid, path, kind)

    @app.post("/api/libraries/{lid}/transfer")
    def transfer(lid: str, paths: list[str] = Body(), destination_library: str = Body(), destination: str = Body(default=""), move: bool = Body(default=False), current=Depends(user)):
        store.transfer_entries(current, lid, paths, destination_library, destination, move)
        return {"ok": True}

    @app.post("/api/libraries/{lid}/batch-delete")
    def batch_delete(lid: str, paths: list[str] = Body(embed=True), current=Depends(user)):
        if not paths or len(paths) > 200:
            raise DriveError(400, "每次选择 1–200 个项目")
        results = []
        for path in paths:
            try:
                store.delete(current, lid, path)
                results.append({"path": path, "ok": True})
            except DriveError as exc:
                results.append({"path": path, "ok": False, "error": exc.message})
        return {"results": results}

    @app.post("/api/libraries/{lid}/archive")
    def archive(lid: str, request: Request, paths: list[str] = Body(embed=True), current=Depends(user)):
        note_download(request, current, 'native', lid, '批量打包')
        return archives.immediate(current, lid, paths)

    @app.get("/api/libraries/{lid}/archive")
    def archive_download(lid: str, request: Request, path: list[str] = Query(), current=Depends(user)):
        note_download(request, current, 'native', lid, '批量打包')
        return archives.immediate(current, lid, path)

    @app.post("/api/libraries/{lid}/archive-jobs", status_code=202)
    def start_archive(lid: str, paths: list[str] = Body(embed=True), current=Depends(user)):
        return archives.start(current, lid, paths)

    @app.get("/api/archive-jobs/{uid}")
    def archive_status(uid: str, current=Depends(user)):
        return archives.status(uid, current)

    @app.delete("/api/archive-jobs/{uid}")
    def cancel_archive(uid: str, current=Depends(user)):
        archives.cancel(uid, current)
        return {"ok": True}

    @app.get("/api/archive-jobs/{uid}/download")
    def archive_file(uid: str, request: Request, current=Depends(user)):
        return archives.download(uid, current, partial="range" in request.headers, request=request)

    @app.get("/api/people")
    def people(q: str = "", current=Depends(user)):
        if store.identity_directory:
            store.identity_directory.sync()
        q = q[:60]
        return store.all("SELECT id,name,username FROM users WHERE active=1 AND (name LIKE ? OR username LIKE ?) ORDER BY name LIMIT 200", ("%"+q+"%", "%"+q+"%"))

    @app.get("/api/shares")
    def shares(library_id: str | None = None, current=Depends(user)):
        return store.shares_for(current, library_id)

    @app.post("/api/libraries/{lid}/shares")
    def share(lid: str, path: str = Body(default=""), subject_type: str = Body(), subject_id: str = Body(), role: str = Body(default="reader"), current=Depends(user)):
        store.share(current, lid, path, subject_type, subject_id, role)
        return {"ok": True}

    @app.delete("/api/shares/{sid}")
    def unshare(sid: str, current=Depends(user)):
        store.unshare(current, sid)
        return {"ok": True}

    @app.get("/api/favorites")
    def favorites(current=Depends(user)):
        return store.favorites_for(current)

    @app.put("/api/favorites")
    def favorite(library_id: str = Body(), path: str = Body(default=""), enabled: bool = Body(default=True), current=Depends(user)):
        store.favorite(current, library_id, path, enabled)
        return {"ok": True}

    @app.put("/api/libraries/{lid}/tags")
    def tags(lid: str, path: str = Body(), tags: list[str] = Body(), current=Depends(user)):
        store.set_tags(current, lid, path, tags)
        return {"ok": True}

    @app.get("/api/libraries/{lid}/comments")
    def comments(lid: str, path: str, current=Depends(user)):
        store.access(current, lid, rel=path)
        store.path(lid, path, True)
        return store.all("SELECT c.id,c.body,c.created,c.user_id,u.name actor FROM comments c JOIN users u ON u.id=c.user_id "
                         "WHERE c.library_id=? AND c.path=? ORDER BY c.created", (lid, clean_path(path)))

    @app.post("/api/libraries/{lid}/comments", status_code=201)
    def comment(lid: str, path: str = Body(), body: str = Body(min_length=1, max_length=4000), current=Depends(user)):
        cid = uuid.uuid4().hex
        with store.file_locks(lid):
            store.access(current, lid, rel=path)
            store.path(lid, path, True)
            with store.db() as db:
                db.execute("INSERT INTO comments VALUES(?,?,?,?,?,?)", (cid, lid, clean_path(path), current["id"], body.strip(), time.time()))
            store.event(current, lid, "comment", path)
        return {"id": cid}

    @app.delete("/api/comments/{cid}")
    def delete_comment(cid: str, current=Depends(user)):
        row = store.one("SELECT * FROM comments WHERE id=?", (cid,))
        if not row:
            raise DriveError(404, "评论不存在")
        with store.file_locks(row["library_id"]), store.db() as db:
            row = db.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
            if not row:
                raise DriveError(404, "评论不存在")
            store.access(current, row["library_id"], rel=row["path"])
            if row["user_id"] != current["id"]:
                store.owner(current, row["library_id"])
            db.execute("DELETE FROM comments WHERE id=?", (cid,))
        return {"ok": True}

    @app.get("/api/search")
    def search(q: str = "", tag: str = "", cursor: str = "", limit: int = Query(500, ge=1, le=500), current=Depends(user)):
        if len(q) > 100 or len(tag) > 32 or not (q.strip() or tag):
            raise DriveError(400, "请输入关键词或标签")
        with store.reuse_read_connection():
            return search_files(store, current, q, tag, cursor, limit)

    @app.get("/api/libraries/{lid}/versions")
    def versions(lid: str, path: str, current=Depends(user)):
        return store.versions_for(current, lid, path)

    @app.get("/api/versions/{vid}/download")
    def version_download(vid: str, request: Request, current=Depends(user)):
        row = store.version(vid)
        with store.file_locks(row["library_id"]):
            row = store.version(vid)
        return content_response(store, current, row["library_id"], row["path"], request,
                                physical=store.version_file(vid), audit=True)

    @app.post("/api/versions/{vid}/restore")
    def version_restore(vid: str, current=Depends(user)):
        store.restore_version(current, vid)
        return {"ok": True}

    @app.get("/api/libraries/{lid}/snapshots")
    def snapshots(lid: str, current=Depends(user)):
        store.access(current, lid, rel="")
        return store.all("SELECT id,label,user_id,created FROM snapshots WHERE library_id=? ORDER BY created DESC", (lid,))

    @app.post("/api/libraries/{lid}/snapshots", status_code=201)
    def snapshot(lid: str, label: str = Body(embed=True), current=Depends(user)):
        return {"id": store.create_snapshot(current, lid, label)}

    @app.post("/api/snapshots/{sid}/restore")
    def snapshot_restore(sid: str, current=Depends(user)):
        store.restore_snapshot(current, sid)
        return {"ok": True}

    @app.get("/api/links")
    def links(current=Depends(user)):
        return store.links_for(current)

    @app.post("/api/libraries/{lid}/links", status_code=201)
    def create_link(lid: str, path: str = Body(default=""), kind: str = Body(default="download"), password: str = Body(default="", max_length=200), expires: float | None = Body(default=None), current=Depends(user)):
        return store.create_link(current, lid, path, kind, password, expires)

    @app.post("/api/libraries/{lid}/batch-links", status_code=201)
    def batch_links(lid: str, paths: list[str] = Body(), password: str = Body(default="", max_length=200),
                    expires: float | None = Body(default=None), current=Depends(user)):
        if not 1 <= len(paths) <= 100:
            raise DriveError(400, "每次选择 1–100 个项目生成链接")
        with store.file_locks(lid):
            store.owner(current, lid)
            selected = list(dict.fromkeys(clean_path(path) for path in paths))
            for path in selected:
                store.path(lid, path, True)
            return [store.create_link(current, lid, path, "download", password, expires) for path in selected]

    @app.delete("/api/links/{link_id}")
    def revoke_link(link_id: str, current=Depends(user)):
        row = store.one("SELECT * FROM share_links WHERE id=?", (link_id,))
        if not row:
            raise DriveError(404, "链接不存在")
        store.owner(current, row["library_id"])
        with store.db() as db:
            db.execute("UPDATE share_links SET active=0 WHERE id=?", (link_id,))
        return {"ok": True}

    @app.post("/api/links/clean-expired")
    def clean_links(current=Depends(user)):
        expired = []
        now = time.time()
        for row in store.all("SELECT * FROM share_links WHERE creator=? AND active=1", (current["id"],)):
            if row["expires"] is not None and row["expires"] <= now:
                expired.append((row["id"],))
                continue
            try:
                store.owner(current, row["library_id"])
                store.path(row["library_id"], row["path"], True)
            except DriveError:
                expired.append((row["id"],))
        with store.db() as db:
            cursor = db.executemany("UPDATE share_links SET active=0 WHERE id=? AND active=1", expired)
        return {"count": cursor.rowcount}

    @app.get("/api/notifications")
    def notifications(current=Depends(user)):
        return store.all("SELECT * FROM notifications WHERE user_id=? ORDER BY created DESC LIMIT 100", (current["id"],))

    @app.post("/api/notifications/read")
    def read_notifications(current=Depends(user)):
        with store.db() as db:
            db.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL", (time.time(), current["id"]))
        return {"ok": True}

    @app.put("/api/announcement")
    def announcement(text: str = Body(embed=True, max_length=1000), current=Depends(admin)):
        with store.db() as db:
            db.execute("INSERT INTO settings VALUES('banner',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (text,))
        return {"ok": True}

    @app.patch("/api/profile")
    def profile(name: str = Body(embed=True, min_length=1, max_length=60), current=Depends(user)):
        if not name.strip():
            raise DriveError(400, "姓名不能为空")
        if store.identity_directory:store.identity_directory.set_name(current['id'],name.strip())
        with store.db() as db:
            db.execute("UPDATE users SET name=? WHERE id=?", (name.strip(), current["id"]))
        return store.get_user(current["id"])

    @app.patch("/api/team-groups/{gid}")
    def group_update(gid: str, name: str = Body(), description: str = Body(default="", max_length=240), current=Depends(user)):
        store.manage_group(current, gid)
        name = valid_name(name.strip())
        with store.db() as db:
            try:
                db.execute("UPDATE groups SET name=?,description=? WHERE id=?", (name, description, gid))
            except Exception:
                raise DriveError(409, "群组名已存在或无效")
        return {"ok": True}

    def public_access(token, request):
        return store.resolve_link(token, request.cookies.get("drive_link_" + token[:12]))

    def public_path(row, suffix):
        suffix = clean_path(suffix)
        root = store.path(row["library_id"], row["path"], True)
        if not root.is_dir() and suffix:
            raise DriveError(404, "分享文件不存在")
        return "/".join(n for n in (row["path"], suffix) if n)

    @app.get("/s/{token}", include_in_schema=False)
    def share_page(token: str):
        return FileResponse(Path(__file__).parent/"static/share.html")

    @app.post("/public/{token}/unlock")
    def public_unlock(token: str, request: Request, password: str = Body(embed=True, max_length=200)):
        proof = store.unlock_link(token, password, request.client.host if request.client else "unknown")
        response = JSONResponse({"ok": True})
        response.set_cookie("drive_link_"+token[:12], proof, httponly=True, secure=os.environ.get("DRIVE_SECURE_COOKIE", "0") == "1", samesite="lax", max_age=3600, path="/public/"+token)
        return response

    @app.get("/public/{token}")
    def public_info(token: str, request: Request, path: str = ""):
        row, owner = public_access(token, request)
        rel = public_path(row, path)
        lib = store.library(row["library_id"])
        fp = store.path(lib["id"], rel, True)
        if not path:
            with store.db() as db:
                db.execute("UPDATE share_links SET visits=visits+1 WHERE id=?", (row["id"],))
        return {"name": Path(row["path"]).name if row["path"] else lib["name"], "kind": row["kind"], "is_dir": fp.is_dir(), "expires": row["expires"],
                "read_only": store.license.status()["read_only"],
                "items": store.listing(owner, lib["id"], rel) if fp.is_dir() and row["kind"] != "upload" else [],
                "size": store.content_size(lib["id"], fp) if fp.is_file() else None, "path": path,
                "base_path": row["path"]}

    @app.get("/public/{token}/download")
    def public_download(token: str, request: Request, path: str = ""):
        row, owner = public_access(token, request)
        if row["kind"] == "upload":
            raise DriveError(403, "上传链接不能下载文件")
        rel = public_path(row, path)
        note_download(request, None, 'native', row["library_id"], rel)
        return content_response(store, owner, row["library_id"], rel, request)

    @app.post("/public/{token}/uploads", status_code=201)
    def public_upload_init(token: str, request: Request, name: str = Body(max_length=180), size: int = Body(ge=0), fingerprint: str = Body(max_length=240)):
        row, owner = public_access(token, request)
        if row["kind"] != "upload":
            raise DriveError(403, "这个链接不能上传文件")
        name = valid_name(name)
        rel = "/".join(n for n in (row["path"], name) if n)
        # Keep anonymous upload state separate from the owner's own tasks.
        fingerprint = "public:" + row["id"] + ":" + fingerprint[:160]
        task = store.init_upload(owner, row["library_id"], rel, size, fingerprint)
        with store.db() as db:
            db.execute("INSERT OR REPLACE INTO public_uploads VALUES(?,?)", (task["id"], row["id"]))
        return task

    def public_task(token, request, uid):
        row, owner = public_access(token, request)
        if row["kind"] != "upload" or not store.one("SELECT 1 FROM public_uploads WHERE upload_id=? AND link_id=?", (uid, row["id"])):
            raise DriveError(404, "上传任务不存在")
        return row, owner

    @app.patch("/public/{token}/uploads/{uid}")
    async def public_chunk(token: str, uid: str, request: Request):
        row, owner = public_task(token, request, uid)
        try:
            offset = int(request.headers.get("Upload-Offset", "-1"))
        except ValueError:
            raise DriveError(400, "上传进度无效")
        if offset < 0:
            raise DriveError(400, "缺少上传进度")
        content = bytearray()
        async for part in request.stream():
            if len(content) + len(part) > 8 * 1024**2:
                raise DriveError(413, "每个分片最多 8MB")
            content.extend(part)
        from starlette.concurrency import run_in_threadpool
        return {"offset": await run_in_threadpool(store.append_upload, owner, uid, offset, content)}

    @app.post("/public/{token}/uploads/{uid}/complete")
    def public_complete(token: str, uid: str, request: Request):
        row, owner = public_task(token, request, uid)
        def verify_link():
            fresh, _ = public_task(token, request, uid)
            if (fresh["library_id"], fresh["path"]) != (row["library_id"], row["path"]):
                raise DriveError(409, "上传目录已经变化，请重新打开链接上传")

        result = store.finish_upload(owner, uid, before_commit=verify_link)
        with store.db() as db:
            db.execute("DELETE FROM public_uploads WHERE upload_id=?", (uid,))
        store.notify(row["creator"], "上传链接收到文件：“"+result["name"]+"”")
        return result
