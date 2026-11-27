"""Authenticated, cursor-based file notifications for the existing Seahub site."""
import json
import posixpath
import re

from constance import config
from django.db import connection, transaction
from django.urls import path
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from seaserv import seafile_api
from seahub.api2.authentication import TokenAuthentication
from seahub.api2.endpoints.share_links import get_share_link_info
from seahub.api2.endpoints.upload_links import get_upload_link_info
from seahub.api2.throttling import UserRateThrottle
from seahub.base.templatetags.seahub_tags import email2nickname
from seahub.share.models import FileShare, UploadLinkShare, make_password
from seahub.urls import urlpatterns as original_urls
from seahub.utils import gen_token, get_password_strength_level, is_valid_password
from seahub.views import check_folder_permission

READABLE = {'r', 'rw', 'admin', 'preview', 'preview_edit', 'cloud_edit'}
PAGE = 40


def visible_path(username, repo_id, pathname, *, directory=False):
    if not pathname.startswith('/') or (pathname != '/' and any(
            p in ('', '.', '..') for p in pathname[1:].split('/'))):
        return None
    if seafile_api.check_permission(repo_id, username) not in READABLE:
        mapped = seafile_api.convert_repo_path(repo_id, pathname, username)
        if not mapped:
            return None
        entry = json.loads(mapped)
        repo_id, pathname = entry['repo_id'], entry['path']
    if seafile_api.check_permission(repo_id, username) not in READABLE:
        return None
    repo = seafile_api.get_repo(repo_id)
    if not repo or (repo.encrypted and not seafile_api.is_password_set(repo_id, username)):
        return None
    folder = '/'
    for part in filter(None, pathname.strip('/').split('/')):
        folder = posixpath.join(folder, part)
        if seafile_api.check_permission_by_path(repo_id, folder, username) not in READABLE:
            return None
    resolve = seafile_api.get_dir_id_by_path if directory else seafile_api.get_file_id_by_path
    if not resolve(repo_id, pathname):
        return None
    return repo_id, repo.name, pathname, bool(repo.encrypted)


class FileChanges(APIView):
    authentication_classes = (TokenAuthentication, SessionAuthentication)
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        try:
            after = int(request.query_params.get('after', '-1'))
            if after < -1 or after >= 2**63:
                raise ValueError
        except ValueError:
            return Response({'detail': '通知游标无效'}, status=400)
        with connection.cursor() as db:
            db.execute('SELECT COALESCE(MAX(id),0) FROM Activity')
            latest = db.fetchone()[0]
            rows = []
            if 0 <= after < latest:
                db.execute('SELECT id,repo_id,path,op_user,op_type,obj_type,detail FROM Activity '
                           'WHERE id>%s AND id<=%s ORDER BY id LIMIT %s', [after, latest, PAGE])
                rows = db.fetchall()
        items = []
        refreshed = set()
        for eid, rid, name, author, action, kind, detail in rows:
            if kind in ('dir', 'file') and (action in ('delete', 'rename', 'move') or (kind == 'dir' and action in ('create', 'recover'))):
                paths = [name]
                if action in ('rename', 'move'):
                    try:
                        metadata = json.loads(detail)
                    except (ValueError, TypeError):
                        metadata = {}
                    if isinstance(metadata, dict) and isinstance(metadata.get('old_path'), str):
                        paths.append(metadata['old_path'])
                for parent in dict.fromkeys(posixpath.dirname(path.rstrip('/')) or '/' for path in paths if path.startswith('/')):
                    entry = visible_path(request.user.username, rid, parent, directory=True)
                    if entry and entry not in refreshed:
                        refreshed.add(entry)
                        repo, library, pathname, encrypted = entry
                        items.append({'actor':'', 'library':library, 'path':pathname,
                                      'action':'refresh', 'repo':repo, 'encrypted':encrypted})
                continue
            if kind != 'file' or action not in ('create', 'edit', 'recover'):
                continue
            entry = visible_path(request.user.username, rid, name)
            if entry:
                repo, library, pathname, encrypted = entry
                items.append({'actor': email2nickname(author), 'library':library, 'path':pathname,
                              'action':action, 'repo':repo, 'encrypted':encrypted})
        cursor = rows[-1][0] if rows else latest
        response = Response({'cursor': str(cursor), 'more': cursor < latest, 'items':items})
        response['Cache-Control'] = 'private, no-store'
        return response


class EffectivePermissions(APIView):
    authentication_classes = (TokenAuthentication, SessionAuthentication)
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        repos = request.query_params.getlist('repo')
        if not 1 <= len(repos) <= 100 or any(
                not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', repo) for repo in repos):
            return Response({'detail': '资料库编号无效'}, status=400)
        result = {}
        for repo in dict.fromkeys(repos):
            permission = check_folder_permission(request, repo, '/')
            if permission:
                result[repo] = permission
        response = Response(result)
        response['Cache-Control'] = 'private, no-store'
        return response


class ChangeShareLinkPassword(APIView):
    authentication_classes = (TokenAuthentication, SessionAuthentication)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (UserRateThrottle,)

    def put(self, request, kind, token):
        if kind not in ('download', 'upload') or not re.fullmatch(r'[a-f0-9]{4,100}', token):
            return Response({'detail': '链接不存在'}, status=404)
        password = request.data.get('password')
        if (not isinstance(password, str) or len(password) > 64 or
                (not password and config.SHARE_LINK_FORCE_USE_PASSWORD) or
                (password and (len(password) < config.SHARE_LINK_PASSWORD_MIN_LENGTH or
                               not is_valid_password(password) or
                               get_password_strength_level(password) < config.SHARE_LINK_PASSWORD_STRENGTH_LEVEL))):
            return Response({'detail': '密码不符合 Seafile 的要求'}, status=400)
        model = FileShare if kind == 'download' else UploadLinkShare
        with transaction.atomic():
            share = model.objects.select_for_update().filter(token=token, username=request.user.username).first()
            if not share:
                return Response({'detail': '链接不存在或无权管理'}, status=404)
            repo = seafile_api.get_repo(share.repo_id)
            folder = posixpath.dirname(share.path) if kind == 'download' and share.s_type == 'f' else share.path
            permission = check_folder_permission(request, share.repo_id, folder or '/')
            if not repo or repo.encrypted or permission not in (('rw',) if kind == 'upload' else ('r', 'rw')):
                return Response({'detail': '没有此链接的管理权限'}, status=403)
            share.password = make_password(password) if password else None
            # Seahub remembers link unlocks by token. A new token retires
            # both the old URL and any browser unlocks tied to that URL.
            share.token = gen_token(max_length=config.SHARE_LINK_TOKEN_LENGTH)
            share.save(update_fields=['password', 'token'])
        item = get_share_link_info(share) if kind == 'download' else get_upload_link_info(share)
        item.pop('password', None)
        item['has_password'] = bool(password)
        response = Response(item)
        response['Cache-Control'] = 'private, no-store'
        return response


urlpatterns = [
    path('api/v2.1/suyan-file-changes/', FileChanges.as_view()),
    path('api/v2.1/suyan-effective-permissions/', EffectivePermissions.as_view()),
    path('api/v2.1/suyan-link-password/<str:kind>/<str:token>/', ChangeShareLinkPassword.as_view()),
] + list(original_urls)
