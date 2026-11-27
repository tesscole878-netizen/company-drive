"""History, trash and external links backed by the owner's Seafile account."""
from datetime import datetime, timezone
import hashlib
import hmac
from pathlib import PurePosixPath
import re
import shutil
from tempfile import TemporaryFile
import time
from typing import Literal
from urllib.parse import urlencode, urlsplit

from fastapi import Body, Depends, Query, Request

from .legacy import legacy_file_response, legacy_path, object_id
from .store import DriveError
from .usage import note_download

ROOT = '/api/legacy/libraries/{repo}'
LINK_KIND = Literal['download', 'upload']


def register_legacy_advanced(app, store, user, bindings):
    upstream = bindings.upstream

    def legacy_token(request, current, repo):
        return app.state.authorize_legacy_read(request, current, repo)

    @app.get(ROOT + '/write-version')
    def write_version(repo: str, path: str, request: Request, current=Depends(user)):
        path = legacy_path(path, allow_root=False)
        store.license.require_write()
        return {'version': upstream.file_for_update(legacy_token(request, current, repo), repo, path)}

    def folder_role(token, repo, path):
        if path == '/':
            detail = upstream._json(f'/api2/repos/{repo}/', token)
        else:
            detail = upstream._json(f'/api/v2.1/repos/{repo}/dir/detail/?' + urlencode({'path': path}), token)
        return detail.get('permission') if isinstance(detail, dict) else None

    def require_role(role, *, write=False):
        if role != 'rw' and (write or role != 'r'):
            raise DriveError(403, '原 Seafile 未授权此操作')

    def receipt(current, kind, repo, path, commit, expires=None):
        deadline = str(expires if expires is not None else int(time.time()) + 900)
        message = '\0'.join((current['id'], kind, repo, path, commit, deadline)).encode()
        signature = hmac.new(store.server_key, message, hashlib.sha256).hexdigest()
        return deadline + '.' + signature

    def check_receipt(current, kind, repo, path, commit, proof):
        deadline, _, _ = proof.partition('.')
        if (not object_id(commit) or not deadline.isascii() or not deadline.isdecimal() or
                len(deadline) > 12 or int(deadline) <= time.time() or
                not hmac.compare_digest(receipt(current, kind, repo, path, commit, deadline), proof)):
            raise DriveError(409, '恢复选择已失效，请重新打开列表')

    @app.get(ROOT + '/history')
    def history(repo: str, path: str, request: Request, page: int = Query(1, ge=1, le=1000), current=Depends(user)):
        path = legacy_path(path, allow_root=False)
        token = legacy_token(request, current, repo)
        file = upstream.file_detail(token, repo, path)
        require_role(file.get('permission'))
        data = upstream._json(f'/api/v2.1/repos/{repo}/file/new_history/?' +
                              urlencode({'path': path, 'page': page, 'per_page': 25}), token)
        entries = []
        parents = {}
        for item in data.get('data', []):
            commit = object_id(item.get('commit_id'))
            previous = legacy_path(item.get('path') or path, allow_root=False)
            if previous != path:
                parent = str(PurePosixPath(previous).parent)
                if parent not in parents:
                    try:
                        parents[parent] = folder_role(token, repo, parent)
                    except DriveError as error:
                        if error.status not in (403, 404):
                            raise
                        parents[parent] = None
                if parents[parent] not in ('r', 'rw'):
                    continue
            if commit:
                entries.append({'commit': commit, 'path': previous, 'time': item.get('ctime'),
                                'author': item.get('creator_name'), 'size': item.get('size'),
                                'proof': receipt(current, 'history:' + path, repo, previous, commit)})
        return {'items': entries, 'more': page * 25 < data.get('total_count', 0),
                'version': file['id'], 'writable': file.get('permission') == 'rw'}

    @app.get(ROOT + '/history/download')
    def download_history(repo: str, path: str, original: str, commit: str, request: Request,
                         proof: str = Query(max_length=96), current=Depends(user)):
        path = legacy_path(path, allow_root=False)
        original = legacy_path(original, allow_root=False)
        token = legacy_token(request, current, repo)
        require_role(upstream.file_detail(token, repo, path).get('permission'))
        require_role(folder_role(token, repo, str(PurePosixPath(original).parent)))
        check_receipt(current, 'history:' + path, repo, original, commit, proof)
        note_download(request, current, 'legacy', repo, original)
        return legacy_file_response(upstream, token, repo, original, revision=commit)

    @app.post(ROOT + '/history/restore')
    def restore_history(repo: str, request: Request, path: str = Body(), commit: str = Body(),
                        version: str = Body(), proof: str = Body(max_length=96), original: str = Body(''),
                        current=Depends(user)):
        path = legacy_path(path, allow_root=False)
        original = legacy_path(original or path, allow_root=False)
        check_receipt(current, 'history:' + path, repo, original, commit, proof)
        with store.file_locks('seafile:' + repo):
            token = legacy_token(request, current, repo)
            if upstream.file_for_update(token, repo, path) != version:
                raise DriveError(409, '文件已更新，请刷新历史版本后再恢复')
            entries = upstream.dav_listing(token, repo, str(PurePosixPath(path).parent))
            item = next((item for item in entries if not item['is_dir'] and item['name'] == PurePosixPath(path).name), None)
            if not item or item['object_id'] != version:
                raise DriveError(409, '文件已变化，请刷新后重试')
            require_role(item.get('permission'), write=True)
            if item['locked_by_other']:
                raise DriveError(423, '原文件正在被他人编辑')
            if original != path:
                require_role(folder_role(token, repo, str(PurePosixPath(original).parent)))
                connection, response = upstream.download_revision(token, repo, original, commit)
                try:
                    size = 0
                    reserve = 2 * 1024**3
                    with TemporaryFile(dir=store.secure_staging) as saved:
                        while block := response.read(1024**2):
                            size += len(block)
                            if size > store.max_file:
                                raise DriveError(413, '此历史版本超过文件大小限制')
                            if shutil.disk_usage(store.secure_staging).free < reserve + len(block):
                                raise DriveError(507, '暂存空间不足，无法恢复此版本')
                            saved.write(block)
                        length = response.getheader('Content-Length')
                        if length and length.isdecimal() and int(length) != size:
                            raise DriveError(502, '原 Seafile 未返回完整历史版本')
                        saved.seek(0)
                        store.license.require_write()
                        upstream.save_file(legacy_token(request, current, repo), repo, path, version, saved, size,
                                           allow_encrypted=True)
                finally:
                    connection.close()
                return {'ok': True}
            result = upstream._json(f'/api2/repos/{repo}/file/revert/', token, {'p': path, 'commit_id': commit}, method='PUT')
            if not isinstance(result, dict) or result.get('success') is not True:
                raise DriveError(502, '旧 Seafile 未确认恢复结果')
            return {'ok': True}

    @app.get(ROOT + '/trash')
    def trash(repo: str, request: Request, path: str = '/', cursor: str = Query('', max_length=6000), current=Depends(user)):
        path = legacy_path(path)
        token = legacy_token(request, current, repo)
        role = folder_role(token, repo, path)
        require_role(role)
        query = {'path': path}
        if cursor:
            query['scan_stat'] = cursor
        result = upstream._json(f'/api/v2.1/repos/{repo}/trash/?' + urlencode(query), token)
        permissions = {path: role}
        items = []
        for row in result.get('data', []):
            commit = object_id(row.get('commit_id'))
            parent = legacy_path(row['parent_dir'].rstrip('/') or '/')
            if not commit or (path != '/' and parent != path and not parent.startswith(path + '/')):
                continue
            if parent not in permissions:
                try:
                    permissions[parent] = folder_role(token, repo, parent)
                except DriveError as error:
                    if error.status not in (403, 404):
                        raise
                    permissions[parent] = None
            if permissions[parent] not in ('r', 'rw'):
                continue
            item_path = legacy_path(parent.rstrip('/') + '/' + row['obj_name'], allow_root=False)
            items.append({'path': item_path, 'commit': commit, 'time': row.get('deleted_time'),
                          'is_dir': row.get('is_dir'), 'writable': permissions[parent] == 'rw',
                          'proof': receipt(current, 'trash', repo, item_path, commit)})
        return {'items': items, 'cursor': result.get('scan_stat') if result.get('more') else None}

    @app.post(ROOT + '/trash/restore')
    def restore_trash(repo: str, request: Request, path: str = Body(), commit: str = Body(),
                      proof: str = Body(max_length=96), current=Depends(user)):
        path = legacy_path(path, allow_root=False)
        check_receipt(current, 'trash', repo, path, commit, proof)
        parent = str(PurePosixPath(path).parent)
        with store.file_locks('seafile:' + repo):
            token = legacy_token(request, current, repo)
            require_role(folder_role(token, repo, parent), write=True)
            if any(item['name'].casefold() == PurePosixPath(path).name.casefold() for item in upstream.listing(token, repo, parent)):
                raise DriveError(409, '已有同名文件或文件夹，请先保留或改名')
            result = upstream._json(f'/api/v2.1/repos/{repo}/trash/revert-dirents/', token,
                                    {'path': path, 'commit_id': commit}, method='POST')
            if not any(item.get('path') == path for item in result.get('success', [])):
                raise DriveError(409, '旧 Seafile 未恢复此项目，请刷新回收站')
            return {'ok': True}

    def links_api(kind, key=''):
        endpoint = '/api/v2.1/upload-links/' if kind == 'upload' else '/api/v2.1/share-links/'
        if key:
            if not re.fullmatch(r'[0-9a-f]{4,100}', key):
                raise DriveError(400, '旧分享链接编号无效')
            return endpoint + key + '/'
        return endpoint

    def present_link(item, kind):
        key = item.get('token')
        if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{4,100}', key):
            raise DriveError(502, '旧 Seafile 未返回有效链接')
        path = urlsplit(item.get('link') or '').path
        if path not in (f'/f/{key}/', f'/d/{key}/', f'/u/d/{key}/'):
            raise DriveError(502, '旧 Seafile 返回了无法识别的链接')
        return {'id': key, 'kind': kind, 'path': item.get('path'), 'url': upstream.origin + path,
                'expires': item.get('expire_date'), 'expired': bool(item.get('is_expired')),
                'has_password': bool(item.get('password') or item.get('has_password'))}

    def link_token(request, current, repo):
        token = bindings.token(current['id'])
        upstream.require_library(token, repo)
        return token

    def linked_item(repo, kind, key, token, current):
        item = upstream._json(links_api(kind, key), token)
        if item.get('repo_id') != repo:
            raise DriveError(404, '该链接不属于此资料库')
        # A Seafile site administrator can inspect other users' links. This
        # employee action manages links created under the caller's own identity.
        if item.get('username') != bindings.get(current['id'])['account']:
            raise DriveError(403, '只有链接创建者可以管理此链接')
        return item

    def expiry(value):
        if value is None:
            return {}
        if value.utcoffset() is None or value <= datetime.now(timezone.utc):
            raise DriveError(400, '到期时间需晚于现在，并含时区')
        return {'expiration_time': value.isoformat()}

    @app.get(ROOT + '/external-links')
    def external_links(repo: str, request: Request, path: str = '/', folder: bool = False, current=Depends(user)):
        path = legacy_path(path)
        token = link_token(request, current, repo)
        items = []
        for kind in (('download', 'upload') if folder or path == '/' else ('download',)):
            result = upstream._json(links_api(kind) + '?' + urlencode({'repo_id': repo, 'path': path}), token)
            if isinstance(result, dict):
                result = result.get('data', [])
            items.extend(present_link(item, kind) for item in result
                         if item.get('repo_id') == repo and (item.get('path') or '').rstrip('/') == path.rstrip('/'))
        return {'items': items}

    @app.post(ROOT + '/external-links', status_code=201)
    def create_external(repo: str, request: Request, path: str = Body(), kind: LINK_KIND = Body(),
                        password: str = Body('', max_length=64), expires: datetime | None = Body(None),
                        current=Depends(user)):
        path = legacy_path(path)
        token = link_token(request, current, repo)
        fields = {'repo_id': repo, 'path': path, **expiry(expires)}
        if password:
            fields['password'] = password
        result = upstream._json(links_api(kind), token, fields, method='POST')
        link = present_link(result, kind)
        store.usage.record(current, 'share', 'legacy', repo, path, name=result.get('repo_name') or '')
        return link

    @app.patch(ROOT + '/external-links/{kind}/{key}')
    def update_external(repo: str, kind: LINK_KIND, key: str, request: Request,
                        expires: datetime | None = Body(None), password: str | None = Body(None, max_length=64),
                        current=Depends(user)):
        token = link_token(request, current, repo)
        linked_item(repo, kind, key, token, current)
        if (expires is None) == (password is None):
            raise DriveError(400, '请选择修改有效期或更换链接密码')
        if password is not None:
            result = upstream._json('/api/v2.1/suyan-link-password/' + kind + '/' + key + '/', token,
                                    {'password': password}, method='PUT')
        else:
            result = upstream._json(links_api(kind, key), token, expiry(expires), method='PUT')
        return present_link(result, kind)

    @app.delete(ROOT + '/external-links/{kind}/{key}')
    def delete_external(repo: str, kind: LINK_KIND, key: str, request: Request, current=Depends(user)):
        token = link_token(request, current, repo)
        linked_item(repo, kind, key, token, current)
        upstream._json(links_api(kind, key), token, method='DELETE')
        return {'ok': True}
