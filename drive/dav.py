"""WebDAV provider shares the same groups and filesystem as the web API."""
import os
import re
import shutil
import tempfile
import time
from contextvars import ContextVar
from functools import wraps
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlparse

from wsgidav.dav_error import DAVError
from wsgidav.dav_provider import DAVCollection, DAVNonCollection
from wsgidav.dc.base_dc import BaseDomainController
from wsgidav.fs_dav_provider import FilesystemProvider, FolderResource, FileResource
from wsgidav.wsgidav_app import WsgiDAVApp

from .file_crypto import plain_size
from .store import DriveError, OFFICE_FILE, clean_path, signature, valid_name
from .library_features import within
from .legacy_dav import (LegacyFile, LegacyFolder, LegacyMount, LegacyStagedFile, OfficeStaging,
                         MOUNT_PATTERN, OFFICE_TEMP, READ_ONLY, destination_path)

_dav_request = ContextVar("drive_dav_request", default=None)
NATIVE_OFFICE_TEMP = re.compile(r'.+~[0-9a-f]{6,12}\.tmp', re.IGNORECASE)


def request_roots(store, environ):
    if "drive.roots" not in environ:
        environ["drive.roots"] = store.dav_roots(environ["drive.user"])
    return environ["drive.roots"]

def read_catalog(store, environ):
    if "drive.read_catalog" not in environ:
        _, catalog = store.library_grants(environ["drive.user"])
        environ["drive.read_catalog"] = catalog
    return environ["drive.read_catalog"]

def move_entry(resource, dest_path):
    provider, environ = resource.provider, resource.environ
    group, rel, _ = provider.resolve(resource.path, environ, True)
    destination, dest, _ = provider.resolve(dest_path, environ, True)
    with provider.store.file_locks(group['id'], destination['id']):
        # The request server has validated the WebDAV locks and If headers.
        provider.store._transfer(environ['drive.user'], group['id'], [(rel, dest)], destination['id'], True, check_lock=False)
        if provider.prop_manager:
            dest_resource = provider.get_resource_inst(dest_path, environ)
            provider.prop_manager.move_properties(resource.get_ref_url(), dest_resource.get_ref_url(),
                                                  with_children=True, environ=environ)


def dav_errors(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DriveError as exc:
            raise DAVError(exc.status, exc.message) from exc
    return run


class DriveDomainController(BaseDomainController):
    def get_domain_realm(self, path_info, environ):
        return "Company Drive"

    def require_authentication(self, realm, environ):
        return True

    def supports_http_digest_auth(self):
        return False

    def basic_auth_user(self, realm, user_name, password, environ):
        store = self.config["drive_store"]
        user = store.dav_auth(user_name, password)
        if not user:
            return False
        directory = getattr(store, 'identity_directory', None)
        if directory:
            try:
                environ['drive.legacy_token'] = directory.authorize(user)
            except DriveError:
                return False
        if hasattr(store, "touch_device"):
            store.touch_device(password.split(".", 1)[0], environ.get("REMOTE_ADDR", ""))
        environ["drive.user"] = user
        environ['drive.device_id'] = password.split('.', 1)[0]
        return True


class RootCollection(DAVCollection):
    def get_member_names(self):
        names = [g["name"] for g in request_roots(self.provider.store, self.environ)]
        legacy = self.provider.legacy_mount(self.environ)
        return names + [legacy.name] if legacy else names

    def get_display_name(self):
        return "苏盐新云盘"

    def get_used_bytes(self):
        usage = self.storage_usage()
        return usage.total - usage.free

    def get_available_bytes(self):
        return self.storage_usage().free

    def storage_usage(self):
        if "drive.storage_usage" not in self.environ:
            self.environ["drive.storage_usage"] = shutil.disk_usage(self.provider.store.data)
        return self.environ["drive.storage_usage"]

    def get_member(self, name):
        return self.provider.get_resource_inst("/" + name, self.environ)


class DriveProvider(FilesystemProvider):
    def __init__(self, store, legacy_bindings=None):
        super().__init__(str(store.files), fs_opts={"follow_symlinks": False})
        self.store = store
        self.legacy_bindings = legacy_bindings
        self.legacy_writeback = os.environ.get('LEGACY_DAV_WRITEBACK', '1') == '1'
        self.blank = store.tmp / ".empty"
        self.blank.touch(exist_ok=True)

    def legacy_mount(self, environ):
        if 'drive.legacy_mount' not in environ:
            mount = None
            token = environ.get('drive.legacy_token')
            user = environ['drive.user']
            if self.legacy_bindings and (token or self.legacy_bindings.get(user['id'])):
                names = [root['name'] for root in request_roots(self.store, environ)]
                mount = LegacyMount(self.legacy_bindings, user, names, token, self.store, environ['drive.device_id'])
                mount.writeback = self.legacy_writeback
            environ['drive.legacy_mount'] = mount
        return environ['drive.legacy_mount']

    def legacy_for_path(self, path, environ):
        if MOUNT_PATTERN.fullmatch(path.strip('/').partition('/')[0]):
            mount = self.legacy_mount(environ)
            if mount and mount.matches(path):
                return mount
        return None

    def changed_directories(self, items, environ):
        from .legacy import legacy_path

        parents = {}
        for item in items:
            parent = item['path'] if item['action'] == 'refresh' else item['path'].rpartition('/')[0]
            parent = parent.rstrip('/')
            parents[item['source'], item['repo'], parent] = None
        if not parents:
            return []
        roots = request_roots(self.store, environ)
        result = set()
        for source, repo, parent in list(parents)[:32]:
            if source == 'native':
                for root in roots:
                    if root['id'] == repo and within(parent, root['prefix']):
                        suffix = parent[len(root['prefix']):].lstrip('/')
                        result.add('/'.join(filter(None, (root['name'], suffix))))
            else:
                try:
                    mount = self.legacy_mount(environ)
                    alias = mount.directory_alias(repo, legacy_path(parent or '/')) if mount else None
                except DriveError:
                    continue
                if alias:
                    result.add(alias)
        return sorted(result)

    def custom_request_handler(self, environ, start_response, default_handler):
        try:
            yield from self.serve_request(environ, start_response, default_handler)
        except DriveError as exc:
            raise DAVError(exc.status, exc.message) from exc

    def legacy_change(self, mount, resource, action, destination='', expected=None):
        from .legacy_coexist import change_entry
        if not isinstance(resource, (LegacyFile, LegacyFolder)) or not resource.repo or not resource.source.strip('/'):
            raise DAVError(403, READ_ONLY)
        if resource.entry.get('permission') != 'rw':
            raise DAVError(403, '原云盘没有此项目的写入权限')
        with self.store.file_locks('seafile:' + resource.repo):
            token = mount.write_token()
            if expected and expected != '*' and expected.strip('"') != resource.entry.get('object_id'):
                raise DAVError(412, '原文件已变化')
            return change_entry(mount.bindings.upstream, token, resource.repo, action, resource.source, destination)

    def serve_request(self, environ, start_response, default_handler):
        path, method = environ['PATH_INFO'], environ['REQUEST_METHOD']
        if method == 'GET' and not path.strip('/'):
            from urllib.parse import parse_qs
            query = parse_qs(environ.get('QUERY_STRING', ''), keep_blank_values=True)
            if query.get('file-changes') == ['1']:
                import json
                data = self.store.file_change_page(environ['drive.user'], query.get('cursor', [''])[0])
                if query.get('refresh') == ['1']:
                    data['refresh_dirs'] = self.changed_directories(data['items'], environ)
                data['items'] = [item for item in data['items'] if item['action'] != 'refresh']
                body = json.dumps(data, ensure_ascii=False).encode()
                start_response('200 OK', [('Content-Type','application/json; charset=utf-8'),
                                          ('Cache-Control','private, no-store'), ('Content-Length',str(len(body)))])
                yield body
                return
        mount = self.legacy_for_path(path, environ)
        target = destination_path(environ) if method in ('COPY', 'MOVE') else ''
        if target and self.legacy_for_path(target, environ):
            if method == 'MOVE' and mount and mount.writeback:
                status = self.replace_legacy_file(mount, path, target, environ)
                start_response(status, [('Content-Length', '0')])
                return
            raise DAVError(403, READ_ONLY)
        if mount:
            if not mount.writeback and method not in ('GET', 'HEAD', 'OPTIONS', 'PROPFIND'):
                raise DAVError(403, '原 Seafile 电脑盘当前为只读')
            if method == 'DELETE':
                self.delete_legacy_file(mount, path, environ)
                start_response('204 No Content', [('Content-Length', '0')])
                return
            if method == 'MKCOL':
                resource = mount.resource(path, environ)
                if isinstance(resource, LegacyFolder):
                    raise DAVError(405, '目录已存在')
                parent_path, _, name = path.rstrip('/').rpartition('/')
                parent = mount.resource(parent_path, environ)
                if resource or not isinstance(parent, LegacyFolder) or not parent.allows_new_entries():
                    raise DAVError(403, READ_ONLY)
                from .legacy_coexist import change_entry
                with self.store.file_locks('seafile:' + parent.repo):
                    result = change_entry(mount.bindings.upstream, mount.write_token(), parent.repo, 'mkdir',
                                          parent.source.rstrip('/') + '/' + valid_name(name))
                    if not isinstance(result, dict) or result.get('obj_name') != name:
                        raise DAVError(409, '目录名称已变化，请刷新')
                start_response('201 Created', [('Content-Length','0')])
                return
            if method not in ('GET', 'HEAD', 'OPTIONS', 'PROPFIND', 'PUT'):
                raise DAVError(403, READ_ONLY)
            if method == 'PROPFIND' and environ.get('HTTP_DEPTH', 'infinity') == 'infinity':
                raise DAVError(403, '请逐层浏览原 Seafile 目录')
            if method in ('GET', 'HEAD'):
                resource = mount.resource(path, environ)
                if isinstance(resource, LegacyFile):
                    yield from resource.serve(start_response)
                    return
            if method == 'PUT':
                resource = mount.resource(path, environ)
                parent = mount.resource(path.rsplit('/', 1)[0], environ)
                if resource is None and isinstance(parent, LegacyFolder):
                    parent.create_empty_resource(path.rsplit('/', 1)[1])
                elif not isinstance(resource, (LegacyFile, LegacyStagedFile)):
                    raise DAVError(403, READ_ONLY)
        if not mount and method in ('DELETE', 'MOVE', 'COPY'):
            yield from self.native_mutation(path, target, environ, start_response, default_handler)
            return
        yield from default_handler(environ, start_response)

    def native_mutation(self, path, target, environ, start_response, default_handler):
        method = environ['REQUEST_METHOD']
        user = environ['drive.user']
        if not path.strip('/'):
            raise DAVError(403, '不能删除或移走云盘根目录')
        library, rel, source = self.resolve(path, environ, write=method != 'COPY')
        lid = library['id']
        if not rel and method != 'COPY':
            raise DAVError(403, '不能删除或移走资料库根目录')
        if not target:
            if method != 'DELETE':
                yield from default_handler(environ, start_response)
                return
            with self.store.file_locks(lid):
                if source.exists() or not self.native_backup_path(lid, rel, source, environ):
                    self.store.require_remove_entries(user, lid, rel)
                yield from default_handler(environ, start_response)
            return
        dest_library, dest_rel, destination = self.resolve(target, environ, write=True)
        dest_lid = dest_library['id']
        if not dest_rel:
            raise DAVError(403, '不能覆盖资料库根目录')
        with self.store.file_locks(lid, dest_lid):
            if method == 'MOVE':
                try:
                    self.store.require_remove_entries(user, lid, rel)
                except DriveError as error:
                    if error.status != 403 or not self.stage_native_backup(lid, rel, source, dest_lid, dest_rel, destination, environ):
                        raise
                    start_response('201 Created', [('Content-Length', '0')])
                    return
            if destination.exists() and environ.get('HTTP_OVERWRITE', 'T') == 'T':
                # WsgiDAV may delete the destination before replacing it.
                if OFFICE_FILE.fullmatch(destination.name) and destination.is_file() and source.is_file():
                    size = plain_size(source) if library['encryption'] else source.stat().st_size
                    self.store.reject_empty_office_write(user, dest_lid, dest_rel, size, 'webdav_' + method.lower(),
                                                         device_id=environ['drive.device_id'])
                try:
                    self.store.require_remove_entries(user, dest_lid, dest_rel)
                except DriveError as error:
                    if error.status != 403 or method != 'MOVE' or not self.replace_native_office(lid, rel, source, dest_lid, dest_rel, destination, environ):
                        raise
                    start_response('204 No Content', [('Content-Length', '0')])
                    return
            yield from default_handler(environ, start_response)

    def native_staging(self, lid, rel, environ):
        return OfficeStaging(self.store, environ['drive.device_id'], 'native:' + lid, str(Path(rel).parent))

    def native_backup_path(self, lid, rel, path, environ):
        staging = self.native_staging(lid, rel, environ)
        cached = staging.path(path.name, 'file')
        try:
            if cached.is_file() and cached.stat().st_mtime > time.time() - staging.lifetime:
                return cached
        except FileNotFoundError:
            pass
        return None

    def valid_native_office_move(self, lid, rel, dest_lid, dest_rel, environ):
        url = urlparse(unquote(environ.get('HTTP_DESTINATION', '')), allow_fragments=False)
        host = environ.get('HTTP_HOST', '').casefold()
        schemes = (environ.get('wsgi.url_scheme'), environ.get('HTTP_X_FORWARDED_PROTO'))
        return (lid == dest_lid and rel != dest_rel and Path(rel).parent == Path(dest_rel).parent
                and (not url.netloc or url.netloc.casefold() == host) and (not url.scheme or url.scheme in schemes)
                and environ.get('HTTP_OVERWRITE', 'T') in ('T', 'F'))

    def stage_native_backup(self, lid, rel, source, dest_lid, dest_rel, destination, environ):
        if (not source.is_file() or destination.exists() or not OFFICE_FILE.fullmatch(source.name)
                or not (OFFICE_TEMP.fullmatch(destination.name) or NATIVE_OFFICE_TEMP.fullmatch(destination.name))
                or not self.valid_native_office_move(lid, rel, dest_lid, dest_rel, environ)):
            return False
        if source.stat().st_size > self.store.max_file:
            raise DriveError(413, '文件超过大小限制')
        self.store.check_unlocked(lid, rel)
        staging = self.native_staging(lid, rel, environ)
        version = signature(source)
        with tempfile.NamedTemporaryFile(dir=self.store.secure_staging, delete=False) as temporary:
            backup = Path(temporary.name)
        try:
            with self.store.read_content(environ['drive.user'], lid, rel) as content, backup.open('wb') as writable:
                shutil.copyfileobj(content, writable)
            staging.put(destination.name, backup)
            staging.mark_replacement(source.name, version)
        finally:
            backup.unlink(missing_ok=True)
        return True

    def replace_native_office(self, lid, rel, source, dest_lid, dest_rel, destination, environ):
        if (not source.is_file() or not destination.is_file() or not OFFICE_FILE.fullmatch(destination.name)
                or not (OFFICE_TEMP.fullmatch(source.name) or NATIVE_OFFICE_TEMP.fullmatch(source.name))
                or environ.get('HTTP_OVERWRITE') == 'F'
                or not self.valid_native_office_move(lid, rel, dest_lid, dest_rel, environ)):
            return False
        staging = self.native_staging(lid, rel, environ)
        self.store.check_unlocked(lid, rel)
        user = environ['drive.user']
        version = staging.replacement(destination.name)
        if version is None:
            return False
        self.store.copy_file(user, lid, rel, lid, dest_rel, version)
        self.store.delete(user, lid, rel)
        staging.path(destination.name, 'replace').unlink(missing_ok=True)
        return True

    def delete_legacy_file(self, mount, path, environ):
        resource = mount.resource(path, environ)
        if isinstance(resource, LegacyStagedFile):
            staging = mount.staging(resource.parent.repo, resource.parent.source)
            with self.store.file_locks(staging.lock):
                resource.local.unlink(missing_ok=True)
            return
        if isinstance(resource, LegacyFile) and OFFICE_FILE.fullmatch(resource.name):
            parent = mount.resource(path.rsplit('/', 1)[0], environ)
            staging = mount.staging(resource.repo, parent.source)
            with self.store.file_locks(staging.lock):
                if staging.has_pending_files():
                    previous = mount.bindings.upstream.file_for_update(mount.write_token(), resource.repo, resource.source)
                    observed = mount.read_version(resource.repo, resource.source) or resource.get_etag()
                    if observed and observed != previous:
                        raise DAVError(409, '原文件已更新，请重新打开')
                    staging.mark_replacement(resource.name, previous)
                    # Preserve the old version in Seafile's trash and never replace
                    # a different file created at this name by another client.
                    staging.path(resource.name, 'deleted').write_text(previous, encoding='ascii')
                    self.legacy_change(mount, resource, 'delete', expected=environ.get('HTTP_IF_MATCH'))
                    return
        self.legacy_change(mount, resource, 'delete', expected=environ.get('HTTP_IF_MATCH'))

    def replace_legacy_file(self, mount, path, target, environ):
        # Validate the authority before interpreting a WebDAV MOVE destination.
        url = urlparse(unquote(environ['HTTP_DESTINATION']), allow_fragments=False)
        if (url.netloc and url.netloc.casefold() != environ.get('HTTP_HOST', '').casefold()
                or url.scheme and url.scheme not in (environ.get('wsgi.url_scheme'), environ.get('HTTP_X_FORWARDED_PROTO'))):
            raise DAVError(403, READ_ONLY)
        source = mount.resource(path, environ)
        destination = mount.resource(target, environ)
        parent_path, _, name = target.strip('/').rpartition('/')
        if isinstance(source, LegacyFile) and destination is None and OFFICE_TEMP.fullmatch(name):
            parent = mount.resource(parent_path, environ)
            if (isinstance(parent, LegacyFolder) and parent.path == source.path.rsplit('/', 1)[0]
                    and OFFICE_FILE.fullmatch(source.name)):
                source.stage_backup(parent, name)
                return '201 Created'
        if isinstance(source, (LegacyFile, LegacyFolder)) and destination is None:
            parent = mount.resource(parent_path, environ)
            if (not isinstance(parent, LegacyFolder) or parent.repo != source.repo
                    or not parent.allows_new_entries()):
                raise DAVError(403, READ_ONLY)
            parent_source = str(PurePosixPath(source.source).parent)
            if parent_source == parent.source:
                self.legacy_change(mount, source, 'rename', valid_name(name), environ.get('HTTP_IF_MATCH'))
            elif name == source.name:
                self.legacy_change(mount, source, 'move', parent.source, environ.get('HTTP_IF_MATCH'))
            else:
                raise DAVError(409, '请先移动，再改名')
            return '201 Created'
        if isinstance(source, LegacyStagedFile) and destination is None:
            parent = mount.resource(parent_path, environ)
            if not isinstance(parent, LegacyFolder) or parent.path != source.parent.path or not OFFICE_FILE.fullmatch(name):
                raise DAVError(403, READ_ONLY)
            name = valid_name(name)
            staging = mount.staging(parent.repo, parent.source)
            with self.store.file_locks(staging.lock, 'seafile:' + parent.repo), source.local.open('rb') as content:
                size = source.local.stat().st_size
                self.store.reject_empty_office_write(mount.user, parent.repo, name, size, 'webdav_move',
                                                     legacy=True, device_id=mount.device_id)
                target_source = parent.source.rstrip('/') + '/' + name
                version = mount.bindings.upstream.upload_new(mount.write_token(), parent.repo, target_source, content, size)
                mount.remember_version(parent.repo, target_source, version)
                self.store.usage.record(mount.user, 'upload', 'legacy', parent.repo, target_source)
            source.local.unlink(missing_ok=True)
            staging.path(name, 'deleted').unlink(missing_ok=True)
            staging.path(name, 'replace').unlink(missing_ok=True)
            return '201 Created'
        if (not isinstance(source, LegacyStagedFile) or not isinstance(destination, LegacyFile)
                or source.parent.repo != destination.repo
                or destination.path.rsplit('/', 1)[0] != source.parent.path
                or not OFFICE_FILE.fullmatch(destination.name)):
            raise DAVError(403, READ_ONLY)
        if environ.get('HTTP_OVERWRITE') == 'F':
            raise DAVError(412, '目标文件已存在')
        staging = mount.staging(destination.repo, source.parent.source)
        with self.store.file_locks(staging.lock):
            if staging.replacement(destination.name, kind='deleted'):
                raise DAVError(409, '其他客户端已重新创建同名文件，请重新打开')
            version = staging.replacement(destination.name) or destination.get_etag()
            if not version:
                raise DAVError(409, '无法确认原文件版本，请重新打开后再保存')
            destination.save(source.local, version)
            source.local.unlink(missing_ok=True)
            staging.path(destination.name, 'replace').unlink(missing_ok=True)
        return '204 No Content'

    def resolve(self, path, environ, write=False):
        if self.legacy_for_path(path, environ):
            raise DriveError(403, READ_ONLY)
        rel = clean_path(path.strip("/"))
        if not rel:
            raise DriveError(403, "不能修改云盘根目录")
        name, _, tail = rel.partition("/")
        root = next((r for r in request_roots(self.store, environ) if r["name"] == name), None)
        if not root:
            # Do not confuse a known but unauthorized library with a missing path.
            known = self.store.one("SELECT id FROM libraries WHERE mount_name=? AND deleted IS NULL", (name,))
            if known:
                self.store.access(environ["drive.user"], known["id"], write, rel=tail)
            raise DriveError(403 if known else 404, "没有该资料库的访问权限" if known else "资料库不存在")
        tail = "/".join(n for n in (root["prefix"], tail) if n)
        if not write and environ.get("REQUEST_METHOD") == "PROPFIND":
            entry = read_catalog(self.store, environ).get(root["id"])
            if entry is None or not self.store.role_for_path(entry[1], tail):
                raise DriveError(403, "没有该位置的访问权限")
            group = entry[0]
            if group["encryption"]:
                self.store.library_key(environ["drive.user"], group["id"])
        else:
            group, _ = self.store.access(environ["drive.user"], root["id"], write, rel=tail)
        return group, tail, self.store.path(group["id"], tail)

    def ref_url_to_path(self, ref_url):
        path = unquote(ref_url)
        environ = _dav_request.get()
        if not path.startswith("/__library/") or environ is None:
            return super().ref_url_to_path(ref_url)
        lid, _, rel = path.removeprefix("/__library/").partition("/")
        rel = rel.rstrip("/")
        roots = request_roots(self.store, environ)
        for root in roots:
            if root["id"] == lid and within(rel, root["prefix"]):
                suffix = rel[len(root["prefix"]):].lstrip("/")
                return "/" + "/".join(filter(None, (root["name"], suffix)))
        # A lock may belong to a parent of an independently mounted folder.
        for root in roots:
            if root["id"] == lid and within(root["prefix"], rel):
                return "/" + root["name"]
        raise DAVError(403)

    @dav_errors
    def _loc_to_file_path(self, path, environ):
        return str(self.resolve(path, environ, write=True)[2])

    @dav_errors
    def get_resource_inst(self, path, environ):
        if not path.strip("/"):
            return RootCollection(path, environ)
        mount = self.legacy_for_path(path, environ)
        if mount:
            return mount.resource(path, environ)
        write = environ.get("REQUEST_METHOD") not in ("GET", "HEAD", "PROPFIND", "OPTIONS", "COPY", "UNLOCK")
        lib, rel, fp = self.resolve(path, environ, write=write)
        if not fp.exists():
            if rel and (OFFICE_TEMP.fullmatch(fp.name) or NATIVE_OFFICE_TEMP.fullmatch(fp.name)):
                cached = self.native_backup_path(lib['id'], rel, fp, environ)
                if cached:
                    return NativeOfficeBackup(path, environ, cached)
            return None
        if fp.is_dir():
            return DriveFolder(path, environ, str(fp), library=lib, rel=rel)
        return DriveFile(path, environ, str(fp), library=lib, rel=rel)


class DriveFolder(FolderResource):
    def __init__(self, path, environ, file_path, *, library=None, rel=None):
        group = library
        if group is None:
            group, rel, _ = environ["wsgidav.provider"].resolve(path, environ)
        self._identity = (group["id"], rel)
        super().__init__(path, environ, file_path)
        self.name = path.rstrip("/").rsplit("/", 1)[-1]

    def get_ref_url(self):
        return quote("/" + "/".join(filter(None, ("__library", *self._identity))) + "/")

    def get_member_names(self):
        if self.environ.get("REQUEST_METHOD") == "PROPFIND":
            # Enumeration only needs safe child names. The resource constructor
            # validates each requested child's path and request-scoped grants.
            names = []
            with os.scandir(self._file_path) as children:
                for child in children:
                    if child.is_symlink() or (os.name == 'nt' and child.is_junction()):
                        continue
                    try:
                        valid_name(child.name)
                        names.append(child.name)
                    except DriveError:
                        continue
            return names
        return [entry["name"] for entry in self.provider.store.listing(
            self.environ["drive.user"], *self._group_path())]

    def _group_path(self):
        g, rel, _ = self.provider.resolve(self.path, self.environ)
        return g["id"], rel

    @dav_errors
    def create_collection(self, name):
        valid_name(name)
        gid, rel = self._group_path()
        self.provider.store.mkdir(self.environ["drive.user"], gid, (rel + "/" + name).lstrip("/"))

    @dav_errors
    def create_empty_resource(self, name):
        valid_name(name)
        path = self.path.rstrip("/") + "/" + name
        _, _, fp = self.provider.resolve(path, self.environ, True)
        return DriveFile(path, self.environ, str(fp), new=True)

    @dav_errors
    def delete(self):
        self.provider.store.delete(self.environ["drive.user"], *self._group_path(), check_lock=False)
        self.remove_all_properties(recursive=True)
        self.remove_all_locks(recursive=True)

    @dav_errors
    def move_recursive(self, dest_path):
        move_entry(self, dest_path)

    def support_recursive_move(self, dest_path):
        return True

    @dav_errors
    def copy_move_single(self, dest_path, *, is_move):
        if not self._group_path()[1]:
            raise DriveError(403, "不能复制分组根目录")
        destination_id = self.provider.resolve(dest_path, self.environ, True)[0]["id"]
        with self.provider.store.file_locks(self._identity[0], destination_id):
            destination, rel, physical = self.provider.resolve(dest_path, self.environ, True)
            if not physical.exists():
                # Record the new directory owner as well as the copied files.
                self.provider.store.mkdir(self.environ['drive.user'], destination['id'], rel)
            super().copy_move_single(dest_path, is_move=is_move)


class LimitedWriter:
    def __init__(self, file, limit):
        self.file, self.limit, self.size = file, limit, 0

    def write(self, content):
        self.size += len(content)
        if self.size > self.limit:
            raise DAVError(413, "文件超过大小限制")
        return self.file.write(content)

    def close(self):
        if not self.file.closed:
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()


class NativeOfficeBackup(DAVNonCollection):
    def __init__(self, path, environ, file_path):
        super().__init__(path, environ)
        self.file_path = file_path

    def get_content_length(self):
        try:
            return self.file_path.stat().st_size
        except FileNotFoundError:
            return 0

    def get_content_type(self):
        return 'application/octet-stream'

    def get_content(self):
        return self.file_path.open('rb')

    def support_etag(self):
        return False

    def get_etag(self):
        return None

    def delete(self):
        self.file_path.unlink(missing_ok=True)


class DriveFile(FileResource):
    def __init__(self, path, environ, file_path, new=False, *, library=None, rel=None):
        group = library
        if group is None:
            group, rel, _ = environ["wsgidav.provider"].resolve(path, environ)
        self._identity = (group["id"], rel)
        self._encrypted = bool(group["encryption"])
        super().__init__(path, environ, str(environ["wsgidav.provider"].blank) if new else file_path)
        self._file_path = file_path
        self.name = path.rsplit("/", 1)[-1]
        self._writer = None
        self._temp = None

    def get_ref_url(self):
        return quote("/__library/" + self._identity[0] + "/" + self._identity[1])

    def get_content_length(self):
        if not Path(self._file_path).exists():
            return 0
        return self.provider.store.content_size(self._identity[0], self._file_path) if self._encrypted else self.file_stat.st_size

    @dav_errors
    def get_content(self):
        reader = self.provider.store.read_content(self.environ["drive.user"], *self._identity)
        if self.environ['REQUEST_METHOD'] == 'GET':
            self.environ['drive.download'] = ('native', *self._identity)
        return reader

    @dav_errors
    def begin_write(self, *, content_type=None):
        self.provider.resolve(self.path, self.environ, True)
        length = self.environ.get("CONTENT_LENGTH", "")
        if length and int(length) > self.provider.store.max_file:
            raise DriveError(413, "文件超过大小限制")
        self._signature = signature(Path(self._file_path))
        temp = tempfile.NamedTemporaryFile(dir=self.provider.store.staging_directory(self._identity[0]), prefix="dav-", delete=False)
        self._temp = Path(temp.name)
        self._writer = LimitedWriter(temp, self.provider.store.max_file)
        return self._writer

    @dav_errors
    def end_write(self, *, with_errors):
        try:
            if self._writer:
                self._writer.close()
            if with_errors or not self._temp:
                return
            length = self.environ.get("CONTENT_LENGTH", "")
            if length and int(length) != self._writer.size:
                raise DriveError(400, "文件接收不完整，原文件已保留")
            with self.provider.store.file_locks(self._identity[0]):
                g, rel, target = self.provider.resolve(self.path, self.environ, True)
                if signature(target) != self._signature:
                    raise DriveError(409, "文件已被其他人修改，请另存为新文件")
                staging = self.provider.native_staging(g['id'], rel, self.environ)
                expected = staging.replacement(target.name) or self._signature
                self.provider.store.commit_file(self.environ["drive.user"], g["id"], rel, self._temp, expected,
                                                check_lock=False, entry_point='webdav_put',
                                                device_id=self.environ.get('drive.device_id'))
                staging.path(target.name, 'replace').unlink(missing_ok=True)
                self.file_stat = target.stat()
        finally:
            if self._temp:
                self._temp.unlink(missing_ok=True)

    @dav_errors
    def delete(self):
        g, rel, _ = self.provider.resolve(self.path, self.environ, True)
        self.provider.store.delete(self.environ["drive.user"], g["id"], rel, check_lock=False)
        self.remove_all_properties(recursive=True)
        self.remove_all_locks(recursive=True)

    @dav_errors
    def move_recursive(self, dest_path):
        move_entry(self, dest_path)

    def support_recursive_move(self, dest_path):
        return True

    @dav_errors
    def copy_move_single(self, dest_path, *, is_move):
        destination_id = self.provider.resolve(dest_path, self.environ, True)[0]["id"]
        with self.provider.store.file_locks(self._identity[0], destination_id):
            source, source_rel, _ = self.provider.resolve(self.path, self.environ)
            group, rel, target = self.provider.resolve(dest_path, self.environ, True)
            self.provider.store.copy_file(self.environ["drive.user"], source["id"], source_rel, group["id"], rel, signature(target), check_lock=False)


def make_dav(store, legacy_bindings=None):
    if legacy_bindings:
        with store.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS legacy_dav_reads ('
                       'device_id TEXT REFERENCES tokens(id) ON DELETE CASCADE, '
                       'repo TEXT NOT NULL, path TEXT NOT NULL, version TEXT NOT NULL, '
                       'PRIMARY KEY(device_id,repo,path))')
    provider = DriveProvider(store, legacy_bindings)
    dav = WsgiDAVApp({
        "mount_path": "/dav",
        "provider_mapping": {"/": provider},
        "http_authenticator": {"domain_controller": DriveDomainController,
                               "accept_basic": True, "accept_digest": False, "default_to_digest": False},
        "drive_store": store,
        "verbose": 0,
        "logging": {"enable": False},
        "dir_browser": {"enable": False},
        "property_manager": True,
        "lock_storage": True,
    })

    def check(gid, rel):
        url = quote("/__library/" + gid + "/" + rel)
        urls = [url]
        while "/" in url.rstrip("/"):
            url = url.rstrip("/").rsplit("/", 1)[0] or "/"
            urls.append(url)
            if url == "/":
                break
        if any(provider.lock_manager.get_url_lock_list(u, recursive=(i == 0)) for i, u in enumerate(urls)):
            raise DriveError(423, "文件正在电脑盘中编辑，请关闭编辑程序后重试")

    store.lock_checker = check
    def serve(environ, start_response):
        token = _dav_request.set(environ)
        success = False

        def report(status, headers, exc_info=None):
            nonlocal success
            success = status.startswith(('200 ', '206 '))
            return start_response(status, headers, exc_info)

        try:
            if environ.get("REQUEST_METHOD") == "PROPFIND":
                with store.reuse_read_connection():
                    yield from dav(environ, start_response)
            else:
                yield from dav(environ, report)
            if success and 'drive.download' in environ and getattr(store, 'usage', None):
                store.usage.record(environ['drive.user'], 'download', *environ['drive.download'], merge=True)
        finally:
            _dav_request.reset(token)

    return serve
