from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event
import time
from zipfile import ZipFile

import pytest

from drive import file_search
from drive.store import DriveError
from drive.workspace import WorkspaceStore
from test_drive import env, upload


@pytest.mark.parametrize('password', ['', 'fixture-password'])
def test_upload_rollback_preserves_existing_file_and_allows_retry(tmp_path, monkeypatch, password):
    store = WorkspaceStore(tmp_path)
    owner = store.create_user('owner', 'Owner', 'test-password')
    lid = store.create_library(owner, 'Retry test', password=password)['id']
    rel = 'report.txt'
    uid = store.init_upload(owner, lid, rel, 3, 'first')['id']
    store.append_upload(owner, uid, 0, b'old')
    old = store.finish_upload(owner, uid)

    uid = store.init_upload(owner, lid, rel, 3, 'overwrite', overwrite=True)['id']
    store.append_upload(owner, uid, 0, b'new')
    original_entry = store.entry
    def interrupted(*args, **kwargs):
        raise OSError('Injected failure after file replacement')

    with monkeypatch.context() as patch:
        patch.setattr(store, 'entry', interrupted)
        with pytest.raises(OSError, match='Injected failure'):
            store.finish_upload(owner, uid)
    with store.read_content(owner, lid, rel) as content:
        assert content.read() == b'old'
    assert store.entry(lid, store.path(lid, rel))['etag'] == old['etag']
    assert store.upload_path(lid, uid).read_bytes() == b'new'
    assert store.upload(owner, uid)['id'] == uid
    assert len(store.all("SELECT id FROM events WHERE group_id=? AND action='upload'", (lid,))) == 1

    assert store.finish_upload(owner, uid)['size'] == 3
    with store.read_content(owner, lid, rel) as content:
        assert content.read() == b'new'
    assert store.one('SELECT id FROM uploads WHERE id=?', (uid,)) is None
    assert store.one('SELECT owner_id FROM entry_owners WHERE library_id=? AND path=?', (lid, rel))['owner_id'] == owner['id']


def test_upload_digest_does_not_block_other_writes_or_bypass_revocation(tmp_path, monkeypatch):
    from io import BytesIO
    from drive import workspace

    store = WorkspaceStore(tmp_path)
    owner = store.create_user('owner', 'Owner', 'test-password')
    editor = store.create_user('editor', 'Editor', 'test-password')
    first = store.create_library(owner, 'First')['id']
    second = store.create_library(owner, 'Second')['id']
    store.share(owner, first, '', 'user', editor['id'], 'editor')
    share_id = store.shares_for(owner)[0]['id']
    _, fingerprint = workspace.upload_digests(BytesIO(b'pending'))
    task = store.init_upload(editor, first, 'pending.txt', 7, fingerprint)['id']
    store.append_upload(editor, task, 0, b'pending')
    other = store.init_upload(owner, second, 'other.txt', 5, 'other-file')['id']
    digest_started, release_digest = Event(), Event()
    digest = workspace.upload_digests

    def blocking_digest(stream):
        if stream.name.endswith(task):
            digest_started.set()
            assert release_digest.wait(8)
        return digest(stream)

    monkeypatch.setattr(workspace, 'upload_digests', blocking_digest)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(store.finish_upload, editor, task)
        try:
            assert digest_started.wait(5)
            def unrelated_write():
                store.append_upload(owner, other, 0, b'other')
                return store.finish_upload(owner, other)

            assert pool.submit(unrelated_write).result(timeout=3)['name'] == 'other.txt'
            store.unshare(owner, share_id)
        finally:
            release_digest.set()
        with pytest.raises(DriveError) as denied:
            slow.result(timeout=5)
        assert denied.value.status == 403
    assert not (store.files / first / 'pending.txt').exists()
    assert store.upload_path(first, task).read_bytes() == b'pending'


def test_search_cursor_continues_past_scan_limit_and_rechecks_grants(env, monkeypatch):
    store, owner = env['store'], env['owner']
    reader = env['clients']['bob']
    lid = store.create_library(env['users']['alice'], '分页测试')['id']
    root = store.files / lid
    for rel in ('allowed', 'allowed/nested', 'hidden'):
        (root / rel).mkdir()
    for rel in ('allowed/a.txt', 'allowed/b.txt', 'allowed/nested/target.txt', 'hidden/target.txt'):
        (root / rel).write_text('test', encoding='utf8')
    store.share(env['users']['alice'], lid, 'allowed', 'user', env['users']['bob']['id'], 'reader')
    store.share(env['users']['alice'], lid, 'allowed/nested', 'user', env['users']['bob']['id'], 'reader')
    monkeypatch.setattr(file_search, 'SCAN_LIMIT', 2)
    cursor = ''
    found = []
    for _ in range(15):
        page = reader.get('/api/search', params={'q': 'TARGET', 'cursor': cursor}).json()
        found.extend(item['path'] for item in page['items'])
        if not page['next_cursor']:
            break
        cursor = page['next_cursor']
    else:
        pytest.fail('Search cursor did not terminate')
    assert found == ['allowed/nested/target.txt']
    assert reader.get('/api/search', params={'q': 'different', 'cursor': cursor}).status_code == 400

    for path in ('allowed/a.txt', 'allowed/nested/target.txt', 'hidden/target.txt'):
        store.set_tags(env['users']['alice'], lid, path, ['测试'])
    first = reader.get('/api/search', params={'tag': '测试', 'limit': 1}).json()
    assert first['items'][0]['path'] == 'allowed/a.txt'
    assert first['items'][0]['tags'] == ['测试']
    assert first['next_cursor']
    for share in owner.get('/api/shares', params={'library_id': lid}).json():
        assert owner.delete('/api/shares/' + share['id']).status_code == 200
    assert reader.get('/api/search', params={'tag': '测试', 'cursor': first['next_cursor']}).json()['items'] == []


def test_archive_job_scope_zip_and_permission_recheck(env):
    alice, bob = (env['clients'][name] for name in ('alice', 'bob'))
    store = env['store']
    lid = store.create_library(env['users']['alice'], '打包测试')['id']
    store.mkdir(env['users']['alice'], lid, '交付')
    upload(alice, lid, '交付/样本.txt', b'contents')
    store.share(env['users']['alice'], lid, '交付', 'user', env['users']['bob']['id'], 'reader')
    result = bob.post(f'/api/libraries/{lid}/archive-jobs', json={'paths': ['交付']})
    assert result.status_code == 202
    job_url = '/api/archive-jobs/' + result.json()['id']
    try:
        assert alice.get(job_url).status_code == 404
        for _ in range(100):
            status = bob.get(job_url).json()
            if status['status'] in ('ready', 'failed'):
                break
            time.sleep(0.03)
        assert status['status'] == 'ready', status
        with ZipFile(BytesIO(bob.get(job_url + '/download').content)) as archive:
            assert archive.namelist() == ['交付/样本.txt']
            assert archive.read('交付/样本.txt') == b'contents'
        store.unshare(env['users']['alice'], store.shares_for(env['users']['alice'])[0]['id'])
        assert bob.get(job_url + '/download').status_code == 403
        assert bob.delete(job_url).status_code == 200
        assert bob.get(job_url).status_code == 404
    finally:
        env['app'].state.archives.close()


def test_activity_trash_and_favorites_only_return_authorized_paths(env):
    store = env['store']
    owner, editor = (env['users'][name] for name in ('alice', 'bob'))
    lid = store.create_library(owner, '活动权限')['id']
    store.mkdir(owner, lid, '可见')
    store.mkdir(owner, lid, '不可见')
    store.share(owner, lid, '可见', 'user', editor['id'], 'editor')
    visible = store.files / lid / '可见/收藏.txt'
    visible.write_text('data', encoding='utf8')
    store.favorite(editor, lid, '可见/收藏.txt', True)
    for path in ('可见/删除.txt', '不可见/删除.txt'):
        upload(env['clients']['alice'], lid, path, b'data')
        store.delete(owner, lid, path)
    assert [row['path'] for row in store.list_trash(editor, lid)] == ['可见/删除.txt']
    assert [row['path'] for row in store.favorites_for(editor)] == ['可见/收藏.txt']
    assert all(row['path'].startswith('可见') for row in store.events_for(editor, lid))
    store.unshare(owner, store.shares_for(owner)[0]['id'])
    assert store.list_trash(editor, lid) == store.events_for(editor, lid) == store.favorites_for(editor) == []
