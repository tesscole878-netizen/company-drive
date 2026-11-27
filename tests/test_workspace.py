from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from threading import Thread
import time
from urllib.parse import urlsplit
from zipfile import ZipFile

from fastapi.testclient import TestClient

from drive.app import create_app
from drive.office import sign
from drive.store import Store
from drive.workspace import WorkspaceStore
from test_drive import PASSWORD, env, upload


def create_library(client, **options):
    response = client.post("/api/libraries", json={"name": "研发资料", **options})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_library_summary_refreshes_and_respects_folder_permissions(env):
    alice, bob = (env['clients'][name] for name in ('alice', 'bob'))
    lid = create_library(alice)
    library = f'/api/libraries/{lid}'
    assert alice.post(f'/api/groups/{lid}/folders', json={'path': '交付'}).status_code == 201
    upload(alice, lid, '内部.txt', b'private data')
    upload(alice, lid, '交付/明细.txt', b'1234')
    assert alice.post(library + '/shares', json={'path': '交付', 'subject_type': 'user',
                                               'subject_id': env['users']['bob']['id']}).status_code == 200
    endpoint = '/api/library-summaries'
    query = [('library_id', lid), ('path', ''), ('library_id', lid), ('path', '交付')]
    scoped = bob.get(endpoint, params=query).json()
    assert len(scoped) == 1 and scoped[0]['size'] == 4 and scoped[0]['path'] == '交付'
    assert scoped[0]['modified'] > 0 and scoped[0]['partial'] is False
    assert alice.get(endpoint, params=query).json()[0]['size'] == 16
    upload(alice, lid, '交付/补充.txt', b'123')
    assert bob.get(endpoint, params=query).json()[0]['size'] == 7
    share = alice.get('/api/shares', params={'library_id': lid}).json()[0]
    assert alice.delete('/api/shares/' + share['id']).status_code == 200
    assert bob.get(endpoint, params=query).json() == []


def client_login(app, username):
    client = TestClient(app)
    result = client.post("/api/login", json={"username": username, "password": PASSWORD})
    result.raise_for_status()
    client.headers["X-Drive-CSRF"] = result.json()["csrf"]
    return client


def test_group_listing_batches_queries_and_preserves_management_permissions(tmp_path):
    store = WorkspaceStore(tmp_path)
    admin = store.create_user('admin', 'Admin', PASSWORD, True)
    owner = store.create_user('owner', 'Owner', PASSWORD)
    manager = store.create_user('manager', 'Manager', PASSWORD)
    outsider = store.create_user('outsider', 'Outsider', PASSWORD)
    native = store.create_group('Native', '', owner)
    original = store.create_group('Original', '', owner)
    hidden = store.create_group('Other team', '', outsider)
    with store.db() as db:
        db.execute('INSERT INTO seafile_groups VALUES(?,?)', (original['id'], 9))
        db.execute('INSERT INTO group_managers VALUES(?,?,?)', (native['id'], manager['id'], 'manager'))
        db.executemany('INSERT INTO members VALUES(?,?,?)',
                       [(native['id'], manager['id'], 'reader'), (original['id'], manager['id'], 'reader')])
    database = store.db
    connections = []

    def counted_database():
        connections.append(1)
        return database()

    store.db = counted_database
    administrative = {group['id']: group for group in store.groups_for(admin)}
    assert len(connections) == 2
    assert set(administrative) == {native['id'], original['id'], hidden['id']}
    assert administrative[native['id']]['management_role'] == 'owner'
    assert administrative[original['id']]['management_role'] == 'member'
    assert administrative[original['id']]['identity_source'] == 'seafile'
    assert administrative[original['id']]['role'] == 'editor'
    assert administrative[original['id']]['members'] == 2
    managed = {group['id']: group for group in store.groups_for(manager)}
    assert set(managed) == {native['id'], original['id']}
    assert managed[native['id']]['management_role'] == 'manager'
    assert managed[native['id']]['role'] == 'reader'
    assert managed[original['id']]['management_role'] == 'member'
    assert 'original_group' not in managed[original['id']]
    assert 'identity_source' not in managed[native['id']]


def test_legacy_database_and_directory_are_reused(tmp_path):
    old = Store(tmp_path)
    old.create_user("admin", "Admin", PASSWORD, True)
    reader = old.create_user("reader", "Reader", PASSWORD)
    group = old.create_group("项目", "")
    with old.db() as db:
        db.execute("INSERT INTO members VALUES(?,?,?)", (group["id"], reader["id"], "reader"))
    original = old.files / group["id"] / "原资料.txt"
    original.write_text("original", encoding="utf-8")
    create_app(tmp_path)
    app = create_app(tmp_path)
    client = client_login(app, "reader")
    libraries = client.get("/api/libraries?view=shared").json()
    assert [lib["id"] for lib in libraries] == [group["id"]]
    assert client.get(f"/api/libraries/{group['id']}/content", params={"path":"原资料.txt"}).text == "original"
    assert original.read_text(encoding="utf-8") == "original"


def test_library_scoped_share_applies_to_web_dav_search_and_links(env):
    alice, bob = (env["clients"][name] for name in ("alice", "bob"))
    lid = create_library(alice)
    assert alice.post(f"/api/groups/{lid}/folders", json={"path":"交付"}).status_code == 201
    upload(alice, lid, "内部.txt", b"private")
    upload(alice, lid, "交付/公开.txt", b"shared")
    share = alice.post(f"/api/libraries/{lid}/shares", json={
        "path":"交付", "subject_type":"user", "subject_id":env["users"]["bob"]["id"], "role":"reader"
    })
    assert share.status_code == 200, share.text
    assert bob.get(f"/api/libraries/{lid}/files").status_code == 403
    assert bob.get(f"/api/libraries/{lid}/content", params={"path":"内部.txt"}).status_code == 403
    assert bob.get(f"/api/libraries/{lid}/content", params={"path":"交付/公开.txt"}).content == b"shared"
    assert bob.get("/api/search?q=内部").json()["items"] == []
    assert bob.post(f"/api/libraries/{lid}/links", json={"path":"交付"}).status_code == 403
    auth = env["auths"]["bob"]
    mount = next(root["name"] for root in env["store"].dav_roots(env["users"]["bob"]) if root["id"] == lid)
    assert bob.get(f"/dav/{mount}/公开.txt", auth=auth).content == b"shared"
    assert bob.put(f"/dav/{mount}/公开.txt", content=b"forbidden", auth=auth).status_code == 403
    assert alice.delete(f"/api/groups/{lid}/files", params={"path":"交付"}).status_code == 200
    assert alice.post(f"/api/groups/{lid}/folders", json={"path":"交付"}).status_code == 201
    upload(alice, lid, "交付/公开.txt", b"unshared replacement")
    assert bob.get(f"/api/libraries/{lid}/content", params={"path":"交付/公开.txt"}).status_code == 403


def test_group_and_public_library_grants(env):
    alice, bob = (env["clients"][name] for name in ("alice", "bob"))
    lid = create_library(alice)
    assert alice.post(f"/api/libraries/{lid}/shares", json={
        "subject_type":"group", "subject_id":env["group"]["id"], "role":"editor"
    }).status_code == 200
    upload(bob, lid, "group.txt", b"group write")
    public = create_library(env["owner"], name="公共", visibility="public")
    upload(env["owner"], public, "notice.txt", b"public read")
    outside = env["clients"]["outside"]
    assert outside.get(f"/api/libraries/{public}/content", params={"path":"notice.txt"}).content == b"public read"
    assert outside.post(f"/api/groups/{public}/folders", json={"path":"no"}).status_code == 403


def test_favorites_labels_and_activity_follow_authorized_files(env):
    alice, bob = (env["clients"][name] for name in ("alice", "bob"))
    lid = create_library(alice)
    root = f"/api/libraries/{lid}"
    upload(alice, lid, "项目进度.txt", b"draft")
    assert alice.put(root + "/tags", json={"path":"项目进度.txt","tags":["待审核"]}).status_code == 200
    assert alice.put("/api/favorites", json={"library_id":lid,"path":"项目进度.txt"}).status_code == 200
    assert alice.post(root + "/comments", json={"path":"项目进度.txt","body":"请复核"}).status_code == 201
    assert alice.get("/api/search", params={"tag":"待审核"}).json()["items"][0]["path"] == "项目进度.txt"
    assert alice.get(root + "/properties").json()["size"] == 5
    assert alice.patch(f"/api/groups/{lid}/files", json={"path":"项目进度.txt","name":"项目更新.txt"}).status_code == 200
    assert alice.get("/api/favorites").json()[0]["path"] == "项目更新.txt"
    assert alice.get(root + "/comments", params={"path":"项目更新.txt"}).json()[0]["body"] == "请复核"
    assert bob.get(root + "/comments", params={"path":"项目更新.txt"}).status_code == 403
    assert alice.get("/api/events", params={"library_id":lid}).json()[0]["action"] == "rename"


def test_encrypted_library_storage_range_and_lock(env):
    client = env["clients"]["alice"]
    password = "Encrypted-library-42"
    lid = create_library(client, password=password)
    data = b"original content\0" * 70000
    upload(client, lid, "内容.bin", data)
    physical = env["store"].files / lid / "内容.bin"
    assert physical.read_bytes().startswith(b"CDENC001")
    assert b"original content" not in physical.read_bytes()
    url = f"/api/libraries/{lid}/content?path=内容.bin"
    segment = client.get(url, headers={"Range":"bytes=1048570-1048580"})
    assert segment.content == data[1048570:1048581]
    etag = segment.headers['ETag']
    assert client.get(url, headers={"Range":"bytes=0-3","If-Range":etag}).status_code == 206
    stale_range = client.get(url, headers={"Range":"bytes=0-3","If-Range":'"earlier-version"'})
    assert stale_range.status_code == 200 and stale_range.content == data
    assert client.post(f"/api/libraries/{lid}/lock").status_code == 200
    assert client.get(url).status_code == 423
    assert client.post(f"/api/libraries/{lid}/unlock", json={"password":"wrong"}).status_code == 403
    assert client.post(f"/api/libraries/{lid}/unlock", json={"password":password}).status_code == 200
    assert client.get(url).content == data
    assert client.post(f"/api/libraries/{lid}/links", json={}).status_code == 400


def test_versions_snapshot_and_zip_follow_document_paths(env):
    client = env["clients"]["alice"]
    lid = create_library(client)
    endpoint = f"/api/libraries/{lid}"
    original = upload(client, lid, "说明.md", b"one")
    snapshot = client.post(endpoint + "/snapshots", json={"label":"保存点"}).json()["id"]
    assert client.put(endpoint + "/text", json={"path":"说明.md","etag":original["etag"],"content":"two"}).status_code == 200
    assert client.put(endpoint + "/text", json={"path":"说明.md","etag":original["etag"],"content":"stale"}).status_code == 409
    assert client.patch(f"/api/groups/{lid}/files", json={"path":"说明.md","name":"改名.md"}).status_code == 200
    assert client.post(f"/api/snapshots/{snapshot}/restore").status_code == 200
    assert client.get(endpoint + "/content", params={"path":"说明.md"}).content == b"one"
    assert client.get(endpoint + "/content", params={"path":"改名.md"}).status_code == 404
    archived = client.get(endpoint + "/archive", params={"path":"说明.md"})
    with ZipFile(BytesIO(archived.content)) as archive:
        assert archive.read("说明.md") == b"one"
    deleted = client.delete(endpoint)
    assert deleted.status_code == 200
    assert client.get(endpoint + "/files").status_code == 404
    assert client.get("/api/deleted-libraries").status_code == 403
    assert client.post(endpoint + "/restore").status_code == 403
    admin = env["owner"]
    assert lid in {row["id"] for row in admin.get("/api/deleted-libraries").json()}
    assert admin.post(endpoint + "/restore").status_code == 200
    assert client.get(endpoint + "/content", params={"path":"说明.md"}).content == b"one"


def test_public_links_password_expiry_revocation_and_upload(env):
    client = env["clients"]["alice"]
    lid = create_library(client)
    upload(client, lid, "可分享.txt", b"link data")
    password = "Share-pass-42"
    link = client.post(f"/api/libraries/{lid}/links", json={
        "path":"可分享.txt", "password":password, "expires":time.time()+3600
    }).json()
    visitor = TestClient(env["app"])
    path = "/public/" + link["token"]
    assert visitor.get(path).status_code == 401
    assert visitor.get(path + "/download").status_code == 401
    assert visitor.post(path + "/unlock", json={"password":"wrong"}).status_code == 403
    assert visitor.post(path + "/unlock", json={"password":password}).status_code == 200
    assert visitor.get(path + "/download").content == b"link data"
    assert visitor.get(path + "/download", params={"path":"../outside"}).status_code == 400
    assert visitor.get("/s/" + link["token"]).status_code == 200
    with env["store"].db() as db:
        db.execute("UPDATE share_links SET expires=? WHERE id=?", (time.time()-1,link["id"]))
    assert visitor.get(path + "/download").status_code == 404
    drop = client.post(f"/api/libraries/{lid}/links", json={"kind":"upload"}).json()
    drop_url = "/public/" + drop["token"]
    assert visitor.get(drop_url).json()["items"] == []
    assert visitor.get(drop_url + "/download", params={"path":"可分享.txt"}).status_code == 403
    task = visitor.post(drop_url + "/uploads", json={"name":"收件.txt","size":4,"fingerprint":"guest"}).json()["id"]
    cursor = client.get('/api/file-changes').json()['cursor']
    assert visitor.patch(drop_url + "/uploads/" + task, content=b"sent",headers={"Upload-Offset":"0"}).status_code == 200
    assert visitor.post(drop_url + "/uploads/" + task + "/complete").status_code == 200
    changes = client.get('/api/file-changes', params={'cursor':cursor}).json()['items']
    assert [(i['actor'], i['action']) for i in changes] == [('上传用户', 'create')]
    assert client.get(f"/api/libraries/{lid}/content",params={"path":"收件.txt"}).content == b"sent"
    assert client.delete("/api/links/" + drop["id"]).status_code == 200
    assert visitor.get(drop_url).status_code == 404


def test_new_office_files_are_valid_packages(env):
    client = env["clients"]["alice"]
    lid = create_library(client)
    endpoint = f"/api/libraries/{lid}"
    for kind, suffix, part in (("word","docx","word/document.xml"), ("excel","xlsx","xl/workbook.xml"), ("ppt","pptx","ppt/presentation.xml")):
        response = client.post(endpoint + "/documents", json={"path":"新文件","kind":kind})
        assert response.status_code == 201, response.text
        name = "新文件." + suffix
        response = client.get(endpoint + "/content", params={"path":name})
        with ZipFile(BytesIO(response.content)) as document:
            assert part in document.namelist()
            assert document.testzip() is None


@contextmanager
def document_server(content):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/saved", "/empty"):
                body = content if self.path == "/saved" else b""
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(503)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_office_callback_saves_version_and_preserves_on_failure(tmp_path, monkeypatch):
    from docx import Document

    secret = b"test-office-key-that-is-at-least-32-bytes"
    output = BytesIO()
    document = Document()
    document.add_paragraph("修改后的内容")
    document.save(output)
    with document_server(output.getvalue()) as address:
        monkeypatch.setenv("OFFICE_PUBLIC_URL", address)
        monkeypatch.setenv("OFFICE_DRIVE_URL", "http://testserver")
        monkeypatch.setenv("OFFICE_JWT_SECRET", secret.decode())
        app = create_app(tmp_path)
        app.state.store.create_user("editor", "Editor", PASSWORD, True)
        client = client_login(app,"editor")
        lid = create_library(client)
        response = client.post(f"/api/libraries/{lid}/documents", json={"kind":"word","path":"文档"})
        assert response.status_code == 201
        original = client.get(f"/api/libraries/{lid}/content", params={"path":"文档.docx"}).content
        config = client.post("/api/office/session", json={"library_id":lid,"path":"文档.docx"}).json()["config"]
        key = config["document"]["key"]
        source = config["document"]["url"]
        source_headers = {"Authorization": "Bearer " + sign({"operation":"download","payload":{"url":source}},secret)}
        assert client.get(urlsplit(source).path).status_code == 403
        assert client.get(urlsplit(source).path,headers=source_headers).content == original
        callback = urlsplit(config["editorConfig"]["callbackUrl"]).path

        def save(url):
            payload = {"status":2, "key":key, "url":url}
            jwt = sign({"operation":"callback","payload":payload},secret)
            return client.post(callback,json=payload,headers={"Authorization":"Bearer "+jwt})

        assert save("https://unconfigured.example/saved").status_code == 403
        assert save(address + "/failure").json() == {"error":1}
        assert save(address + "/empty").json() == {"error":1}
        assert app.state.store.all('SELECT account,device_name,storage,path,entry_point FROM blocked_office_writes') == [
            {'account':'editor','device_name':None,'storage':'native','path':'文档.docx','entry_point':'office_callback'},
        ]
        assert client.get(f"/api/libraries/{lid}/content",params={"path":"文档.docx"}).content == original
        from drive.store import DriveError
        class RevokedDirectory:
            def __init__(self, allow_calls):
                self.allow_calls = allow_calls

            def authorize(self, current):
                if self.allow_calls <= 0:
                    raise DriveError(403, "账号已撤权")
                self.allow_calls -= 1

        with monkeypatch.context() as changes:
            changes.setattr(app.state.store, 'identity_directory', RevokedDirectory(0))
            assert client.get(urlsplit(source).path, headers=source_headers).status_code == 403
            assert save(address + "/saved").status_code == 403
            changes.setattr(app.state.store, 'identity_directory', RevokedDirectory(1))
            assert save(address + "/saved").json() == {"error":1}
        assert client.get(f"/api/libraries/{lid}/content",params={"path":"文档.docx"}).content == original
        assert save(address + "/saved").json() == {"error":0}
        assert save(address + "/saved").json() == {"error":0}
        assert client.get(f"/api/libraries/{lid}/content",params={"path":"文档.docx"}).content == output.getvalue()
        versions = client.get(f"/api/libraries/{lid}/versions",params={"path":"文档.docx"}).json()
        assert len(versions) == 1
        assert client.get(f"/api/versions/{versions[0]['id']}/download").content == original
