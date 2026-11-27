from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import time
import uuid
from urllib.parse import urlsplit

from a2wsgi import WSGIMiddleware
from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .dav import make_dav
from .file_changes import register_changes
from .legacy import register_legacy_routes
from .identity_directory import configured_directory
from .office import register_office_routes
from .licensing import LicenseManager, allowed_when_readonly
from .store import DriveError, password_hash, token_hash
from .workspace import WorkspaceStore
from .workspace_api import content_response, register_workspace_routes
from .usage import DownloadAudit, Usage, register_usage

APP_VERSION = "0.4.2"
WINDOWS_CLIENT_VERSION = "0.2.14"


class Credentials(BaseModel):
    username: str = Field(max_length=254)
    password: str = Field(max_length=400)
    otp: str = Field(default='', pattern=r'^(?:[0-9]{6})?$')


class NewUser(Credentials):
    username: str = Field(max_length=40)
    password: str = Field(max_length=200)
    name: str = Field(max_length=60)
    admin: bool = False


class Setup(NewUser):
    key: str = Field(max_length=200)


class Group(BaseModel):
    name: str = Field(max_length=50)
    description: str = Field(default="", max_length=240)


class Entry(BaseModel):
    path: str = Field(default="", max_length=1800)
    name: str = Field(default="", max_length=180)


class Upload(BaseModel):
    group_id: str
    path: str = Field(max_length=1800)
    size: int = Field(ge=0)
    fingerprint: str = Field(max_length=240)
    overwrite: bool = False


class DriveStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if isinstance(response, FileResponse) and Path(response.path).suffix.lower() == ".js":
            # Windows may register .js as text/plain, which browsers refuse with nosniff.
            response.headers["Content-Type"] = "text/javascript; charset=utf-8"
        return response


def create_app(data_dir=None, state_dir=None):
    data_dir = data_dir or os.environ.get("DRIVE_DATA_DIR", "data")
    store = WorkspaceStore(data_dir, state_dir or os.environ.get("DRIVE_STATE_DIR"))
    store.usage = Usage(store)
    directory = configured_directory(store)
    store.identity_directory = directory
    licensing = LicenseManager(store)
    store.license = licensing
    @asynccontextmanager
    async def lifespan(app):
        prune = getattr(app.state, 'prune_legacy_uploads', None)
        async def maintain_uploads():
            while True:
                try:
                    await run_in_threadpool(prune)
                except (OSError, sqlite3.Error):
                    logging.getLogger(__name__).exception('Unable to prune upload staging')
                await asyncio.sleep(300)
        maintenance = asyncio.create_task(maintain_uploads()) if prune else None
        try:
            yield
        finally:
            if maintenance is not None:
                maintenance.cancel()
                with suppress(asyncio.CancelledError):
                    await maintenance
            await run_in_threadpool(getattr(app.state, 'close_legacy_uploads', lambda: None))
            await run_in_threadpool(app.state.archives.close)

    app = FastAPI(title="苏盐新云盘", version=APP_VERSION, docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.license = licensing
    app.add_middleware(DownloadAudit, usage=store.usage)
    limiter = {}
    static = Path(__file__).parent / "static"

    @app.exception_handler(DriveError)
    async def drive_error(request, exc):
        result = {'detail': exc.message}
        if code := getattr(exc, 'code', None): result['code'] = code
        return JSONResponse(result, status_code=exc.status)

    @app.exception_handler(OSError)
    async def filesystem_error(request, exc):
        import logging
        logging.getLogger("drive").exception("Storage operation failed")
        return JSONResponse({"detail": "存储操作失败，请检查 NAS 连接、剩余空间和目录权限"}, status_code=507)

    @app.middleware("http")
    async def protections(request: Request, call_next):
        if request.url.path.startswith(("/api/", "/public/", "/dav", "/office/")) and not allowed_when_readonly(request.url.path, request.method):
            try:
                licensing.require_write()
            except DriveError as exc:
                return JSONResponse({"detail": exc.message, "code": "license_read_only"}, status_code=exc.status,
                                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        if request.url.path.startswith(("/api/", "/public/", "/dav/.suyan-encrypted/")) and request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "Content-Length 无效"}, status_code=400)
            maximum = 4096 if request.url.path.startswith("/dav/.suyan-encrypted/") else (
                8 * 1024**2 if "/uploads/" in request.url.path or request.url.path.endswith("/text") else 1024**2)
            if length > maximum:
                return JSONResponse({"detail": "单次请求超过限制，请分片上传"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; media-src 'self' blob:; frame-src 'self' blob:; object-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'")
        if request.url.path.startswith(("/api/", "/s/", "/public/", "/office/", "/dav/.suyan-encrypted/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    def user(request: Request):
        token = request.cookies.get("drive_session", "")
        session_hash = token_hash(token)
        row = store.one("SELECT u.id,u.username,u.name,u.admin,u.active,u.seafile_identity,s.csrf,w.ip,w.last_seen, "
                        "u.admin OR EXISTS(SELECT 1 FROM member_managers WHERE user_id=u.id) AS can_manage_members "
                        "FROM sessions s JOIN users u ON u.id=s.user_id LEFT JOIN usage_web w ON w.session_hash=s.hash "
                        "WHERE s.hash=? AND s.expires>? AND u.active=1",
                        (session_hash, time.time()))
        if not row:
            raise DriveError(401, "请先登录")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not hmac.compare_digest(request.headers.get("X-Drive-CSRF", ""), row["csrf"]):
                raise DriveError(403, "会话校验失败，请刷新后重试")
        current = store.public_user(row)
        if directory:
            directory.authorize(current)
        if request.client:
            store.usage.touch_web(current['id'], session_hash, request.client.host, row['last_seen'], row['ip'])
        return dict(**current, csrf=row["csrf"], can_manage_members=bool(row["can_manage_members"]))

    def admin(current=Depends(user)):
        if not current["admin"]:
            raise DriveError(403, "此操作需要管理员权限")
        return current

    def member_manager(current=Depends(user)):
        if not current["can_manage_members"]:
            raise DriveError(403, "此操作需要成员管理权限")
        return current

    def session(current, request):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with store.db() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (token_hash(token), current["id"], csrf, time.time()+86400*7))
            can_manage_members = bool(current["admin"] or db.execute(
                "SELECT 1 FROM member_managers WHERE user_id=?", (current["id"],)).fetchone())
        response = JSONResponse(dict(**current, csrf=csrf, can_manage_members=can_manage_members))
        response.set_cookie("drive_session", token, httponly=True, samesite="strict", max_age=86400*7,
                            secure=os.environ.get("DRIVE_SECURE_COOKIE", "0") == "1")
        return response

    @app.get("/api/status")
    def status():
        return {"initialized": store.initialized(), "version": APP_VERSION}

    @app.get("/api/license")
    def license_info(current=Depends(admin)):
        return licensing.status()

    @app.get("/api/access-mode")
    def access_mode(current=Depends(user)):
        return licensing.access_mode()

    @app.post("/api/license/activate")
    def activate_license(activation_code: str = Body(embed=True, min_length=1, max_length=16384), current=Depends(admin)):
        result = licensing.activate(activation_code)
        store.event(current, None, "license_activate", result["customer"])
        return result

    @app.post("/api/setup")
    def setup(body: Setup, request: Request):
        with store.mutex:
            if store.initialized():
                raise DriveError(409, "系统已经初始化")
            key = store.state / "setup.key"
            if not key.exists() or not hmac.compare_digest(body.key, key.read_text().strip()):
                raise DriveError(403, "初始化密钥错误，请从服务器 state/setup.key 获取")
            current = store.create_user(body.username, body.name, body.password, True)
            key.unlink(missing_ok=True)
        return session(current, request)

    @app.post("/api/login")
    def login(body: Credentials, request: Request):
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        attempts = [t for t in limiter.get(ip, []) if t > now - 300]
        if len(attempts) >= 20:
            raise DriveError(429, "登录尝试过多，请 5 分钟后重试")
        if directory and body.username.strip().casefold() != 'admin':
            if not is_loopback(request):
                raise DriveError(403, '公司账号请通过 HTTPS 云盘入口登录')
            try:
                current, _ = directory.login(body.username, body.password, body.otp)
            except DriveError:
                limiter[ip] = attempts + [now]
                raise
        else:
            current = store.authenticate(body.username, body.password)
        if not current:
            limiter[ip] = attempts + [now]
            raise DriveError(401, "账号或密码错误，或账号已停用")
        limiter.pop(ip, None)
        return session(current, request)

    def is_loopback(request):
        try:
            return bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            return False

    def require_loopback(request):
        if not is_loopback(request) or directory is None:
            raise DriveError(403, '此接口仅供本机统一登录服务使用')

    @app.post('/api/identity/session')
    def identity_session(body: Credentials, request: Request):
        require_loopback(request)
        if body.username.strip().casefold() == 'admin':
            raise DriveError(403, '系统管理员使用原管理登录入口')
        current, token = directory.login(body.username, body.password, body.otp)
        response = session(current, request)
        response.body = json.dumps(dict(json.loads(response.body), legacy_token=token)).encode()
        response.headers['Content-Length'] = str(len(response.body))
        return response

    @app.get('/api/identity/active')
    def identity_active(request: Request):
        require_loopback(request)
        token = request.cookies.get('drive_session', '')
        row = store.one('SELECT 1 AS active FROM sessions s JOIN users u ON s.user_id=u.id '
                        'WHERE s.hash=? AND s.expires>? AND u.active=1 AND u.seafile_identity IS NOT NULL',
                        (token_hash(token), time.time()))
        if row is None:
            raise DriveError(401, '账号会话已结束，请重新登录')
        return {'ok': True}

    @app.post("/api/logout")
    def logout(request: Request, current=Depends(user)):
        for key in list(store.key_cache):
            if key[0] == current["id"]:
                store.key_cache.pop(key, None)
        with store.db() as db:
            db.execute("DELETE FROM sessions WHERE hash=?", (token_hash(request.cookies.get("drive_session", "")),))
        response = JSONResponse({"ok": True})
        response.delete_cookie("drive_session")
        return response

    @app.post("/api/password")
    def change_password(old_password: str = Body(max_length=400), new_password: str = Body(min_length=6, max_length=200), current=Depends(user)):
        if directory and current.get('identity_source') == 'seafile':
            verified, _ = directory.login(current['username'], old_password)
            if verified['id'] != current['id']: raise DriveError(403, '当前密码错误')
            directory.set_password(current['id'], new_password)
        elif not store.authenticate(current["username"], old_password):
            raise DriveError(403, "当前密码错误")
        with store.db() as db:
            db.execute("UPDATE users SET password=? WHERE id=?", (password_hash(new_password), current["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (current["id"],))
            db.execute("DELETE FROM tokens WHERE user_id=?", (current["id"],))
        for key in list(store.key_cache):
            if key[0] == current["id"]:
                store.key_cache.pop(key, None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("drive_session")
        return response

    @app.get("/api/me")
    def me(current=Depends(user)):
        return current

    @app.get("/api/groups")
    def groups(current=Depends(user)):
        return store.groups_for(current)

    @app.post("/api/groups", status_code=201)
    def new_group(body: Group, current=Depends(user)):
        return store.create_group(body.name, body.description, creator=current)

    @app.get("/api/users")
    def users(current=Depends(member_manager)):
        if directory: directory.sync()
        return store.all("SELECT id,username,name,admin,active,created FROM users ORDER BY created")

    @app.post("/api/users", status_code=201)
    def new_user(body: NewUser, current=Depends(member_manager)):
        if body.admin and not current["admin"]:
            raise DriveError(403, "只有系统管理员可以创建管理员账号")
        if directory:
            return directory.create_user(body.username, body.name, body.password, body.admin)
        return store.create_user(body.username, body.name, body.password, body.admin)

    @app.patch("/api/users/{uid}")
    def update_user(uid: str, active: bool = Body(embed=True), current=Depends(admin)):
        if uid == current["id"]:
            raise DriveError(400, "不能停用自己的账号")
        if not store.one("SELECT id FROM users WHERE id=?", (uid,)):
            raise DriveError(404, "用户不存在")
        if directory: directory.set_active(uid, active)
        with store.db() as db:
            db.execute("UPDATE users SET active=? WHERE id=?", (int(active), uid))
            if not active:
                db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
                db.execute("DELETE FROM tokens WHERE user_id=?", (uid,))
        return {"ok": True}

    @app.post("/api/users/{uid}/password")
    def reset_user_password(uid: str, new_password: str = Body(embed=True, min_length=6, max_length=200), current=Depends(admin)):
        target = store.one("SELECT id,name FROM users WHERE id=?", (uid,))
        if not target:
            raise DriveError(404, "用户不存在")
        if directory: directory.set_password(uid, new_password)
        encoded = password_hash(new_password)
        with store.mutex, store.db() as db:
            db.execute("UPDATE users SET password=? WHERE id=?", (encoded, uid))
            db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            db.execute("DELETE FROM tokens WHERE user_id=?", (uid,))
            for key in list(store.key_cache):
                if key[0] == uid:
                    store.key_cache.pop(key, None)
        store.event(current, None, "password_reset", target["name"])
        return {"ok": True, "reauthentication_required": uid == current["id"]}

    @app.get("/api/groups/{gid}/members")
    def members(gid: str, current=Depends(user)):
        store.manage_group(current, gid)
        return store.all("SELECT u.id,u.username,u.name,u.active,m.role,gm.role management_role "
                         "FROM users u JOIN members m ON u.id=m.user_id LEFT JOIN group_managers gm "
                         "ON gm.group_id=m.group_id AND gm.user_id=u.id WHERE m.group_id=?", (gid,))

    @app.put("/api/groups/{gid}/members/{uid}")
    def set_member(gid: str, uid: str, role: str = Body(embed=True), current=Depends(user)):
        store.manage_group(current, gid)
        target_user = store.get_user(uid)
        if role not in ("reader", "editor"):
            raise DriveError(400, "权限必须为只读或可编辑")
        with store.db() as db:
            db.execute("INSERT INTO members VALUES(?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET role=excluded.role", (gid, uid, role))
        store.event(current, gid, "membership", target_user["name"] + " · " + ("可编辑" if role == "editor" else "只读"))
        return {"ok": True}

    @app.delete("/api/groups/{gid}/members/{uid}")
    def remove_member(gid: str, uid: str, current=Depends(user)):
        store.manage_group(current, gid)
        if store.one("SELECT 1 FROM group_managers WHERE group_id=? AND user_id=? AND role='owner'", (gid, uid)):
            raise DriveError(409, "不能直接移除群组所有者")
        target_user = store.one("SELECT name FROM users WHERE id=?", (uid,))
        with store.db() as db:
            db.execute("DELETE FROM members WHERE group_id=? AND user_id=?", (gid, uid))
        store.event(current, gid, "membership", (target_user or {}).get("name", "成员") + " · 移出分组")
        return {"ok": True}

    @app.get("/api/groups/{gid}/files")
    def files(gid: str, path: str = "", q: str = "", current=Depends(user)):
        return store.listing(current, gid, path, q)

    @app.get("/api/groups/{gid}/download")
    def download(gid: str, path: str, request: Request, current=Depends(user)):
        store.access(current, gid, rel=path)
        store.event(current, gid, "download", path)
        return content_response(store, current, gid, path, request, audit=True)

    @app.post("/api/groups/{gid}/folders", status_code=201)
    def folder(gid: str, body: Entry, current=Depends(user)):
        store.mkdir(current, gid, body.path)
        return {"ok": True}

    @app.patch("/api/groups/{gid}/files")
    def rename(gid: str, body: Entry, current=Depends(user)):
        store.rename(current, gid, body.path, body.name)
        return {"ok": True}

    @app.delete("/api/groups/{gid}/files")
    def remove(gid: str, path: str, current=Depends(user)):
        store.delete(current, gid, path)
        return {"ok": True}

    @app.post("/api/uploads", status_code=201)
    def upload_init(body: Upload, current=Depends(user)):
        return store.init_upload(current, body.group_id, body.path, body.size, body.fingerprint, body.overwrite)

    @app.get("/api/uploads/{uid}")
    def upload_status(uid: str, current=Depends(user)):
        row = store.upload(current, uid)
        return {"offset": store.upload_path(row["group_id"], uid).stat().st_size, "size": row["size"]}

    @app.patch("/api/uploads/{uid}")
    async def upload_chunk(uid: str, request: Request, current=Depends(user)):
        try:
            offset = int(request.headers.get("Upload-Offset", "-1"))
        except ValueError:
            raise DriveError(400, "上传偏移量无效")
        if offset < 0:
            raise DriveError(400, "缺少 Upload-Offset")
        chunks, size = [], 0
        async for part in request.stream():
            size += len(part)
            if size > 8 * 1024**2:
                raise DriveError(413, "每个分片最多 8MB")
            chunks.append(part)
        content = b"".join(chunks)
        result = await run_in_threadpool(store.append_upload, current, uid, offset, content)
        return {"offset": result}

    @app.post("/api/uploads/{uid}/complete")
    def upload_complete(uid: str, current=Depends(user)):
        return store.finish_upload(current, uid)

    @app.delete("/api/uploads/{uid}")
    def upload_cancel(uid: str, current=Depends(user)):
        with store.file_locks("upload:" + uid):
            row = store.upload(current, uid)
            store.upload_path(row["group_id"], uid).unlink(missing_ok=True)
            with store.db() as db:
                db.execute("DELETE FROM uploads WHERE id=?", (uid,))
        return {"ok": True}

    @app.get("/api/trash")
    def trash(library_id: str | None = None, current=Depends(admin)):
        return store.list_trash(current, library_id)

    @app.post("/api/trash/{tid}/restore")
    def restore(tid: str, current=Depends(admin)):
        store.restore(current, tid)
        return {"ok": True}

    @app.get("/api/events")
    def events(library_id: str | None = None, current=Depends(user)):
        return store.events_for(current, library_id)

    @app.get("/api/storage")
    def storage(current=Depends(user)):
        usage = shutil.disk_usage(store.data)
        return {"total": usage.total, "free": usage.free, "used": usage.used, "max_file": store.max_file}

    def windows_installer():
        return Path(os.environ.get("DRIVE_CLIENT_INSTALLER", str(Path(__file__).parent.parent / "downloads" / "CompanyDriveSetup.exe")))

    @app.get("/api/client")
    def client_info(current=Depends(user)):
        path = windows_installer()
        available = path.is_file()
        return {"available": available, "platform": "windows-x64", "version": WINDOWS_CLIENT_VERSION,
                "size": path.stat().st_size if available else 0, "download": "/api/client/windows" if available else None}

    @app.get("/api/client/windows")
    def client_download(current=Depends(user)):
        path = windows_installer()
        if not path.is_file():
            raise DriveError(404, "管理员尚未放置 Windows 安装包，请联系管理员")
        return FileResponse(path, filename="CompanyDriveSetup.exe", media_type="application/octet-stream")

    @app.get("/api/tokens")
    def tokens(current=Depends(user)):
        return store.all("SELECT id,name,created,platform,last_ip,last_seen FROM tokens WHERE user_id=? ORDER BY created DESC", (current["id"],))

    @app.post("/api/tokens", status_code=201)
    def new_token(name: str = Body(), platform: str = Body(default="windows", max_length=40), current=Depends(user)):
        if not name.strip() or len(name) > 60:
            raise DriveError(400, "请填写 1–60 字的设备名称")
        tid = secrets.token_hex(8)
        secret = tid + "." + secrets.token_urlsafe(32)
        with store.db() as db:
            db.execute("INSERT INTO tokens(id,user_id,name,hash,created,platform) VALUES(?,?,?,?,?,?)",
                       (tid, current["id"], name.strip(), token_hash(secret), time.time(), platform))
        return {"id": tid, "password": secret}

    @app.delete("/api/tokens/{tid}")
    def revoke_token(tid: str, current=Depends(user)):
        with store.db() as db:
            db.execute("DELETE FROM tokens WHERE id=? AND user_id=?", (tid, current["id"]))
        return {"ok": True}

    register_usage(app, store.usage, admin)
    register_workspace_routes(app, store, user, admin)
    legacy_bindings = register_legacy_routes(app, store, user)
    register_changes(app, store, user, legacy_bindings)
    register_office_routes(app, store, user, legacy_bindings)
    app.mount("/dav", WSGIMiddleware(make_dav(store, legacy_bindings), workers=8))
    app.mount("/assets", DriveStaticFiles(directory=static), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html")

    return app
