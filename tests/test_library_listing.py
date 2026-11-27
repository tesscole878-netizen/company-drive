from contextlib import contextmanager

import pytest

from drive.store import DriveError
from drive.workspace import WorkspaceStore


def test_library_listing_batches_permissions_and_rechecks_revocation(tmp_path):
    store = WorkspaceStore(tmp_path)
    owner = store.create_user('owner', '所有者', 'test-password')
    reader = store.create_user('reader', '成员', 'test-password')
    admin = store.create_user('admin', '管理员', 'test-password', True)
    group = store.create_group('项目组', '', owner)
    with store.db() as db:
        db.execute('INSERT INTO members VALUES(?,?,?)', (group['id'], reader['id'], 'reader'))
    libraries = [store.create_library(owner, f'项目{i}') for i in range(24)]
    for lib in libraries:
        store.share(owner, lib['id'], '', 'group', group['id'], 'reader')
    scoped = store.create_library(owner, '仅共享文件夹')
    store.mkdir(owner, scoped['id'], '只读')
    store.mkdir(owner, scoped['id'], '可编辑')
    store.share(owner, scoped['id'], '只读', 'user', reader['id'], 'reader')
    store.share(owner, scoped['id'], '可编辑', 'user', reader['id'], 'editor')
    public = store.create_library(admin, '公共资料', visibility='public')
    encrypted = store.create_library(owner, '加密资料', password='library-password')
    store.share(owner, encrypted['id'], '', 'user', reader['id'], 'reader')
    hidden = store.create_library(owner, '未共享')
    deleted = store.create_library(owner, '已删除')
    store.share(owner, deleted['id'], '', 'user', reader['id'], 'reader')
    store.remove_library(owner, deleted['id'])
    original_db = store.db
    connections = []

    @contextmanager
    def counted_db():
        connections.append(1)
        with original_db() as db:
            yield db

    store.db = counted_db
    listing = store.libraries_for(reader)
    assert len(connections) == 2
    by_id = {}
    for lib in listing:
        by_id.setdefault(lib['id'], []).append(lib)
    assert hidden['id'] not in by_id
    assert deleted['id'] not in by_id
    assert by_id[public['id']][0]['role'] == 'reader'
    assert by_id[encrypted['id']][0]['locked'] is True
    roots = {lib['root_path']: lib for lib in by_id[scoped['id']]}
    assert set(roots) == {'只读', '可编辑'}
    assert roots['只读']['role'] == 'reader'
    assert roots['可编辑']['role'] == 'editor'
    assert roots['只读']['write_paths'] == ['可编辑']
    assert all(by_id[lib['id']][0]['role'] == 'reader' for lib in libraries)
    assert {lib['id'] for lib in store.libraries_for(reader, 'public')} == {public['id']}
    assert store.libraries_for(reader, 'mine') == []
    assert {lib['id'] for lib in store.libraries_for(reader, group_id=group['id'])} == {group['id'], *(lib['id'] for lib in libraries)}
    assert store.group_library_catalog(reader)['libraries'] == {
        group['id']: store.libraries_for(reader, group_id=group['id']),
    }
    assert all(lib['role'] == 'editor' for lib in store.libraries_for(admin))
    # The same user object must observe permission changes on the next call.
    with original_db() as db:
        db.execute('DELETE FROM members WHERE group_id=? AND user_id=?', (group['id'], reader['id']))
        db.execute('DELETE FROM library_shares WHERE library_id=?', (scoped['id'],))
    refreshed = {lib['id'] for lib in store.libraries_for(reader)}
    assert refreshed == {public['id'], encrypted['id']}
    assert store.group_library_catalog(reader) == {'groups': [], 'libraries': {}}
    with pytest.raises(DriveError) as denied:
        store.libraries_for(reader, group_id=group['id'])
    assert denied.value.status == 403
    with original_db() as db:
        db.execute('UPDATE users SET active=0 WHERE id=?', (reader['id'],))
    with pytest.raises(DriveError) as inactive:
        store.libraries_for(reader)
    assert inactive.value.status == 401


def test_share_listing_filters_owner_and_library(tmp_path):
    store = WorkspaceStore(tmp_path)
    owner = store.create_user('owner', '所有者', 'test-password')
    other = store.create_user('other', '其他成员', 'test-password')
    admin = store.create_user('admin', '管理员', 'test-password', True)
    first = store.create_library(owner, '甲')
    second = store.create_library(other, '乙')
    store.share(owner, first['id'], '', 'user', other['id'], 'reader')
    store.share(other, second['id'], '', 'user', owner['id'], 'reader')
    assert {row['library_id'] for row in store.shares_for(owner)} == {first['id']}
    assert store.shares_for(owner, second['id']) == []
    assert len(store.shares_for(admin)) == 2
    assert [row['library_id'] for row in store.shares_for(admin, second['id'])] == [second['id']]
