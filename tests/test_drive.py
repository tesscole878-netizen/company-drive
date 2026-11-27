import hashlib
import os
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from starlette import responses

from drive.app import create_app

PASSWORD = "test-only-Strong-password-42"


def test_scripts_keep_javascript_type_with_windows_mime_override(tmp_path, monkeypatch):
    guess_type = responses.guess_type

    def guess_windows_type(path):
        if Path(path).suffix == ".js":
            return "text/plain", None
        return guess_type(path)

    monkeypatch.setattr(responses, "guess_type", guess_windows_type)
    client = TestClient(create_app(tmp_path / "data"))

    for name in ("app.js", "workspace-ui.js", "share.js", "office.js"):
        response = client.get(f"/assets/{name}")
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "text/javascript; charset=utf-8"
        assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path / "data")
    owner = TestClient(app)
    key = (tmp_path / "data/state/setup.key").read_text()
    r = owner.post("/api/setup", json={"key": key, "name": "管理员", "username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    owner.headers["X-Drive-CSRF"] = r.json()["csrf"]
    users = {}
    for name in ("alice", "bob", "outside"):
        r = owner.post("/api/users", json={"username": name, "name": name, "password": PASSWORD})
        assert r.status_code == 201, r.text
        users[name] = r.json()
    group = owner.post("/api/groups", json={"name": "项目资料", "description": "共享项目文件"}).json()
    other = owner.post("/api/groups", json={"name": "保密资料"}).json()
    for name, role in (("alice", "editor"), ("bob", "reader")):
        r = owner.put(f"/api/groups/{group['id']}/members/{users[name]['id']}", json={"role": role})
        assert r.status_code == 200, r.text
    clients = {}
    auths = {}
    for name in users:
        c = TestClient(app)
        r = c.post("/api/login", json={"username": name, "password": PASSWORD})
        c.headers["X-Drive-CSRF"] = r.json()["csrf"]
        password = c.post("/api/tokens", json={"name": "测试电脑"}).json()["password"]
        clients[name] = c
        auths[name] = (name, password)
    return dict(app=app, store=app.state.store, owner=owner, users=users, group=group, other=other, clients=clients, auths=auths)


def upload(c, gid, path, data=b"hello"):
    r = c.post("/api/uploads", json={"group_id": gid, "path": path, "size": len(data), "fingerprint": "test:"+path})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    r = c.patch(f"/api/uploads/{uid}", content=data, headers={"Content-Type": "application/octet-stream", "Upload-Offset": "0"})
    assert r.status_code == 200, r.text
    r = c.post(f"/api/uploads/{uid}/complete")
    assert r.status_code == 200, r.text
    return r.json()


def dav(group, tail=""):
    return "/dav/" + quote(group["name"]) + ("/" + quote(tail) if tail else "/")


def test_shared_files_and_isolation(env):
    gid = env["group"]["id"]
    alice, bob, outsider = (env["clients"][n] for n in ("alice", "bob", "outside"))
    body = b"\x00\x01hello\xff" * 10000
    result = upload(alice, gid, "方案.bin", body)
    assert result["sha256"] == hashlib.sha256(body).hexdigest()
    assert bob.get(f"/api/groups/{gid}/download", params={"path": "方案.bin"}).content == body
    for c in (outsider,):
        assert c.get(f"/api/groups/{gid}/files").status_code == 403
        assert c.get(f"/api/groups/{gid}/download", params={"path": "方案.bin"}).status_code == 403
    assert alice.get(f"/api/groups/{env['other']['id']}/files").status_code == 403
    assert bob.post(f"/api/groups/{gid}/folders", json={"path": "forbidden"}).status_code == 403
    assert bob.delete(f"/api/groups/{gid}/files", params={"path": "方案.bin"}).status_code == 403


def test_resume_upload_and_conflict(env):
    from io import BytesIO
    from drive.upload_integrity import upload_digests

    c, gid = env["clients"]["alice"], env["group"]["id"]
    _, fingerprint = upload_digests(BytesIO(b"0123456789"))
    payload = {"group_id": gid, "path": "续传.txt", "size": 10, "fingerprint": fingerprint}
    uid = c.post("/api/uploads", json=payload).json()["id"]
    assert c.patch(f"/api/uploads/{uid}", content=b"01234", headers={"Upload-Offset": "0"}).json()["offset"] == 5
    assert c.post(f"/api/uploads/{uid}/complete").status_code == 409
    assert not env["store"].path(gid, "续传.txt").exists()
    resumed = c.post("/api/uploads", json=payload).json()
    assert resumed == {"id": uid, "offset": 5}
    assert c.patch(f"/api/uploads/{uid}", content=b"01234", headers={"Upload-Offset": "0"}).status_code == 409
    assert c.patch(f"/api/uploads/{uid}", content=b"56789", headers={"Upload-Offset": "5"}).status_code == 200
    assert c.post(f"/api/uploads/{uid}/complete").status_code == 200
    assert c.get(f"/api/groups/{gid}/download", params={"path": "续传.txt"}).content == b"0123456789"
    assert c.post("/api/uploads", json=payload).status_code == 409

    wrong = c.post("/api/uploads", json={**payload, "path":"校验.txt"}).json()["id"]
    c.patch(f"/api/uploads/{wrong}", content=b"9123456789", headers={"Upload-Offset":"0"}).raise_for_status()
    assert c.post(f"/api/uploads/{wrong}/complete").status_code == 409
    assert not env["store"].path(gid, "校验.txt").exists()


def test_folders_rename_trash_restore(env):
    c, gid = env["clients"]["alice"], env["group"]["id"]
    assert c.post(f"/api/groups/{gid}/folders", json={"path": "文档"}).status_code == 201
    upload(c, gid, "文档/a.txt", b"recover me")
    assert c.patch(f"/api/groups/{gid}/files", json={"path": "文档/a.txt", "name": "b.txt"}).status_code == 200
    assert c.delete(f"/api/groups/{gid}/files", params={"path": "文档"}).status_code == 200
    assert c.get("/api/trash").status_code == 403
    assert c.get("/api/trash", params={"library_id": gid}).status_code == 403
    admin = env["owner"]
    row = admin.get("/api/trash", params={"library_id": gid}).json()[0]
    assert row["path"] == "文档"
    assert c.post(f"/api/trash/{row['id']}/restore").status_code == 403
    assert admin.post(f"/api/trash/{row['id']}/restore").status_code == 200
    assert c.get(f"/api/groups/{gid}/download", params={"path": "文档/b.txt"}).content == b"recover me"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../../b", "a\\b", "CON", "NUL.txt", "a:", "a ", "a/./b", "a//b"])
def test_path_validation(env, name):
    c, gid = env["clients"]["alice"], env["group"]["id"]
    assert c.post(f"/api/groups/{gid}/folders", json={"path": name}).status_code == 400


def test_csrf_and_admin(env):
    c, gid = env["clients"]["alice"], env["group"]["id"]
    assert c.post("/api/users", json={"username": "hacker", "name": "x", "password": PASSWORD}).status_code == 403
    assert c.get("/api/users").status_code == 403
    assert c.post(f"/api/groups/{gid}/folders", json={"path": "x"}, headers={"X-Drive-CSRF": "wrong"}).status_code == 403
    assert c.post(f"/api/groups/{gid}/folders", json={"path": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert TestClient(env["app"]).get("/api/groups").status_code == 401


def test_delegated_member_management(env):
    delegate = env["clients"]["alice"]
    ordinary = env["clients"]["bob"]
    owner = env["owner"]
    store = env["store"]
    delegate_id = env["users"]["alice"]["id"]
    assert not delegate.get("/api/me").json()["can_manage_members"]
    assert delegate.get("/api/users").status_code == 403
    with store.db() as db:
        db.execute("INSERT INTO member_managers VALUES(?)", (delegate_id,))

    me = delegate.get("/api/me").json()
    assert me["can_manage_members"] is True and not me["admin"]
    assert owner.get("/api/me").json()["can_manage_members"] is True
    assert delegate.get("/api/users").status_code == 200
    assert ordinary.get("/api/users").status_code == 403
    account = {"username": "new-member", "name": "新成员", "password": PASSWORD}
    assert delegate.post("/api/users", json=dict(account, admin=True)).status_code == 403
    created = delegate.post("/api/users", json=account)
    assert created.status_code == 201 and not created.json()["admin"]
    target = created.json()["id"]
    assert delegate.patch(f"/api/users/{target}", json={"active": False}).status_code == 403
    assert delegate.post(f"/api/users/{target}/password", json={"new_password": PASSWORD}).status_code == 403
    assert delegate.get("/api/license").status_code == 403
    assert delegate.get("/api/deleted-libraries").status_code == 403

    with store.db() as db:
        db.execute("DELETE FROM member_managers WHERE user_id=?", (delegate_id,))
    assert not delegate.get("/api/me").json()["can_manage_members"]
    assert delegate.get("/api/users").status_code == 403
    assert delegate.post("/api/users", json=account).status_code == 403


def test_dav_read_write_permissions(env):
    c, group = env["clients"]["alice"], env["group"]
    auth = env["auths"]["alice"]
    root = c.request("PROPFIND", "/dav/", headers={"Depth": "1"}, auth=auth)
    assert root.status_code == 207, root.text
    assert "项目资料" in root.text or quote("项目资料") in root.text
    assert "保密资料" not in root.text and quote("保密资料") not in root.text
    url = dav(group, "电脑.txt")
    r = c.put(url, content=b"from desktop", auth=auth)
    assert r.status_code == 201, r.text
    assert c.get(url, auth=auth).content == b"from desktop"
    assert c.get(f"/api/groups/{group['id']}/download", params={"path": "电脑.txt"}).content == b"from desktop"
    assert c.put(url, content=b"changed", auth=auth).status_code == 204
    assert c.get(url, headers={"Range": "bytes=1-3"}, auth=auth).content == b"han"
    assert c.get(url, auth=env["auths"]["bob"]).content == b"changed"
    assert c.put(url, content=b"no", auth=env["auths"]["bob"]).status_code == 403
    assert c.get(url, auth=env["auths"]["outside"]).status_code == 403
    assert c.get(url, auth=("alice", PASSWORD)).status_code == 401
    assert c.get(url).status_code == 401


def test_dav_mkdir_move_copy_delete(env):
    c, group, auth = env["clients"]["alice"], env["group"], env["auths"]["alice"]
    assert c.request("MKCOL", dav(group, "desktop"), auth=auth).status_code == 201
    assert c.put(dav(group, "desktop/a.txt"), content=b"hello", auth=auth).status_code == 201
    target = "http://testserver" + dav(group, "desktop/b.txt")
    r = c.request("MOVE", dav(group, "desktop/a.txt"), headers={"Destination": target}, auth=auth)
    assert r.status_code == 201, r.text
    r = c.request("COPY", dav(group, "desktop/b.txt"), headers={"Destination": "http://testserver"+dav(group, "copy.txt")}, auth=auth)
    assert r.status_code == 201, r.text
    r = c.request("COPY", dav(group, "copy.txt"), headers={"Destination": "http://testserver"+dav(env["other"], "leak.txt")}, auth=auth)
    assert r.status_code == 403, r.text
    assert c.delete(dav(group, "desktop"), auth=auth).status_code == 204
    assert env["owner"].get("/api/trash").json()[0]["path"] == "desktop"
    assert c.delete(dav(group), auth=auth).status_code == 403


def test_shared_edits_do_not_transfer_file_ownership_or_allow_removal(env):
    alice, bob = (env['clients'][name] for name in ('alice', 'bob'))
    lib = alice.post('/api/libraries', json={'name': '归属测试'}).json()
    gid = lib['id']
    base = f'/api/libraries/{gid}'
    entries = f'/api/groups/{gid}/files'
    auth = env['auths']['bob']
    assert alice.post(f'/api/groups/{gid}/folders', json={'path': '交付'}).status_code == 201
    upload(alice, gid, '交付/原件.docx', b'original')
    assert alice.post(base + '/shares', json={'path': '交付', 'subject_type':'user',
                     'subject_id':env['users']['bob']['id'], 'role':'editor'}).status_code == 200
    root = next(r['name'] for r in env['store'].dav_roots(env['users']['bob']) if r['id'] == gid)
    mounted = lambda name: '/dav/' + quote(root) + '/' + quote(name)
    item = mounted('原件.docx')

    assert bob.put(item, content=b'edited', auth=auth).status_code == 204
    assert alice.get(base + '/content', params={'path':'交付/原件.docx'}).content == b'edited'
    assert bob.delete(entries, params={'path':'交付/原件.docx'}).status_code == 403
    batch = bob.post(base + '/batch-delete', json={'paths':['交付/原件.docx']}).json()['results']
    assert batch[0]['ok'] is False
    assert bob.patch(entries, json={'path':'交付/原件.docx','name':'移走.docx'}).status_code == 403
    assert bob.delete(item, auth=auth).status_code == 403
    assert bob.request('MOVE', item, auth=auth, headers={'Destination':'http://testserver' + mounted('移走.docx')}).status_code == 403
    upload(bob, gid, '交付/自建.txt', b'my copy')
    assert bob.request('COPY', mounted('自建.txt'), auth=auth, headers={'Destination':'http://testserver' + item}).status_code == 403
    assert bob.request('MOVE', mounted('自建.txt'), auth=auth, headers={'Destination':'http://testserver' + item}).status_code == 403
    listed = {file['name']: file for file in bob.get(base + '/files', params={'path':'交付'}).json()}
    assert listed['原件.docx']['can_remove'] is False and listed['自建.txt']['can_remove'] is True
    assert alice.delete(entries, params={'path':'交付/自建.txt'}).status_code == 403
    assert alice.delete(entries, params={'path':'交付'}).status_code == 403
    assert alice.delete(base).status_code == 403
    assert bob.delete(mounted('自建.txt'), auth=auth).status_code == 204
    trash = env['owner'].get('/api/trash', params={'library_id':gid}).json()[0]
    assert env['owner'].post('/api/trash/' + trash['id'] + '/restore').status_code == 200
    assert bob.delete(entries, params={'path':'交付/自建.txt'}).status_code == 200
    assert alice.delete(entries, params={'path':'交付/原件.docx'}).status_code == 200


def test_shared_office_backup_move_keeps_original_and_supports_save(env):
    alice, bob = (env['clients'][name] for name in ('alice', 'bob'))
    lib = alice.post('/api/libraries', json={'name':'Office 测试'}).json()
    gid = lib['id']
    upload(alice, gid, '报告.docx', b'original')
    assert alice.post(f'/api/libraries/{gid}/shares', json={'subject_type':'user',
                      'subject_id':env['users']['bob']['id'], 'role':'editor'}).status_code == 200
    auth = env['auths']['bob']
    original, backup = dav(lib, '报告.docx'), dav(lib, '报告~A1B2C3.tmp')
    assert bob.request('MOVE', original, auth=auth, headers={'Destination':'http://testserver' + backup}).status_code == 201
    assert bob.get(original, auth=auth).content == b'original'
    assert bob.get(backup, auth=auth).content == b'original'
    assert bob.delete(original, auth=auth).status_code == 403
    assert bob.delete(backup, auth=auth).status_code == 204
    assert bob.put(original, auth=auth, content=b'office save').status_code == 204
    assert bob.get(original, auth=auth).content == b'office save'
    assert bob.delete(original, auth=auth).status_code == 403

    replacement = dav(lib, 'A1B2C3D4.tmp')
    assert bob.put(replacement, auth=auth, content=b'office replacement').status_code == 201
    dest = {'Destination': 'http://testserver' + original}
    assert bob.request('MOVE', replacement, auth=auth, headers=dest).status_code == 403
    assert bob.request('MOVE', original, auth=auth, headers={'Destination':'http://testserver' + backup}).status_code == 201
    assert bob.request('MOVE', replacement, auth=auth, headers=dest).status_code == 204
    assert bob.get(original, auth=auth).content == b'office replacement'
    assert bob.delete(original, auth=auth).status_code == 403


def test_existing_ownership_backfill_does_not_treat_edits_as_creation(env):
    from drive.entry_ownership import backfill_entry_owners

    store = env['store']
    owner, editor = (env['users'][name] for name in ('alice', 'bob'))
    lid = store.create_library(owner, '旧数据归属')['id']
    store.share(owner, lid, '', 'user', editor['id'], 'editor')
    for name in ('known.txt', 'changed.txt', 'untracked.txt'):
        (store.files / lid / name).write_bytes(b'existing')
    store.event(owner, lid, 'upload', 'known.txt')
    store.record_version(editor, lid, 'changed.txt')
    store.event(editor, lid, 'upload', 'changed.txt')
    with store.db() as db:
        db.execute("DELETE FROM settings WHERE key='entry_ownership_v1'")
    backfill_entry_owners(store)
    entries = {row['name']: row['can_remove'] for row in store.listing(owner, lid)}
    assert entries == {'known.txt': True, 'changed.txt': False, 'untracked.txt': False}
    assert not any(row['can_remove'] for row in store.listing(editor, lid))


def test_revocation_immediate(env):
    c, gid, group = env["clients"]["alice"], env["group"]["id"], env["group"]
    upload(c, gid, "a.txt")
    auth = env["auths"]["alice"]
    env["owner"].delete(f"/api/groups/{gid}/members/{env['users']['alice']['id']}")
    assert c.get(f"/api/groups/{gid}/files").status_code == 403
    assert c.get(dav(group,"a.txt"),auth=auth).status_code == 403
    token = c.get("/api/tokens").json()[0]
    assert c.delete("/api/tokens/"+token["id"]).status_code == 200
    assert c.request("PROPFIND", "/dav/", auth=auth).status_code == 401


def test_disable_account_and_password_change(env):
    alice = env["clients"]["alice"]
    env["owner"].patch("/api/users/"+env["users"]["alice"]["id"], json={"active": False})
    assert alice.get("/api/me").status_code == 401
    assert alice.request("PROPFIND", "/dav/", auth=env["auths"]["alice"]).status_code == 401
    bob = env["clients"]["bob"]
    assert bob.post("/api/password", json={"old_password": PASSWORD, "new_password": PASSWORD+"new"}).status_code == 200
    assert bob.get("/api/me").status_code == 401
    assert bob.post("/api/login", json={"username": "bob", "password": PASSWORD+"new"}).status_code == 200


def test_dav_lock_blocks_web_changes(env):
    c, gid, group, auth = env["clients"]["alice"], env["group"]["id"], env["group"], env["auths"]["alice"]
    upload(c, gid, "locked.txt")
    body = '<?xml version="1.0"?><D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope><D:locktype><D:write/></D:locktype><D:owner>test</D:owner></D:lockinfo>'
    r = c.request("LOCK", dav(group,"locked.txt"), content=body, headers={"Content-Type":"application/xml","Timeout":"Second-60"},auth=auth)
    assert r.status_code == 200, r.text
    assert c.delete(f"/api/groups/{gid}/files",params={"path":"locked.txt"}).status_code == 423
    assert c.put(dav(group,"locked.txt"),content=b"no",auth=env["auths"]["bob"]).status_code == 403
    token=r.headers["Lock-Token"]
    assert c.request("UNLOCK",dav(group,"locked.txt"),headers={"Lock-Token":token},auth=auth).status_code==204
    assert c.delete(f"/api/groups/{gid}/files",params={"path":"locked.txt"}).status_code==200


def test_storage_persists_across_restart(tmp_path):
    store = create_app(tmp_path/"data").state.store
    user=store.create_user("admin","管理员",PASSWORD,True)
    g=store.create_group("持久化","restart test")
    (store.files/g["id"]/"saved.txt").write_bytes(b"persistent")
    app2=create_app(tmp_path/"data")
    assert app2.state.store.authenticate("admin",PASSWORD)
    assert app2.state.store.listing(user,g["id"])[0]["name"]=="saved.txt"


def test_setup_once(env):
    assert env["owner"].post("/api/setup",json={"key":"x","username":"again","name":"Again","password":PASSWORD}).status_code==409
    assert not (env["store"].state/"setup.key").exists()


def test_upload_ownership_and_racing_target(env):
    c, gid = env["clients"]["alice"], env["group"]["id"]
    payload={"group_id":gid,"path":"race.txt","size":3,"fingerprint":"one"}
    first=c.post('/api/uploads',json=payload).json()['id']
    payload['fingerprint']='two'
    second=c.post('/api/uploads',json=payload).json()['id']
    assert env['clients']['bob'].get('/api/uploads/'+first).status_code==404
    for uid in (first,second):
        assert c.patch('/api/uploads/'+uid,content=b'abc',headers={'Upload-Offset':'0'}).status_code==200
    assert c.post('/api/uploads/'+first+'/complete').status_code==200
    assert c.post('/api/uploads/'+second+'/complete').status_code==409
    assert env['store'].path(gid,'race.txt').read_bytes()==b'abc'


def test_dav_conditional_write_and_truncated_upload(env):
    from drive.dav import DriveFile
    from wsgidav.dav_error import DAVError
    c,g,auth=env['clients']['alice'],env['group'],env['auths']['alice']
    url=dav(g,'safe.txt')
    assert c.put(url,content=b'original',auth=auth).status_code==201
    etag=c.head(url,auth=auth).headers['etag']
    assert c.put(url,content=b'new',headers={'If-Match':'"wrong"'},auth=auth).status_code==412
    assert c.get(url,auth=auth).content==b'original'
    assert c.put(url,content=b'new',headers={'If-Match':etag},auth=auth).status_code==204
    # Exercise an interrupted data stream: staging must not replace the file.
    from drive.dav import DriveProvider
    provider=DriveProvider(env['store'])
    environ={'wsgidav.provider':provider,'drive.user':env['store'].get_user(env['users']['alice']['id']),
             'CONTENT_LENGTH':'10','REQUEST_METHOD':'PUT'}
    resource=DriveFile('/'+g['name']+'/safe.txt',environ,str(env['store'].path(g['id'],'safe.txt')))
    resource.begin_write().write(b'partial')
    with pytest.raises(DAVError):
        resource.end_write(with_errors=False)
    assert env['store'].path(g['id'],'safe.txt').read_bytes()==b'new'


def test_empty_office_overwrites_keep_original_and_audit_request(env):
    client, group, auth = env['clients']['alice'], env['group'], env['auths']['alice']
    store = env['store']
    target = dav(group, '计划.XLS')
    temporary = dav(group, 'ABCDEF12.tmp')
    original = b'original workbook'
    assert client.put(target, content=original, auth=auth).status_code == 201
    etag = client.head(target, auth=auth).headers['etag']
    assert client.put(temporary, content=b'', auth=auth).status_code == 201
    assert client.put(target, content=b'', auth=auth).status_code == 422
    for method in ('MOVE','COPY'):
        assert client.request(method, temporary, headers={'Destination':str(client.base_url.join(target))}, auth=auth).status_code == 422
    init = {'group_id':group['id'],'path':'计划.XLS','size':0,'fingerprint':'empty-office','overwrite':True}
    assert client.post('/api/uploads', json=init).status_code == 422
    assert client.get(target, auth=auth).content == original
    assert client.head(target, auth=auth).headers['etag'] == etag
    assert client.get(temporary, auth=auth).status_code == 200
    assert not store.all('SELECT 1 FROM file_versions WHERE library_id=? AND path=?', (group['id'],'计划.XLS'))
    assert not store.all('SELECT 1 FROM uploads WHERE fingerprint=?', ('empty-office',))
    rows = store.all('SELECT account,device_name,storage,path,entry_point FROM blocked_office_writes ORDER BY id')
    assert [(row['account'],row['device_name'],row['storage'],row['path'],row['entry_point']) for row in rows] == [
        ('alice','测试电脑','native','计划.XLS','webdav_put'),
        ('alice','测试电脑','native','计划.XLS','webdav_move'),
        ('alice','测试电脑','native','计划.XLS','webdav_copy'),
        ('alice',None,'native','计划.XLS','web_upload'),
    ]
    text = dav(group, '空笔记.txt')
    assert client.put(text, content=b'notes', auth=auth).status_code == 201
    assert client.put(text, content=b'', auth=auth).status_code == 204
    assert client.get(text, auth=auth).content == b''
    assert client.put(target, content=b'next workbook', auth=auth).status_code == 204
    assert client.get(target, auth=auth).content == b'next workbook'


def test_backup_recovery_to_another_directory(env,tmp_path):
    import shutil
    import sqlite3
    c,gid=env['clients']['alice'],env['group']['id']
    content=b'restore bytes\x00\xff'
    upload(c,gid,'restore.txt',content)
    recovered=tmp_path/'recovered'
    shutil.copytree(env['store'].files,recovered/'files')
    shutil.copytree(env['store'].trash,recovered/'trash')
    (recovered/'state').mkdir(parents=True)
    with sqlite3.connect(env['store'].db_path) as src,sqlite3.connect(recovered/'state/drive.sqlite3') as dest:
        src.backup(dest)
    restored=create_app(recovered).state.store
    alice=restored.authenticate('alice',PASSWORD)
    assert alice
    assert restored.access(alice,gid)[1]=='editor'
    assert restored.path(gid,'restore.txt').read_bytes()==content


def test_client_download_requires_login_and_supports_range(env,tmp_path,monkeypatch):
    installer=tmp_path/'installer.exe'
    installer.write_bytes(b'MZ-test-client-content')
    monkeypatch.setenv('DRIVE_CLIENT_INSTALLER',str(installer))
    anon=TestClient(env['app'])
    assert anon.get('/api/client/windows').status_code==401
    c=env['clients']['bob']
    info=c.get('/api/client').json()
    assert info['available'] is True and info['size']==installer.stat().st_size
    assert c.get('/api/client/windows').content==installer.read_bytes()
    partial=c.get('/api/client/windows',headers={'Range':'bytes=0-1'})
    assert partial.status_code==206 and partial.content==b'MZ'
    monkeypatch.setenv('DRIVE_CLIENT_INSTALLER',str(tmp_path/'missing.exe'))
    assert c.get('/api/client').json()['available'] is False
    assert c.get('/api/client/windows').status_code==404


def test_admin_password_reset_revokes_sessions_and_device_access(env):
    alice = env['clients']['alice']
    bob = env['clients']['bob']
    uid = env['users']['alice']['id']
    path = f'/api/users/{uid}/password'
    replacement = 'new-password-for-test-only-42'
    assert bob.post(path, json={'new_password': replacement}).status_code == 403
    assert env['owner'].post(path, json={'new_password': 'short'}).status_code == 422
    response = env['owner'].post(path, json={'new_password': replacement})
    assert response.status_code == 200
    assert alice.get('/api/me').status_code == 401
    assert alice.request('PROPFIND', '/dav/', auth=env['auths']['alice']).status_code == 401
    assert alice.post('/api/login',json={'username':'alice','password':PASSWORD}).status_code == 401
    response = alice.post('/api/login',json={'username':'alice','password':replacement})
    assert response.status_code == 200
    assert response.json()['id'] == uid


def test_chinese_account_and_six_character_password_work_across_login_and_reset(env):
    username = '小王'
    created = env['owner'].post('/api/users', json={
        'username': username, 'name': '测试成员', 'password': '123456',
    })
    assert created.status_code == 201, created.text
    uid = created.json()['id']
    assert created.json()['username'] == username
    assert env['owner'].post('/api/users',json={
        'username':username, 'name':'重复账号', 'password':'123456',
    }).status_code == 409
    assert env['owner'].post('/api/users',json={
        'username':'密码过短', 'name':'测试成员', 'password':'12345',
    }).status_code == 400
    client = TestClient(env['app'])
    response = client.post('/api/login',json={'username':username,'password':'123456'})
    assert response.status_code == 200
    client.headers['X-Drive-CSRF'] = response.json()['csrf']
    device = client.post('/api/tokens',json={'name':'中文账号电脑'})
    assert device.status_code == 201
    assert client.request('PROPFIND','/dav/',auth=(username,device.json()['password'])).status_code == 207
    changed = client.post('/api/password',json={'old_password':'123456','new_password':'abcdef'})
    assert changed.status_code == 200
    assert client.post('/api/login',json={'username':username,'password':'abcdef'}).status_code == 200
    assert env['owner'].post(f'/api/users/{uid}/password',json={'new_password':'654321'}).status_code == 200
    assert client.post('/api/login',json={'username':username,'password':'654321'}).status_code == 200
