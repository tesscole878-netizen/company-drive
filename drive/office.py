"""ONLYOFFICE Docs integration for the self-hosted workspace."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import time
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
import uuid
import zipfile

from fastapi import Body, Depends, Request
from fastapi.responses import FileResponse, JSONResponse

from .legacy import legacy_file_response, legacy_path
from .store import DriveError, clean_path, signature, token_hash
from .workspace_api import content_response

DOCUMENT_TYPES = {".docx": "word", ".xlsx": "cell", ".pptx": "slide"}
DOCUMENT_LIMIT = 100 * 1024**2


def encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode(data):
    return base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)


def sign(payload, secret):
    header = encode(b'{"alg":"HS256","typ":"JWT"}')
    body = encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    message = f"{header}.{body}"
    mac = encode(hmac.new(secret, message.encode(), hashlib.sha256).digest())
    return message + "." + mac


def verify(token, secret, operation):
    try:
        if not token or len(token) > 65536:
            raise ValueError
        header, body, mac = token.split(".")
        if json.loads(decode(header)).get("alg") != "HS256":
            raise ValueError
        expected = hmac.new(secret, f"{header}.{body}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(decode(mac), expected):
            raise ValueError
        claims = json.loads(decode(body))
        if claims.get("operation", operation) != operation:
            raise ValueError
        if "exp" in claims and float(claims["exp"]) <= time.time():
            raise ValueError
        payload = claims.get("payload", claims)
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (ValueError, TypeError, AttributeError) as exc:
        raise DriveError(403, "在线编辑服务签名无效") from exc


def origin(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("Use an HTTP(S) URL without credentials")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def service_base(name, default=""):
    value = os.environ.get(name, default).rstrip("/")
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment or parsed.path:
        raise ValueError(f"{name} must be an origin without a path")
    return origin(value)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def register_office_routes(app, store, user, legacy_bindings=None):
    app.state.office_enabled = False
    if not any(os.environ.get(name) for name in ("OFFICE_PUBLIC_URL", "OFFICE_JWT_SECRET", "OFFICE_DRIVE_URL")):
        return
    editor_origin = service_base("OFFICE_PUBLIC_URL")
    drive_origin = service_base("OFFICE_DRIVE_URL")
    result_origin = service_base("OFFICE_RESULT_URL", editor_origin)
    secret = os.environ.get("OFFICE_JWT_SECRET", "").encode()
    if len(secret) < 32:
        raise ValueError("OFFICE_JWT_SECRET must be at least 32 bytes")
    jwt_header = os.environ.get("OFFICE_JWT_HEADER", "Authorization")
    app.state.office_enabled = True

    def signed_payload(request, body, operation):
        header = request.headers.get(jwt_header, "")
        token = header[7:] if header.startswith("Bearer ") else body.get("token")
        return verify(token, secret, operation)

    def session(sid):
        row = store.one("SELECT * FROM office_sessions WHERE id=? AND expires>?", (sid, time.time()))
        if not row:
            raise DriveError(404, "编辑会话已过期，请重新打开文档")
        return row

    def refresh_identity(current):
        directory = getattr(store, 'identity_directory', None)
        if directory is not None:
            directory.authorize(current)

    def refresh_edit_access(current, row):
        refresh_identity(current)
        store.access(current, row["library_id"], True, rel=row["path"])

    def legacy_editor(request, current, repo, path):
        if legacy_bindings is None:
            raise DriveError(404, "原资料库未启用")
        token = app.state.authorize_legacy_edit(request, current, repo)
        store.license.require_write()
        detail = legacy_editable_file(token, repo, path)
        size = detail.get('size')
        if type(size) is not int or not 0 <= size <= DOCUMENT_LIMIT:
            raise DriveError(413, '在线编辑支持不超过 100 MB 的 Office 文档')
        return token, detail['id']

    def legacy_editable_file(token, repo, path):
        detail = legacy_bindings.upstream.file_detail(token, repo, path)
        if detail.get('permission') != 'rw':
            raise DriveError(403, '你的 Seafile 账号没有此文件的编辑权限')
        if detail.get('can_edit') is False:
            raise DriveError(403, '此文件在原 Seafile 不允许在线编辑，请在电脑盘打开')
        return detail

    def legacy_session_token(row):
        live = store.one("SELECT 1 AS active FROM sessions WHERE hash=? AND user_id=? AND expires>?",
                         (row["legacy_session"], row["user_id"], time.time()))
        if not live or legacy_bindings is None:
            raise DriveError(403, "编辑会话已结束，请重新打开文件")
        token = legacy_bindings.token(row["user_id"])
        if not hmac.compare_digest(token_hash(token), row["legacy_account"]):
            raise DriveError(403, "原账号已变更，请重新打开文件")
        return token

    @app.get("/office/editor", include_in_schema=False)
    def office_page(library_id: str, path: str, request: Request, legacy: bool = False, current=Depends(user)):
        if legacy:
            legacy_editor(request, current, library_id, legacy_path(path, allow_root=False))
        else:
            store.access(current, library_id, rel=path)
        return FileResponse(
            Path(__file__).parent / "static/office.html",
            headers={"Content-Security-Policy": f"default-src 'self'; script-src 'self' {editor_origin}; frame-src {editor_origin}; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"},
        )

    @app.post("/api/office/session")
    def begin_session(request: Request, library_id: str = Body(), path: str = Body(), legacy: bool = Body(False), current=Depends(user)):
        path = legacy_path(path, allow_root=False) if legacy else clean_path(path)
        name = PurePosixPath(path).name
        suffix = PurePosixPath(path).suffix.lower()
        if suffix not in DOCUMENT_TYPES:
            raise DriveError(400, "此文件无法使用在线 Office 编辑器")
        source_session = source_account = None
        lock_key = 'seafile:' + library_id if legacy else library_id
        with store.file_locks(lock_key):
            if legacy:
                token, file_signature = legacy_editor(request, current, library_id, path)
                source_session = token_hash(request.cookies.get('drive_session', ''))
                source_account = token_hash(token)
                mode = 'edit'
            else:
                lib, role = store.access(current, library_id, rel=path)
                fp = store.path(library_id, path, True)
                if lib['encryption'] or not fp.is_file():
                    raise DriveError(400, "此文件无法使用在线 Office 编辑器")
                if fp.stat().st_size > DOCUMENT_LIMIT:
                    raise DriveError(413, "在线编辑支持不超过 100 MB 的 Office 文档")
                mode = "edit" if role == "editor" and store.license.status()["can_write"] else "view"
                file_signature = signature(fp)
            key_input = f"{lock_key}:{path}:{file_signature}"
            if legacy:
                # A shared document key must not let one editor's session stand in for another.
                key_input += ':' + source_session
            key = hashlib.sha256(key_input.encode()).hexdigest()
            sid = uuid.uuid4().hex
            with store.db() as db:
                db.execute("DELETE FROM office_sessions WHERE expires<=?", (time.time(),))
                db.execute("INSERT INTO office_sessions(id,library_id,path,user_id,file_signature,document_key,expires,mode,legacy_session,legacy_account) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (sid, library_id, path, current["id"], file_signature, key, time.time() + 48 * 3600, mode, source_session, source_account))
        config = {
            "documentType": DOCUMENT_TYPES[suffix],
            "height": "100%",
            "width": "100%",
            "document": {"title": name, "fileType": suffix[1:], "key": key,
                         "url": f"{drive_origin}/office/source/{sid}",
                         "permissions": {"edit": mode == "edit", "download": True}},
            "editorConfig": {"mode": mode, "lang": "zh-CN", "callbackUrl": f"{drive_origin}/office/callback/{sid}",
                             "user": {"id": current["id"], "name": current["name"]}},
        }
        config["token"] = sign(config, secret)
        return {"script": editor_origin + "/web-apps/apps/api/documents/api.js", "config": config}

    @app.get("/office/source/{sid}", include_in_schema=False)
    def source(sid: str, request: Request):
        payload = signed_payload(request, {}, "download")
        if payload.get("url") != f"{drive_origin}/office/source/{sid}":
            raise DriveError(403, "下载授权与编辑会话不匹配")
        row = session(sid)
        current = store.get_user(row["user_id"])
        if row['legacy_session']:
            token = legacy_session_token(row)
            upstream = legacy_bindings.upstream
            detail = legacy_editable_file(token, row['library_id'], row['path'])
            if detail['id'] != row['file_signature']:
                raise DriveError(409, '文件已更新，请重新打开')
            return legacy_file_response(upstream, token, row['library_id'], row['path'], expected=row['file_signature'])
        refresh_identity(current)
        store.access(current, row["library_id"], rel=row["path"])
        if signature(store.path(row["library_id"], row["path"])) != row["file_signature"]:
            raise DriveError(409, "文件已更新，请重新打开")
        return content_response(store, current, row["library_id"], row["path"], request)

    @app.post("/office/callback/{sid}", include_in_schema=False)
    def save(sid: str, request: Request, body: dict = Body()):
        payload = signed_payload(request, body, "callback")
        row = session(sid)
        if payload.get("key") != row["document_key"]:
            raise DriveError(403, "文件标识与编辑会话不匹配")
        status = payload.get("status")
        if status in (1, 4):
            return {"error": 0}
        if status != 2 or row["mode"] != "edit":
            return {"error": 1}
        current = store.get_user(row["user_id"])
        if row['legacy_session']:
            legacy_session_token(row)
            store.license.require_write()
        else:
            refresh_identity(current)
            store.access(current, row["library_id"], True, rel=row["path"])
        if row["completed"] is not None:
            return {"error": 0}
        result_url = payload.get("url", "")
        try:
            parsed = urlsplit(result_url)
            if origin(result_url) not in (editor_origin, result_origin) or parsed.fragment:
                raise ValueError
        except (ValueError, TypeError):
            raise DriveError(403, "保存地址与已配置的文档服务器不匹配")
        network = urlsplit(result_origin)
        result_url = urlunsplit((network.scheme, network.netloc, parsed.path, parsed.query, ""))
        directory = store.secure_staging if row['legacy_session'] else store.tmp
        temporary = directory / ("office-" + uuid.uuid4().hex)
        try:
            opener = build_opener(ProxyHandler({}), NoRedirect())
            with opener.open(result_url, timeout=20) as response, open(temporary, "xb") as target:
                remaining = min(DOCUMENT_LIMIT, store.max_file)
                while chunk := response.read(1024 * 1024):
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise DriveError(413, "保存文件超出大小限制")
                    target.write(chunk)
            store.reject_empty_office_write(current, row['library_id'], row['path'], temporary.stat().st_size,
                                            'office_callback', legacy=bool(row['legacy_session']))
            with zipfile.ZipFile(temporary) as saved:
                part = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}[Path(row["path"]).suffix.lower()]
                if part not in saved.namelist():
                    raise DriveError(400, "编辑服务返回的文件格式不匹配")
            lock_key = 'seafile:' + row['library_id'] if row['legacy_session'] else row['library_id']
            with store.file_locks(lock_key):
                if session(sid)["completed"] is None:
                    if row['legacy_session']:
                        token = legacy_session_token(row)
                        store.license.require_write()
                        legacy_editable_file(token, row['library_id'], row['path'])
                        with temporary.open('rb') as content:
                            legacy_bindings.upstream.save_file(token, row['library_id'], row['path'], row['file_signature'], content, temporary.stat().st_size)
                    else:
                        store.commit_file(current, row["library_id"], row["path"], temporary, row["file_signature"],
                                          "edit", before_commit=lambda: refresh_edit_access(current, row), entry_point='office_callback')
                    with store.db() as db:
                        db.execute("UPDATE office_sessions SET completed=? WHERE document_key=?", (time.time(), row["document_key"]))
            return {"error": 0}
        except (DriveError, URLError, OSError, zipfile.BadZipFile):
            return JSONResponse({"error": 1})
        finally:
            temporary.unlink(missing_ok=True)
