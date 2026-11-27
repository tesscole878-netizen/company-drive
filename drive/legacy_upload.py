"""Chunk uploads to Seafile while retaining the original path and write checks."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath
import hashlib
import hmac
import io
import logging
import os
import shutil
import time
import uuid

from fastapi import Body, Depends, Request
from starlette.concurrency import run_in_threadpool

from .legacy import legacy_path, object_id
from .store import DriveError, token_hash, valid_name
from .upload_integrity import fingerprint_matches, upload_digests, verified_fingerprint
from .upload_staging import MAX_CHUNK, StagedReader, append_frame, staging_cipher

ROOT = '/api/legacy/libraries/{repo}/uploads'
RETENTION = 86400
MIN_FREE = 2 * 1024**3
log = logging.getLogger(__name__)


def register_legacy_upload(app, store, user, bindings):
    upstream = bindings.upstream
    staging = store.tmp / 'legacy-upload'
    staging.mkdir(mode=0o700, exist_ok=True)
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='legacy-upload')
    app.state.close_legacy_uploads = executor.shutdown
    with store.db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS legacy_uploads (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session TEXT NOT NULL,
            repo TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
            fingerprint TEXT NOT NULL, expected TEXT NOT NULL, updated REAL NOT NULL,
            state TEXT NOT NULL DEFAULT 'receiving', version TEXT, error TEXT
        )''')
        columns = {item['name'] for item in db.execute('PRAGMA table_info(legacy_uploads)')}
        for column in ('received', 'stored'):
            if column not in columns:
                db.execute(f'ALTER TABLE legacy_uploads ADD COLUMN {column} INTEGER')
        db.execute("UPDATE legacy_uploads SET state='failed', error=? WHERE received IS NULL AND state IN ('receiving','committing')",
                   ('旧上传任务需要重新选择文件',))
        db.execute("UPDATE legacy_uploads SET state='retry', error=? WHERE state='committing'",
                   ('提交连接已中断，可重试写回旧盘',))

    def prune():
        for row in store.all("SELECT id FROM legacy_uploads WHERE updated<? AND state!='committing'",
                             (time.time() - RETENTION,)):
            with store.file_locks('legacy-upload:' + row['id']), store.db() as db:
                result = db.execute("DELETE FROM legacy_uploads WHERE id=? AND updated<? AND state!='committing'",
                                    (row['id'], time.time() - RETENTION))
                if result.rowcount:
                    (staging / row['id']).unlink(missing_ok=True)

    app.state.prune_legacy_uploads = prune

    def open_staging(row):
        return io.BufferedReader(StagedReader(staging / row['id'], staging_cipher(store.server_key, row['id']),
                                             row['received'], row['stored']))

    def owner_request(request):
        return token_hash(request.cookies.get('drive_session', ''))

    def task(repo, uid, request, current):
        row = store.one('SELECT * FROM legacy_uploads WHERE id=? AND repo=? AND user_id=?',
                        (uid, repo, current['id']))
        if row is None or not hmac.compare_digest(row['session'], owner_request(request)):
            raise DriveError(404, '旧库上传任务不存在')
        return row

    @app.post(ROOT, status_code=201)
    def begin(repo: str, request: Request, path: str = Body(), size: int = Body(ge=0),
              fingerprint: str = Body(min_length=1, max_length=240), expected: str = Body(''),
              current=Depends(user)):
        path = legacy_path(path, allow_root=False)
        token = app.state.authorize_legacy_edit(request, current, repo, allow_encrypted=True)
        if size > store.max_file:
            raise DriveError(413, '文件超过云盘大小限制')
        parent = str(PurePosixPath(path).parent)
        if expected:
            if not object_id(expected):
                raise DriveError(400, '文件版本无效')
            store.reject_empty_office_write(current, repo, path, size, 'legacy_web_upload', legacy=True)
        else:
            valid_name(PurePosixPath(path).name)
            upstream.folder_for_save(token, repo, parent)
        observed = upstream.file_for_update(token, repo, path) if expected else ''
        now = time.time()
        session = owner_request(request)
        with store.mutex, store.db() as db:
            row = db.execute('''SELECT id,state,received FROM legacy_uploads WHERE
                user_id=? AND session=? AND repo=? AND path=? AND size=? AND fingerprint=? AND expected=?
                AND received IS NOT NULL AND updated>? AND state IN ('receiving','committing','retry')
                ORDER BY updated DESC LIMIT 1''',
                             (current['id'], session, repo, path, size, fingerprint, expected, now - RETENTION)).fetchone() if verified_fingerprint(fingerprint) else None
            if row is not None and (staging / row['id']).exists():
                return {'id': row['id'], 'offset': row['received'], 'state': row['state']}
            count = db.execute("SELECT COUNT(*) FROM legacy_uploads WHERE user_id=? AND updated>? AND state IN ('receiving','committing','retry')",
                               (current['id'], now - RETENTION)).fetchone()[0]
            if count >= 4:
                raise DriveError(429, '请先完成或取消当前上传')
            if shutil.disk_usage(staging).free < size + MIN_FREE:
                raise DriveError(507, '服务器暂存空间不足')
            uid = uuid.uuid4().hex
            if expected and observed != expected:
                raise DriveError(409, '文件版本已变化，请重新打开后上传')
            fd = os.open(staging / uid, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            try:
                db.execute('''INSERT INTO legacy_uploads
                    (id,user_id,session,repo,path,size,fingerprint,expected,updated,received,stored) VALUES (?,?,?,?,?,?,?,?,?,0,0)''',
                           (uid, current['id'], session, repo, path, size, fingerprint, expected, now))
            except Exception:
                (staging / uid).unlink(missing_ok=True)
                raise
        return {'id': uid, 'offset': 0, 'state': 'receiving'}

    @app.get(ROOT + '/{uid}')
    def status(repo: str, uid: str, request: Request, current=Depends(user)):
        with store.file_locks('legacy-upload:' + uid):
            row = task(repo, uid, request, current)
            result = {key: row[key] for key in ('state', 'version', 'error')}
            if row['received'] is not None:
                result['offset'] = row['received']
            return result

    @app.patch(ROOT + '/{uid}')
    async def chunk(repo: str, uid: str, request: Request, current=Depends(user)):
        task(repo, uid, request, current)
        try:
            offset = int(request.headers.get('Upload-Offset', '-1'))
        except ValueError:
            raise DriveError(400, '上传偏移量无效') from None
        buffer = bytearray()
        async for part in request.stream():
            if len(buffer) + len(part) > MAX_CHUNK:
                raise DriveError(413, '每个分片最多 8 MiB')
            buffer.extend(part)

        def append():
            with store.file_locks('legacy-upload:' + uid):
                row = task(repo, uid, request, current)
                file = staging / uid
                actual = row['received']
                if row['state'] != 'receiving' or actual != offset or offset + len(buffer) > row['size']:
                    raise DriveError(409, '文件分片位置或状态已改变，请重新选择文件续传')
                if not buffer:
                    return {'offset': offset}
                if shutil.disk_usage(staging).free < MIN_FREE + len(buffer):
                    raise DriveError(507, '服务器暂存空间不足')
                stored = append_frame(file, row['stored'], offset, bytes(buffer), staging_cipher(store.server_key, uid))
                with store.db() as db:
                    db.execute('UPDATE legacy_uploads SET received=?,stored=?,updated=? WHERE id=?',
                               (offset + len(buffer), stored, time.time(), uid))
                return {'offset': offset + len(buffer)}

        return await run_in_threadpool(append)

    def reconcile(token, repo, path, expected, size, digest):
        try:
            current = upstream.file_detail(token, repo, path)
        except DriveError as error:
            if error.status == 404 and not expected:
                return None
            raise
        version = current['id']
        if version == expected:
            return None
        if current.get('size') == size:
            connection, response = upstream.download(token, repo, path, expected=version)
            try:
                check = hashlib.sha256()
                left = size
                while left and (block := response.read(min(left, 1024**2))):
                    check.update(block)
                    left -= len(block)
                if left == 0 and not response.read(1) and hmac.compare_digest(check.hexdigest(), digest):
                    return version
            finally:
                connection.close()
        raise DriveError(409, '旧盘文件已变化，请刷新目录确认后再操作')

    def commit(row):
        uid, repo, path, expected = (row[key] for key in ('id', 'repo', 'path', 'expected'))
        state, version, message = 'failed', None, '上传提交失败，请刷新目录核对'
        try:
            with store.file_locks('seafile:' + repo):
                current = store.get_user(row['user_id'])
                session = store.one('SELECT hash FROM sessions WHERE hash=? AND user_id=? AND expires>?',
                                    (row['session'], current['id'], time.time()))
                if not session:
                    raise DriveError(401, '登录会话已结束，请重新登录后上传')
                token = app.state.scoped_legacy_web_token(
                    current, repo, bytes.fromhex(row['session']), for_edit=True, allow_encrypted=True)
                store.license.require_write()
                with open_staging(row) as stream:
                    digest, measured = upload_digests(stream)
                if not fingerprint_matches(row['fingerprint'], measured):
                    raise DriveError(422, '上传内容校验失败，请重新选择文件')
                if row['state'] == 'retry':
                    version = reconcile(token, repo, path, expected, row['size'], digest)
                if not version:
                    with open_staging(row) as stream:
                        if expected:
                            version = upstream.save_file(token, repo, path, expected, stream, row['size'], allow_encrypted=True)
                        else:
                            version = upstream.upload_new(token, repo, path, stream, row['size'])
                store.usage.record(current, 'upload', 'legacy', repo, path)
                state, message = 'done', None
        except DriveError as exc:
            message = exc.message
            if exc.status in (429, 502, 503, 504) or (exc.status == 409 and row['state'] != 'retry'):
                state = 'retry'
        except Exception:
            log.exception('Seafile upload commit failed')
        finally:
            with store.file_locks('legacy-upload:' + uid):
                with store.db() as db:
                    db.execute('UPDATE legacy_uploads SET state=?,version=?,error=?,updated=? WHERE id=?',
                               (state, version, message, time.time(), uid))
                if state != 'retry':
                    (staging / uid).unlink(missing_ok=True)

    @app.post(ROOT + '/{uid}/complete', status_code=202)
    def complete(repo: str, uid: str, request: Request, current=Depends(user)):
        with store.file_locks('legacy-upload:' + uid):
            row = task(repo, uid, request, current)
            if row['state'] not in ('receiving','retry'):
                return {'state': row['state']}
            if row['received'] != row['size']:
                raise DriveError(409, '文件分片尚未传完')
            app.state.authorize_legacy_edit(request, current, repo, allow_encrypted=True)
            with store.db() as db:
                db.execute("UPDATE legacy_uploads SET state='committing',updated=? WHERE id=?", (time.time(), uid))
            executor.submit(commit, row)
        return {'state': 'committing'}

    @app.delete(ROOT + '/{uid}')
    def cancel(repo: str, uid: str, request: Request, current=Depends(user)):
        with store.file_locks('legacy-upload:' + uid):
            row = task(repo, uid, request, current)
            if row['state'] == 'committing':
                raise DriveError(409, '文件正在提交给旧 Seafile')
            (staging / uid).unlink(missing_ok=True)
            with store.db() as db:
                db.execute('DELETE FROM legacy_uploads WHERE id=?', (uid,))
        return {'ok': True}
