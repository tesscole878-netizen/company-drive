from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ssl
from threading import Thread
from urllib.parse import parse_qs, quote, unquote, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
import pytest

from drive.app import create_app
from drive.legacy import legacy_upstream
from drive.legacy_portal import create_legacy_portal
from drive.store import password_hash


REPO_A = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
REPO_B = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
TOKEN_A = 'a' * 40
TOKEN_B = 'b' * 40
OLD_PASSWORD = 'synthetic-old-password'
LOCAL_PASSWORD = 'synthetic-local-password'
LIBRARY_PASSWORD = ' 资料库+%& 密码 '


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    grants = {TOKEN_A: [REPO_A], TOKEN_B: [REPO_B]}
    events = []
    result = {'link': 'http://unreachable.invalid/seafhttp/files/test-download-token/%E6%96%87%E4%BB%B6.txt'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, body, headers=None):
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(raw)))
            for key,value in (headers or {}).items():
                self.send_header(key,value)
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            events.append(('POST', self.path))
            raw = self.rfile.read(int(self.headers['Content-Length']))
            if self.path.startswith('/seafhttp/upload-api/'):
                message = BytesParser(policy=policy.default).parsebytes(
                    ('Content-Type: ' + self.headers['Content-Type'] + '\r\n\r\n').encode() + raw)
                file = next(part for part in message.iter_parts() if part.get_param('name',header='content-disposition') == 'file')
                result['content'] = file.get_payload(decode=True)
                result['object_id'] = hashlib.sha1(result['content']).hexdigest()
                result['removed'] = False
                self.respond(200,[{'name':file.get_filename(),'id':result['object_id']}])
                return
            if self.path.startswith('/seafhttp/update-api/'):
                assert self.headers.get('Authorization') is None
                message = BytesParser(policy=policy.default).parsebytes(
                    ('Content-Type: ' + self.headers['Content-Type'] + '\r\n\r\n').encode() + raw)
                parts = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
                         for part in message.iter_parts()}
                assert parts['target_file'].decode() == result.get('save_path', '/合同/文件.txt')
                result['content'] = parts['file']
                result['object_id'] = hashlib.sha1(parts['file']).hexdigest()
                self.respond(200, result['object_id'].encode())
                return
            data = parse_qs(raw.decode())
            if 'management' in result and self.management_write(data):
                return
            if self.path == f'/api/v2.1/repos/{REPO_A}/set-password/':
                token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                if REPO_A not in grants.get(token, []):
                    self.respond(403, {})
                elif data.get('password') != [LIBRARY_PASSWORD]:
                    self.respond(400, {'error_msg': 'Wrong password'})
                else:
                    result['upstream_unlocked'] = True
                    self.respond(200, {'success': True})
                return
            username = data.get('username', [''])[0]
            if self.path != '/api2/auth-token/' or data.get('password') != [OLD_PASSWORD]:
                self.respond(400, {})
            elif username == 'otp@old.test' and self.headers.get('X-Seafile-OTP') != '123456':
                self.respond(400, {}, {'X-Seafile-OTP':'required'})
            else:
                self.respond(200, {'token': TOKEN_A if username == 'a@old.test' else TOKEN_B})

        def do_PUT(self):
            events.append(('PUT', self.path))
            data = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
            if not self.management_write(data):
                self.respond(404, {})

        def do_DELETE(self):
            events.append(('DELETE', self.path))
            if urlsplit(self.path).path == f'/api/v2.1/repos/{REPO_A}/file/':
                if result.get('write_permission') != 'rw':
                    self.respond(403,{})
                else:
                    result['removed'] = True
                    self.respond(200,{'success':True})
                return
            if not self.management_write({}):
                self.respond(404, {})

        def management_write(self, data):
            management = result.get('management')
            if not management:
                return False
            url = urlsplit(self.path)
            base = f'/api2/repos/{REPO_A}/'
            args = parse_qs(url.query)
            if not url.path.startswith(base):
                return False
            assert self.headers['Authorization'] == 'Bearer ' + TOKEN_A
            if url.path == base + 'dir/shared_items/':
                path = args['p'][0]
                kind = (data if self.command == 'PUT' else args)['share_type'][0]
                key = 'username' if kind == 'user' else 'group_id'
                target = (data if self.command == 'PUT' else args)[key][0]
                if self.command == 'DELETE':
                    management['shares'].pop((path, kind, target), None)
                else:
                    management['shares'][path, kind, target] = data['permission'][0]
                self.respond(200, {'success':[target],'failed':[]} if self.command == 'PUT' else {'success':True})
            elif url.path == base + 'dir/':
                assert args['p'] == ['/合同'] and data['operation'] == ['rename']
                management['folder'] = data['newname'][0]
                self.respond(200, 'success')
            elif self.command == 'DELETE' and url.path == base:
                management['deleted'] = True
                self.respond(200, 'success')
            else:
                assert args['op'] == ['rename']
                management['name'] = data['repo_name'][0]
                self.respond(200, 'success')
            return True

        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            events.append(('GET', path))
            auth = self.headers.get('Authorization', '')
            if path.startswith('/seafhttp/files/'):
                content = result.get('content', b'legacy data')
                byte_range = self.headers.get('Range')
                if auth:
                    self.respond(403, {})
                elif byte_range:
                    start, end = map(int, byte_range.removeprefix('bytes=').split('-'))
                    self.respond(206, content[start:end+1], {'Content-Range': f'bytes {start}-{end}/{len(content)}'})
                else:
                    self.respond(200, content)
                return
            token = auth.removeprefix('Bearer ')
            if token not in grants:
                self.respond(403, {})
            elif path == '/api2/account/info/':
                self.respond(200, {'email': 'a@old.test' if token == TOKEN_A else 'b@old.test',
                                   'name': '甲成员' if token == TOKEN_A else '乙成员'})
            elif path == '/api/v2.1/groups/':
                ids = result.get('groups', {}).get(token, [12] if token == TOKEN_A else [24])
                self.respond(200, [{'id': gid, 'name': '甲组' if gid == 12 else '乙组'} for gid in ids])
            elif path == '/api2/devices/':
                self.respond(200,[{'device_name':'my device','platform':'windows','key':'secret-device-key','password':'secret-password'}])
            elif path == '/api/v2.1/notifications/':
                self.respond(200,{'unseen_count':1,'count':1,'notification_list':[
                    {'id':1,'type':'repo-share','detail':{'repo_name':'my repo','share_from_user_name':'Colleague','password':'secret'},'seen':False,'time':'2026-11-12T10:00:00Z'}]})
            elif path == '/api2/repos/':
                libraries = result.get('catalog', {}).get(token)
                if libraries is None:
                    libraries = [{'id': repo, 'name': '测试资料库', 'encrypted': False,
                                  'type': 'grepo', 'groupid': 12 if repo == REPO_A else 24} for repo in grants[token]]
                self.respond(200, libraries)
            elif path == '/api/v2.1/suyan-effective-permissions/' and 'effective_permissions' in result:
                ids = parse_qs(parsed.query).get('repo', [])
                permissions = result['effective_permissions'].get(token, {})
                self.respond(200, {rid: permissions[rid] for rid in ids if rid in permissions})
            elif any(path == f'/api2/repos/{repo}/dir/' for repo in grants[token]):
                directory = parse_qs(parsed.query)['p'][0]
                if result.get('preview_only'):
                    self.respond(403, {})
                elif directory == '/':
                    management = result.get('management')
                    self.respond(200, [{'name': management['folder'] if management else '合同', 'type': 'dir',
                                        'mtime': 1794477600, 'permission': management['folder_permission'] if management else 'rw'}])
                else:
                    assert directory == '/合同'
                    entry = {'name': result.get('save_name', '文件.txt'), 'type': 'file',
                                        'id': result.get('object_id', '1'*40),
                                        'size': len(result.get('content', b'legacy data')),
                                        'last_modified': '2026-11-12T10:00:00Z'}
                    if 'write_permission' in result:
                        entry['permission'] = result['write_permission']
                    self.respond(200, [] if result.get('removed') else [entry])
            elif path == f'/api2/repos/{REPO_A}/' and REPO_A in grants[token]:
                management = result.get('management')
                details = {'head_commit_id': '2'*40, 'encrypted': bool(result.get('encrypted'))}
                if management:
                    details.update(name=management['name'], owner='self' if token == TOKEN_A else 'share', permission='rw')
                self.respond(200, details)
            elif path == f'/api2/repos/{REPO_A}/dir/shared_items/' and REPO_A in grants[token] and 'management' in result:
                directory = parse_qs(parsed.query)['p'][0]
                shares = [{'share_type':kind, 'user_info':{'name':target,'nickname':'乙成员'},
                           'group_info':{'id':int(target) if kind == 'group' else None,'name':'甲组'}, 'permission':permission}
                          for (location,kind,target),permission in result['management']['shares'].items() if location == directory]
                self.respond(200, shares)
            elif path == f'/api2/repos/{REPO_A}/file/detail/' and REPO_A in grants[token]:
                self.respond(200, {'type': 'file', 'permission': result.get('write_permission', 'r'),
                                   'size': len(result.get('content', b'legacy data')),
                                   'id': result.get('object_id', '1'*40)})
            elif path == f'/api2/repos/{REPO_A}/upload-link/' and REPO_A in grants[token]:
                if result.get('write_permission') == 'rw':
                    self.respond(200, 'https://seafile.test/seafhttp/upload-api/abcdefghijklmnop')
                else:
                    self.respond(403, {})
            elif path == f'/api2/repos/{REPO_A}/update-link/' and REPO_A in grants[token]:
                if result.get('write_permission') == 'rw':
                    self.respond(200, 'https://unreachable.invalid/seafhttp/update-api/test-write-token')
                else:
                    self.respond(403, {})
            elif any(path == f'/api2/repos/{repo}/file/' for repo in grants[token]):
                if path == f'/api2/repos/{REPO_A}/file/' and result.get('upstream_unlocked') is False:
                    self.respond(440, {'error_msg': 'Library password is needed.'})
                    return
                assert parse_qs(parsed.query)['p'] == [result.get('save_path', '/合同/文件.txt')]
                self.respond(200, result['link'], {'oid': result.get('object_id', '1'*40)})
            else:
                self.respond(403, {})

    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Seafile Test')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
                   .not_valid_after(now + timedelta(days=1)).sign(private, hashes.SHA256()))
    pem = tmp_path / 'test-seafile.pem'
    pem.write_bytes(certificate.public_bytes(serialization.Encoding.PEM)
                    + private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(pem)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    monkeypatch.setenv('LEGACY_SEAFILE_URL', f'https://127.0.0.1:{server.server_port}')
    monkeypatch.setenv('LEGACY_SEAFILE_CERT_SHA256', hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest())
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield grants, events, result
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def signed_in(app, username, url='https://cloud.test'):
    client = TestClient(app, base_url=url)
    response = client.post('/api/login', json={'username': username, 'password': LOCAL_PASSWORD})
    assert response.status_code == 200
    client.headers['X-Drive-CSRF'] = response.json()['csrf']
    return client


def test_browser_seafile_permissions_and_text_writeback(tmp_path, upstream):
    _, _, source = upstream
    source['catalog'] = {TOKEN_A: [{'id': REPO_A, 'name': '原库', 'type': 'repo', 'permission': 'rw'}]}
    source['write_permission'] = 'rw'
    source['content'] = b'\xef\xbb\xbf' + '原文\r\n第二行\r\n'.encode()
    app = create_app(tmp_path / 'drive')
    app.state.store.create_user('alpha', 'Alpha', LOCAL_PASSWORD)
    alpha = signed_in(app, 'alpha')
    assert alpha.post('/api/legacy/binding', json={'username': 'a@old.test', 'password': OLD_PASSWORD}).status_code == 200
    base = '/api/legacy/libraries/' + REPO_A
    path = '/合同/文件.txt'
    assert alpha.get('/api/legacy/libraries?view=mine').json()[0]['permission'] == 'rw'
    assert alpha.get(base + '/files', params={'path': '/合同'}).json()[0]['permission'] == 'rw'
    doc = alpha.get(base + '/text', params={'path': path}).json()
    assert doc['content'] == '原文\r\n第二行\r\n'
    update = {'path': path, 'content': '修改后\n第二行\n', 'version': doc['version']}
    assert alpha.put(base + '/text', json=update).status_code == 200
    assert source['content'] == b'\xef\xbb\xbf' + '修改后\r\n第二行\r\n'.encode()
    assert alpha.put(base + '/text', json=update).status_code == 409
    update['version'] = source['object_id']
    source['write_permission'] = 'r'
    assert alpha.get(base + '/files', params={'path': '/合同'}).json()[0]['permission'] == 'r'
    assert alpha.get(base + '/text', params={'path': path}).status_code == 403
    assert alpha.put(base + '/text', json=update).status_code == 403
    assert alpha.get(base + '/download', params={'path': path}).content == source['content']


def test_browser_seafile_office_checks_permission_and_session_on_save(tmp_path, upstream, monkeypatch):
    from io import BytesIO
    from docx import Document
    from drive.office import sign
    from test_workspace import document_server

    def word(text):
        buffer = BytesIO()
        doc = Document()
        doc.add_paragraph(text)
        doc.save(buffer)
        return buffer.getvalue()

    _, _, source = upstream
    path = '/合同/文件.docx'
    original, saved = word('原文'), word('修改后')
    source.update(save_path=path, save_name='文件.docx', content=original, write_permission='rw')
    secret = b'office-test-only-secret-with-32-bytes'
    with document_server(saved) as address:
        monkeypatch.setenv('OFFICE_PUBLIC_URL', address)
        monkeypatch.setenv('OFFICE_DRIVE_URL', 'http://testserver')
        monkeypatch.setenv('OFFICE_JWT_SECRET', secret.decode())
        app = create_app(tmp_path / 'drive')
        app.state.store.create_user('alpha', 'Alpha', LOCAL_PASSWORD)
        alpha = signed_in(app, 'alpha')
        assert alpha.post('/api/legacy/binding', json={'username': 'a@old.test', 'password': OLD_PASSWORD}).status_code == 200
        fields = {'library_id': REPO_A, 'path': path, 'legacy': True}

        def begin():
            response = alpha.post('/api/office/session', json=fields)
            assert response.status_code == 200, response.text
            return response.json()['config']

        config = begin()
        assert config['document']['permissions']['edit'] is True
        url = config['document']['url']
        signed = 'Bearer ' + sign({'operation': 'download', 'payload': {'url': url}}, secret)
        assert alpha.get(urlsplit(url).path, headers={'Authorization': signed}).content == original

        def callback(cfg):
            data = {'status': 2, 'key': cfg['document']['key'], 'url': address + '/saved'}
            signed = 'Bearer ' + sign({'operation': 'callback', 'payload': data}, secret)
            return alpha.post(urlsplit(cfg['editorConfig']['callbackUrl']).path, json=data, headers={'Authorization': signed})

        source['write_permission'] = 'r'
        assert alpha.post('/api/office/session', json=fields).status_code == 403
        assert callback(config).json() == {'error': 1}
        assert source['content'] == original
        source['write_permission'] = 'rw'
        assert callback(config).json() == {'error': 0}
        assert source['content'] == saved
        config = begin()
        assert alpha.post('/api/logout').status_code == 200
        assert callback(config).status_code == 403


def test_original_encrypted_library_unlock_is_limited_to_current_session(tmp_path, upstream):
    grants, events, source = upstream
    source['catalog'] = {TOKEN_A: [{'id': REPO_A, 'name': '原加密资料库', 'type': 'repo', 'encrypted': True}]}
    source['upstream_unlocked'] = False
    app = create_app(tmp_path / 'drive')
    for username in ('alpha', 'beta'):
        app.state.store.create_user(username, username, LOCAL_PASSWORD)
    alpha, beta = (signed_in(app, name) for name in ('alpha', 'beta'))
    for client, username in ((alpha, 'a'), (beta, 'b')):
        assert client.post('/api/legacy/binding', json={'username': f'{username}@old.test', 'password': OLD_PASSWORD}).status_code == 200

    base = '/api/legacy/libraries/' + REPO_A
    unlock = base + '/unlock'
    path = {'path': '/合同/文件.txt'}
    assert alpha.get('/api/legacy/libraries?view=mine').json()[0]['encrypted'] is True
    assert alpha.get(base + '/files').json()['code'] == 'legacy_library_locked'
    assert alpha.get(base + '/download-access', params=path).status_code == 423
    assert alpha.get(base + '/download', params=path).status_code == 423
    assert alpha.get(base + '/preview', params=path).status_code == 423
    assert beta.post(unlock, json={'password': LIBRARY_PASSWORD}).status_code == 403
    assert ('POST', f'/api/v2.1/repos/{REPO_A}/set-password/') not in events
    assert alpha.post(unlock, json={'password': LIBRARY_PASSWORD}, headers={'X-Drive-CSRF': 'bad'}).status_code == 403
    assert alpha.post(unlock, json={'password': 'wrong-password'}).status_code == 422
    assert alpha.get(base + '/files').status_code == 423

    response = alpha.post(unlock, json={'password': LIBRARY_PASSWORD})
    assert response.status_code == 200 and response.json() == {'ok': True}
    assert response.headers['cache-control'] == 'no-store'
    assert alpha.get(base + '/files').json()[0]['name'] == '合同'
    assert alpha.get(base + '/files', params={'path': '/合同'}).json()[0]['name'] == '文件.txt'
    assert alpha.get(base + '/download-access', params=path).json() == {'ok': True}
    assert alpha.get(base + '/download', params=path).content == b'legacy data'
    assert alpha.get(base + '/preview', params=path).content == b'legacy data'
    assert signed_in(app, 'alpha').get(base + '/files').status_code == 423

    source['upstream_unlocked'] = False
    assert alpha.get(base + '/download-access', params=path).json()['code'] == 'legacy_library_locked'
    assert alpha.get(base + '/preview', params=path).json()['code'] == 'legacy_library_locked'
    assert alpha.post(unlock, json={'password': LIBRARY_PASSWORD}).status_code == 200
    source['catalog'][TOKEN_A] = []
    assert alpha.get(base + '/download', params=path).status_code == 403
    source['catalog'][TOKEN_A] = [{'id': REPO_A, 'name': '原加密资料库', 'type': 'repo', 'encrypted': True}]
    assert alpha.post('/api/logout').status_code == 200
    assert alpha.get(base + '/files').status_code == 401
    assert signed_in(app, 'alpha').get(base + '/files').status_code == 423


def test_encrypted_library_reader_unlock_isolated_by_device(tmp_path, upstream):
    from drive.legacy_device import DEVICE_LIBRARY_ROOT

    grants, _, source = upstream
    source['catalog'] = {TOKEN_A: [{'id': REPO_A, 'name': '原加密资料库', 'type': 'repo', 'encrypted': True}]}
    source['upstream_unlocked'] = False
    app = create_app(tmp_path / 'drive')
    for username in ('alpha', 'beta'):
        app.state.store.create_user(username, username, LOCAL_PASSWORD)
    alpha, beta = (signed_in(app, name) for name in ('alpha', 'beta'))
    for client, name in ((alpha, 'a'), (beta, 'b')):
        client.post('/api/legacy/binding', json={'username': f'{name}@old.test', 'password': OLD_PASSWORD}).raise_for_status()
    first = alpha.post('/api/tokens', json={'name': '一号助手'}).json()
    second = alpha.post('/api/tokens', json={'name': '二号助手'}).json()
    outsider = beta.post('/api/tokens', json={'name': '其他员工助手'}).json()
    dav = TestClient(app, base_url='https://testserver', headers={'X-Suyan-Encrypted': '1'})
    a, a2, b = [(user, token['password']) for user, token in [('alpha', first), ('alpha', second), ('beta', outsider)]]
    base = DEVICE_LIBRARY_ROOT + '/' + REPO_A
    file = base + '/download'
    path = {'path': '/合同/文件.txt'}
    web = '/api/legacy/libraries/' + REPO_A
    assert dav.get(DEVICE_LIBRARY_ROOT + '/libraries').status_code == 401
    assert dav.get(DEVICE_LIBRARY_ROOT + '/libraries', auth=a).json()[0]['unlocked'] is False
    assert dav.get(DEVICE_LIBRARY_ROOT + '/libraries', auth=b).json() == []
    assert dav.get(base + '/files', auth=a).status_code == 423
    assert dav.post(base + '/unlock', auth=a, json={'password': 'wrong'}).status_code == 422
    assert dav.post(base + '/unlock', auth=b, json={'password': LIBRARY_PASSWORD}).status_code == 403
    assert dav.post(base + '/unlock', auth=a, json={'password': LIBRARY_PASSWORD}).status_code == 200
    assert dav.get(base + '/files', params={'path': '/合同'}, auth=a).json()[0]['name'] == '文件.txt'
    assert dav.get(file, params=path, auth=a).content == b'legacy data'
    assert dav.get(file, params=path, auth=a2).status_code == 423
    assert alpha.get(web + '/files').status_code == 423
    assert dav.post(DEVICE_LIBRARY_ROOT + '/lock', auth=a).json() == {'ok': True}
    assert dav.get(file, params=path, auth=a).status_code == 423
    assert alpha.post(web + '/unlock', json={'password': LIBRARY_PASSWORD}).status_code == 200
    assert dav.get(file, params=path, auth=a).status_code == 423
    assert dav.post(base + '/unlock', auth=a, json={'password': LIBRARY_PASSWORD}).status_code == 200
    source['catalog'][TOKEN_A] = []
    grants[TOKEN_A] = []
    assert dav.get(file, params=path, auth=a).status_code == 403
    alpha.delete('/api/tokens/' + first['id']).raise_for_status()
    assert dav.get(file, params=path, auth=a).status_code == 401


def test_legacy_compatible_del_path_and_effective_permission(tmp_path, upstream):
    from drive.legacy_dav import named_entries

    _, _, source = upstream
    name = '预算\x7f.txt'
    original_path = '/合同/' + name
    source.update(catalog={TOKEN_A: [{'id': REPO_A, 'name': '资料库', 'type': 'srepo', 'permission': 'r'}]},
                  effective_permissions={TOKEN_A: {REPO_A: 'rw'}},
                  save_name=name, save_path=original_path, write_permission='rw',
                  link='https://old.test/seafhttp/files/test-download-token/' + quote(name, safe=''))
    app = create_app(tmp_path / 'drive')
    app.state.store.create_user('alpha', 'Alpha', LOCAL_PASSWORD)
    alpha = signed_in(app, 'alpha')
    alpha.post('/api/legacy/binding', json={'username': 'a@old.test', 'password': OLD_PASSWORD}).raise_for_status()
    libs = alpha.get('/api/legacy/libraries?view=shared&effective=1').json()
    assert libs[0]['permission'] == 'r' and libs[0]['effective_permission'] == 'rw'
    base = '/api/legacy/libraries/' + REPO_A
    assert alpha.get(base + '/files', params={'path': '/合同'}).json()[0]['name'] == name
    assert alpha.get(base + '/download', params={'path': original_path}).content == b'legacy data'
    saved = alpha.put(base + '/text', json={'path': original_path, 'content': '预算修改', 'version': '1'*40})
    assert saved.status_code == 200 and source['content'] == '预算修改'.encode()
    token = alpha.post('/api/tokens', json={'name': '测试助手'}).json()['password']
    mount = '/dav/原 Seafile/资料库/合同/预算_.txt'
    assert TestClient(app).get(mount, auth=('alpha', token)).content == source['content']
    aliases = named_entries([{'name': '预算_.txt'}, {'name': name}])
    assert [item['name'] for item in aliases.values()] == ['预算_.txt', name]
    assert len(aliases) == 2 and all('\x7f' not in alias for alias in aliases)


def test_legacy_permissions_and_streaming_follow_the_original_account(tmp_path, upstream):
    grants, events, result = upstream
    app = create_app(tmp_path / 'drive')
    store = app.state.store
    user = store.create_user('alpha', 'Alpha', LOCAL_PASSWORD)
    store.create_user('beta', 'Beta', LOCAL_PASSWORD, True)
    alpha, beta = (signed_in(app, name) for name in ('alpha', 'beta'))
    assert alpha.get('/api/workspace').json()['legacy_enabled'] is True
    assert TestClient(app).get('/api/legacy/libraries').status_code == 401
    assert alpha.get('/api/legacy/libraries').status_code == 409
    for client, name in ((alpha, 'a'), (beta, 'b')):
        response = client.post('/api/legacy/binding', json={'username': f'{name}@old.test', 'password': OLD_PASSWORD})
        assert response.json() == {'account': f'{name}@old.test'}
    stored = store.one('SELECT credential FROM legacy_seafile_bindings WHERE user_id=?', (user['id'],))['credential']
    assert TOKEN_A.encode() not in stored
    assert [item['id'] for item in alpha.get('/api/legacy/libraries').json()] == [REPO_A]
    assert [item['id'] for item in beta.get('/api/legacy/libraries').json()] == [REPO_B]
    route = f'/api/legacy/libraries/{REPO_A}'
    assert beta.get(route + '/files', params={'path': '/合同'}).status_code == 403
    assert beta.get(route + '/download', params={'path': '/合同/文件.txt'}).status_code == 403
    assert alpha.get(route + '/files', params={'path': '/合同'}).json() == [
        {'name': '文件.txt', 'is_dir': False, 'size': 11, 'modified': datetime(2026, 11, 12, 10, tzinfo=timezone.utc).timestamp()}
    ]
    response = alpha.get(route + '/download', params={'path': '/合同/文件.txt'})
    assert response.content == b'legacy data'
    assert response.headers['Content-Disposition'].startswith('attachment;')
    assert response.headers['Cache-Control'] == 'no-store'
    preview=alpha.get(route + '/preview',params={'path':'/合同/文件.txt'})
    assert preview.content==b'legacy data'
    assert preview.headers['content-type'].startswith('text/plain')
    assert beta.get(route + '/preview',params={'path':'/合同/文件.txt'}).status_code==403
    assert alpha.post(route + '/files', json={'path': '/合同'}).status_code == 405
    assert alpha.get(route + '/files', params={'path': '/合同/..'}).status_code == 400
    result['preview_only'] = True
    start = len(events)
    assert alpha.get(route + '/download', params={'path': '/合同/文件.txt'}).status_code == 403
    assert all(not path.endswith('/file/') for _, path in events[start:])
    result['preview_only'] = False
    result['link'] = 'https://unreachable.invalid/api2/ping/'
    assert alpha.get(route + '/download', params={'path': '/合同/文件.txt'}).status_code == 502
    grants[TOKEN_A].clear()
    assert alpha.get(route + '/files', params={'path': '/合同'}).status_code == 403
    assert alpha.get(route + '/download', params={'path': '/合同/文件.txt'}).status_code == 403
    with store.db() as db:
        db.execute('UPDATE users SET password=? WHERE id=?', (password_hash('local-password-reset'), user['id']))
    assert alpha.get('/api/legacy/libraries').status_code == 409
    assert [path for method, path in events if method != 'GET'] == ['/api2/auth-token/'] * 2


def test_dav_old_files_follow_each_account_and_reject_mutations(tmp_path, upstream):
    grants, events, result = upstream
    result['catalog'] = {TOKEN_A: [{'id': REPO_A, 'name': '甲资料库', 'encrypted': False},
                                   {'id': REPO_B, 'name': '加密资料库', 'encrypted': True}],
                         TOKEN_B: [{'id': REPO_B, 'name': '乙资料库', 'encrypted': False}]}
    app = create_app(tmp_path / 'drive')
    store = app.state.store
    owner = store.create_user('alpha', 'Alpha', LOCAL_PASSWORD)
    store.create_user('beta', 'Beta', LOCAL_PASSWORD, True)
    store.create_user('unlinked', 'Unlinked', LOCAL_PASSWORD)
    native = store.create_library(owner, '普通资料')
    (store.files / native['id'] / 'sample.txt').write_bytes(b'native file')
    alpha, beta, unlinked = (signed_in(app, name) for name in ('alpha', 'beta', 'unlinked'))
    for client, name in ((alpha, 'a'), (beta, 'b')):
        client.post('/api/legacy/binding', json={'username': f'{name}@old.test', 'password': OLD_PASSWORD}).raise_for_status()
    auth = {name: (name, client.post('/api/tokens', json={'name':'test computer'}).raise_for_status().json()['password'])
            for name, client in (('alpha', alpha), ('beta', beta), ('unlinked', unlinked))}
    dav = TestClient(app, base_url='https://cloud.test')
    mount = '/dav/原 Seafile'
    path = mount + '/甲资料库/合同/文件.txt'
    root = dav.request('PROPFIND', '/dav/', headers={'Depth':'1'}, auth=auth['alpha'])
    assert root.status_code == 207 and '原 Seafile' in unquote(root.text)
    assert '原 Seafile' not in unquote(dav.request('PROPFIND', '/dav/', headers={'Depth':'1'}, auth=auth['unlinked']).text)
    for name, expected, hidden in (('alpha','甲资料库','乙资料库'), ('beta','乙资料库','甲资料库')):
        listing = dav.request('PROPFIND', mount, headers={'Depth':'1'}, auth=auth[name])
        assert listing.status_code == 207
        assert expected in unquote(listing.text) and hidden not in unquote(listing.text)
        assert '加密资料库' not in unquote(listing.text)
    assert dav.get(path, auth=auth['beta']).status_code == 404
    assert dav.get(path, auth=auth['alpha']).content == b'legacy data'
    head = dav.head(path, auth=auth['alpha'])
    assert head.status_code == 200 and head.headers['Content-Length'] == '11'
    partial = dav.get(path, headers={'Range':'bytes=2-5'}, auth=auth['alpha'])
    assert (partial.status_code, partial.headers['Content-Range'], partial.content) == (206, 'bytes 2-5/11', b'gacy')
    assert dav.get(path, headers={'Range':'bytes=50-'}, auth=auth['alpha']).status_code == 416
    assert dav.request('PROPFIND', mount, headers={'Depth':'infinity'}, auth=auth['alpha']).status_code == 403
    start = len(events)
    for method in ('PUT', 'MKCOL', 'PROPPATCH', 'LOCK', 'UNLOCK', 'DELETE', 'MOVE', 'COPY'):
        assert dav.request(method, path, auth=auth['alpha']).status_code == 403, method
    for method in ('COPY', 'MOVE'):
        assert dav.request(method, '/dav/普通资料/sample.txt', headers={'Destination': dav.base_url.join(path).raw_path.decode()},
                           auth=auth['alpha']).status_code == 403
    assert all(method == 'GET' for method, _ in events[start:])
    assert dav.get('/dav/普通资料/sample.txt', auth=auth['alpha']).content == b'native file'
    result['preview_only'] = True
    assert dav.get(path, auth=auth['alpha']).status_code == 403
    result['preview_only'] = False
    result['catalog'][TOKEN_A].clear()
    grants[TOKEN_A].clear()
    assert dav.get(path, auth=auth['alpha']).status_code == 404
    assert all(method == 'GET' for method, _ in events[start:])


def test_dav_seafile_writeback_and_office_replacement(tmp_path, upstream):
    _, events, result = upstream
    result.update(save_name='文件..xls', save_path='/合同/文件..xls', write_permission='rw')
    app = create_app(tmp_path / 'drive')
    app.state.store.create_user('alpha', 'Alpha', LOCAL_PASSWORD)
    client = signed_in(app, 'alpha')
    client.post('/api/legacy/binding', json={'username':'a@old.test', 'password': OLD_PASSWORD}).raise_for_status()
    def device():
        return 'alpha', client.post('/api/tokens', json={'name':'test device'}).raise_for_status().json()['password']
    auth, other = device(), device()
    path = '/dav/原 Seafile/测试资料库/合同/文件.xls'
    parent = path.rsplit('/', 1)[0]
    temp = parent + '/ABCDEF12'
    dest = str(client.base_url.join(path))
    etag = client.head(path, auth=auth).headers['etag']
    assert client.put(path, content=b'edited', headers={'If-Match':etag}, auth=auth).status_code == 204
    assert client.get(path, auth=auth).content == b'edited'
    assert client.put(path, content=b'', auth=auth).status_code == 422
    assert client.put(temp, content=b'', auth=auth).status_code == 201
    assert client.request('MOVE', temp, headers={'Destination':dest}, auth=auth).status_code == 422
    assert client.get(path, auth=auth).content == b'edited'
    audits = app.state.store.all('SELECT account,device_name,storage,library_id,path,entry_point FROM blocked_office_writes ORDER BY id')
    assert [tuple(row.values()) for row in audits] == [
        ('alpha','test device','seafile',REPO_A,'/合同/文件..xls','webdav_put'),
        ('alpha','test device','seafile',REPO_A,'/合同/文件..xls','webdav_move'),
    ]
    assert client.delete(temp, auth=auth).status_code == 204
    assert client.put(path, content=b'stale', headers={'If-Match':etag}, auth=auth).status_code == 412
    result['write_permission'] = 'r'
    assert client.delete(path, auth=auth).status_code == 403
    result['write_permission'] = 'rw'
    assert client.request('MKCOL', parent + '/', auth=auth).status_code == 405
    assert client.put(temp, content=b'Office save', auth=auth).status_code == 201
    assert client.get(temp, auth=other).status_code == 404
    assert client.delete(path, auth=auth).status_code == 204
    assert result['removed'] and client.get(path, auth=auth).status_code == 404
    result['write_permission'] = 'r'
    assert client.request('MOVE', temp, headers={'Destination':dest}, auth=auth).status_code == 403
    assert client.put(path, content=b'no permission', auth=auth).status_code == 403
    assert client.get(path, auth=auth).status_code == 404
    result['write_permission'] = 'rw'
    assert client.request('MOVE', temp, headers={'Destination':dest}, auth=auth).status_code == 201
    assert client.get(path, auth=auth).content == b'Office save'
    assert client.get(temp, auth=auth).status_code == 404
    assert not result['removed']
    assert len([p for method,p in events if method == 'POST' and p.startswith('/seafhttp/update-api/')]) == 1

    # Word first renames the original to a backup, then uploads its replacement.
    backup = parent + '/12345678.tmp'
    original = client.get(path, auth=auth).content
    assert client.request('MOVE', path, headers={'Destination':str(client.base_url.join(backup))}, auth=auth).status_code == 201
    assert client.get(path, auth=auth).content == original
    assert client.get(backup, auth=other).status_code == 404
    assert client.delete(backup, auth=auth).status_code == 204
    assert client.put(path, content=b'Word replaced content', auth=auth).status_code == 204
    assert client.get(path, auth=auth).content == b'Word replaced content'

    # A separate computer read an older version and cannot overwrite the new one.
    assert client.get(path, auth=other).content == b'Word replaced content'
    assert client.put(path, content=b'Newer revision', auth=auth).status_code == 204
    assert client.put(path, content=b'Unsaved older edit', auth=other).status_code == 409
    assert client.get(path, auth=other).content == b'Newer revision'
    assert client.put(path, content=b'Edit after reopening', auth=other).status_code == 204

    # Interrupted/truncated requests keep the last committed original.
    assert client.put(path, content=b'abc', headers={'Content-Length':'7'}, auth=other).status_code == 400
    assert client.get(path, auth=other).content == b'Edit after reopening'
    result['write_permission'] = 'r'
    assert client.put(parent + '/~$office.xlsx', content=b'lock', auth=other).status_code == 403
    assert client.put(path, content=b'no permission', auth=other).status_code == 403
    assert client.get(path, auth=other).content == b'Edit after reopening'


def test_owner_can_manage_original_libraries_and_folder_shares(tmp_path, upstream):
    grants, events, result = upstream
    grants[TOKEN_B].append(REPO_A)
    management = {'name':'原资料库', 'folder':'合同', 'folder_permission':'rw', 'shares':{}, 'deleted':False}
    result['management'] = management
    app = create_app(tmp_path / 'drive')
    store = app.state.store
    store.create_user('alpha','Alpha',LOCAL_PASSWORD)
    recipient = store.create_user('beta','Beta',LOCAL_PASSWORD,admin=True)
    store.create_user('native','Native',LOCAL_PASSWORD)
    alpha, beta = (signed_in(app, name) for name in ('alpha','beta'))
    for client, account in ((alpha,'a@old.test'),(beta,'b@old.test')):
        client.post('/api/legacy/binding',json={'username':account,'password':OLD_PASSWORD}).raise_for_status()
    with store.db() as db:
        db.execute('UPDATE users SET seafile_identity=? WHERE id=?', ('b@old.test', recipient['id']))
    base = f'/api/legacy/libraries/{REPO_A}'
    share = {'path':'/', 'subject_type':'user','subject_id':recipient['id'],'role':'reader'}
    confirm = {'name':'原资料库','version':'2'*40}
    start = len(events)
    assert beta.get(base + '/shares').status_code == 403
    assert beta.put(base + '/shares', json=share).status_code == 403
    assert beta.patch(base,json={'name':'别人的库'}).status_code == 403
    assert beta.patch(base + '/folders',json={'path':'/合同','name':'别人的文件夹'}).status_code == 403
    assert beta.delete(base,params=confirm).status_code == 403
    assert all(method == 'GET' for method,_ in events[start:])

    assert alpha.get(base + '/share-targets',params={'subject_type':'user','q':'Beta'}).json()[0]['id'] == recipient['id']
    assert alpha.get(base + '/share-targets',params={'subject_type':'user','q':'Native'}).json() == []
    assert alpha.get(base + '/share-targets',params={'subject_type':'group'}).json() == [{'id':12,'name':'甲组'}]
    assert alpha.put(base + '/shares',json=share).json() == {'ok':True}
    assert alpha.get(base + '/shares').json() == [{'subject_type':'user','target':'b@old.test','name':'乙成员','permission':'r'}]
    assert alpha.put(base + '/shares',json={**share,'role':'editor'}).status_code == 200
    assert management['shares']['/','user','b@old.test'] == 'rw'
    assert alpha.put(base + '/shares',json={**share,'subject_type':'group','subject_id':'12','path':'/合同'}).status_code == 200
    assert management['shares']['/合同','group','12'] == 'r'
    assert alpha.delete(base + '/shares',params={'path':'/合同','subject_type':'group','target':'12'}).status_code == 200
    assert ('/合同','group','12') not in management['shares']
    assert alpha.delete(base + '/shares',params={'path':'/','subject_type':'user','target':'b@old.test'}).status_code == 200
    assert management['shares'] == {}

    assert alpha.patch(base + '/folders',json={'path':'/../合同','name':'重命名'}).status_code == 400
    management['folder_permission'] = 'r'
    assert alpha.patch(base + '/folders',json={'path':'/合同','name':'重命名'}).status_code == 403
    management['folder_permission'] = 'rw'
    assert alpha.patch(base + '/folders',json={'path':'/合同','name':'重命名'}).status_code == 200
    assert management['folder'] == '重命名'
    assert alpha.patch(base,json={'name':'新名称'}).status_code == 200
    assert alpha.delete(base,params=confirm).status_code == 409
    assert not management['deleted']
    refreshed = alpha.get(base + '/management').json()
    assert refreshed == {'name':'新名称','version':'2'*40}
    assert alpha.delete(base,params=refreshed).status_code == 200
    assert management['deleted']


def test_legacy_link_requires_safe_transport_and_rejects_wrong_certificate(tmp_path, upstream, monkeypatch):
    _, events, _ = upstream
    monkeypatch.setenv('LEGACY_SEAFILE_CERT_SHA256', '0' * 64)
    app = create_app(tmp_path / 'drive')
    app.state.store.create_user('alpha', 'Alpha', LOCAL_PASSWORD)
    insecure = signed_in(app, 'alpha', 'http://cloud.test')
    secure = signed_in(app, 'alpha')
    fields = {'username': 'a@old.test', 'password': OLD_PASSWORD}
    assert insecure.get('/api/legacy/binding').json()['can_link_here'] is False
    assert insecure.post('/api/legacy/binding', json=fields).status_code == 403
    assert secure.post('/api/legacy/binding', json=fields).status_code == 502
    assert secure.get('/api/legacy/binding').json()['account'] is None
    assert events == []


def test_workspace_group_catalog_shows_original_libraries_to_current_members(tmp_path, upstream):
    grants, _, source = upstream
    app = create_app(tmp_path / 'drive')
    store = app.state.store
    store.create_user('alpha', 'Alpha', LOCAL_PASSWORD)
    store.create_user('beta', 'Beta', LOCAL_PASSWORD, True)
    alpha, beta = (signed_in(app, name) for name in ('alpha', 'beta'))
    with store.db() as db:
        db.executemany('INSERT INTO groups VALUES(?,?,?,?)', [
            ('mapped-a', '甲组', '', 0), ('mapped-b', '乙组', '', 0),
        ])
        db.executemany('INSERT INTO seafile_groups VALUES(?,?)', [('mapped-a', 12), ('mapped-b', 24)])
    for client, name in ((alpha, 'a'), (beta, 'b')):
        assert client.post('/api/legacy/binding', json={'username': f'{name}@old.test', 'password': OLD_PASSWORD}).status_code == 200

    endpoint = '/api/legacy/group-libraries'
    assert TestClient(app).get(endpoint).status_code == 401
    grants[TOKEN_A].append(REPO_A)
    listed = alpha.get(endpoint, params={'group_id': 'mapped-a'}).json()
    assert list(listed) == ['mapped-a']
    assert [lib['id'] for lib in listed['mapped-a']] == [REPO_A]
    assert alpha.get(endpoint, params={'group_id': 'mapped-b'}).status_code == 403
    assert beta.get(endpoint, params={'group_id': 'mapped-a'}).status_code == 403
    assert list(beta.get(endpoint).json()) == ['mapped-b']

    # The source can remove membership before an old library entry disappears.
    source['groups'] = {TOKEN_A: []}
    assert alpha.get(endpoint).json() == {}
    assert alpha.get(endpoint, params={'group_id': 'mapped-a'}).status_code == 403


def test_original_library_tabs_follow_each_linked_account(tmp_path, upstream):
    _, _, source = upstream
    app = create_app(tmp_path / 'drive')
    for name in ('alpha', 'beta'):
        app.state.store.create_user(name, name, LOCAL_PASSWORD, admin=name == 'beta')
    alpha, beta = (signed_in(app, name) for name in ('alpha', 'beta'))
    endpoint = '/api/legacy/libraries'
    assert alpha.get(endpoint, params={'view': 'mine'}).json() == []
    for client, name in ((alpha, 'a'), (beta, 'b')):
        assert client.post('/api/legacy/binding', json={'username': f'{name}@old.test', 'password': OLD_PASSWORD}).status_code == 200

    public_id = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
    group_id = 'dddddddd-dddd-dddd-dddd-dddddddddddd'
    public = {'id': public_id, 'type': 'grepo', 'name': '原公共资料'}
    shared = {'id': REPO_B, 'type': 'srepo', 'name': '共享给甲的资料'}
    source['catalog'] = {
        TOKEN_A: [{'id': REPO_A, 'type': 'repo', 'name': '甲的资料', 'owner_name': '甲', 'size': 0, 'mtime': 1794477600}, shared, shared, public,
                  {'id': group_id, 'type': 'grepo', 'groupid': 12, 'name': '群组资料'}],
        TOKEN_B: [{'id': REPO_B, 'type': 'repo', 'name': '乙的资料'}, public],
    }

    def listed(client, view):
        response = client.get(endpoint, params={'view': view})
        assert response.status_code == 200
        return [library['id'] for library in response.json()]

    assert listed(alpha, 'mine') == [REPO_A]
    own = alpha.get(endpoint, params={'view': 'mine'}).json()[0]
    assert own['size'] == 0 and own['modified'] == 1794477600
    assert own['source_url'] == f'{legacy_upstream().origin}/library/{REPO_A}/'
    assert listed(alpha, 'shared') == [REPO_B]
    assert listed(alpha, 'public') == [public_id]
    assert listed(beta, 'mine') == [REPO_B]
    assert listed(beta, 'shared') == []
    assert listed(beta, 'public') == [public_id]
    assert beta.get(f'{endpoint}/{REPO_A}/files').status_code == 403
    assert alpha.get(endpoint, params={'view': 'group'}).status_code == 400
    assert TestClient(app).get(endpoint, params={'view': 'public'}).status_code == 401


def test_https_portal_direct_login_groups_and_isolation(upstream):
    grants, events, result = upstream
    app = create_legacy_portal(legacy_upstream(), ['cloud.test'])
    insecure = TestClient(app, base_url='http://cloud.test')
    assert insecure.get('/').status_code == 403
    assert insecure.post('/api/login', json={'username': 'a@old.test', 'password': OLD_PASSWORD}).status_code == 403
    assert events == []
    alpha = TestClient(app, base_url='https://cloud.test')
    beta = TestClient(app, base_url='https://cloud.test')
    assert alpha.get('/api/catalog').status_code == 401
    assert alpha.post('/api/login', json={'username': 'a@old.test', 'password': OLD_PASSWORD},
                      headers={'Origin': 'https://elsewhere.test'}).status_code == 403
    for client, name, label in ((alpha, 'a', '甲成员'), (beta, 'b', '乙成员')):
        response = client.post('/api/login', json={'username': f'{name}@old.test', 'password': OLD_PASSWORD})
        assert response.json() == {'name': label}
        cookie = response.headers['Set-Cookie']
        assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=strict' in cookie
        assert TOKEN_A not in cookie and TOKEN_B not in cookie
        assert client.get('/api/session').json() == {'name': label}
    a = alpha.get('/api/catalog').json()
    b = beta.get('/api/catalog').json()
    assert a['groups'] == [{'id': 12, 'name': '甲组'}] and [item['id'] for item in a['libraries']] == [REPO_A]
    assert alpha.get('/api/tools/devices').json() == [{'device_name':'my device','platform':'windows'}]
    notices=alpha.get('/api/notifications').json()
    assert notices['unread']==1 and notices['items'][0]['detail']=={'repo_name':'my repo','share_from_user_name':'Colleague'}
    assert alpha.put('/api/notifications').status_code==405
    assert b['groups'] == [{'id': 24, 'name': '乙组'}] and [item['id'] for item in b['libraries']] == [REPO_B]
    root = f'/api/libraries/{REPO_A}'
    assert alpha.get(root + '/files').json()[0]['name'] == '合同'
    assert alpha.get(root + '/download', params={'path': '/合同/文件.txt'}).content == b'legacy data'
    assert beta.get(root + '/download', params={'path': '/合同/文件.txt'}).status_code == 403
    assert beta.get(root + '/preview', params={'path': '/合同/文件.txt'}).status_code == 403
    preview = alpha.get(root + '/preview', params={'path': '/合同/文件.txt'})
    assert preview.content == b'legacy data' and preview.headers['content-type'].startswith('text/plain')
    assert alpha.delete(root + '/files', params={'path': '/合同/文件.txt'}).status_code == 405
    grants[TOKEN_A].clear()
    result['groups'] = {TOKEN_A: [], TOKEN_B: [24]}
    assert alpha.get('/api/catalog').json() == {'groups': [], 'libraries': []}
    assert alpha.get(root + '/download', params={'path': '/合同/文件.txt'}).status_code == 403
    assert alpha.post('/api/logout').status_code == 200
    assert alpha.get('/api/session').status_code == 401
    assert [path for method, path in events if method != 'GET'] == ['/api2/auth-token/'] * 2


def test_https_admin_reaches_full_backend_without_seafile_account(tmp_path, upstream, monkeypatch):
    from drive import legacy_portal
    from drive.portal_admin import PortalAdmin

    backend = TestClient(create_app(tmp_path / 'native'))
    backend.app.state.store.create_user('admin', 'Administrator', LOCAL_PASSWORD, True)

    class Backend(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def relay(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            response = backend.request(self.command, self.path, content=body,
                                       headers={key: value for key, value in self.headers.items() if key.lower() != 'host'})
            self.send_response(response.status_code)
            for key, value in response.headers.multi_items():
                if key.lower() not in ('transfer-encoding','connection','server','date'):
                    self.send_header(key,value)
            self.end_headers()
            self.wfile.write(response.content)

        do_POST = relay
        do_GET = relay

    server = ThreadingHTTPServer(('127.0.0.1',0),Backend)
    worker = Thread(target=server.serve_forever,daemon=True)
    worker.start()
    monkeypatch.setattr(legacy_portal,'PortalAdmin',lambda: PortalAdmin(port=server.server_port))
    try:
        app = create_legacy_portal(legacy_upstream(),['cloud.test'])
        client = TestClient(app,base_url='https://cloud.test')
        reply = client.post('/api/login',json={'username':'admin','password':LOCAL_PASSWORD})
        assert reply.status_code == 200 and reply.json()['mode'] == 'admin'
        client.headers['X-Drive-CSRF']=reply.json()['csrf']
        assert client.get('/api/me').json()['admin']
        assert client.get('/api/users').status_code == 200
        assert client.get('/api/license').status_code == 200
        assert 'id="app"' in client.get('/').text
        assert 'location.reload();' in client.get('/assets/app.js').text
        assert client.post('/api/logout').status_code == 200
        assert client.get('/api/session').status_code == 401
        assert backend.app.state.store.one('SELECT count(*) n FROM sessions')['n'] == 0
        assert all(path != '/api2/auth-token/' for method,path in upstream[1])
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_unified_https_login_supports_windows_client_without_exposing_seafile_token(upstream, monkeypatch):
    from drive import legacy_portal

    class Workspace:
        def identity_login(self, username, password, otp):
            return {'name': 'Employee', 'csrf': 'workspace-csrf', 'legacy_token': 'private-upstream-token'}, 'workspace-session'

        def identity_active(self, token):
            return token == 'workspace-session'

    monkeypatch.setattr(legacy_portal, 'PortalAdmin', Workspace)
    client = TestClient(create_legacy_portal(legacy_upstream(), ['cloud.test'], unified_identity=True),
                        base_url='https://cloud.test')
    response = client.post('/api/login', json={'username': 'employee', 'password': OLD_PASSWORD})
    assert response.status_code == 200
    assert response.json() == {'name': 'Employee', 'unified_identity': True, 'mode': 'workspace', 'csrf': 'workspace-csrf'}
    assert 'private-upstream-token' not in response.text
    assert 'workspace-session' not in response.headers['Set-Cookie']
    assert client.get('/api/session').json()['mode'] == 'workspace'


def test_nonstandard_office_container_is_rejected_before_conversion():
    from io import BytesIO
    from drive.legacy_preview import preview_bytes
    from drive.store import DriveError

    class Binary(BytesIO):
        headers = {}

    class Connection:
        closed = False
        def close(self):
            self.closed = True

    connection=Connection()
    class Source:
        def download(self,*_):
            return connection,Binary(b'not a standard workbook')

    with pytest.raises(DriveError) as error:
        preview_bytes(Source(),'token',REPO_A,'/protected.xlsx',object())
    assert error.value.status == 422
    assert connection.closed


@pytest.mark.parametrize('external_result', [False, True])
def test_loopback_office_conversion_serves_source_briefly_and_checks_result_origin(monkeypatch, external_result):
    from urllib.request import ProxyHandler, build_opener
    from urllib.error import URLError
    from drive.legacy_preview import OfficePreview
    from drive.office import verify
    from drive.store import DriveError

    secret = b'local-converter-test-secret-32-bytes'
    document, pdf = b'private test document', b'%PDF-1.4 test conversion'
    fetched = []
    client = build_opener(ProxyHandler({}))

    class Converter(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            assert self.path == '/converter'
            params = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            claims = verify(params['token'], secret, 'converter')
            assert claims['url'] == params['url']
            with client.open(params['url'], timeout=2) as response:
                assert response.read() == document
            fetched.append(params['url'])
            address = 'https://untrusted.test' if external_result else f'http://127.0.0.1:{self.server.server_port}'
            payload = json.dumps({'endConvert': True, 'fileUrl': address + '/cache/files/test.pdf'}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            assert self.path == '/cache/files/test.pdf'
            self.send_response(200)
            self.end_headers()
            self.wfile.write(pdf)

    with ThreadingHTTPServer(('127.0.0.1', 0), Converter) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        monkeypatch.setenv('LEGACY_PREVIEW_URL', f'http://127.0.0.1:{server.server_port}')
        monkeypatch.setenv('LEGACY_PREVIEW_LOOPBACK_CONVERTER', '1')
        preview = OfficePreview(secret=secret.decode())
        try:
            if external_result:
                with pytest.raises(DriveError) as error:
                    preview.convert(document, 'docx')
                assert error.value.status == 502
            else:
                assert preview.convert(document, 'docx') == pdf
            assert len(fetched) == 1
            with pytest.raises(URLError):
                client.open(fetched[0], timeout=1)
        finally:
            server.shutdown()
            worker.join(timeout=2)


def test_portal_requests_otp_only_when_seafile_requires_it(upstream):
    app=create_legacy_portal(legacy_upstream(),['cloud.test'])
    client=TestClient(app,base_url='https://cloud.test')
    fields={'username':'otp@old.test','password':OLD_PASSWORD}
    challenge=client.post('/api/login',json=fields)
    assert challenge.status_code==422
    assert challenge.json()['code']=='seafile_otp_required'
    fields['otp']='123456'
    assert client.post('/api/login',json=fields).status_code==200
    client.post('/api/logout')
    assert client.post('/api/login',json={'username':'a@old.test','password':OLD_PASSWORD}).status_code==200
