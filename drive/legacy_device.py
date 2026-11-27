"""Read-only access to encrypted Seafile libraries from the Windows assistant."""
import base64
import hashlib
import json

from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool

from .legacy import LegacyLibraryLocked, legacy_file_response, legacy_path, pairing_allowed
from .store import DriveError
from .usage import note_download

DEVICE_LIBRARY_ROOT = '/dav/.suyan-encrypted'


def register_encrypted_library_reader(app, store, bindings, library_access, is_unlocked, unlock_scoped, relock_scope):
    upstream = bindings.upstream

    def device(request: Request):
        if not pairing_allowed(request) or request.headers.get('X-Suyan-Encrypted') != '1':
            raise DriveError(403, '请通过 HTTPS 在连接助手中访问加密库')
        header = request.headers.get('Authorization', '')
        if header[:6].lower() != 'basic ' or len(header) > 1500:
            raise DriveError(401, '设备授权已失效，请重新连接')
        try:
            username, password = base64.b64decode(header[6:], validate=True).decode('utf8').split(':', 1)
        except (ValueError, UnicodeError):
            raise DriveError(401, '设备授权已失效，请重新连接') from None
        current = store.dav_auth(username, password)
        if not current:
            raise DriveError(401, '设备授权已失效，请重新连接')
        directory = getattr(store, 'identity_directory', None)
        if directory:
            directory.authorize(current)
        token_id = password.split('.', 1)[0]
        if request.client:
            store.touch_device(token_id, request.client.host)
        return current, ('device', token_id)

    def unlocked(repo, identity):
        current, scope = identity
        token, library, key = library_access(current, repo, scope)
        if not library['encrypted']:
            raise DriveError(404, '此入口仅用于加密资料库')
        if not is_unlocked(key):
            raise LegacyLibraryLocked()
        return token

    @app.get(DEVICE_LIBRARY_ROOT + '/libraries', include_in_schema=False)
    def libraries(identity=Depends(device)):
        current, scope = identity
        if bindings.get(current['id']) is None:
            return []
        token = bindings.token(current['id'])
        token_hash = hashlib.sha256(token.encode()).digest()
        libraries = {item['id']: item for item in upstream.libraries(token) if item['encrypted']}
        return [{'id': rid, 'name': item['name'], 'unlocked': is_unlocked((current['id'], scope, token_hash, rid))}
                for rid, item in libraries.items()]

    @app.post(DEVICE_LIBRARY_ROOT + '/{repo}/unlock', include_in_schema=False)
    async def unlock(repo: str, request: Request, identity=Depends(device)):
        current, scope = identity
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 4096:
                raise DriveError(413, '解锁请求过大')
            body.extend(chunk)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            raise DriveError(400, '请输入有效的原资料库密码') from None
        return await run_in_threadpool(unlock_scoped, current, repo, scope, data)

    @app.post(DEVICE_LIBRARY_ROOT + '/lock', include_in_schema=False)
    def lock(identity=Depends(device)):
        current, scope = identity
        relock_scope(current['id'], scope)
        return {'ok': True}

    @app.get(DEVICE_LIBRARY_ROOT + '/{repo}/files', include_in_schema=False)
    def files(repo: str, path: str = '/', identity=Depends(device)):
        return upstream.listing(unlocked(repo, identity), repo, legacy_path(path))

    @app.get(DEVICE_LIBRARY_ROOT + '/{repo}/download', include_in_schema=False)
    def download(repo: str, path: str, request: Request, identity=Depends(device)):
        note_download(request, identity[0], 'legacy', repo, path)
        return legacy_file_response(upstream, unlocked(repo, identity), repo, path)
