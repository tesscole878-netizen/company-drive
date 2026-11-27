from datetime import datetime, timedelta, timezone
from io import BytesIO
import time
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from drive.app import create_app
from drive.legacy import Seafile
from drive.store import DriveError
from drive.upload_integrity import upload_digests
from test_legacy import LIBRARY_PASSWORD, LOCAL_PASSWORD, OLD_PASSWORD, REPO_A, TOKEN_A, TOKEN_B
from test_legacy import signed_in, upstream

ROOT = '/api/legacy/libraries/' + REPO_A
PATH = '/合同/文件.txt'
COMMIT = '3' * 40


def clients(tmp_path):
    app = create_app(tmp_path / 'app')
    for name in ('alpha', 'beta'):
        app.state.store.create_user(name, name, LOCAL_PASSWORD)
    a = signed_in(app, 'alpha')
    b = signed_in(app, 'beta')
    a.post('/api/legacy/binding', json={'username': 'a@old.test', 'password': OLD_PASSWORD}).raise_for_status()
    b.post('/api/legacy/binding', json={'username': 'b@old.test', 'password': OLD_PASSWORD}).raise_for_status()
    return app, a, b


def test_encrypted_upload_requires_session_and_replaces_original(tmp_path, upstream, monkeypatch):
    _, _, data = upstream
    data.update(catalog={TOKEN_A: [{'id': REPO_A, 'name': '加密资料库', 'type': 'repo', 'encrypted': True}]},
                encrypted=True, write_permission='rw', save_path=PATH, upstream_unlocked=False)
    app, a, b = clients(tmp_path)
    begin = ROOT + '/uploads'
    data_to_upload = b'x' * 9 * 1024**2
    _, fingerprint = upload_digests(BytesIO(data_to_upload))
    declaration = {'path': PATH, 'size': len(data_to_upload), 'fingerprint': fingerprint, 'expected': '1' * 40}
    assert a.post(begin, json=declaration).status_code == 423
    assert b.post(begin, json=declaration).status_code == 403
    a.post(ROOT + '/unlock', json={'password': LIBRARY_PASSWORD}).raise_for_status()
    uid = a.post(begin, json=declaration).json()['id']
    endpoint = begin + '/' + uid
    assert signed_in(app, 'alpha').get(endpoint).status_code == 404
    assert b.patch(endpoint, content=b'x', headers={'Upload-Offset': '0'}).status_code == 404
    assert a.post(endpoint + '/complete').status_code == 409
    for i in range(3):
        response = a.patch(endpoint, content=b'x' * 3 * 1024**2, headers={'Upload-Offset': str(i * 3 * 1024**2)})
        assert response.json()['offset'] == (i + 1) * 3 * 1024**2
    spool = app.state.store.tmp / 'legacy-upload' / uid
    assert b'x' * 256 not in spool.read_bytes()
    original_save = Seafile.save_file
    attempts = []

    def response_lost(self, *args, **kwargs):
        attempts.append(True)
        original_save(self, *args, **kwargs)
        raise DriveError(502, '提交成功后连接中断')

    monkeypatch.setattr(Seafile, 'save_file', response_lost)
    assert a.post(endpoint + '/complete').status_code == 202
    for _ in range(80):
        result = a.get(endpoint).json()
        if result['state'] != 'committing':
            break
        time.sleep(.05)
    assert result['state'] == 'retry' and spool.is_file()
    assert a.post(endpoint + '/complete').status_code == 202
    for _ in range(80):
        result = a.get(endpoint).json()
        if result['state'] != 'committing':
            break
        time.sleep(.05)
    assert len(attempts) == 1
    assert result['state'] == 'done' and len(data['content']) == declaration['size']
    assert a.get(ROOT + '/download', params={'path': PATH}).content == data['content']


def test_recovery_and_links_stay_with_the_original_member(tmp_path, upstream, monkeypatch):
    grants, _, data = upstream
    grants[TOKEN_B].append(REPO_A)
    data.update(write_permission='rw', management={'name':'旧库','folder':'合同','folder_permission':'rw','shares':{}})
    _, a, b = clients(tmp_path)
    original = Seafile._json
    source_link = {'repo_id': REPO_A, 'path': PATH, 'token':'a'*16, 'username':'a@old.test',
                   'link':'https://old.test/f/'+'a'*16+'/', 'password':'not returned'}
    deletes = []

    def mock(self, path, token=None, fields=None, *args, **kwargs):
        parsed = urlsplit(path)
        name = parsed.path
        if name.endswith('/file/new_history/'):
            return {'data':[{'commit_id':COMMIT,'path':PATH}], 'total_count': 1}
        if name.endswith('/file/revert/') or name.endswith('/trash/revert-dirents/'):
            deletes.append((name, fields))
            return {'success': [{'path': fields['path']}]} if 'trash' in name else {'success': True}
        if name.endswith('/trash/'):
            return {'data':[{'parent_dir':'/合同','obj_name':'误删.txt','commit_id':COMMIT}], 'more':False}
        if name.endswith('/dir/detail/'):
            return {'permission': data['write_permission']}
        if name.endswith('/share-links/'):
            assert parse_qs(parsed.query).get('path') == [PATH] or kwargs.get('method') == 'POST'
            return source_link if kwargs.get('method') == 'POST' else [source_link]
        if '/share-links/' in name:
            return source_link
        if 'suyan-link-password' in name:
            return {**source_link,'token':'b'*16,'link':'https://old.test/f/'+'b'*16+'/'}
        return original(self, path, token, fields, *args, **kwargs)

    monkeypatch.setattr(Seafile, '_json', mock)
    valid = a.get(ROOT + '/history', params={'path':PATH}).json()
    item = valid['items'][0]
    body = {'path': PATH, 'commit': COMMIT, 'version': valid['version'], 'proof': item['proof']}
    assert b.post(ROOT + '/history/restore', json=body).status_code == 409
    assert a.post(ROOT + '/history/restore', json={**body,'version':'9'*40}).status_code == 409
    assert a.post(ROOT + '/history/restore', json=body).status_code == 200
    removed = a.get(ROOT + '/trash', params={'path':'/合同'}).json()['items'][0]
    assert a.post(ROOT + '/trash/restore', json=removed).status_code == 200
    assert len(deletes) == 2
    listed = a.get(ROOT + '/external-links', params={'path':PATH}).json()['items'][0]
    assert listed['has_password'] and 'password' not in listed
    endpoint = ROOT + '/external-links/download/' + listed['id']
    assert b.delete(endpoint).status_code == 403
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert a.patch(endpoint,json={'expires':expires}).status_code == 200
    assert a.patch(endpoint,json={'password':'temporary'}).json()['id'] == 'b' * 16


def test_only_owner_can_change_original_group_administrators(tmp_path, upstream, monkeypatch):
    _, _, _ = upstream
    _, a, b = clients(tmp_path)
    original = Seafile._json
    name = '原群组'

    def mock(self, path, token=None, fields=None, *args, **kwargs):
        nonlocal name
        if path == '/api/v2.1/groups/':
            return [{'id':12, 'owner':'a@old.test', 'admins':['b@old.test']}]
        if path == '/api/v2.1/groups/12/':
            if fields:
                name = fields['name']
            return {'name':name}
        if path == '/api/v2.1/groups/12/members/b%40old.test/':
            assert token == TOKEN_A
            return {'role':'Admin' if fields['is_admin']=='true' else 'Member'}
        return original(self, path, token, fields, *args, **kwargs)

    monkeypatch.setattr(Seafile, '_json', mock)
    base = '/api/legacy/groups/12'
    assert a.get(base+'/settings').json()['can_manage_admins']
    assert b.patch(base+'/settings',json={'name':'原群组新名称'}).json()['name'] == '原群组新名称'
    body = {'email':'b@old.test', 'administrator':False}
    assert b.patch(base+'/members/admin',json=body).status_code == 403
    assert a.patch(base+'/members/admin',json=body).json()['role'] == 'Member'
