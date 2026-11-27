import sqlite3
from xml.etree import ElementTree

from test_drive import env, upload


def test_directory_page_search_sort_and_permission_recheck(env):
    alice, bob = (env['clients'][key] for key in ('alice', 'bob'))
    store, user = env['store'], env['users']['alice']
    lid = store.create_library(user, 'Paged')['id']
    store.mkdir(user, lid, 'folder')
    for i in range(121):
        (store.files / lid / f'item-{i}.txt').write_bytes(b'x' * i)
    with store.db() as db:
        db.executemany('INSERT INTO entry_owners VALUES(?,?,?)',
                       [(lid, f'item-{i}.txt', user['id']) for i in range(121)])
    url = f'/api/libraries/{lid}/files'
    assert isinstance(alice.get(url).json(), list)
    first = alice.get(url, params={'limit': 100}).json()
    second = alice.get(url, params={'limit': 100, 'offset': 100}).json()
    assert first['total'] == second['total'] == 122
    assert len(first['items']) == 100 and len(second['items']) == 22
    paths = [item['path'] for item in first['items'] + second['items']]
    assert paths == ['folder'] + [f'item-{i}.txt' for i in range(121)]
    assert second['items'][-1]['can_remove'] is True
    filtered = alice.get(url, params={'limit': 100, 'q': 'ITEM-12'}).json()
    assert {item['path'] for item in filtered['items']} == {'item-12.txt', 'item-120.txt'}
    reverse = alice.get(url, params={'limit': 3, 'order': 'size', 'descending': True}).json()
    assert [item['path'] for item in reverse['items']] == ['folder','item-120.txt','item-119.txt']
    share = alice.post(f'/api/libraries/{lid}/shares',
                      json={'subject_type':'user','subject_id':env['users']['bob']['id'],'role':'reader'}).json()
    assert bob.get(url, params={'limit':100, 'offset':100}).status_code == 200
    sid = store.shares_for(user, lid)[0]['id']
    assert alice.delete('/api/shares/' + sid).status_code == 200
    assert bob.get(url, params={'limit':100, 'offset':100}).status_code == 403


def test_dav_directory_batches_database_reads_and_rechecks_next_request(env, monkeypatch):
    alice, store = env['clients']['alice'], env['store']
    lid = env['group']['id']
    for i in range(100):
        (store.files / lid / f'{i:03}.txt').write_bytes(b'data')
    connect = sqlite3.connect
    opened = []
    def counted(*args, **kwargs):
        opened.append(1)
        return connect(*args, **kwargs)
    url = '/dav/' + env['group']['name'] + '/'
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, 'connect', counted)
        response = alice.request('PROPFIND', url, auth=env['auths']['alice'], headers={'Depth':'1'})
    assert response.status_code == 207, response.text
    tree = ElementTree.fromstring(response.content)
    assert len(tree.findall('{DAV:}response')) == 101
    assert len(opened) < 20
    for node in tree.findall('{DAV:}response'):
        length = node.find('.//{DAV:}getcontentlength')
        if length is not None:
            assert length.text == '4'
    with store.db() as db:
        db.execute('DELETE FROM members WHERE group_id=? AND user_id=?', (lid,env['users']['alice']['id']))
    assert alice.request('PROPFIND', url, auth=env['auths']['alice'], headers={'Depth':'1'}).status_code == 403
