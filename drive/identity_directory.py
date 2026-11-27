"""Use Seafile as the employee identity directory for the whole workspace."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import stat
from threading import RLock
import time
from urllib.parse import quote, urlencode
import uuid
import unicodedata
import re

from .legacy import LegacyBindings, OTPRequired, PinnedConnection, legacy_upstream
from .store import DriveError, password_hash


class DirectoryAdmin:
    def __init__(self, upstream, credential):
        self.upstream = upstream
        self.token = credential['token']
        self.service_identity = credential['identity']
        self.read_only = os.environ.get('DRIVE_IDENTITY_READ_ONLY') == '1'

    def request(self, path, method='GET', data=None):
        if self.read_only and method != 'GET':
            raise DriveError(403, '试跑环境仅允许读取原账号目录，请在正式入口管理成员')
        if not path.startswith('/api/v2.1/admin/users/') and not (method == 'GET' and path.startswith('/api/v2.1/admin/groups/?')):
            raise ValueError('Directory management only supports account and group directory endpoints')
        connection = PinnedConnection(self.upstream.host, self.upstream.port, self.upstream.fingerprint)
        headers = {'Authorization':'Bearer '+self.token, 'Accept':'application/json'}
        payload = None
        if data is not None:
            # Seafile 11's account handlers parse status flags as strings.
            fields = {key: str(value).lower() if isinstance(value, bool) else value
                      for key, value in data.items()}
            payload = json.dumps(fields).encode()
            headers['Content-Type'] = 'application/json'
        try:
            connection.request(method,path,payload,headers)
            response=connection.getresponse()
            raw=response.read(8*1024**2+1)
            if len(raw)>8*1024**2: raise DriveError(502,'账号目录响应过大')
            if response.status not in (200,201):
                if response.status in (400,409):
                    raise DriveError(409,'账号已存在或资料不符合原账号目录的要求')
                raise DriveError(502,'账号目录暂时无法更新，请稍后重试')
            return json.loads(raw)
        except (OSError,ValueError):
            raise DriveError(502,'暂时无法连接统一账号目录') from None
        finally:
            connection.close()

    def users(self):
        def read_page(page):
            return self.request('/api/v2.1/admin/users/?' + urlencode({'page': page, 'per_page': 100}))

        result = []
        fetched = 0
        page = 1
        total = 10**9
        # The first page establishes the page count. Read the remaining pages
        # with bounded concurrency, preserving their order and complete results.
        with ThreadPoolExecutor(max_workers=3) as workers:
            while page < 100:
                last = page if page == 1 else min(page + 2, 99, (total + 99) // 100)
                values = list(workers.map(read_page, range(page, last + 1)))
                for value in values:
                    rows = value.get('data') if isinstance(value, dict) else None
                    if not isinstance(rows, list):
                        raise DriveError(502, '统一账号目录返回格式异常')
                    total = value.get('total_count', 10**9)
                    if not isinstance(total, int) or total < 0:
                        raise DriveError(502, '统一账号目录总数格式异常')
                    result.extend(row for row in rows if row.get('email') != self.service_identity)
                    fetched += len(rows)
                    if len(rows) < 100 or fetched >= total:
                        return result
                page = last + 1
        raise DriveError(502,'统一账号目录分页超出限制')

    def groups(self):
        result = []
        for page in range(1, 100):
            value = self.request('/api/v2.1/admin/groups/?' + urlencode({'page': page, 'per_page': 100}))
            groups = value.get('groups') if isinstance(value, dict) else None
            if not isinstance(groups, list):
                raise DriveError(502, '原群组目录返回格式异常')
            result.extend(groups)
            if not value.get('page_info', {}).get('has_next_page'):
                return result
        raise DriveError(502, '原群组目录分页超出限制')

    def create(self, username, name, password, administrator=False, contact_email=None):
        return self.request('/api/v2.1/admin/users/','POST',{
            'email':contact_email or uuid.uuid4().hex+'@accounts.suyan.invalid','login_id':username,
            'name':name,'password':password,'is_staff':False,'is_active':True})

    def update(self, identity, data):
        return self.request('/api/v2.1/admin/users/'+quote(identity,safe='')+'/', 'PUT',data)


class IdentityDirectory:
    def __init__(self, store, upstream, administrator, approved_merges=None):
        self.store,self.upstream,self.administrator=store,upstream,administrator
        self.bindings=LegacyBindings(store,upstream)
        self.merges=approved_merges or {}
        self.lock=RLock()
        self.last_sync=0

    def upsert(self, record):
        with self.lock, self.store.db() as db:
            uid = self._upsert(db, record)
        return self.store.get_user(uid) if record.get('is_active',True) else self.store.public_user(self.store.one('SELECT * FROM users WHERE id=?',(uid,)))

    def _upsert(self, db, record):
        identity=record['email']
        username=record.get('login_id') or record.get('contact_email') or identity
        name=record.get('name') or username
        existing=db.execute('SELECT * FROM users WHERE seafile_identity=?',(identity,)).fetchone()
        conflict=db.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE',(username,)).fetchone()
        if existing is None and conflict is not None:
            migration=db.execute('SELECT contact_email FROM identity_migrations WHERE user_id=?',(conflict['id'],)).fetchone()
            approved=self.merges.get(identity)==conflict['id'] or (migration and migration['contact_email']==record.get('contact_email'))
            if not approved or conflict['username']=='admin':
                raise DriveError(409,'账号目录存在同名账号，需要管理员确认身份后合并')
            existing=conflict
        if existing is None:
            uid=uuid.uuid4().hex
            db.execute('INSERT INTO users(id,username,name,password,admin,active,created,seafile_identity) VALUES(?,?,?,?,?,?,?,?)',
                       (uid,username,name,password_hash(secrets.token_urlsafe(32)),int(bool(record.get('_native_admin'))),int(bool(record.get('is_active',True))),time.time(),identity))
        else:
            uid=existing['id']
            if existing['username']=='admin':raise DriveError(409,'系统管理账号不能被员工账号覆盖')
            if not existing['seafile_identity']:
                for table in ('sessions', 'tokens', 'legacy_seafile_bindings'):
                    db.execute(f'DELETE FROM {table} WHERE user_id=?', (uid,))
                for key in list(self.store.key_cache):
                    if key[0] == uid:
                        self.store.key_cache.pop(key, None)
            db.execute('UPDATE users SET username=?,name=?,admin=?,active=?,seafile_identity=? WHERE id=?',
                       (username,name,int(bool(record.get('_native_admin',existing['admin']))),int(bool(record.get('is_active',True))),identity,uid))
        return uid

    def sync(self, force=False):
        with self.lock:
            if not force and time.monotonic()-self.last_sync<60:return
            records=self.administrator.users()
            groups=self.administrator.groups()
            with self.store.db() as db:
                seen=set()
                for record in records:
                    self._upsert(db, record)
                    seen.add(record['email'])
                for row in db.execute('SELECT id,seafile_identity FROM users WHERE seafile_identity IS NOT NULL').fetchall():
                    if row['seafile_identity'] not in seen:
                        db.execute('UPDATE users SET active=0 WHERE id=?',(row['id'],))
                        db.execute('DELETE FROM sessions WHERE user_id=?',(row['id'],))
                        db.execute('DELETE FROM tokens WHERE user_id=?',(row['id'],))
                for group in groups:
                    self.ensure_group(db, group)
            self.last_sync=time.monotonic()

    def ensure_group(self, db, group):
        row = db.execute('SELECT group_id FROM seafile_groups WHERE external_id=?', (group['id'],)).fetchone()
        if row:
            return row['group_id']
        gid = uuid.uuid5(uuid.NAMESPACE_URL, self.upstream.origin + '/groups/' + str(group['id'])).hex
        name = group['name']
        if db.execute('SELECT 1 FROM groups WHERE name=? COLLATE NOCASE', (name,)).fetchone():
            name += f'（原群组 {group["id"]}）'
        db.execute('INSERT INTO groups VALUES(?,?,?,?)', (gid, name, 'Seafile 原群组', time.time()))
        db.execute('INSERT INTO seafile_groups VALUES(?,?)', (gid, group['id']))
        return gid

    def sync_groups(self, user_id, token):
        # Serialize a user's remote membership checks and writes so an older
        # response cannot restore a membership removed by a newer request.
        with self.store.file_locks('identity:' + user_id):
            groups = self.upstream.groups(token)
            with self.lock, self.store.db() as db:
                original = {row['external_id']: row['group_id'] for row in
                            db.execute('SELECT external_id,group_id FROM seafile_groups')}
                expected = {original.get(group['id']) or self.ensure_group(db, group) for group in groups}
                current = {row['group_id'] for row in db.execute(
                    'SELECT m.group_id FROM members m JOIN seafile_groups s ON s.group_id=m.group_id WHERE m.user_id=?',
                    (user_id,))}
                if expected != current:
                    db.executemany('INSERT OR IGNORE INTO members VALUES(?,?,?)',
                                   [(gid, user_id, 'reader') for gid in expected - current])
                    db.executemany('DELETE FROM members WHERE user_id=? AND group_id=?',
                                   [(user_id, gid) for gid in current - expected])

    def login(self, username, password, otp=''):
        username=username.strip()
        if username.casefold()=='admin':raise DriveError(403,'系统管理员使用管理登录入口')
        try:
            token=self.upstream.login_token(username,password,otp)
        except OTPRequired:
            raise
        except DriveError as error:
            # Preserve the one remaining local employee's password by moving that
            # account into the directory on their first successful login.
            pending=self.store.authenticate(username,password) if error.status==422 and username!='admin' else None
            if not pending:raise
            with self.store.db() as db:
                db.execute('INSERT OR IGNORE INTO identity_migrations VALUES(?,?)',(pending['id'],uuid.uuid4().hex+'@accounts.suyan.invalid'))
                email=db.execute('SELECT contact_email FROM identity_migrations WHERE user_id=?',(pending['id'],)).fetchone()[0]
            record=self.administrator.create(pending['username'],pending['name'],password,bool(pending['admin']),contact_email=email)
            token=self.upstream.login_token(username,password,otp)
        profile=self.upstream.profile(token,username)
        record=self.administrator.request('/api/v2.1/admin/users/'+quote(profile['identity'],safe='')+'/')
        current=self.upsert(record)
        self.bindings.bind_token(current['id'],profile['identity'],token)
        self.sync_groups(current['id'],token)
        return current,token

    def authorize(self, current):
        if current.get('identity_source') != 'seafile':
            return
        row = self.store.one('SELECT u.seafile_identity,u.password,b.credential FROM users u '
                             'LEFT JOIN legacy_seafile_bindings b ON b.user_id=u.id WHERE u.id=? AND u.active=1',
                             (current['id'],))
        if not row:
            raise DriveError(401, '请重新登录')
        if not row['credential']:
            raise DriveError(409, '请先关联你的 Seafile 账号')
        token = self.bindings.decrypt_token(current['id'], row['credential'], row['password'])
        profile = self.upstream.profile(token, current['username'])
        if profile['identity'] != row['seafile_identity']:
            raise DriveError(403, '统一账号身份校验失败')
        self.sync_groups(current['id'], token)
        return token

    def create_user(self, username, name, password, admin=False):
        username=unicodedata.normalize('NFC',username).strip()
        if username.casefold()=='admin':raise DriveError(409,'系统管理账号已存在')
        if not re.fullmatch(r'[\w.-]{2,40}',username):raise DriveError(400,'账号使用 2–40 位中文、字母、数字、点、横线或下划线')
        if not name.strip() or len(name)>60 or '/' in name:raise DriveError(400,'请填写 1–60 字的姓名，避免使用斜线')
        self.sync(force=True)
        if self.store.one('SELECT id FROM users WHERE username=? COLLATE NOCASE',(username,)):
            raise DriveError(409,'账号已经存在')
        if not 6<=len(password)<=200:raise DriveError(400,'密码至少 6 位即可')
        record=self.administrator.create(username,name,password,admin)
        record['_native_admin']=bool(admin)
        return self.upsert(record)

    def set_password(self, uid, password):
        user=self.store.one('SELECT seafile_identity FROM users WHERE id=?',(uid,))
        if user and user['seafile_identity']:
            self.administrator.update(user['seafile_identity'],{'password':password})

    def set_active(self, uid, active):
        user=self.store.one('SELECT seafile_identity FROM users WHERE id=?',(uid,))
        if user and user['seafile_identity']:
            self.administrator.update(user['seafile_identity'],{'is_active':bool(active)})

    def set_name(self, uid, name):
        user=self.store.one('SELECT seafile_identity FROM users WHERE id=?',(uid,))
        if user and user['seafile_identity']:
            if '/' in name:raise DriveError(400,'姓名不能包含斜线')
            self.administrator.update(user['seafile_identity'],{'name':name})


def configured_directory(store):
    path=os.environ.get('DRIVE_IDENTITY_CREDENTIAL_FILE')
    if not path:return None
    upstream=legacy_upstream()
    if upstream is None:raise ValueError('Unified identities require Seafile configuration')
    if os.name == 'nt':
        from .windows_storage import _dpapi

        credential = json.loads(_dpapi(Path(path).read_bytes(), False))
    else:
        # Linux deployments mount a private, read-only secret file into the service.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or info.st_uid not in (0, os.geteuid())):
                raise ValueError('Identity credentials must be a private file owned by the service or root')
            credential = json.load(source)
    merges_path=os.environ.get('DRIVE_IDENTITY_MERGES_FILE')
    merges=json.loads(Path(merges_path).read_text(encoding='utf-8-sig')) if merges_path else {}
    return IdentityDirectory(store,upstream,DirectoryAdmin(upstream,credential),merges)
