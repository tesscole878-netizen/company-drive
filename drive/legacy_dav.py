"""Expose an account's Seafile catalog as a WebDAV directory."""
from email.utils import formatdate, parsedate_to_datetime
import hashlib
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
from urllib.parse import unquote, urlparse

from wsgidav import util
from wsgidav.dav_error import DAVError
from wsgidav.dav_provider import DAVCollection, DAVNonCollection

from .legacy import legacy_path
from .store import DriveError, valid_name


MOUNT_NAME = '原 Seafile'
READ_ONLY = '原 Seafile 电脑盘支持保存到已有文件，请在原系统进行此操作'
MOUNT_PATTERN = re.compile(re.escape(MOUNT_NAME) + r'(?: \([0-9]+\))?', re.IGNORECASE)
OFFICE_TEMP = re.compile(r'(?:~\$[^/]+|~(?:WRD|WRL|WRA|DF)[^/]*\.tmp|[0-9a-f]{8}(?:\.tmp)?)', re.IGNORECASE)


def named_entries(entries):
    """Keep Windows-safe names and distinguish duplicates without changing their source paths."""
    names = {}
    occupied = set()
    for entry in entries:
        original = entry['name']
        label = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', '_', original).rstrip(' .')
        stem, dot, extension = label.rpartition('.')
        if not stem or len(extension) > 24:
            stem, dot, extension = label, '', ''
        suffix = dot + extension
        base = stem[:150].rstrip(' .') or '未命名'
        try:
            valid_name(base + suffix)
        except DriveError:
            base = '_' + base
        name = valid_name(base + suffix)
        index = 2
        while name.casefold() in occupied:
            qualifier = f' ({index}){suffix}'
            name = valid_name(base[:180 - len(qualifier)] + qualifier)
            index += 1
        names[name] = entry
        occupied.add(name.casefold())
    return names


class LegacyMount:
    def __init__(self, bindings, user, native_names, token, store, device_id):
        self.bindings = bindings
        self.user = user
        self.name = MOUNT_NAME
        occupied = {name.casefold() for name in native_names}
        index = 2
        while self.name.casefold() in occupied:
            self.name = f'{MOUNT_NAME} ({index})'
            index += 1
        self._catalog = None
        self._token = token
        self._directories = {}
        self.writeback = False
        self.store = store
        self.device_id = device_id

    @property
    def token(self):
        if self._token is None:
            self._token = self.bindings.token(self.user['id'])
        return self._token

    def matches(self, path):
        return path.strip('/').partition('/')[0].casefold() == self.name.casefold()

    def directory_alias(self, repo, path):
        library = next((alias for alias, entry in self.children(None, '/').items() if entry['id'] == repo), None)
        if library is None:
            return None
        aliases = [self.name, library]
        upstream = '/'
        for name in filter(None, legacy_path(path).split('/')):
            matches = self.children(repo, upstream)
            alias = next((alias for alias, entry in matches.items() if entry['is_dir'] and entry['name'] == name), None)
            if alias is None:
                return None
            aliases.append(alias)
            upstream = upstream.rstrip('/') + '/' + name
        return '/'.join(aliases)

    def children(self, repo, path):
        if repo is None:
            if self._catalog is None:
                libraries = self.bindings.upstream.catalog_libraries(self.token)
                accessible = {item['id']: item for item in libraries
                              if not item['encrypted'] and isinstance(item['name'], str)}
                self._catalog = named_entries(sorted(accessible.values(), key=lambda item: (item['name'].casefold(), item['id'])))
            return self._catalog
        key = (repo, path)
        if key not in self._directories:
            entries = self.bindings.upstream.dav_listing(self.token, repo, legacy_path(path))
            self._directories[key] = named_entries(sorted(entries, key=lambda item: (item['name'].casefold(), item['name'])))
        return self._directories[key]

    def resource(self, path, environ):
        _, _, tail = path.strip('/').partition('/')
        current = LegacyFolder('/' + self.name, environ, self, None, '/')
        if not tail:
            return current
        for part in tail.split('/'):
            if not isinstance(current, LegacyFolder):
                return None
            current = current.get_member(part)
        return current

    def staging(self, repo, parent):
        return OfficeStaging(self.store, self.device_id, repo, parent)

    def write_token(self):
        uid = self.user['id']
        if not self.store.one('SELECT 1 FROM tokens WHERE id=? AND user_id=?', (self.device_id, uid)):
            raise DriveError(403, '电脑连接已撤销，请重新登录')
        return self.bindings.token(uid)

    def read_version(self, repo, path):
        row = self.store.one('SELECT version FROM legacy_dav_reads WHERE device_id=? AND repo=? AND path=?',
                             (self.device_id, repo, path))
        return row['version'] if row else None

    def remember_version(self, repo, path, version):
        if not version:
            return
        with self.store.db() as db:
            db.execute('INSERT INTO legacy_dav_reads(device_id,repo,path,version) '
                       'SELECT ?,?,?,? WHERE EXISTS(SELECT 1 FROM tokens WHERE id=?) '
                       'ON CONFLICT(device_id,repo,path) DO UPDATE SET version=excluded.version',
                       (self.device_id, repo, path, version, self.device_id))


class OfficeStaging:
    """Store transient Office files separately for each authenticated device."""
    lifetime = 24 * 60 * 60

    def __init__(self, store, device_id, repo, parent):
        self.store = store
        key = hashlib.sha256(f'{device_id}\0{repo}\0{parent}'.encode()).hexdigest()
        self.directory = store.secure_staging / 'seafile-office' / key
        self.lock = 'seafile-office:' + key

    def path(self, name, kind):
        digest = hashlib.sha256(name.casefold().encode()).hexdigest()
        return self.directory / f'{digest}.{kind}'

    def put(self, name, body):
        with self.store.file_locks(self.lock):
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.prune()
            if len(list(self.directory.iterdir())) >= 256 and not self.path(name, 'file').exists():
                raise DAVError(507, '待处理的 Office 临时文件过多')
            body.replace(self.path(name, 'file'))

    def prune(self):
        cutoff = time.time() - self.lifetime
        for entry in self.directory.iterdir():
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)

    def has_pending_files(self):
        with self.store.file_locks(self.lock):
            if not self.directory.is_dir():
                return False
            self.prune()
            return any(self.directory.glob('*.file'))

    def mark_replacement(self, name, version):
        with self.store.file_locks(self.lock):
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.prune()
            if not any(path.stat().st_size > 0 for path in self.directory.glob('*.file')):
                raise DAVError(403, READ_ONLY)
            self.path(name, 'replace').write_text(version, encoding='ascii')

    def replacement(self, name, kind='replace'):
        marker = self.path(name, kind)
        try:
            if time.time() - marker.stat().st_mtime < self.lifetime:
                return marker.read_text(encoding='ascii')
        except FileNotFoundError:
            pass
        return None


class LegacyResource:
    def prevent_locking(self):
        return True

    def get_last_modified(self):
        return self.entry.get('modified')


class LegacyFolder(LegacyResource, DAVCollection):
    def __init__(self, path, environ, mount, repo, source, entry=None):
        super().__init__(path, environ)
        self.mount, self.repo, self.source = mount, repo, source
        self.entry = entry or {}

    def get_member_names(self):
        return list(self.mount.children(self.repo, self.source))

    def get_descendants(self, **options):
        # A recursive listing of the drive root can enumerate local folders, but
        # it must not crawl all of Seafile.
        if options.get('depth') == 'infinity':
            options['depth'] = '0'
        return super().get_descendants(**options)

    def get_member(self, name):
        children = self.mount.children(self.repo, self.source)
        entry = children.get(name)
        if entry is None:
            staged = self.staged_file(name)
            if staged and staged.is_file():
                return LegacyStagedFile(self, name, staged)
            return None
        path = self.path.rstrip('/') + '/' + name
        if self.repo is None:
            return LegacyFolder(path, self.environ, self.mount, entry['id'], '/', entry)
        source = self.source.rstrip('/') + '/' + entry['name']
        if entry['is_dir']:
            return LegacyFolder(path, self.environ, self.mount, self.repo, source, entry)
        return LegacyFile(path, self.environ, self.mount, self.repo, source, entry)

    def allows_new_entries(self):
        if not self.repo or not self.mount.writeback:
            return False
        if self.source == '/':
            details = self.mount.bindings.upstream._json(f'/api2/repos/{self.repo}/', self.mount.token)
            return details.get('permission') == 'rw'
        return self.entry.get('permission') == 'rw'

    def staged_file(self, name):
        if self.mount.writeback and self.repo and OFFICE_TEMP.fullmatch(name):
            return self.mount.staging(self.repo, self.source).path(name, 'file')
        return None

    def create_empty_resource(self, name):
        staged = self.staged_file(name)
        if staged:
            return LegacyStagedFile(self, name, staged)
        if not self.allows_new_entries():
            raise DAVError(403, READ_ONLY)
        name = valid_name(name)
        path = self.path.rstrip('/') + '/' + name
        return LegacyFile(path, self.environ, self.mount, self.repo, self.source.rstrip('/') + '/' + name,
                          {'name': name, 'size': 0, 'new': True})


class LegacyFile(LegacyResource, DAVNonCollection):
    def __init__(self, path, environ, mount, repo, source, entry):
        super().__init__(path, environ)
        self.mount, self.repo, self.source, self.entry = mount, repo, source, entry

    def get_content_length(self):
        return self.entry['size']

    def get_content_type(self):
        return 'application/octet-stream'

    def get_etag(self):
        return self.entry.get('object_id')

    def support_etag(self):
        return self.get_etag() is not None

    def support_ranges(self):
        return self.get_content_length() is not None

    def get_content(self):
        raise DAVError(403, READ_ONLY)

    def begin_write(self, *, content_type=None):
        try:
            if self.entry.get('new'):
                self.mount.bindings.upstream.folder_for_save(self.mount.write_token(), self.repo, str(PurePosixPath(self.source).parent))
                self._pending, self._writer = staged_writer(self.provider.store, self.environ)
                self._expected = None
                return self._writer
            self._expected = self.mount.bindings.upstream.file_for_update(self.mount.token, self.repo, self.source)
            if self.entry.get('locked_by_other'):
                raise DriveError(423, '原 Seafile 中此文件正由他人锁定')
            observed = self.mount.read_version(self.repo, self.source) or self.get_etag()
            if observed and self._expected != observed:
                raise DriveError(409, '此文件已更新，请重新打开后再编辑')
            self._pending, self._writer = staged_writer(self.provider.store, self.environ)
            return self._writer
        except DriveError as exc:
            raise DAVError(exc.status, exc.message) from exc

    def end_write(self, *, with_errors):
        pending = getattr(self, '_pending', None)
        if pending is None:
            return
        try:
            pending.close()
            if with_errors:
                return
            check_size(self.environ, self._writer.size)
            self.save_uploaded(Path(pending.name), self._expected)
        except DriveError as exc:
            raise DAVError(exc.status, exc.message) from exc
        finally:
            Path(pending.name).unlink(missing_ok=True)

    def save(self, source, expected):
        store = self.provider.store
        with store.file_locks('seafile:' + self.repo), source.open('rb') as content:
            token = self.mount.write_token()
            size = source.stat().st_size
            store.reject_empty_office_write(self.mount.user, self.repo, self.source, size,
                                            'webdav_' + self.environ['REQUEST_METHOD'].lower(),
                                            legacy=True, device_id=self.mount.device_id)
            if self.entry.get('new'):
                self.entry['object_id'] = self.mount.bindings.upstream.upload_new(token, self.repo, self.source, content, size)
            else:
                self.entry['object_id'] = self.mount.bindings.upstream.save_file(
                    token, self.repo, self.source, expected, content, size)
            self.mount.remember_version(self.repo, self.source, self.entry['object_id'])
            store.usage.record(self.mount.user, 'upload', 'legacy', self.repo, self.source)

    def save_uploaded(self, source, expected):
        parent = str(PurePosixPath(self.source).parent)
        staging = self.mount.staging(self.repo, parent)
        with self.provider.store.file_locks(staging.lock):
            if not self.entry.get('new') and staging.replacement(self.name, kind='deleted'):
                raise DriveError(409, '其他客户端已重新创建同名文件，请重新打开')
            version = staging.replacement(self.name) or expected
            self.save(source, version)
            staging.path(self.name, 'replace').unlink(missing_ok=True)
            staging.path(self.name, 'deleted').unlink(missing_ok=True)

    def stage_backup(self, parent, name):
        staging = self.mount.staging(self.repo, parent.source)
        upstream = self.mount.bindings.upstream
        token = self.mount.write_token()
        version = upstream.file_for_update(token, self.repo, self.source)
        if version != (self.mount.read_version(self.repo, self.source) or self.get_etag()):
            raise DAVError(409, '原文件已更新，请重新打开后再编辑')
        store = self.provider.store
        with tempfile.NamedTemporaryFile(dir=store.secure_staging, prefix='seafile-', delete=False) as backup:
            path = Path(backup.name)
            try:
                connection, response = upstream.download(token, self.repo, self.source)
                try:
                    size = 0
                    while chunk := response.read(256 * 1024):
                        size += len(chunk)
                        if size > store.max_file:
                            raise DAVError(413, '文件超过大小限制')
                        backup.write(chunk)
                finally:
                    connection.close()
                if size != self.get_content_length():
                    raise DAVError(409, '原文件大小已更新，请重新打开后再编辑')
                backup.close()
                with store.file_locks(staging.lock):
                    staging.put(name, path)
                    staging.mark_replacement(self.name, version)
            finally:
                backup.close()
                path.unlink(missing_ok=True)

    def serve(self, start_response):
        environ = self.environ
        size = self.get_content_length()
        modified = self.get_last_modified()
        etag = self.get_etag()
        util.evaluate_http_conditionals(self, int(modified or 0), etag, environ)
        range_header = environ.get('HTTP_RANGE', '')
        if_range = environ.get('HTTP_IF_RANGE')
        if if_range and (not etag or if_range != f'"{etag}"'):
            try:
                if modified is None or int(parsedate_to_datetime(if_range).timestamp()) != int(modified):
                    range_header = ''
            except (TypeError, ValueError, OverflowError, OSError):
                range_header = ''

        byte_range = None
        if size is not None and range_header.lower().startswith('bytes='):
            ranges, _ = util.obtain_content_ranges(range_header, size) if size and len(range_header) <= 128 else ([], 0)
            if not ranges or ranges[0][2] <= 0:
                raise DAVError(416, add_headers=[('Content-Range', f'bytes */{size}')])
            start, end, _ = ranges[0]
            byte_range = (start, end)
        partial = byte_range is not None
        headers = [('Content-Type', self.get_content_type()), ('Content-Disposition', 'attachment'),
                   ('Cache-Control', 'private, no-store'), ('Content-Security-Policy', 'sandbox')]
        if size is not None:
            headers.append(('Accept-Ranges', 'bytes'))
        if modified is not None:
            headers.append(('Last-Modified', formatdate(modified, usegmt=True)))
        if etag:
            headers.append(('ETag', f'"{etag}"'))
        if partial:
            expected_range = f'bytes {start}-{end}/{size}'
            headers.append(('Content-Range', expected_range))
        status = '206 Partial Content' if partial else '200 OK'
        if environ['REQUEST_METHOD'] == 'HEAD':
            if size is not None:
                headers.append(('Content-Length', str(end - start + 1 if partial else size)))
            start_response(status, headers)
            return

        connection, response = self.mount.bindings.upstream.download(
            self.mount.token, self.repo, legacy_path(self.source, allow_root=False), byte_range=byte_range)
        try:
            length = response.getheader('Content-Length')
            if partial and (response.status != 206 or response.getheader('Content-Range') != expected_range
                            or length != str(end - start + 1)):
                raise DAVError(502, '旧资料库未返回请求的文件片段，请刷新后重试')
            if length and length.isdecimal():
                headers.append(('Content-Length', length))
            self.mount.remember_version(self.repo, self.source, etag)
            environ['drive.download'] = ('legacy', self.repo, self.source)
            start_response(status, headers)
            while chunk := response.read(256 * 1024):
                yield chunk
        finally:
            connection.close()


class LegacyStagedFile(LegacyResource, DAVNonCollection):
    def __init__(self, parent, name, path):
        super().__init__(parent.path.rstrip('/') + '/' + name, parent.environ)
        self.parent, self.local = parent, path
        self.entry = {}

    def get_content_length(self):
        try:
            return self.local.stat().st_size
        except FileNotFoundError:
            return 0

    def get_etag(self):
        return None

    def support_etag(self):
        return False

    def support_ranges(self):
        return True

    def get_content(self):
        try:
            self.parent.mount.bindings.upstream.folder_for_save(self.parent.mount.token, self.parent.repo, self.parent.source)
        except DriveError as exc:
            raise DAVError(exc.status, exc.message) from exc
        return self.local.open('rb')

    def begin_write(self, *, content_type=None):
        mount = self.parent.mount
        try:
            mount.bindings.upstream.folder_for_save(mount.write_token(), self.parent.repo, self.parent.source)
        except DriveError as exc:
            raise DAVError(exc.status, exc.message) from exc
        self._pending, self._writer = staged_writer(self.provider.store, self.environ)
        return self._writer

    def end_write(self, *, with_errors):
        pending = getattr(self, '_pending', None)
        if pending is None:
            return
        path = Path(pending.name)
        try:
            pending.close()
            if not with_errors:
                check_size(self.environ, self._writer.size)
                self.parent.mount.staging(self.parent.repo, self.parent.source).put(self.name, path)
        finally:
            path.unlink(missing_ok=True)


def check_size(environ, size):
    length = environ.get('CONTENT_LENGTH', '')
    if length and int(length) != size:
        raise DAVError(400, '文件接收不完整，原文件已保留')


def destination_path(environ):
    raw = environ.get('HTTP_DESTINATION', '')
    try:
        target = urlparse(unquote(raw), allow_fragments=False).path
    except ValueError:
        return ''
    return util.normalize_path(target).removeprefix('/dav/')


def staged_writer(store, environ):
    from .dav import LimitedWriter

    length = environ.get('CONTENT_LENGTH', '')
    if length and int(length) > store.max_file:
        raise DAVError(413, '文件超过大小限制')
    pending = tempfile.NamedTemporaryFile(dir=store.secure_staging, prefix='seafile-', delete=False)
    return pending, LimitedWriter(pending, store.max_file)
