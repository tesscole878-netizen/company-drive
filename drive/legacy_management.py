"""Manage personal Seafile libraries using the acting member's own credentials."""
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlencode

from fastapi import Body, Depends

from .legacy import legacy_path, legacy_permission, valid_repo
from .store import DriveError, valid_name


SHARE_SUBJECT = Literal['user', 'group']
SHARE_PERMISSIONS = {'reader': 'r', 'editor': 'rw'}
ROUTE = '/api/legacy/libraries/{repo}'


def register_legacy_management(app, store, user, bindings):
    upstream = bindings.upstream

    def owner(current, repo):
        if not valid_repo(repo):
            raise DriveError(404, '原资料库不存在')
        token = bindings.token(current['id'])
        details = upstream._json(f'/api2/repos/{repo}/', token)
        if not isinstance(details, dict) or details.get('owner') != 'self' or details.get('permission') != 'rw':
            raise DriveError(403, '只有原资料库所有者可以管理')
        return token, details

    def directory(token, repo, details, path):
        if path == '/':
            return
        if details.get('encrypted'):
            raise DriveError(403, '原加密资料库的子目录暂不可管理')
        location = PurePosixPath(path)
        entries = upstream.listing(token, repo, str(location.parent))
        found = next((item for item in entries if item['name'] == location.name and item['is_dir']), None)
        if found is None:
            raise DriveError(404, '原文件夹不存在')
        if found.get('permission') != 'rw':
            raise DriveError(403, '没有此原文件夹的管理权限')
        return entries

    def shares_url(repo, path, **fields):
        return f'/api2/repos/{repo}/dir/shared_items/?' + urlencode({'p': path, **fields})

    def shares(token, repo, path):
        response = upstream._json(shares_url(repo, path), token)
        if not isinstance(response, list):
            raise DriveError(502, '原 Seafile 未返回有效的共享列表')
        result = []
        for row in response:
            if not isinstance(row, dict):
                continue
            kind = row.get('share_type')
            info = row.get('user_info') if kind == 'user' else row.get('group_info')
            if kind not in ('user', 'group') or not isinstance(info, dict):
                continue
            target = info.get('name') if kind == 'user' else info.get('id')
            if kind == 'user' and not isinstance(target, str) or kind == 'group' and type(target) is not int:
                continue
            result.append({'subject_type': kind, 'target': str(target),
                           'name': info.get('nickname') or info.get('name') or str(target),
                           'permission': 'admin' if row.get('is_admin') else legacy_permission(row)})
        return result

    @app.patch(ROUTE, summary='重命名本人拥有的原 Seafile 资料库')
    def rename_library(repo: str, name: str = Body(embed=True), current=Depends(user)):
        name = valid_name(name.strip())
        with store.file_locks('seafile:' + repo):
            token, _ = owner(current, repo)
            upstream._json(f'/api2/repos/{repo}/?op=rename', token, {'repo_name': name}, method='POST')
        return {'ok': True}

    @app.delete(ROUTE, summary='删除本人拥有的原 Seafile 资料库')
    def delete_library(repo: str, name: str, version: str, current=Depends(user)):
        with store.file_locks('seafile:' + repo):
            token, details = owner(current, repo)
            if not version or details.get('name') != name or details.get('head_commit_id') != version:
                raise DriveError(409, '原资料库已变更，请刷新后重新确认删除')
            upstream._json(f'/api2/repos/{repo}/', token, method='DELETE')
        return {'ok': True}

    @app.get(ROUTE + '/management', summary='读取本人原资料库的名称和版本')
    def management(repo: str, current=Depends(user)):
        _, details = owner(current, repo)
        return {'name': details['name'], 'version': details.get('head_commit_id', '')}

    @app.patch(ROUTE + '/folders', summary='重命名本人原资料库中的可编辑文件夹')
    def rename_folder(repo: str, path: str = Body(), name: str = Body(), current=Depends(user)):
        path = legacy_path(path, allow_root=False)
        name = valid_name(name.strip())
        with store.file_locks('seafile:' + repo):
            token, details = owner(current, repo)
            entries = directory(token, repo, details, path)
            location = PurePosixPath(path)
            if name == location.name:
                return {'ok': True}
            if any(item['name'].casefold() == name.casefold() and item['name'] != location.name
                   for item in entries):
                raise DriveError(409, '已经存在同名文件或文件夹')
            endpoint = f'/api2/repos/{repo}/dir/?' + urlencode({'p': path})
            upstream._json(endpoint, token, {'operation': 'rename', 'newname': name}, method='POST')
        return {'ok': True}

    @app.get(ROUTE + '/shares', summary='列出本人原资料库或文件夹的内部共享')
    def list_shares(repo: str, path: str = '/', current=Depends(user)):
        path = legacy_path(path)
        token, details = owner(current, repo)
        directory(token, repo, details, path)
        return shares(token, repo, path)

    @app.get(ROUTE + '/share-targets', summary='搜索可选的原 Seafile 员工或群组')
    def share_targets(repo: str, subject_type: SHARE_SUBJECT, q: str = '', current=Depends(user)):
        token, _ = owner(current, repo)
        if subject_type == 'group':
            return upstream.groups(token)
        needle = '%' + q[:60] + '%'
        return store.all('SELECT id,name,username FROM users WHERE active=1 AND id!=? AND seafile_identity IS NOT NULL '
                         'AND (name LIKE ? OR username LIKE ?) ORDER BY name LIMIT 200',
                         (current['id'], needle, needle))

    @app.put(ROUTE + '/shares', summary='共享本人原资料库或文件夹给员工或原群组')
    def save_share(repo: str, path: str = Body(), subject_type: SHARE_SUBJECT = Body(),
                   subject_id: str = Body(max_length=254), role: Literal['reader', 'editor'] = Body(), current=Depends(user)):
        path = legacy_path(path)
        with store.file_locks('seafile:' + repo):
            token, details = owner(current, repo)
            directory(token, repo, details, path)
            if subject_type == 'user':
                member = store.one('SELECT seafile_identity FROM users WHERE id=? AND active=1', (subject_id,))
                if not member or not member['seafile_identity'] or subject_id == current['id']:
                    raise DriveError(400, '请选择已关联原 Seafile 的其他员工')
                target = member['seafile_identity']
                key = 'username'
            else:
                group = next((group for group in upstream.groups(token) if str(group['id']) == subject_id), None)
                if not group:
                    raise DriveError(400, '请选择原 Seafile 群组')
                target = str(group['id'])
                key = 'group_id'
            permission = SHARE_PERMISSIONS[role]
            previous = next((row for row in shares(token, repo, path)
                             if row['subject_type'] == subject_type and row['target'] == target), None)
            if previous:
                if previous['permission'] == permission:
                    return {'ok': True}
                if previous['permission'] not in SHARE_PERMISSIONS.values():
                    raise DriveError(409, '现有授权使用原 Seafile 的自定义权限，请先撤销该授权')
                result = upstream._json(shares_url(repo, path, share_type=subject_type, **{key: target}), token,
                                        {'permission': permission}, method='POST')
                if not isinstance(result, dict) or result.get('success') is not True:
                    raise DriveError(502, '原 Seafile 未确认权限更新')
            else:
                result = upstream._json(shares_url(repo, path), token,
                                        {'share_type': subject_type, key: target, 'permission': permission}, method='PUT')
                if not isinstance(result, dict) or not result.get('success') or result.get('failed'):
                    raise DriveError(409, '原 Seafile 拒绝了共享，请检查共享对象和原库权限')
            store.usage.record(current, 'share', 'legacy', repo, path, name=details.get('name') or '')
        return {'ok': True}

    @app.delete(ROUTE + '/shares', summary='撤销本人原资料库或文件夹的内部共享')
    def revoke_share(repo: str, path: str, subject_type: SHARE_SUBJECT, target: str, current=Depends(user)):
        path = legacy_path(path)
        with store.file_locks('seafile:' + repo):
            token, details = owner(current, repo)
            directory(token, repo, details, path)
            if not any(row['subject_type'] == subject_type and row['target'] == target for row in shares(token, repo, path)):
                return {'ok': True}
            key = 'username' if subject_type == 'user' else 'group_id'
            result = upstream._json(shares_url(repo, path, share_type=subject_type, **{key: target}), token, method='DELETE')
            if not isinstance(result, dict) or result.get('success') is not True:
                raise DriveError(502, '原 Seafile 未确认撤销共享')
        return {'ok': True}
