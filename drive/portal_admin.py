"""Forward authenticated local administrators to the full cloud application."""
import http.client
import json
from http.cookies import SimpleCookie

from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .store import DriveError
from .legacy import OTPRequired


class PortalAdmin:
    def __init__(self, host='127.0.0.1', port=8765):
        self.host, self.port = host, port

    def login(self, username, password):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=8)
        try:
            connection.request('POST', '/api/login', json.dumps({'username': username, 'password': password}).encode(),
                               {'Content-Type': 'application/json'})
            response = connection.getresponse()
            body = response.read(4096)
            if response.status != 200:
                raise DriveError(422, '管理员账号或密码错误')
            identity = json.loads(body)
            if not identity.get('admin'):
                raise DriveError(403, '需要系统管理员账号')
            cookies = SimpleCookie()
            for header, value in response.getheaders():
                if header.lower() == 'set-cookie':
                    cookies.load(value)
            session = cookies.get('drive_session')
            if session is None:
                raise DriveError(502, '管理员会话未创建')
            return identity, session.value
        except (OSError, http.client.HTTPException, ValueError):
            raise DriveError(502, '管理服务暂时不可用，请稍后重试') from None
        finally:
            connection.close()

    def identity_login(self, username, password, otp=''):
        connection=http.client.HTTPConnection(self.host,self.port,timeout=60)
        try:
            connection.request('POST','/api/identity/session',json.dumps({'username':username,'password':password,'otp':otp}).encode(),{'Content-Type':'application/json'})
            response=connection.getresponse()
            body=json.loads(response.read(16384))
            if response.status!=200:
                if body.get('code')=='seafile_otp_required':raise OTPRequired()
                raise DriveError(response.status,body.get('detail','统一账号登录失败'))
            cookies=SimpleCookie()
            for key,value in response.getheaders():
                if key.lower()=='set-cookie':cookies.load(value)
            session=cookies.get('drive_session')
            if session is None or not body.get('legacy_token'):
                raise DriveError(502,'统一账号会话创建失败')
            return body,session.value
        except (OSError,http.client.HTTPException,ValueError):
            raise DriveError(502,'统一账号服务暂时不可用') from None
        finally:connection.close()

    def end_session(self, token, csrf):
        connection=http.client.HTTPConnection(self.host,self.port,timeout=8)
        try:
            connection.request('POST','/api/logout',headers={'Cookie':'drive_session='+token,'X-Drive-CSRF':csrf})
            connection.getresponse().read(4096)
        except (OSError,http.client.HTTPException):
            pass
        finally:connection.close()

    def identity_active(self, token):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=8)
        try:
            connection.request('GET', '/api/identity/active', headers={'Cookie': 'drive_session=' + token})
            response = connection.getresponse()
            response.read(4096)
            if response.status in (200, 401):
                return response.status == 200
            raise DriveError(502, '统一账号服务暂时不可用')
        except (OSError, http.client.HTTPException):
            raise DriveError(502, '统一账号服务暂时不可用') from None
        finally:
            connection.close()

    async def forward(self, request, account):
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 8 * 1024**2:
                return JSONResponse({'detail': '单次请求超过限制'}, status_code=413)
            chunks.append(chunk)
        endpoint = request.url.path + (('?' + request.url.query) if request.url.query else '')
        headers = {name: request.headers[name] for name in ('content-type', 'x-drive-csrf', 'upload-offset', 'range', 'if-range')
                   if name in request.headers}
        headers['Cookie'] = 'drive_session=' + account['token']
        headers['X-Forwarded-Proto'] = request.url.scheme
        if request.client:
            headers['X-Forwarded-For'] = request.client.host

        def send():
            connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
            try:
                connection.request(request.method, endpoint, b''.join(chunks), headers)
                return connection, connection.getresponse()
            except (OSError, http.client.HTTPException):
                connection.close()
                raise DriveError(502, '管理服务连接失败') from None

        connection, response = await run_in_threadpool(send)
        excluded = {'connection', 'keep-alive', 'transfer-encoding', 'set-cookie', 'server', 'date'}
        output = {key: value for key, value in response.getheaders() if key.lower() not in excluded}
        if request.url.path == '/assets/app.js' and response.status == 200:
            try:
                script = response.read().decode('utf-8').replace('authPage(false);', 'location.reload();')
            finally:
                connection.close()
            for header in ('content-length', 'etag', 'last-modified'):
                output = {key: value for key, value in output.items() if key.lower() != header}
            return Response(script, media_type='text/javascript', headers=output)

        def stream():
            try:
                while chunk := response.read(256 * 1024):
                    yield chunk
            finally:
                connection.close()

        return StreamingResponse(stream(), status_code=response.status, headers=output,
                                 background=BackgroundTask(connection.close))
