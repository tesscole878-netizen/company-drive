"""Access Seafile libraries through the user's own account."""
import hashlib
import hmac
import http.client
import ipaddress
from io import BytesIO
import json
import math
import os
from datetime import datetime, timezone
from pathlib import PurePosixPath
import re
import secrets
import ssl
import time
from urllib.parse import quote, unquote, urlencode, urlsplit

from cryptography import x509
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Body, Depends, Request
from starlette.background import BackgroundTask
from fastapi.responses import Response, StreamingResponse

from .store import DriveError


def legacy_permission(item):
    value = item.get('permission')
    return value if isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', value) else None


def modified_at(item):
    for field in ('mtime', 'last_modified', 'last_modify'):
        value = item.get(field)
        if type(value) in (int, float) and 0 < value < 253402300800 and math.isfinite(value):
            return value
        if isinstance(value, str):
            try:
                stamp = datetime.fromisoformat(value)
                return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
            except (ValueError, OverflowError, OSError):
                continue
    return None


class OTPRequired(DriveError):
    code = 'seafile_otp_required'

    def __init__(self):
        super().__init__(422, '该账号已启用两步验证，请输入当前验证码')


class LegacyLibraryLocked(DriveError):
    code = 'legacy_library_locked'

    def __init__(self):
        super().__init__(423, '请输入原资料库密码解锁')


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, fingerprint):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(host, port, context=context, timeout=15)
        self.fingerprint = fingerprint

    def connect(self):
        super().connect()
        certificate = self.sock.getpeercert(binary_form=True)
        actual = hashlib.sha256(certificate).digest()
        if not hmac.compare_digest(actual, self.fingerprint):
            self.close()
            raise ssl.SSLError('Seafile certificate fingerprint mismatch')
        parsed = x509.load_der_x509_certificate(certificate)
        if not parsed.not_valid_before_utc <= datetime.now(timezone.utc) <= parsed.not_valid_after_utc:
            self.close()
            raise ssl.SSLError('Seafile certificate is outside its validity period')


class Seafile:
    def __init__(self, url, fingerprint):
        origin = urlsplit(url)
        if (origin.scheme != 'https' or not origin.hostname or origin.username is not None
                or origin.path not in ('', '/') or origin.query or origin.fragment):
            raise ValueError('LEGACY_SEAFILE_URL must be an HTTPS origin')
        if not re.fullmatch(r'[0-9a-fA-F]{64}', fingerprint):
            raise ValueError('LEGACY_SEAFILE_CERT_SHA256 must be a SHA-256 certificate fingerprint')
        self.origin = f'https://{origin.netloc.lower()}'
        self.host, self.port = origin.hostname, origin.port or 443
        self.fingerprint = bytes.fromhex(fingerprint)

    def _request(self, path, token=None, fields=None, otp=None, *, byte_range=None, unlock=False, method=None):
        connection = PinnedConnection(self.host, self.port, self.fingerprint)
        headers = {'Accept': 'application/json'}
        body = None
        if token:
            headers['Authorization'] = f'Bearer {token}'
        if fields is not None:
            body = urlencode(fields).encode('ascii')
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        if otp:
            headers['X-SEAFILE-OTP'] = otp
        if byte_range is not None:
            start, end = byte_range
            headers['Range'] = f'bytes={start}-{end}'
        try:
            connection.request(method or ('POST' if body is not None else 'GET'), path, body, headers)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException):
            connection.close()
            raise DriveError(502, '暂时无法连接旧资料库，请稍后重试') from None
        accepted = (200, 201, 204) if method else (200, 206) if byte_range is not None else (200,)
        if response.status not in accepted:
            status = response.status
            otp_required = response.getheader('X-Seafile-OTP') == 'required'
            connection.close()
            if status == 440:
                raise LegacyLibraryLocked()
            if unlock and status == 400:
                raise DriveError(422, '原资料库密码不正确，请重试')
            if method is None and fields is not None and not unlock and status in (400, 401, 403):
                if otp_required:
                    raise OTPRequired()
                raise DriveError(422, '旧账号验证失败，请检查账号、密码或验证码')
            if status in (401, 403):
                raise DriveError(403, '旧账号无权访问此处，或需要重新关联')
            if status == 404:
                raise DriveError(404, '旧资料库中的项目不存在或已移动')
            if status == 429:
                raise DriveError(429, '旧资料库暂时限制访问频率，请稍后重试')
            if method and status in (400, 409):
                raise DriveError(status, '原 Seafile 拒绝此操作，请检查名称和共享设置')
            if status == 416 and byte_range is not None:
                raise DriveError(416, '文件内容可能已更新，请刷新后重试')
            raise DriveError(502, '旧资料库返回了异常响应')
        return connection, response

    def _json(self, path, token=None, fields=None, otp=None, *, unlock=False, method=None):
        connection, response = self._request(path, token, fields, otp, unlock=unlock, method=method)
        try:
            raw = response.read(16 * 1024**2 + 1)
            if method and not raw:
                return None
            if len(raw) > 16 * 1024**2:
                raise DriveError(502, '旧目录内容过多，无法一次加载')
            return json.loads(raw)
        except (OSError, http.client.HTTPException, UnicodeError, ValueError):
            raise DriveError(502, '无法读取旧资料库的响应') from None
        finally:
            connection.close()

    def login_token(self, username, password, otp=None):
        value = self._json('/api2/auth-token/', fields={'username': username, 'password': password}, otp=otp)
        token = value.get('token') if isinstance(value, dict) else None
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,256}', token):
            raise DriveError(502, '旧资料库没有返回有效的账号凭据')
        return token

    def profile(self, token, username):
        account = self._json('/api2/account/info/', token)
        if not isinstance(account, dict):
            raise DriveError(502, '无法读取 Seafile 账号信息')
        identity = account.get('email') or account.get('login_id') or username
        return {'identity': identity, 'name': account.get('name') or account.get('login_id') or username}

    def authenticate(self, username, password, otp=None):
        token = self.login_token(username, password, otp)
        return self.profile(token, username)['identity'], token

    def libraries(self, token):
        return [{key: item[key] for key in ('id', 'name', 'encrypted', 'permission')}
                for item in self.catalog_libraries(token)]

    def catalog_libraries(self, token, view=None):
        source_view = {'mine': 'mine', 'shared': 'shared', 'public': 'org'}.get(view, 'mine,shared,group,org')
        items = self._json('/api2/repos/?type=' + source_view, token)
        if not isinstance(items, list):
            raise DriveError(502, '旧资料库返回了无法识别的列表')
        categories = {'repo': 'mine', 'srepo': 'shared', 'grepo': 'public'}
        libraries = []
        for item in items:
            if not isinstance(item, dict) or not valid_repo(item.get('id')):
                continue
            group_id = item.get('groupid')
            group_id = group_id if isinstance(group_id, int) and group_id > 0 else None
            libraries.append({'id': item['id'], 'name': item.get('name') or '未命名资料库',
                              'encrypted': bool(item.get('encrypted')),
                              'permission': legacy_permission(item),
                              'category': 'group' if group_id else categories.get(item.get('type'), 'shared'),
                              'group_id': group_id, 'owner': item.get('owner_name') or item.get('owner_nickname') or item.get('share_from_name') or '',
                              'size': item.get('size') if type(item.get('size')) is int and item['size'] >= 0 else None,
                              'modified': modified_at(item), 'source_url': f'{self.origin}/library/{item["id"]}/'})
        return libraries

    def groups(self, token):
        items = self._json('/api/v2.1/groups/', token)
        if not isinstance(items, list):
            raise DriveError(502, '无法读取 Seafile 群组列表')
        return [{'id': item['id'], 'name': item['name']} for item in items if isinstance(item, dict)
                and isinstance(item.get('id'), int) and isinstance(item.get('name'), str)]

    def with_effective_permissions(self, token, libraries):
        ids = list(dict.fromkeys(item['id'] for item in libraries))
        for start in range(0, len(ids), 100):
            batch = ids[start:start + 100]
            try:
                data = self._json('/api/v2.1/suyan-effective-permissions/?' + urlencode({'repo': batch}, doseq=True), token)
            except DriveError as error:
                if error.status in (404, 429, 502, 503):
                    continue
                raise
            if not isinstance(data, dict):
                raise DriveError(502, '旧资料库返回了无法识别的有效权限')
            for item in libraries:
                if item['id'] in batch:
                    item['effective_permission'] = legacy_permission({'permission': data.get(item['id'])})
        return [item for item in libraries
                if 'effective_permission' not in item or item['effective_permission'] is not None]

    def tool(self, token, name):
        endpoints = {
            'favorites': ('/api/v2.1/starred-items/', 'starred_item_list'),
            'events': ('/api/v2.1/activities/?page=1&per_page=25', 'events'),
            'published': ('/api/v2.1/wikis/', 'data'),
            'devices': ('/api2/devices/', None),
            'shared-libraries': ('/api/v2.1/shared-repos/', None),
            'shared-folders': ('/api/v2.1/shared-folders/', None),
            'download-links': ('/api/v2.1/share-links/', None),
            'upload-links': ('/api/v2.1/upload-links/', None),
        }
        if name not in endpoints:
            raise DriveError(404, '此工具不存在')
        endpoint, collection = endpoints[name]
        try:
            result = self._json(endpoint, token)
        except DriveError as error:
            if name == 'events' and error.status in (502, 404):
                raise DriveError(502, '原 Seafile 的文件活动接口暂时无法读取，请稍后重试') from None
            raise
        rows = result.get(collection) if collection and isinstance(result, dict) else result
        if not isinstance(rows, list):
            raise DriveError(502, 'Seafile 返回了无法识别的记录')
        # Upstream device records contain authentication keys, and share links
        # include passwords. Return only the fields displayed by this portal.
        allowed = ('repo_id','repo_name','path','is_dir','obj_name','name','deleted','mtime','time',
                   'author_name','op_type','obj_type','platform','device_name','client_version',
                   'last_accessed','last_login_ip','share_type','share_permission','user_name',
                   'group_name','link','view_cnt','ctime','expire_date','is_expired','slug')
        return [{key: item[key] for key in allowed if key in item} for item in rows if isinstance(item, dict)]

    def notifications(self, token, page):
        value = self._json('/api/v2.1/notifications/?' + urlencode({'page': page, 'per_page': 20}), token)
        if not isinstance(value, dict) or not isinstance(value.get('notification_list'), list):
            raise DriveError(502, '无法读取 Seafile 通知')
        notices = []
        fields = ('repo_id','repo_name','group_id','group_name','path','share_from_user_name','user_name',
                  'name','file_name','author_name','msg','comment','detail')
        for item in value['notification_list']:
            detail = item.get('detail')
            notices.append({'id': item.get('id'), 'type': item.get('type'), 'time': item.get('time'),
                            'seen': bool(item.get('seen')),
                            'detail': {key: detail[key] for key in fields if key in detail} if isinstance(detail, dict) else {}})
        return {'items': notices, 'unread': value.get('unseen_count', 0), 'total': value.get('count', 0)}

    def require_library(self, token, repo, *, allow_encrypted=False):
        if not valid_repo(repo):
            raise DriveError(404, '旧资料库不存在')
        library = next((item for item in self.libraries(token) if item['id'] == repo), None)
        if library is None:
            raise DriveError(403, '旧账号无权访问此资料库')
        if library['encrypted'] and not allow_encrypted:
            raise DriveError(403, '加密资料库请在 Seafile 中打开')
        return library

    def unlock_library(self, token, repo, password):
        reply = self._json(f'/api/v2.1/repos/{repo}/set-password/', token, {'password': password}, unlock=True)
        if not isinstance(reply, dict) or reply.get('success') is not True:
            raise DriveError(502, '旧资料库未确认解锁，请稍后重试')

    def listing(self, token, repo, path):
        return self._listing(token, repo, path, include_object_id=False)

    def dav_listing(self, token, repo, path):
        return self._listing(token, repo, path, include_object_id=True)

    def _listing(self, token, repo, path, *, include_object_id):
        items = self._json(f'/api2/repos/{repo}/dir/?' + urlencode({'p': path}), token)
        if not isinstance(items, list):
            raise DriveError(502, '旧资料库返回了无法识别的目录')
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get('name')
            if not isinstance(name, str) or not valid_segment(name) or item.get('type') not in ('dir', 'file'):
                continue
            size = item.get('size')
            entry = {'name': name, 'is_dir': item['type'] == 'dir',
                     'size': size if type(size) is int and size >= 0 else None,
                     'modified': modified_at(item)}
            permission = legacy_permission(item)
            if permission is not None:
                entry['permission'] = permission
            if include_object_id:
                entry['object_id'] = object_id(item.get('id'))
                entry['locked_by_other'] = bool(item.get('is_locked') and not item.get('locked_by_me'))
            result.append(entry)
        return sorted(result, key=lambda entry: (not entry['is_dir'], entry['name'].casefold()))

    def file_detail(self, token, repo, path):
        if not valid_repo(repo):
            raise DriveError(404, '原资料库不存在')
        path = legacy_path(path, allow_root=False)
        detail = self._json(f'/api2/repos/{repo}/file/detail/?' + urlencode({'p': path}), token)
        if not isinstance(detail, dict) or detail.get('type') != 'file' or not object_id(detail.get('id')):
            raise DriveError(502, '原 Seafile 未返回有效的文件版本')
        return detail

    def file_for_update(self, token, repo, path):
        detail = self.file_detail(token, repo, path)
        if detail.get('permission') != 'rw':
            raise DriveError(403, '你的 Seafile 账号没有此文件的编辑权限')
        return detail['id']

    def folder_for_save(self, token, repo, parent):
        if not valid_repo(repo):
            raise DriveError(404, '原资料库不存在')
        parent = legacy_path(parent)
        # For Office staging we need a fresh folder permission check. The
        # update token is discarded here and a new one is requested at commit.
        self._json(f'/api2/repos/{repo}/update-link/?' + urlencode({'p': parent}), token)

    def save_file(self, token, repo, path, expected, content, size, *, allow_encrypted=False):
        library = self._json(f'/api2/repos/{repo}/', token)
        head = object_id(library.get('head_commit_id')) if isinstance(library, dict) else None
        if not head:
            raise DriveError(502, '原 Seafile 未返回有效的资料库版本')
        if library.get('encrypted') and not allow_encrypted:
            raise DriveError(403, '请在原 Seafile 编辑加密资料库')
        if self.file_for_update(token, repo, path) != expected:
            raise DriveError(409, '此文件已更新，请重新打开后再编辑')
        parent = str(PurePosixPath(path).parent)
        current = next((entry for entry in self.dav_listing(token, repo, parent)
                        if entry['name'] == PurePosixPath(path).name and not entry['is_dir']), None)
        if current is None or current['object_id'] != expected:
            raise DriveError(409, '此文件已更新，请重新打开后再编辑')
        if current.get('permission', 'rw') != 'rw':
            raise DriveError(403, '你的 Seafile 账号没有此文件的编辑权限')
        if current['locked_by_other']:
            raise DriveError(423, '原 Seafile 中此文件正由他人锁定')
        link = self._json(f'/api2/repos/{repo}/update-link/?' + urlencode({'p': parent}), token)
        try:
            signed = urlsplit(link) if isinstance(link, str) else None
        except ValueError:
            signed = None
        if (signed is None or signed.scheme not in ('https', 'http') or not signed.hostname
                or not re.fullmatch(r'/seafhttp/update-api/[A-Za-z0-9_-]{12,128}/?', signed.path)):
            raise DriveError(502, '原 Seafile 返回了无效的文件更新地址')
        # Use only the signed path on the pinned server. The user's token is
        # used to request permission, and is never sent to the upload endpoint.
        endpoint = signed.path + '?' + urlencode({'head': head})
        boundary = 'Suyan' + secrets.token_hex(24)
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="target_file"\r\n\r\n{path}\r\n'
                  f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="content.bin"\r\n'
                  'Content-Type: application/octet-stream\r\n\r\n').encode()
        suffix = f'\r\n--{boundary}--\r\n'.encode()
        raw = self.send_multipart(endpoint, boundary, prefix, suffix, content, size)
        result = object_id(raw.decode('ascii', errors='replace').strip().strip('"'))
        if not result:
            raise DriveError(502, '原 Seafile 未确认保存，请检查上传状态')
        if self.file_for_update(token, repo, path) != result:
            raise DriveError(409, '原 Seafile 检测到版本冲突，请检查原资料库中的文件版本')
        return result

    def send_multipart(self, endpoint, boundary, prefix, suffix, content, size):
        connection = PinnedConnection(self.host, self.port, self.fingerprint)
        connection.timeout = 120
        try:
            connection.putrequest('POST', endpoint)
            connection.putheader('Content-Type', 'multipart/form-data; boundary=' + boundary)
            connection.putheader('Content-Length', str(len(prefix) + size + len(suffix)))
            connection.endheaders()
            connection.send(prefix)
            while chunk := content.read(256 * 1024):
                connection.send(chunk)
            connection.send(suffix)
            response = connection.getresponse()
            if response.status in (401, 403):
                raise DriveError(403, '原 Seafile 拒绝保存，请检查编辑权限')
            if response.status in (404, 409, 441):
                raise DriveError(409, '原文件已变更或移动，请重新打开')
            if response.status in (413, 442, 443, 507):
                raise DriveError(507, '原 Seafile 的文件大小或存储配额不足')
            result = response.read(8192)
            if response.status not in (200, 201):
                raise DriveError(502, '原 Seafile 未确认保存，请检查上传状态')
            return result
        except (OSError, http.client.HTTPException):
            raise DriveError(502, '保存连接中断，请检查文件版本和上传状态') from None
        finally:
            connection.close()

    def upload_new(self, token, repo, path, content, size):
        location = PurePosixPath(legacy_path(path, allow_root=False))
        from .store import valid_name
        name = valid_name(location.name)
        parent = str(location.parent)
        if any(entry['name'].casefold() == name.casefold() for entry in self.listing(token, repo, parent)):
            raise DriveError(409, '已存在同名项目，请重新选择名称')
        signed = self._json(f'/api2/repos/{repo}/upload-link/?' + urlencode({'p': parent}), token)
        parts = urlsplit(signed) if isinstance(signed, str) else None
        if not parts or not re.fullmatch(r'/seafhttp/upload-api/[A-Za-z0-9_-]{12,128}/?', parts.path):
            raise DriveError(502, '原云盘未返回文件上传地址')
        boundary = 'Suyan' + secrets.token_hex(24)
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="parent_dir"\r\n\r\n{parent}\r\n'
                  f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                  'Content-Type: application/octet-stream\r\n\r\n').encode()
        suffix = f'\r\n--{boundary}--\r\n'.encode()
        raw = self.send_multipart(parts.path + '?ret-json=1', boundary, prefix, suffix, content, size)
        try:
            result = json.loads(raw)[0]
            if not object_id(result.get('id')) or result.get('name') != name:
                raise DriveError(409, '原云盘返回了不同的文件名称或版本，请刷新目录核对')
            return result['id']
        except (ValueError, TypeError, IndexError, KeyError, AttributeError):
            raise DriveError(502, '原云盘未确认上传结果，请刷新目录核对') from None

    def download_url(self, token, repo, path, *, expected=None):
        endpoint = f'/api2/repos/{repo}/file/?' + urlencode({'p': path, 'op': 'download'})
        if expected is None:
            link = self._json(endpoint, token)
        else:
            connection, response = self._request(endpoint, token)
            try:
                if response.getheader('oid') != expected:
                    raise DriveError(409, '文件已更新，请重新打开')
                link = json.loads(response.read(16384))
            except (OSError, http.client.HTTPException, ValueError):
                raise DriveError(502, '无法读取原资料库的文件下载地址') from None
            finally:
                connection.close()
        if not isinstance(link, str):
            raise DriveError(502, '旧资料库没有返回有效的下载地址')
        return link

    def download(self, token, repo, path, *, byte_range=None, expected=None):
        file = PurePosixPath(path)
        # Seafile's directory API rejects preview-only folders for API clients.
        # Verify the file is listed there before requesting a download token.
        directory = self.listing(token, repo, str(file.parent))
        if not any(item['name'] == file.name and not item['is_dir'] for item in directory):
            raise DriveError(404, '旧资料库中的文件不存在或不可访问')
        link = self.download_url(token, repo, path, expected=expected)
        return self.follow_file_link(link, byte_range=byte_range)

    def follow_file_link(self, link, *, byte_range=None):
        if not isinstance(link, str):
            raise DriveError(502, '旧资料库没有返回有效的下载地址')
        try:
            signed = urlsplit(link)
        except ValueError:
            raise DriveError(502, '旧资料库返回了无效的下载地址') from None
        # Follow only the Seafile file-serving path on the pinned server, never a
        # supplied host or a redirect. The account token is not sent to this URL.
        if signed.scheme not in ('https', 'http') or not signed.hostname or signed.fragment:
            raise DriveError(502, '旧资料库返回了无效的下载地址')
        parts = signed.path.split('/')
        if (len(parts) != 5 or parts[:3] != ['', 'seafhttp', 'files']
                or not re.fullmatch(r'[a-zA-Z0-9_-]{12,128}', parts[3])
                or not valid_segment(unquote(parts[4]))):
            raise DriveError(502, '旧资料库返回了不支持的下载路径')
        endpoint = '/seafhttp/files/' + parts[3] + '/' + quote(unquote(parts[4]), safe='')
        return self._request(endpoint, byte_range=byte_range)

    def download_revision(self, token, repo, path, commit):
        endpoint = f'/api2/repos/{repo}/file/revision/?' + urlencode({'p': path, 'commit_id': commit})
        return self.follow_file_link(self._json(endpoint, token))


def valid_repo(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', value))


def object_id(value):
    return value if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value) else None


def valid_segment(value):
    return bool(value and value not in ('.', '..') and '/' not in value and '\\' not in value
                and all(ord(character) >= 32 for character in value))


def legacy_path(path, allow_root=True):
    if path == '/' and allow_root:
        return path
    if len(path) > 4000 or not path.startswith('/') or not all(valid_segment(part) for part in path[1:].split('/')):
        raise DriveError(400, '旧资料库路径无效')
    return path


def legacy_file_response(upstream, token, repo, path, *, expected=None, revision=None):
    path = legacy_path(path, allow_root=False)
    if revision is None:
        connection, response = upstream.download(token, repo, path, expected=expected)
    else:
        connection, response = upstream.download_revision(token, repo, path, revision)
    filename = quote(PurePosixPath(path).name, safe='')
    headers = {'Content-Disposition': f"attachment; filename*=UTF-8''{filename}"}
    length = response.getheader('Content-Length')
    if length and length.isdecimal():
        headers['Content-Length'] = length

    def chunks():
        try:
            while chunk := response.read(256 * 1024):
                yield chunk
        finally:
            connection.close()

    return StreamingResponse(chunks(), media_type='application/octet-stream', headers=headers,
                             background=BackgroundTask(connection.close))


class LegacyBindings:
    def __init__(self, store, upstream):
        self.store, self.upstream = store, upstream
        self.cipher = AESGCM(hmac.digest(store.server_key, b'suyan-seafile-binding/v1', 'sha256'))
        with store.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS legacy_seafile_bindings ('
                       'user_id TEXT PRIMARY KEY REFERENCES users(id), account TEXT NOT NULL, credential BLOB NOT NULL)')

    def _context(self, user_id):
        user = self.store.one('SELECT password FROM users WHERE id=? AND active=1', (user_id,))
        if not user:
            raise DriveError(401, '请重新登录')
        return self._credential_context(user_id, user['password'])

    def _credential_context(self, user_id, password_hash):
        # A cloud password reset must not let someone inherit a legacy login.
        return '\0'.join((self.upstream.origin, user_id, password_hash)).encode()

    def get(self, user_id):
        return self.store.one('SELECT account,credential FROM legacy_seafile_bindings WHERE user_id=?', (user_id,))

    def link(self, user_id, username, password, otp=None):
        account, token = self.upstream.authenticate(username, password, otp)
        return self.bind_token(user_id, account, token)

    def bind_token(self, user_id, account, token):
        expected=self.store.one('SELECT seafile_identity FROM users WHERE id=?',(user_id,))
        if expected and expected['seafile_identity'] and expected['seafile_identity']!=account:
            raise DriveError(403,'统一账号只能关联本人的 Seafile 身份')
        context = self._context(user_id)
        nonce = secrets.token_bytes(12)
        credential = nonce + self.cipher.encrypt(nonce, token.encode(), context)
        with self.store.db() as db:
            db.execute('INSERT OR REPLACE INTO legacy_seafile_bindings VALUES(?,?,?)', (user_id, account, credential))
        return account

    def token(self, user_id):
        row = self.store.one('SELECT u.active,u.password,b.credential FROM users u '
                             'LEFT JOIN legacy_seafile_bindings b ON b.user_id=u.id WHERE u.id=?', (user_id,))
        if not row:
            raise DriveError(409, '请先关联你的 Seafile 账号')
        if not row['active']:
            raise DriveError(401, '请重新登录')
        if not row['credential']:
            raise DriveError(409, '请先关联你的 Seafile 账号')
        return self.decrypt_token(user_id, row['credential'], row['password'])

    def decrypt_token(self, user_id, credential, password_hash):
        try:
            context = self._credential_context(user_id, password_hash)
            return self.cipher.decrypt(credential[:12], credential[12:], context).decode()
        except (InvalidTag, ValueError):
            raise DriveError(409, '旧账号的关联已失效，请重新关联') from None

    def unlink(self, user_id):
        with self.store.db() as db:
            db.execute('DELETE FROM legacy_seafile_bindings WHERE user_id=?', (user_id,))


def legacy_upstream():
    url = os.environ.get('LEGACY_SEAFILE_URL')
    return Seafile(url, os.environ.get('LEGACY_SEAFILE_CERT_SHA256', '')) if url else None


def pairing_allowed(request):
    if request.url.scheme == 'https':
        return True
    if request.url.hostname not in ('localhost', '127.0.0.1', '::1') or not request.client:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def register_legacy_routes(app, store, user):
    upstream = legacy_upstream()
    app.state.legacy_enabled = upstream is not None
    if upstream is None:
        return
    from .legacy_preview import OfficePreview, TEXT, preview_bytes, read_limited

    secret = os.environ.get('OFFICE_JWT_SECRET')
    office_preview = OfficePreview(secret=secret) if secret else None
    bindings = LegacyBindings(store, upstream)
    attempts = {}
    unlock_attempts = {}
    unlocked_libraries = {}
    # Seafile unlocks by original account. Keep a grant per web session or
    # device so other sessions cannot inherit that account's unlock.
    unlock_duration = 30 * 60

    @app.get('/api/legacy/binding')
    def binding(request: Request, current=Depends(user)):
        row = bindings.get(current['id'])
        return {'account': row['account'] if row else None, 'can_link_here': pairing_allowed(request) and current.get('identity_source')!='seafile'}

    @app.post('/api/legacy/binding')
    def link(request: Request, body: dict = Body(), current=Depends(user)):
        if not pairing_allowed(request):
            raise DriveError(403, '请通过 HTTPS 或服务器本机关联旧账号')
        username, password, otp = body.get('username'), body.get('password'), body.get('otp', '')
        if (not isinstance(username, str) or not 1 <= len(username.strip()) <= 254
                or not isinstance(password, str) or not 1 <= len(password) <= 400
                or not isinstance(otp, str) or not re.fullmatch(r'(?:[0-9]{6})?', otp)):
            raise DriveError(400, '请输入正确格式的旧账号、密码和验证码')
        uid = current['id']
        now = time.monotonic()
        with store.mutex:
            recent = [stamp for stamp in attempts.get(uid, []) if now - stamp < 300]
            if len(recent) >= 5:
                raise DriveError(429, '关联尝试过多，请 5 分钟后重试')
            attempts[uid] = recent + [now]
        account = bindings.link(uid, username.strip(), password, otp)
        with store.mutex:
            attempts.pop(uid, None)
        return {'account': account}

    @app.delete('/api/legacy/binding')
    def unlink(current=Depends(user)):
        if current.get('identity_source')=='seafile':raise DriveError(409,'统一账号登录会自动关联本人身份')
        bindings.unlink(current['id'])
        return {'ok': True}

    @app.get('/api/legacy/libraries')
    def libraries(view: str | None = None, effective: bool = False, current=Depends(user)):
        uid = current['id']
        if view is None:
            token = bindings.token(uid)
            items = upstream.libraries(token)
            return upstream.with_effective_permissions(token, items) if effective else items
        if view not in ('mine', 'shared', 'public'):
            raise DriveError(400, '无法识别的原资料库栏目')
        if bindings.get(uid) is None:
            return []
        token = bindings.token(uid)
        catalog = upstream.catalog_libraries(token, view)
        items = list({library['id']: library for library in catalog if library['category'] == view}.values())
        return upstream.with_effective_permissions(token, items) if effective else items

    @app.get('/api/legacy/group-libraries')
    def group_libraries(group_id: str | None = None, effective: bool = False, current=Depends(user)):
        token = bindings.token(current['id'])
        visible = {group['id'] for group in upstream.groups(token)}
        mappings = store.all('SELECT group_id, external_id FROM seafile_groups')
        group_ids = {row['external_id']: row['group_id'] for row in mappings
                     if row['external_id'] in visible and (group_id is None or row['group_id'] == group_id)}
        if group_id is not None and group_id not in group_ids.values():
            raise DriveError(403, '当前账号无权查看这个原群组')
        grouped = {native_id: {} for native_id in group_ids.values()}
        if grouped:
            items = [item for item in upstream.catalog_libraries(token)
                     if item['category'] == 'group' and item['group_id'] in group_ids]
            if effective:
                items = upstream.with_effective_permissions(token, items)
            for library in items:
                grouped[group_ids[library['group_id']]][library['id']] = library
        return {native_id: list(libraries.values()) for native_id, libraries in grouped.items()}

    def library_access(current, repo, scope):
        token = bindings.token(current['id'])
        library = upstream.require_library(token, repo, allow_encrypted=True)
        key = (current['id'], scope, hashlib.sha256(token.encode()).digest(), repo)
        return token, library, key

    def web_scope(request):
        return ('web', hashlib.sha256(request.cookies.get('drive_session', '').encode()).digest())

    def is_unlocked(key):
        with store.mutex:
            if unlocked_libraries.get(key, 0) > time.monotonic():
                return True
            unlocked_libraries.pop(key, None)
            return False

    def scoped_web_token(current, repo, session_hash, *, for_edit=False, allow_encrypted=False):
        token, library, key = library_access(current, repo, ('web', session_hash))
        if library['encrypted']:
            if not is_unlocked(key):
                raise LegacyLibraryLocked()
            if for_edit and not allow_encrypted:
                raise DriveError(403, '此编辑方式暂不支持原加密资料库，请上传新版本')
        return token

    def authorized_library(request, current, repo):
        return scoped_web_token(current, repo, web_scope(request)[1])

    def authorize_edit(request, current, repo, *, allow_encrypted=False):
        return scoped_web_token(current, repo, web_scope(request)[1], for_edit=True, allow_encrypted=allow_encrypted)

    app.state.authorize_legacy_edit = authorize_edit
    app.state.authorize_legacy_read = authorized_library
    app.state.scoped_legacy_web_token = scoped_web_token

    def unlock_scoped(current, repo, scope, body):
        password = body.get('password') if isinstance(body, dict) else None
        if not isinstance(password, str) or not 1 <= len(password) <= 400 or '\0' in password:
            raise DriveError(400, '请输入有效的原资料库密码')
        token, library, key = library_access(current, repo, scope)
        if not library['encrypted']:
            raise DriveError(400, '此资料库无需解锁')
        with store.mutex:
            now = time.monotonic()
            uid = current['id']
            recent = [stamp for stamp in unlock_attempts.get(uid, []) if stamp > now - 300]
            if len(recent) >= 8:
                raise DriveError(429, '尝试次数过多，请 5 分钟后重试')
            unlock_attempts[uid] = recent + [now]
        upstream.unlock_library(token, repo, password)
        now = time.monotonic()
        with store.mutex:
            unlock_attempts.pop(uid, None)
            for previous in list(unlocked_libraries):
                if unlocked_libraries[previous] <= now:
                    del unlocked_libraries[previous]
            if len(unlocked_libraries) >= 4096 and key not in unlocked_libraries:
                unlocked_libraries.pop(next(iter(unlocked_libraries)))
            unlocked_libraries[key] = now + unlock_duration
        return {'ok': True}

    @app.post('/api/legacy/libraries/{repo}/unlock')
    def unlock(repo: str, request: Request, body: object = Body(), current=Depends(user)):
        if not pairing_allowed(request):
            raise DriveError(403, '请通过 HTTPS 或服务器本机解锁旧资料库')
        return unlock_scoped(current, repo, web_scope(request), body)

    def relock_scope(uid, scope):
        with store.mutex:
            for key in list(unlocked_libraries):
                if key[:2] == (uid, scope):
                    unlocked_libraries.pop(key, None)

    from .legacy_device import register_encrypted_library_reader
    register_encrypted_library_reader(app, store, bindings, library_access, is_unlocked, unlock_scoped, relock_scope)

    @app.get('/api/legacy/libraries/{repo}/files')
    def files(repo: str, request: Request, path: str = '/', current=Depends(user)):
        return upstream.listing(authorized_library(request, current, repo), repo, legacy_path(path))

    @app.get('/api/legacy/libraries/{repo}/download-access')
    def download_access(repo: str, request: Request, path: str, current=Depends(user)):
        path = legacy_path(path, allow_root=False)
        token = authorized_library(request, current, repo)
        upstream.download_url(token, repo, path)
        return {'ok': True}

    @app.get('/api/legacy/libraries/{repo}/download')
    def download(repo: str, request: Request, path: str, current=Depends(user)):
        from .usage import note_download
        note_download(request, current, 'legacy', repo, path)
        return legacy_file_response(upstream, authorized_library(request, current, repo), repo, path)

    @app.get('/api/legacy/libraries/{repo}/preview')
    def preview(repo: str, request: Request, path: str, current=Depends(user)):
        token = authorized_library(request, current, repo)
        data, media_type = preview_bytes(upstream, token, repo, legacy_path(path, allow_root=False), office_preview)
        return Response(data, media_type=media_type, headers={'Content-Security-Policy': "default-src 'none'; sandbox"})

    def read_editable_text(token, repo, path, version):
        connection, response = upstream.download(token, repo, path, expected=version)
        try:
            raw = read_limited(response, 2 * 1024**2)
        finally:
            connection.close()
        utf8 = 'utf-8-sig' if raw.startswith(b'\xef\xbb\xbf') else 'utf-8'
        for encoding in (utf8, 'gb18030'):
            try:
                content = raw.decode(encoding)
                if '\0' not in content:
                    return content, encoding
            except UnicodeDecodeError:
                continue
        raise DriveError(415, '此文件的编码不支持在线编辑，请在电脑盘打开')

    @app.get('/api/legacy/libraries/{repo}/text')
    def text(repo: str, request: Request, path: str, current=Depends(user)):
        path = legacy_path(path, allow_root=False)
        if PurePosixPath(path).suffix.lower().lstrip('.') not in TEXT:
            raise DriveError(415, '此格式不支持文本编辑')
        store.license.require_write()
        token = authorize_edit(request, current, repo, allow_encrypted=True)
        version = upstream.file_for_update(token, repo, path)
        content, _ = read_editable_text(token, repo, path, version)
        return {'content': content, 'version': version}

    @app.put('/api/legacy/libraries/{repo}/text')
    def save_text(repo: str, request: Request, path: str = Body(), content: str = Body(max_length=2 * 1024**2),
                  version: str = Body(), current=Depends(user)):
        path = legacy_path(path, allow_root=False)
        if PurePosixPath(path).suffix.lower().lstrip('.') not in TEXT or not object_id(version) or '\0' in content:
            raise DriveError(400, '文件格式或版本无效，请重新打开')
        token = authorize_edit(request, current, repo, allow_encrypted=True)
        # Preserve the source's encoding and line endings when saving text.
        with store.file_locks('seafile:' + repo):
            if upstream.file_for_update(token, repo, path) != version:
                raise DriveError(409, '文件已更新，请重新打开')
            original, encoding = read_editable_text(token, repo, path, version)
            if '\r\n' in original and '\n' not in original.replace('\r\n', ''):
                content = content.replace('\r\n', '\n').replace('\n', '\r\n')
            try:
                data = content.encode(encoding)
            except UnicodeEncodeError:
                raise DriveError(400, '输入内容包含原文件编码无法保存的字符') from None
            if len(data) > 2 * 1024**2:
                raise DriveError(413, '文本编辑最多支持 2 MB')
            store.license.require_write()
            return {'version': upstream.save_file(token, repo, path, version, BytesIO(data), len(data),
                                                  allow_encrypted=True)}

    from .legacy_management import register_legacy_management

    from .legacy_coexist import register_coexist
    register_coexist(app, store, user, bindings)
    register_legacy_management(app, store, user, bindings)
    from .legacy_advanced import register_legacy_advanced
    from .legacy_upload import register_legacy_upload
    register_legacy_advanced(app, store, user, bindings)
    register_legacy_upload(app, store, user, bindings)
    return bindings
