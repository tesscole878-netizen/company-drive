"""Operations backed by the member's existing Seafile library and group."""
from pathlib import PurePosixPath
import re
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import Body, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from tempfile import SpooledTemporaryFile

from .legacy import legacy_path, valid_repo
from .store import DriveError, valid_name


def register_coexist(app, store, user, bindings):
    upstream = bindings.upstream

    def groups_for(current, token):
        account = bindings.get(current['id'])['account']
        memberships = upstream._json('/api/v2.1/groups/', token)
        result = {}
        for group in memberships:
            gid = group['id']
            admins = group.get('admins') or []
            role = 'owner' if group.get('owner') == account else 'manager' if account in admins else 'member'
            result[gid] = role
        return result

    @app.get('/api/legacy/group-roles')
    def roles(current=Depends(user)):
        if not bindings.get(current['id']):
            return {}
        token = bindings.token(current['id'])
        roles = groups_for(current, token)
        return {r['group_id']: {'id': r['external_id'], 'role': roles[r['external_id']]}
                for r in store.all('SELECT group_id,external_id FROM seafile_groups') if r['external_id'] in roles}

    def managed_group(current, group_id):
        token = bindings.token(current['id'])
        if groups_for(current, token).get(group_id) not in ('owner', 'manager'):
            raise DriveError(403, '需要原群组的管理权限')
        return token

    @app.get('/api/legacy/groups/{group_id}/members')
    def group_members(group_id: int, page: int = Query(1, ge=1), current=Depends(user)):
        token = managed_group(current, group_id)
        rows = upstream._json(f'/api/v2.1/groups/{group_id}/members/?' + urlencode({'page': page, 'per_page': 100}), token)
        return [{'email': r['email'], 'name': r['name'], 'role': r['role']} for r in rows]

    @app.post('/api/legacy/groups/{group_id}/members')
    def add_member(group_id: int, uid: str = Body(embed=True), current=Depends(user)):
        token = managed_group(current, group_id)
        row = store.one('SELECT seafile_identity FROM users WHERE id=? AND active=1', (uid,))
        if not row or not row['seafile_identity']:
            raise DriveError(400, '请选择已关联原云盘的员工')
        upstream._json(f'/api/v2.1/groups/{group_id}/members/', token, {'email':row['seafile_identity']}, method='POST')
        return {'ok': True}

    @app.delete('/api/legacy/groups/{group_id}/members')
    def remove_member(group_id: int, email: str = Query(min_length=1, max_length=254), current=Depends(user)):
        token = managed_group(current, group_id)
        if '/' in email or email in ('.', '..'):
            raise DriveError(400, '成员账号格式无效')
        upstream._json(f'/api/v2.1/groups/{group_id}/members/{quote(email, safe="")}/', token, method='DELETE')
        return {'ok': True}

    @app.get('/api/legacy/groups/{group_id}/settings')
    def group_settings(group_id: int, current=Depends(user)):
        token = managed_group(current, group_id)
        group = upstream._json(f'/api/v2.1/groups/{group_id}/', token)
        return {'name': group['name'], 'can_manage_admins': groups_for(current, token).get(group_id) == 'owner'}

    @app.patch('/api/legacy/groups/{group_id}/settings')
    def rename_group(group_id: int, name: str = Body(embed=True, min_length=1, max_length=80), current=Depends(user)):
        name = valid_name(name.strip())
        with store.file_locks('seafile-group:' + str(group_id)):
            token = managed_group(current, group_id)
            result = upstream._json(f'/api/v2.1/groups/{group_id}/', token, {'name': name}, method='PUT')
            if not isinstance(result, dict) or result.get('name') != name:
                raise DriveError(502, '旧 Seafile 未确认群组重命名')
            with store.db() as db:
                mapped = db.execute('SELECT group_id FROM seafile_groups WHERE external_id=?', (group_id,)).fetchone()
                if mapped:
                    title = name
                    while db.execute('SELECT 1 FROM groups WHERE name=? COLLATE NOCASE AND id<>?',
                                     (title, mapped['group_id'])).fetchone():
                        title += f'（原群组 {group_id}）'
                    db.execute('UPDATE groups SET name=? WHERE id=?', (title, mapped['group_id']))
            return {'name': name}

    @app.patch('/api/legacy/groups/{group_id}/members/admin')
    def set_group_admin(group_id: int, email: str = Body(min_length=1, max_length=254),
                        administrator: bool = Body(), current=Depends(user)):
        if '/' in email or '\\' in email or email in ('.', '..'):
            raise DriveError(400, '成员账号格式无效')
        with store.file_locks('seafile-group:' + str(group_id)):
            token = bindings.token(current['id'])
            if groups_for(current, token).get(group_id) != 'owner':
                raise DriveError(403, '只有原群组所有者可以任免管理员')
            if bindings.get(current['id'])['account'] == email:
                raise DriveError(400, '所有者角色不能在此修改')
            result = upstream._json(f'/api/v2.1/groups/{group_id}/members/{quote(email, safe="")}/',
                                    token, {'is_admin': str(administrator).lower()}, method='PUT')
            return {'ok': True, 'role': result.get('role')}

    @app.post('/api/legacy/open')
    def open_original(repo: str = Body(''), path: str = Body('/'), group_id: int = Body(0),
                      current=Depends(user)):
        token = bindings.token(current['id'])
        if group_id:
            if group_id not in groups_for(current, token):
                raise DriveError(403, '没有此原群组的访问权限')
            target = f'/group/{group_id}/'
        else:
            library = upstream.require_library(token, repo, allow_encrypted=True)
            path = legacy_path(path)
            # The authoritative web UI performs its own folder and encrypted-library checks.
            target = f'/library/{repo}/' + quote(library['name'], safe='') + '/' + quote(path.strip('/'), safe='/')
        ticket = upstream._json('/api2/client-login/', token, {}, method='POST')
        key = ticket.get('token') if isinstance(ticket, dict) else None
        if not key:
            return {'url': upstream.origin + target}
        if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9]{32}', key):
            raise DriveError(502, '原云盘未返回有效的网页登录凭据')
        return {'url': upstream.origin + '/client-login/?' + urlencode({'token': key, 'next': target})}

    @app.post('/api/legacy/libraries', status_code=201)
    def create(name: str = Body(max_length=80), description: str = Body('', max_length=500),
               password: str = Body('', max_length=200), current=Depends(user)):
        token = bindings.token(current['id'])
        fields = {'name': valid_name(name.strip()), 'desc': description}
        if password:
            if len(password) < 10:
                raise DriveError(400, '资料库密码至少需要 10 位')
            fields['passwd'] = password
        result = upstream._json('/api2/repos/?from=web', token, fields, method='POST')
        if not isinstance(result, dict) or not valid_repo(result.get('repo_id')):
            raise DriveError(502, '原云盘未确认创建结果，请刷新资料库列表')
        return {'id': result['repo_id'], 'name': fields['name'], 'encrypted': bool(password)}

    @app.get('/api/legacy/search')
    def search(q: str = Query(min_length=1, max_length=100), after: str = '', offset: int = Query(0, ge=0),
               current=Depends(user)):
        if not bindings.get(current['id']):
            return {'items': [], 'next': None}
        token = bindings.token(current['id'])
        catalog = {r['id']: r for r in upstream.libraries(token) if not r['encrypted']}
        candidates = sorted(k for k in catalog if k >= after)
        items = []
        for repo in candidates[:3]:
            try:
                raw = upstream._json('/api/v2.1/search-file/?' + urlencode({'repo_id': repo, 'q': q}), token)
            except DriveError as error:
                if error.status in (403, 404):
                    continue
                raise
            if not isinstance(raw, dict) or not isinstance(raw.get('data'), list):
                raise DriveError(502, '无法读取原云盘的搜索结果')
            listing = {}
            def visible(path):
                parent, _, name = path.rpartition('/')
                parent = parent or '/'
                if parent not in listing:
                    if parent != '/' and not visible(parent):
                        listing[parent] = {}
                    else:
                        try:
                            listing[parent] = {e['name']: e for e in upstream.listing(token, repo, parent)}
                        except DriveError as error:
                            if error.status not in (403, 404):
                                raise
                            listing[parent] = {}
                return listing[parent].get(name)
            start = offset if repo == after else 0
            for item in raw['data'][start:start+50]:
                path = legacy_path(item['path'], allow_root=False)
                entry = visible(path)
                if entry:
                    items.append({'repo': repo, 'library': catalog[repo]['name'], 'path': path, **entry})
            if len(raw['data']) > start + 50:
                return {'items': items, 'next': {'after': repo, 'offset': start + 50}}
        return {'items': items, 'next': {'after': candidates[3], 'offset': 0} if len(candidates) > 3 else None}

    @app.get('/api/legacy/libraries/{repo}/folder-permission')
    def folder_permission(repo: str, request: Request, path: str = '/', current=Depends(user)):
        token = app.state.authorize_legacy_read(request, current, repo)
        path = legacy_path(path)
        if path == '/':
            detail = upstream._json(f'/api2/repos/{repo}/', token)
        else:
            detail = upstream._json(f'/api/v2.1/repos/{repo}/dir/detail/?' + urlencode({'path': path}), token)
        return {'writable': detail.get('permission') == 'rw'}

    @app.post('/api/legacy/libraries/{repo}/uploads/content', status_code=201)
    async def upload(repo: str, path: str, request: Request, current=Depends(user)):
        path = legacy_path(path, allow_root=False)
        token = bindings.token(current['id'])
        await run_in_threadpool(upstream.require_library, token, repo)
        size = 0
        with SpooledTemporaryFile(max_size=256*1024, dir=store.secure_staging) as pending:
            async for chunk in request.stream():
                size += len(chunk)
                if size > min(store.max_file, 8*1024**2):
                    raise DriveError(413, '较大的文件请使用当前目录的文件管理入口或电脑盘上传')
                pending.write(chunk)
            pending.seek(0)
            def commit():
                with store.file_locks('seafile:' + repo):
                    store.license.require_write()
                    version = upstream.upload_new(bindings.token(current['id']), repo, path, pending, size)
                    store.usage.record(current, 'upload', 'legacy', repo, path)
                    return version
            return {'version': await run_in_threadpool(commit)}

    @app.post('/api/legacy/libraries/{repo}/entries')
    def change(repo: str, action: Literal['mkdir','rename','delete','move'] = Body(),
               path: str = Body(), destination: str = Body(''), current=Depends(user)):
        token = bindings.token(current['id'])
        upstream.require_library(token, repo)
        with store.file_locks('seafile:' + repo):
            return change_entry(upstream, token, repo, action, path, destination)


def change_entry(upstream, token, repo, action, path, destination=''):
    path = legacy_path(path, allow_root=False)
    parent = str(PurePosixPath(path).parent)
    name = PurePosixPath(path).name
    entries = upstream.dav_listing(token, repo, parent)
    source = next((entry for entry in entries if entry['name'] == name), None)
    if action == 'mkdir':
        valid_name(name)
        if any(e['name'].casefold() == name.casefold() for e in entries):
            raise DriveError(409, '已存在同名项目')
        result = upstream._json(f'/api/v2.1/repos/{repo}/dir/?' + urlencode({'p': path}), token,
                                {'operation':'mkdir'}, method='POST')
        if not isinstance(result, dict) or result.get('obj_name') != name:
            raise DriveError(409, '目录已发生同名冲突，请刷新核对实际名称')
        return result
    if not source:
        raise DriveError(404, '文件或文件夹已经移动')
    if source.get('permission') != 'rw' or source.get('locked_by_other'):
        raise DriveError(403, '文件只读或已由其他员工锁定')
    kind = 'dir' if source['is_dir'] else 'file'
    url = f'/api/v2.1/repos/{repo}/{kind}/?' + urlencode({'p': path})
    if action == 'delete':
        result = upstream._json(url, token, method='DELETE')
        if not isinstance(result, dict) or result.get('success') is not True:
            raise DriveError(502, '原云盘未确认删除结果')
        return {'ok': True}
    if action == 'rename':
        newname = valid_name(destination)
        if newname == name:
            return {'ok': True}
        if any(e['name'].casefold() == newname.casefold() for e in entries):
            raise DriveError(409, '已存在同名项目')
        result = upstream._json(url, token, {'operation':'rename', 'newname':newname}, method='POST')
        if not isinstance(result, dict) or result.get('obj_name') != newname:
            raise DriveError(409, '名称已发生冲突，请刷新后核对')
        return result
    dest = legacy_path(destination)
    if dest == parent or dest == path or dest.startswith(path + '/'):
        raise DriveError(409, '请选择其他目标文件夹')
    if any(e['name'].casefold() == name.casefold() for e in upstream.listing(token, repo, dest)):
        raise DriveError(409, '目标中已存在同名项目')
    if kind == 'file':
        return upstream._json(url, token, {'operation':'move', 'dst_repo':repo, 'dst_dir':dest}, method='POST')
    if ':' in name:
        raise DriveError(409, '此名称请在原云盘文件管理中移动')
    return upstream._json(f'/api2/repos/{repo}/fileops/move/?' + urlencode({'p':parent}), token,
                          {'dst_repo':repo,'dst_dir':dest,'file_names':name}, method='POST')
