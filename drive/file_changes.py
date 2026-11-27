"""Deliver file activity only while the recipient can still see the file."""
from pathlib import PurePosixPath
from urllib.parse import urlencode

from fastapi import Depends, Request

from .store import DriveError
from .legacy import valid_repo

ACTIONS = {'upload': 'upload', 'edit': 'edit', 'create': 'create', 'version_restore': 'recover',
           'copy': 'copy', 'restore': 'recover'}
PAGE = 80
DIRECTORY_CHANGES = {'delete', 'move', 'rename', 'mkdir'}


def native_page(store, current, after):
    latest = store.one('SELECT coalesce(max(id),0) AS id FROM events')['id']
    if after < 0 or after >= latest:
        return latest, [], False
    rows = store.all('SELECT e.id,e.group_id,e.action,e.path,e.entry_created,e.previous_path,u.name AS actor FROM events e '
                     'LEFT JOIN users u ON u.id=e.user_id WHERE e.id>? AND e.id<=? ORDER BY e.id LIMIT ?',
                     (after, latest, PAGE))
    changes = [row for row in rows if (row['action'] in ACTIONS or row['action'] in DIRECTORY_CHANGES)
               and row['group_id'] and row['path']]
    items = []
    if changes:
        _, catalog = store.library_grants(current)
        for row in changes:
            entry = catalog.get(row['group_id'])
            if not entry:
                continue
            lib, grants = entry
            try:
                if lib['encryption']:
                    store.library_key(current, lib['id'])
                if row['action'] in DIRECTORY_CHANGES:
                    if row['action'] == 'rename' and not row['previous_path']:
                        continue  # Earlier releases stored a display string instead of paths.
                    paths = [row['path']]
                    if row['previous_path']:
                        paths.append(row['previous_path'])
                    for parent in dict.fromkeys(path.rpartition('/')[0] for path in paths):
                        if store.role_for_path(grants, parent) and store.path(lib['id'], parent).is_dir():
                            items.append({'actor':'', 'library':lib['name'], 'path':parent,
                                          'action':'refresh', 'repo':lib['id'], 'source':'native'})
                    continue
                if not store.role_for_path(grants, row['path']):
                    continue
                if not store.path(lib['id'],row['path']).is_file() or PurePosixPath(row['path']).name.startswith('~$'):
                    continue
            except (DriveError, FileNotFoundError):
                continue
            action = ACTIONS[row['action']]
            if action == 'upload' and row['entry_created'] is not None:
                action = 'create' if row['entry_created'] else 'update'
            elif action == 'copy' and row['entry_created'] == 0:
                action = 'update'
            items.append({'actor': row['actor'] or '上传用户', 'library':lib['name'], 'path':row['path'],
                          'action':action, 'repo':lib['id'], 'source':'native'})
    cursor = rows[-1]['id'] if rows else latest
    return cursor, items, cursor < latest


def register_changes(app, store, user, bindings):
    def page(current, cursor='', request=None):
        try:
            local, original = map(int, cursor.split(':')) if cursor else (-1,-1)
            if min(local, original) < -1 or max(local, original) >= 2**63:
                raise ValueError
        except (ValueError, AttributeError):
            raise DriveError(400, '通知游标无效') from None
        local, items, more = native_page(store, current, local)
        available = True
        if bindings and bindings.get(current['id']):
            token = bindings.token(current['id'])
            try:
                data = bindings.upstream._json('/api/v2.1/suyan-file-changes/?' + urlencode({'after':original}), token)
                next_id = int(data['cursor'])
                checked = []
                for item in data['items']:
                    if not valid_repo(item.get('repo')):
                        continue
                    if item['encrypted']:
                        if request is None:
                            continue
                        try:
                            app.state.authorize_legacy_read(request,current,item['repo'])
                        except DriveError as exc:
                            if exc.status not in (403,404,423):
                                raise
                            continue
                    checked.append({**{k:item[k] for k in ('library','path','actor','action','repo')}, 'source':'legacy'})
                items.extend(checked)
                original = next_id
                more = more or data['more']
            except DriveError as exc:
                if exc.status not in (404,429,502,503):
                    raise
                available = False
        return {'cursor':f'{local}:{original}', 'items':items, 'more':more, 'legacy_available':available}

    store.file_change_page = page

    @app.get('/api/file-changes')
    def file_changes(request: Request, cursor: str = '', current=Depends(user)):
        return page(current,cursor,request)
