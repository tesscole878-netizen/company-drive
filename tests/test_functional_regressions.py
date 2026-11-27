from io import BytesIO
from zipfile import ZipFile
import time

from test_drive import env, upload
from test_workspace import create_library
from drive.workspace import WorkspaceStore


def test_upgrade_separates_legacy_trash_versions_by_deletion_time(env):
    client, store = env['owner'], env['store']
    lid = create_library(client)
    url = '/api/libraries/' + lid
    item = upload(client, lid, 'same.txt', b'old')
    body = {'path': 'same.txt', 'etag': item['etag'], 'content': 'before deletion'}
    assert client.put(url + '/text', json=body).status_code == 200
    old_version = client.get(url + '/versions', params={'path': 'same.txt'}).json()[0]['id']
    assert client.delete('/api/groups/' + lid + '/files', params={'path': 'same.txt'}).status_code == 200
    item = upload(client, lid, 'same.txt', b'new')
    body.update(etag=item['etag'], content='after deletion')
    assert client.put(url + '/text', json=body).status_code == 200
    new_version = client.get(url + '/versions', params={'path': 'same.txt'}).json()[0]['id']
    with store.db() as db:
        db.execute("DELETE FROM settings WHERE key='file_metadata_v1'")
        db.execute("DELETE FROM detached_versions")
        db.execute("DELETE FROM trash_metadata")
    upgraded = WorkspaceStore(store.data, store.state)
    rows = upgraded.versions_for(store.get_user(client.get('/api/me').json()['id']), lid, 'same.txt')
    assert [row['id'] for row in rows] == [new_version]
    assert upgraded.one('SELECT trash_id FROM detached_versions WHERE version_id=?', (old_version,))['trash_id']


def test_trash_recreation_and_snapshot_restore_keep_distinct_history(env):
    client = env['owner']
    lid = create_library(client)
    url = '/api/libraries/' + lid
    group = '/api/groups/' + lid + '/files'
    upload(client, lid, 'old.txt', b'original')
    assert client.put(url + '/tags', json={'path': 'old.txt', 'tags': ['original']}).status_code == 200
    assert client.post(url + '/comments', json={'path': 'old.txt', 'body': 'original comment'}).status_code == 201
    assert client.put('/api/favorites', json={'library_id': lid, 'path': 'old.txt'}).status_code == 200
    sid = client.post(url + '/snapshots', json={'label': 'original'}).json()['id']
    vid = client.get(url + '/versions', params={'path': 'old.txt'}).json()[0]['id']
    assert client.delete(group, params={'path': 'old.txt'}).status_code == 200
    tid = next(row['id'] for row in client.get('/api/trash', params={'library_id': lid}).json() if row['path'] == 'old.txt')
    upload(client, lid, 'old.txt', b'unrelated')
    assert client.get('/api/versions/' + vid + '/download').status_code == 404
    assert client.get(url + '/versions', params={'path': 'old.txt'}).json() == []
    assert client.get(url + '/comments', params={'path': 'old.txt'}).json() == []
    assert client.get(url + '/files').json()[0]['tags'] == []
    assert client.get('/api/favorites').json() == []
    assert client.patch(group, json={'path': 'old.txt', 'name': 'new.txt'}).status_code == 200
    assert client.post('/api/trash/' + tid + '/restore').status_code == 200
    assert client.get('/api/versions/' + vid + '/download').content == b'original'
    assert client.get(url + '/files').json()[1]['tags'] == ['original']
    assert client.patch(group, json={'path': 'old.txt', 'name': 'renamed.txt'}).status_code == 200
    assert client.post('/api/snapshots/' + sid + '/restore').status_code == 200
    versions = client.get(url + '/versions', params={'path': 'old.txt'}).json()
    assert [client.get('/api/versions/' + row['id'] + '/download').content for row in versions] == [b'original']
    assert client.get(url + '/comments', params={'path': 'old.txt'}).json()[0]['body'] == 'original comment'
    assert client.get(url + '/files').json()[0]['tags'] == ['original']
    assert client.get('/api/favorites').json()[0]['path'] == 'old.txt'


def test_batch_preflight_rollbacks_and_encrypted_cross_library_move(env, monkeypatch):
    client, store = env['owner'], env['store']
    lid = create_library(client, name='source', password='source-password')
    dest = create_library(client, name='target', password='target-password')
    url = '/api/libraries/' + lid
    target = '/api/libraries/' + dest
    doc = upload(client, lid, 'a.txt', b'first')
    assert client.put(url + '/text', json={'path': 'a.txt', 'etag': doc['etag'], 'content': 'latest'}).status_code == 200
    assert client.put(url + '/tags', json={'path': 'a.txt', 'tags': ['keep']}).status_code == 200
    upload(client, lid, 'b.txt', b'B')
    upload(client, dest, 'b.txt', b'conflict')
    move = {'paths': ['a.txt', 'b.txt'], 'destination_library': dest, 'move': True}
    assert client.post(url + '/transfer', json=move).status_code == 409
    assert client.get(url + '/content', params={'path': 'a.txt'}).content == b'latest'
    assert client.delete('/api/groups/' + dest + '/files', params={'path': 'b.txt'}).status_code == 200
    remap = store._remap_metadata

    def fail_second(db, lid, source, *args):
        if source == 'b.txt':
            raise OSError("simulated failure")
        remap(db, lid, source, *args)

    with monkeypatch.context() as patch:
        patch.setattr(store, '_remap_metadata', fail_second)
        assert client.post(url + '/transfer', json=move).status_code == 507
    assert len(client.get(url + '/files').json()) == 2
    assert client.get(target + '/files').json() == []
    assert client.post(url + '/transfer', json=move).status_code == 200
    assert client.get(target + '/files').json()[0]['tags'] == ['keep']
    assert client.get(target + '/content', params={'path': 'a.txt'}).content == b'latest'
    vid = client.get(target + '/versions', params={'path': 'a.txt'}).json()[0]['id']
    assert client.get('/api/versions/' + vid + '/download').content == b'first'


def test_repeat_archive_download_and_empty_directories(env):
    client = env['owner']
    lid = create_library(client)
    assert client.post('/api/groups/' + lid + '/folders', json={'path': 'empty'}).status_code == 201
    try:
        for _ in range(10):
            job = client.post('/api/libraries/' + lid + '/archive-jobs', json={'paths': ['empty']})
            assert job.status_code == 202, job.text
            url = '/api/archive-jobs/' + job.json()['id']
            for _ in range(100):
                state = client.get(url).json()
                if state['status'] != 'queued' and state['status'] not in ('packing', 'scanning'):
                    break
                time.sleep(.02)
            assert state['status'] == 'ready', state
            with ZipFile(BytesIO(client.get(url + '/download').content)) as archive:
                assert archive.namelist() == ['empty/']
    finally:
        env['app'].state.archives.close()


def test_text_editor_preserves_encoding_and_line_endings(env):
    client = env['owner']
    lid = create_library(client)
    url = '/api/libraries/' + lid
    for index, (codec, bom) in enumerate((('utf-8', b'\xef\xbb\xbf'), ('gb18030', b''), ('utf-16-be', b'\xfe\xff'))):
        path = f'{index}.txt'
        original = bom + '原文\r\n第二行\r\n'.encode(codec)
        entry = upload(client, lid, path, original)
        edit = {'path': path, 'etag': entry['etag'], 'content': '原文\n第二行\n'}
        assert client.put(url + '/text', json=edit).json()['etag'] == entry['etag']
        assert client.get(url + '/content', params={'path': path}).content == original
        edit['content'] = '已更改\n第二行\n'
        assert client.put(url + '/text', json=edit).status_code == 200
        assert client.get(url + '/content', params={'path': path}).content == bom + '已更改\r\n第二行\r\n'.encode(codec)
