from copy import deepcopy
import json
import secrets
from urllib.parse import unquote
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

import drive.app as app_module
from drive.identity_directory import IdentityDirectory
from drive import identity_directory as directory_module
from drive.legacy import OTPRequired
from drive.store import DriveError
from drive.workspace import WorkspaceStore


PASSWORD='old-password-123'
IDENTITY='old-user@auth.local'


class Provider:
    origin='https://old.test'
    def __init__(self):
        self.records={IDENTITY:{'email':IDENTITY,'login_id':'old-user','name':'旧用户','is_active':True,'is_staff':True}}
        self.passwords={IDENTITY:PASSWORD}
        self.tokens={}
        self.memberships={IDENTITY:[{'id':7,'name':'原项目组'}]}

    def login_token(self, username, password, otp=''):
        person=next((r for r in self.records.values() if username in (r['email'],r['login_id'])),None)
        if person is None or not person['is_active'] or self.passwords[person['email']]!=password:
            raise DriveError(422,'旧账号验证失败')
        if username=='mfa-user' and otp!='123456':raise OTPRequired()
        token=secrets.token_hex(20)
        self.tokens[token]=person['email']
        return token

    def profile(self, token, username):
        identity=self.tokens.get(token)
        if identity not in self.records or not self.records[identity]['is_active']:
            raise DriveError(403,'账号失效')
        return {'identity':identity,'name':self.records[identity]['name']}

    def groups(self, token):
        return deepcopy(self.memberships.get(self.profile(token,'')['identity'],[]))


class Administrator:
    def __init__(self,provider):self.provider=provider
    def users(self):return deepcopy(list(self.provider.records.values()))
    def groups(self):return [{'id':7,'name':'原项目组'}]
    def request(self,path):
        identity=unquote(path.rstrip('/').split('/')[-1])
        return deepcopy(self.provider.records[identity])
    def create(self,username,name,password,administrator=False,contact_email=None):
        if any(r['login_id']==username for r in self.provider.records.values()):raise DriveError(409,'重复')
        identity=secrets.token_hex(16)+'@auth.local'
        row={'email':identity,'login_id':username,'name':name,'is_active':True,'is_staff':administrator,'contact_email':contact_email or ''}
        self.provider.records[identity]=row
        self.provider.passwords[identity]=password
        return deepcopy(row)
    def update(self,identity,data):
        if 'password' in data:self.provider.passwords[identity]=data['password']
        self.provider.records[identity].update({k:v for k,v in data.items() if k!='password'})
        return deepcopy(self.provider.records[identity])


@pytest.mark.parametrize('initial_total', [329, 250])
def test_directory_pages_preserve_all_accounts_and_handle_growth(monkeypatch, initial_total):
    service = 'user-128@old.local'
    administrator = directory_module.DirectoryAdmin(Provider(), {'token': 'test-only', 'identity': service})
    requested = []

    def request(path):
        query = parse_qs(urlsplit(path).query)
        page = int(query['page'][0])
        requested.append(page)
        start = (page - 1) * 100
        return {'data': [{'email': f'user-{number}@old.local'} for number in range(start, min(start + 100, 329))],
                'total_count': initial_total if page == 1 else 329}

    monkeypatch.setattr(administrator, 'request', request)
    accounts = administrator.users()
    assert sorted(requested) == [1, 2, 3, 4]
    assert [account['email'] for account in accounts] == [f'user-{number}@old.local' for number in range(329) if number != 128]


def test_trial_directory_cannot_change_original_accounts(monkeypatch):
    monkeypatch.setenv('DRIVE_IDENTITY_READ_ONLY', '1')

    def reject_connection(*_):
        pytest.fail('A trial account mutation reached the original server')

    monkeypatch.setattr(directory_module, 'PinnedConnection', reject_connection)
    directory = directory_module.DirectoryAdmin(Provider(), {'token': 'test-only', 'identity': 'service'})
    for method, path, fields in (
        ('POST', '/api/v2.1/admin/users/', {'login_id': 'new-member'}),
        ('PUT', '/api/v2.1/admin/users/member/', {'password': 'changed-password'}),
        ('DELETE', '/api/v2.1/admin/users/member/', None),
    ):
        with pytest.raises(DriveError) as error:
            directory.request(path, method, fields)
        assert error.value.status == 403


def test_seafile_11_account_requests_use_string_status_flags(monkeypatch):
    recorded = []

    class Connection:
        def __init__(self, *_):
            pass

        def request(self, method, path, payload, headers):
            recorded.append((method, json.loads(payload)))

        def getresponse(self):
            return self

        status = 200

        def read(self, _):
            return b'{}'

        def close(self):
            pass

    class Upstream:
        host, port, fingerprint = 'seafile.test', 443, b''

    monkeypatch.setattr(directory_module, 'PinnedConnection', Connection)
    directory = directory_module.DirectoryAdmin(Upstream(), {'token': 'test-only', 'identity': 'test-service'})
    directory.create('new-member', 'New member', 'temporary-test-password')
    directory.update('member@auth.local', {'is_active': False})
    assert recorded[0][1]['is_active'] == 'true'
    assert recorded[0][1]['is_staff'] == 'false'
    assert recorded[1] == ('PUT', {'is_active': 'false'})


def test_confirmed_identity_merge_preserves_uid_and_never_accepts_old_local_hash(tmp_path):
    store=WorkspaceStore(tmp_path/'data')
    local=store.create_user('old-user','旧用户','local-old-password')
    provider=Provider()
    manager=IdentityDirectory(store,provider,Administrator(provider))
    with pytest.raises(DriveError):manager.sync()
    manager.merges[IDENTITY]=local['id']
    with store.db() as db:
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)', ('old-local-session',local['id'],'csrf',9999999999))
    manager.sync(force=True)
    assert store.one('SELECT hash FROM sessions WHERE user_id=?',(local['id'],)) is None
    actual,token=manager.login('old-user',PASSWORD)
    assert actual['id']==local['id'] and not actual['admin']
    assert store.authenticate('old-user','local-old-password') is None
    assert manager.bindings.token(actual['id'])==token
    with pytest.raises(DriveError):manager.bindings.bind_token(actual['id'],'another@auth.local',token)


def test_directory_refresh_rolls_back_the_batch_if_an_identity_conflicts(tmp_path):
    store = WorkspaceStore(tmp_path/'data')
    local = store.create_user('collision-user', 'Existing person', 'local-password')
    provider = Provider()
    provider.records['second@old.local'] = {'email': 'second@old.local', 'login_id': 'collision-user',
                                          'name': 'Another person', 'is_active': True}
    manager = IdentityDirectory(store, provider, Administrator(provider))
    with pytest.raises(DriveError) as error:
        manager.sync(force=True)
    assert error.value.status == 409
    assert store.one('SELECT id FROM users WHERE seafile_identity=?', (IDENTITY,)) is None
    assert store.one('SELECT count(*) n FROM users')['n'] == 1
    assert store.authenticate('collision-user', 'local-password')['id'] == local['id']
    assert manager.last_sync == 0


def test_existing_local_employee_moves_once_without_changing_password_or_uid(tmp_path):
    store=WorkspaceStore(tmp_path/'data')
    local=store.create_user('local-member','新用户','same-password')
    provider=Provider()
    manager=IdentityDirectory(store,provider,Administrator(provider))
    actual,_=manager.login('local-member','same-password')
    assert actual['id']==local['id'] and actual['identity_source']=='seafile'
    replacement=IdentityDirectory(store,provider,Administrator(provider))
    replacement.sync(force=True)
    again,_=replacement.login('local-member','same-password')
    assert again['id']==local['id']
    assert len([r for r in provider.records.values() if r['login_id']=='local-member'])==1


def login(app,username,password):
    client=TestClient(app,client=('127.0.0.1',50000))
    response=client.post('/api/login',json={'username':username,'password':password})
    assert response.status_code==200,response.text
    client.headers['X-Drive-CSRF']=response.json()['csrf']
    return client,response.json()


def test_new_account_shares_with_old_account_and_password_change_has_one_authority(tmp_path,monkeypatch):
    provider=Provider()
    admin_api=Administrator(provider)
    monkeypatch.setattr(app_module,'configured_directory',lambda store:IdentityDirectory(store,provider,admin_api))
    app=app_module.create_app(tmp_path/'data')
    app.state.store.create_user('admin','管理员','admin-password',True)
    admin,_=login(app,'admin','admin-password')
    response=admin.post('/api/users',json={'username':'new-member','name':'新员工','password':'new-password'})
    assert response.status_code==201,response.text
    created=response.json()
    new,new_identity=login(app,'new-member','new-password')
    old,old_identity=login(app,'old-user',PASSWORD)
    assert created['id']==new_identity['id']
    assert TestClient(app).post('/api/login',json={'username':'old-user','password':PASSWORD}).status_code==403
    assert TestClient(app).post('/api/identity/session',json={'username':'old-user','password':PASSWORD}).status_code==403
    assert old.get('/api/identity/active').status_code==200
    people=new.get('/api/people',params={'q':'旧用户'}).json()
    assert people[0]['id']==old_identity['id']
    lid=new.post('/api/libraries',json={'name':'同一套账号的共享'}).json()['id']
    payload=b'unified identity file'
    upload=new.post('/api/uploads',json={'group_id':lid,'path':'proof.txt','size':len(payload),'fingerprint':'unified'}).json()['id']
    assert new.patch('/api/uploads/'+upload,content=payload,headers={'Upload-Offset':'0'}).status_code==200
    assert new.post('/api/uploads/'+upload+'/complete').status_code==200
    assert new.post(f'/api/libraries/{lid}/shares',json={'subject_type':'user','subject_id':old_identity['id'],'role':'reader'}).status_code==200
    assert old.get(f'/api/libraries/{lid}/content',params={'path':'proof.txt'}).content==payload
    assert old.delete(f'/api/groups/{lid}/files',params={'path':'proof.txt'}).status_code==403
    uid=old_identity['id']
    assert admin.post(f'/api/users/{uid}/password',json={'new_password':'changed-password'}).status_code==200
    assert old.get('/api/identity/active').status_code==401
    assert app.state.store.authenticate('old-user',PASSWORD) is None
    assert TestClient(app,client=('127.0.0.1',50000)).post('/api/login',json={'username':'old-user','password':PASSWORD}).status_code==422
    updated,new_old=login(app,'old-user','changed-password')
    assert new_old['id']==uid
    assert updated.get(f'/api/libraries/{lid}/content',params={'path':'proof.txt'}).content==payload
    assert app.state.store.one('SELECT count(*) n FROM users WHERE username=?',('old-user',))['n']==1


def test_original_group_membership_revocation_removes_new_file_group_access(tmp_path):
    store=WorkspaceStore(tmp_path/'data')
    owner=store.create_user('admin','管理员','admin-password',True)
    provider=Provider()
    manager=IdentityDirectory(store,provider,Administrator(provider))
    user,_=manager.login('old-user',PASSWORD)
    group=store.groups_for(user)[0]
    assert group['management_role']=='member'
    lib=store.create_library(owner,'新资料')
    store.share(owner,lib['id'],'','group',group['id'],'reader')
    assert store.access(user,lib['id'])[1]=='reader'
    provider.memberships[IDENTITY]=[]
    manager.authorize(user)
    with pytest.raises(DriveError):store.access(user,lib['id'])
