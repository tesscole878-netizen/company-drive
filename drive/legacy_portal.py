"""HTTPS-only, read-only browser portal using Seafile identities directly."""
import json
import os
from pathlib import Path
import re
import secrets
from threading import Lock
import time

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .legacy import legacy_file_response, legacy_path
from .legacy_preview import preview_bytes
from .portal_admin import PortalAdmin
from .store import DriveError


COOKIE = '__Host-suyan_legacy'
SESSION_LIFETIME = 8 * 3600
STATIC = Path(__file__).parent / 'static'
ASSETS = {
    'style.css': 'text/css', 'workspace.css': 'text/css',
    'legacy-portal.css': 'text/css', 'legacy-portal.js': 'text/javascript',
    'suyan-logo.png': 'image/png', 'suyan.ico': 'image/x-icon',
}


class PortalSessions:
    def __init__(self):
        self.sessions = {}
        self.attempts = {}
        self.lock = Lock()

    def check_attempt(self, address, username):
        now = time.monotonic()
        with self.lock:
            for key, stamps in list(self.attempts.items()):
                recent = [stamp for stamp in stamps if now - stamp < 300]
                if recent:
                    self.attempts[key] = recent
                else:
                    self.attempts.pop(key)
            keys = [('address', address), ('account', username.casefold())]
            if any(len(self.attempts.get(key, ())) >= limit for key, limit in zip(keys, (25, 6))):
                raise DriveError(429, '登录尝试过多，请 5 分钟后重试')
            for key in keys:
                self.attempts.setdefault(key, []).append(now)
        return keys[1], now

    def clear_attempt(self, attempt):
        key, stamp = attempt
        with self.lock:
            recent = self.attempts.get(key, [])
            if stamp in recent:
                recent.remove(stamp)
                if not recent:
                    self.attempts.pop(key, None)

    def issue(self, previous, token, name, mode='seafile', workspace_token=None, workspace_csrf=None):
        now = time.monotonic()
        with self.lock:
            self.sessions.pop(previous, None)
            self.sessions = {sid: row for sid, row in self.sessions.items() if row['expires'] > now}
            if len(self.sessions) >= 4096:
                raise DriveError(503, '当前在线会话较多，请稍后再试')
            session = secrets.token_urlsafe(32)
            self.sessions[session] = {'token': token, 'name': name, 'mode': mode, 'workspace_token':workspace_token,'workspace_csrf':workspace_csrf,'expires': now + SESSION_LIFETIME}
        return session

    def current(self, request):
        session = request.cookies.get(COOKIE)
        with self.lock:
            row = self.sessions.get(session)
            if not row or row['expires'] <= time.monotonic():
                self.sessions.pop(session, None)
                raise DriveError(401, '请登录苏盐新云盘')
            return row

    def logout(self, session):
        with self.lock:
            self.sessions.pop(session, None)


def create_legacy_portal(upstream, allowed_hosts, office_preview=None, unified_identity=False):
    app = FastAPI(title='苏盐新云盘', docs_url=None, redoc_url=None, openapi_url=None)
    sessions = PortalSessions()
    administrator = PortalAdmin()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts, www_redirect=False)

    @app.exception_handler(DriveError)
    async def handle_drive_error(request, error):
        payload = {'detail': error.message}
        if code := getattr(error, 'code', None):
            payload['code'] = code
        return JSONResponse(payload, status_code=error.status)

    @app.middleware('http')
    async def protections(request, call_next):
        origin = request.headers.get('origin')
        expected_origin = f'{request.url.scheme}://{request.url.netloc}'
        if request.url.scheme != 'https':
            response = JSONResponse({'detail': '请通过 HTTPS 访问'}, status_code=403)
        elif request.url.path.startswith('/api/identity/'):
            response = JSONResponse({'detail': '接口不存在'}, status_code=404)
        elif request.method not in ('GET', 'HEAD', 'OPTIONS') and origin and origin != expected_origin:
            response = JSONResponse({'detail': '拒绝跨站请求'}, status_code=403)
        else:
            try:
                account = sessions.current(request)
            except DriveError:
                account = None
            if account and account['mode'] in ('admin','workspace') and request.url.path not in ('/health', '/api/session', '/api/login', '/api/logout','/workspace','/legacy-home'):
                try:
                    target = dict(account,token=account['workspace_token']) if account['mode']=='workspace' else account
                    response = await administrator.forward(request, target)
                except DriveError as error:
                    response = JSONResponse({'detail': error.message}, status_code=error.status)
                if response.status_code == 401 or (request.url.path=='/api/me' and response.status_code in (403,409)):
                    sessions.logout(request.cookies.get(COOKIE))
            else:
                response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers.setdefault('Content-Security-Policy', (
            "default-src 'self'; img-src 'self' blob:; frame-src blob:; object-src blob:; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        ))
        return response

    @app.get('/')
    def index():
        return FileResponse(STATIC / 'legacy-portal.html', media_type='text/html')

    @app.get('/assets/{filename}')
    def asset(filename: str):
        if filename not in ASSETS:
            raise DriveError(404, '文件不存在')
        return FileResponse(STATIC / filename, media_type=ASSETS[filename])

    @app.get('/health')
    def health():
        return {'ok': True}

    def current(request: Request):
        account = sessions.current(request)
        if unified_identity and account.get('workspace_token') and not administrator.identity_active(account['workspace_token']):
            sessions.logout(request.cookies.get(COOKIE))
            raise DriveError(401, '账号会话已结束，请重新登录')
        return account

    @app.get('/api/session')
    def session(account=Depends(current)):
        return {'name': account['name'],**({'unified_identity':True} if unified_identity else {}), **({'mode': account['mode']} if account['mode'] in ('admin','workspace') else {})}

    @app.post('/api/login')
    async def login(request: Request):
        if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
            raise DriveError(415, '登录需要 JSON 请求')
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 4096:
                raise DriveError(413, '登录请求过长')
            data.extend(chunk)
        try:
            body = json.loads(data)
        except (ValueError, UnicodeError):
            raise DriveError(400, '登录请求格式无效') from None
        if not isinstance(body, dict):
            raise DriveError(400, '登录请求格式无效')
        username, password, otp = body.get('username'), body.get('password'), body.get('otp', '')
        if (not isinstance(username, str) or not 1 <= len(username.strip()) <= 254
                or not isinstance(password, str) or not 1 <= len(password) <= 400
                or not isinstance(otp, str) or not re.fullmatch(r'(?:[0-9]{6})?', otp)):
            raise DriveError(400, '请输入正确格式的账号、密码和验证码')
        username = username.strip()
        attempt = sessions.check_attempt(request.client.host if request.client else '', username)
        if username.casefold() == 'admin':
            identity, token = await run_in_threadpool(administrator.login, username, password)
            sessions.clear_attempt(attempt)
            sid = sessions.issue(request.cookies.get(COOKIE), token, identity['name'], 'admin',workspace_csrf=identity['csrf'])
            response = JSONResponse(dict(identity, mode='admin'))
            response.set_cookie(COOKIE, sid, path='/', secure=True, httponly=True, samesite='strict', max_age=SESSION_LIFETIME)
            return response
        if unified_identity:
            profile,workspace_token=await run_in_threadpool(administrator.identity_login,username,password,otp)
            token=profile['legacy_token']
        else:
            token = await run_in_threadpool(upstream.login_token, username, password, otp)
            profile = await run_in_threadpool(upstream.profile, token, username)
            workspace_token=None
        sessions.clear_attempt(attempt)
        mode = 'workspace' if unified_identity else 'seafile'
        sid = sessions.issue(request.cookies.get(COOKIE), token, profile['name'], mode,
                             workspace_token=workspace_token, workspace_csrf=profile.get('csrf'))
        response = JSONResponse({'name': profile['name'],
                                 **({'unified_identity': True, 'mode': mode, 'csrf': profile['csrf']}
                                    if unified_identity else {})})
        response.set_cookie(COOKIE, sid, path='/', secure=True, httponly=True, samesite='strict', max_age=SESSION_LIFETIME)
        return response

    @app.post('/api/logout')
    async def logout(request: Request):
        try:
            account = sessions.current(request)
        except DriveError:
            account = None
        if account and (account['mode']=='admin' or account['workspace_token']):
            token=account['token'] if account['mode']=='admin' else account['workspace_token']
            await run_in_threadpool(administrator.end_session,token,account['workspace_csrf'])
        sessions.logout(request.cookies.get(COOKIE))
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='strict')
        return response

    @app.get('/workspace')
    def enter_workspace(account=Depends(current)):
        if not unified_identity or not account.get('workspace_token'):
            raise DriveError(409,'请重新登录以启用统一工作区')
        account['mode']='workspace'
        return RedirectResponse('/',status_code=303)

    @app.get('/legacy-home')
    def legacy_home(account=Depends(current)):
        if account['mode']=='workspace':account['mode']='seafile'
        return RedirectResponse('/',status_code=303)

    @app.get('/api/catalog')
    def catalog(account=Depends(current)):
        token = account['token']
        groups = upstream.groups(token)
        ids = {group['id'] for group in groups}
        libraries = [lib for lib in upstream.catalog_libraries(token) if not lib['group_id'] or lib['group_id'] in ids]
        return {'groups': groups, 'libraries': libraries}

    @app.get('/api/tools/{name}')
    def tool(name: str, account=Depends(current)):
        return upstream.tool(account['token'], name)

    @app.get('/api/notifications')
    def notifications(page: int = 1, account=Depends(current)):
        if not 1 <= page <= 10000:
            raise DriveError(400, '通知页码无效')
        return upstream.notifications(account['token'], page)

    @app.get('/api/libraries/{repo}/files')
    def list_files(repo: str, path: str = '/', account=Depends(current)):
        token = account['token']
        upstream.require_library(token, repo)
        return upstream.listing(token, repo, legacy_path(path))

    @app.get('/api/libraries/{repo}/download')
    def download(repo: str, path: str, account=Depends(current)):
        token = account['token']
        upstream.require_library(token, repo)
        return legacy_file_response(upstream, token, repo, path)

    @app.get('/api/libraries/{repo}/preview')
    def preview(repo: str, path: str, account=Depends(current)):
        token = account['token']
        upstream.require_library(token, repo)
        data, media_type = preview_bytes(upstream, token, repo, legacy_path(path, allow_root=False), office_preview)
        return Response(data, media_type=media_type, headers={'Content-Security-Policy': "default-src 'none'; sandbox"})

    return app
