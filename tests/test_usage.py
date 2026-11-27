import time

from fastapi.testclient import TestClient

from test_drive import PASSWORD, env, upload

USAGE = '/api/admin/usage'


def test_connections_require_system_admin_and_exclude_stale_grants(env):
    app, store, admin = (env[key] for key in ('app', 'store', 'owner'))
    staff = TestClient(app, client=('192.0.2.12', 12345))
    login = staff.post('/api/login', json={'username': 'alice', 'password': PASSWORD})
    staff.headers['X-Drive-CSRF'] = login.json()['csrf']
    staff.get('/api/me').raise_for_status()
    with store.db() as db:
        db.execute('INSERT INTO member_managers VALUES(?)', (env['users']['alice']['id'],))
    assert staff.get(USAGE).status_code == 403
    assert staff.get(USAGE + '/operations').status_code == 403
    assert TestClient(app).get(USAGE).status_code == 401
    assert staff.get('/api/me').json()['can_manage_members']

    new_device = staff.post('/api/tokens', json={'name': '新设备'}).json()
    data = admin.get(USAGE).json()
    web = [row for row in data['connections'] if row['ip'] == '192.0.2.12']
    assert len(web) == 1 and web[0]['username'] == 'alice' and web[0]['channel'] == 'web'
    assert not any(row['device'] == '新设备' for row in data['connections'])
    dav = TestClient(app, client=('192.0.2.13', 12345))
    auth = ('alice', new_device['password'])
    dav.request('PROPFIND', '/dav/', headers={'Depth': '0'}, auth=auth).raise_for_status()
    data = admin.get(USAGE).json()
    assert data['total'] == 3 and data['enabled'] == 3 and data['recent_accounts'] >= 1
    assert any(row['ip'] == '192.0.2.13' and row['device'] == '新设备' for row in data['connections'])
    assert data['active_accounts'] == len({row['username'] for row in data['connections']})
    assert dav.get(USAGE, auth=auth).status_code == 401
    staff.delete('/api/tokens/' + new_device['id']).raise_for_status()
    staff.post('/api/logout').raise_for_status()
    with store.db() as db:
        db.execute('UPDATE usage_web SET last_seen=?', (time.time() - 301,))
    data = admin.get(USAGE).json()
    assert not any(row['username'] == 'alice' for row in data['connections'])
    assert data['recent_accounts'] >= 1


def test_operations_record_successful_file_actions_and_merge_dav_ranges(env):
    app, admin = env['app'], env['owner']
    alice = env['clients']['alice']
    library = alice.post('/api/libraries', json={'name': '操作测试'}).json()
    lid = library['id']
    upload(alice, lid, '记录.txt', b'abcdef')
    file = f'/api/libraries/{lid}/content'
    params = {'path': '记录.txt'}
    assert alice.get(file, params={**params, 'inline': True}).content == b'abcdef'
    assert alice.get(file, params=params, headers={'Range': 'bytes=900-999'}).status_code == 416
    assert alice.get(file, params={'path': '不存在.txt'}).status_code == 404
    assert not admin.get(USAGE + '/operations', params={'action':'download'}).json()['items']
    assert alice.get(file, params=params).content == b'abcdef'
    path = '/dav/' + library['mount_name'] + '/记录.txt'
    dav = TestClient(app)
    for section in ('bytes=0-1', 'bytes=2-3'):
        response = dav.get(path, auth=env['auths']['alice'], headers={'Range': section})
        assert response.status_code == 206
    assert len(admin.get(USAGE + '/operations', params={'action':'download'}).json()['items']) == 1
    # A second file accessed only through the desktop also records a download.
    upload(alice, lid, '电脑盘.txt', b'desktop')
    path = '/dav/' + library['mount_name'] + '/电脑盘.txt'
    for section in ('bytes=0-1', 'bytes=2-3'):
        assert dav.get(path, auth=env['auths']['alice'], headers={'Range': section}).status_code == 206
    link = alice.post(f'/api/libraries/{lid}/links', json={'path': '记录.txt', 'password': 'fixture-only-password'}).json()
    records = admin.get(USAGE + '/operations').json()['items']
    assert [row['action'] for row in records] == ['share', 'download', 'upload', 'download', 'upload']
    assert all(row['username'] == 'alice' and row['library_name'] == '操作测试' for row in records)
    assert link['token'] not in str(records) and 'fixture-only-password' not in str(records)
