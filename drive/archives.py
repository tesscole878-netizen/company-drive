"""Bound temporary ZIP creation and keep finished downloads for a short time."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
import logging
from pathlib import Path
import shutil
from threading import BoundedSemaphore, Event, Lock, Timer
import uuid
import zipfile

from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .file_crypto import plain_size
from .store import DriveError, clean_path
from .usage import note_download

CHUNK = 1024 * 1024
RETENTION = 15 * 60
MIN_FREE = 256 * CHUNK


@dataclass
class ArchiveJob:
    user: dict
    library_id: str
    status: str = 'queued'
    bytes_done: int = 0
    total: int = 0
    files: list = field(default_factory=list)
    target: Path | None = None
    error: str | None = None
    downloading: int = 0
    downloaded: bool = False
    expired: bool = False
    cancel: Event = field(default_factory=Event)
    timer: Timer | None = None


class Archives:
    def __init__(self, store):
        self.store = store
        self.maximum = int(os.environ.get('DRIVE_ZIP_MAX_GB', '10')) * 1024**3
        self.jobs = {}
        self.lock = Lock()
        self.slots = BoundedSemaphore(2)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='drive-archive')
        for directory in {store.tmp, store.secure_staging}:
            for path in directory.glob('archive-task-*.zip'):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logging.getLogger(__name__).warning('Could not remove an interrupted archive')

    @contextmanager
    def slot(self):
        if not self.slots.acquire(blocking=False):
            raise DriveError(429, '正在处理其他打包任务，请稍后重试')
        try:
            yield
        finally:
            self.slots.release()

    def selection(self, user, lid, paths, check_cancel=lambda: None):
        if not 1 <= len(paths) <= 200:
            raise DriveError(400, '每次选择 1–200 个项目')
        user, catalog = self.store.library_grants(user, library_id=lid)
        if lid not in catalog:
            raise DriveError(403, '没有该位置的访问权限')
        library, grants = catalog[lid]
        if library['encryption']:
            self.store.library_key(user, lid)
        selected, seen, total = [], set(), 0
        for path in paths:
            check_cancel()
            rel = clean_path(path)
            if not self.store.role_for_path(grants, rel):
                raise DriveError(403, '没有该位置的访问权限')
            fp = self.store.path(lid, rel, True)
            candidates = [fp]
            while candidates:
                child = candidates.pop()
                check_cancel()
                if child.is_symlink() or child.is_junction():
                    continue
                child_rel = '' if child == self.store.files / lid else child.relative_to(self.store.files / lid).as_posix()
                if child_rel in seen:
                    continue
                self.store.path(lid, child_rel, True)
                if not self.store.role_for_path(grants, child_rel):
                    raise DriveError(403, '没有该位置的访问权限')
                seen.add(child_rel)
                is_dir = child.is_dir()
                if is_dir:
                    children = sorted((item for item in child.iterdir() if not item.is_symlink() and not item.is_junction()
                                       and (item.is_dir() or item.is_file())), reverse=True)
                    if children:
                        candidates.extend(children)
                        continue
                    if not child_rel:
                        continue
                else:
                    total += plain_size(child) if library['encryption'] else child.stat().st_size
                if total > self.maximum or len(selected) >= 10000:
                    raise DriveError(413, '批量下载超过大小或文件数限制，请使用电脑盘')
                selected.append((child_rel, is_dir))
        return selected, total

    def write(self, user, lid, files, total, target, progress=lambda _: None):
        if shutil.disk_usage(target.parent).free < total + MIN_FREE:
            raise DriveError(507, '临时存储空间不足，无法打包')
        done = 0
        check_space_at = 0
        with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as zipped:
            for rel, is_dir in files:
                progress(done)
                if is_dir:
                    self.store.access(user, lid, rel=rel)
                    if not self.store.path(lid, rel, True).is_dir():
                        raise DriveError(409, '打包目录已变化，请重新选择')
                    zipped.writestr(rel + '/', b'')
                    continue
                with self.store.read_content(user, lid, rel) as src, zipped.open(rel, 'w', force_zip64=True) as dst:
                    while chunk := src.read(CHUNK):
                        done += len(chunk)
                        if done > self.maximum:
                            raise DriveError(413, '文件大小已变化，超过打包限制')
                        if done >= check_space_at:
                            if shutil.disk_usage(target.parent).free < MIN_FREE:
                                raise DriveError(507, '临时存储空间不足，无法打包')
                            check_space_at = done + 32 * CHUNK
                        progress(done)
                        dst.write(chunk)

    def immediate(self, user, lid, paths):
        with self.slot():
            files, total = self.selection(user, lid, paths)
            target = self.store.staging_directory(lid) / ('archive-' + uuid.uuid4().hex + '.zip')
            try:
                self.write(user, lid, files, total, target)
            except Exception:
                target.unlink(missing_ok=True)
                raise
        return self.response(target, BackgroundTask(target.unlink, missing_ok=True))

    @staticmethod
    def response(target, cleanup):
        return FileResponse(target, filename='共享文件.zip', media_type='application/zip', background=cleanup)

    def start(self, user, lid, paths):
        if not 1 <= len(paths) <= 200:
            raise DriveError(400, '每次选择 1–200 个项目')
        paths = list(dict.fromkeys(clean_path(path) for path in paths))
        self.store.access(user, lid, rel=paths[0])
        with self.lock:
            if sum(job.user['id'] == user['id'] and not job.downloaded for job in self.jobs.values()) >= 2:
                raise DriveError(429, '打包任务已达上限，请下载或关闭已有任务后重试')
            if len(self.jobs) >= 8:
                disposable = next(((key, job) for key, job in self.jobs.items() if job.downloaded and not job.downloading), None)
                if disposable is None:
                    raise DriveError(429, '打包任务已达上限，请稍后重试')
                self._remove(*disposable)
            uid = uuid.uuid4().hex
            job = ArchiveJob(user, lid)
            self.jobs[uid] = job
            self.executor.submit(self._run, uid, job, paths)
        return {'id': uid}

    def _run(self, uid, job, paths):
        def check_cancel():
            if job.cancel.is_set():
                raise DriveError(409, '打包已取消')

        def progress(done):
            check_cancel()
            job.bytes_done = done

        try:
            with self.slot():
                check_cancel()
                job.status = 'scanning'
                job.files, job.total = self.selection(job.user, job.library_id, paths, check_cancel)
                job.status = 'packing'
                job.target = self.store.staging_directory(job.library_id) / ('archive-task-' + uid + '.zip')
                self.write(job.user, job.library_id, job.files, job.total, job.target, progress)
                check_cancel()
            with self.lock:
                check_cancel()
                job.status = 'ready'
        except DriveError as error:
            with self.lock:
                job.error = error.message
                job.status = 'failed'
        except Exception:
            logging.getLogger(__name__).exception('Archive creation failed')
            with self.lock:
                job.error = '打包失败，请重试'
                job.status = 'failed'
        finally:
            with self.lock:
                if self.jobs.get(uid) is not job:
                    return
                if job.cancel.is_set() and not job.downloading:
                    self._remove(uid, job)
                else:
                    if job.status != 'ready' and job.target:
                        try:
                            job.target.unlink(missing_ok=True)
                            job.target = None
                        except OSError:
                            logging.getLogger(__name__).warning('Could not remove a failed archive')
                    job.timer = Timer(RETENTION, self.expire, args=(uid,))
                    job.timer.daemon = True
                    job.timer.start()

    def _job(self, uid, user):
        job = self.jobs.get(uid)
        if not job or job.expired or job.user['id'] != user['id']:
            raise DriveError(404, '打包任务不存在或已过期')
        return job

    def status(self, uid, user):
        with self.lock:
            job = self._job(uid, user)
            return {'status': job.status, 'bytes_done': job.bytes_done, 'total': job.total, 'error': job.error}

    def _remove(self, uid, job):
        if job.timer:
            job.timer.cancel()
        try:
            if job.target:
                job.target.unlink(missing_ok=True)
        except OSError:
            logging.getLogger(__name__).warning('Could not remove a temporary archive')
        self.jobs.pop(uid, None)

    def expire(self, uid):
        with self.lock:
            job = self.jobs.get(uid)
            if job:
                job.expired = True
                if not job.downloading:
                    self._remove(uid, job)

    def cancel(self, uid, user):
        with self.lock:
            job = self._job(uid, user)
            job.cancel.set()
            if job.status in ('ready', 'failed') and not job.downloading:
                self._remove(uid, job)

    def download(self, uid, user, *, partial=False, request=None):
        with self.lock:
            job = self._job(uid, user)
            if job.status != 'ready' or job.cancel.is_set():
                raise DriveError(409, '文件还未打包完成')
            job.downloading += 1
        try:
            _, catalog = self.store.library_grants(user, library_id=job.library_id)
            entry = catalog.get(job.library_id)
            if not entry or any(not self.store.role_for_path(entry[1], rel) for rel, _ in job.files):
                raise DriveError(403, '打包内容的访问权限已变化，请重新选择文件')
            if entry[0]['encryption']:
                self.store.library_key(user, job.library_id)
            if request is not None:
                note_download(request, user, 'native', job.library_id, '批量打包')
            return self.response(job.target, BackgroundTask(self._download_complete, uid, job, not partial))
        except Exception:
            self._download_complete(uid, job)
            raise

    def _download_complete(self, uid, job, downloaded=False):
        with self.lock:
            job.downloading -= 1
            job.downloaded |= downloaded
            if not job.downloading and (job.expired or job.cancel.is_set()):
                self._remove(uid, job)

    def close(self):
        with self.lock:
            for job in self.jobs.values():
                job.cancel.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
        with self.lock:
            for uid, job in list(self.jobs.items()):
                self._remove(uid, job)
