#!/usr/bin/env python3
"""Capture and verify a consistent copy of the new drive on the host backup disk."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from itertools import chain
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import time

BASE = Path('/opt/suyan-cloud-drive')
DATA = Path('/data/suyan-cloud-drive')
BACKUPS = BASE / 'backups' / 'daily'
SHANGHAI = timezone(timedelta(hours=8))
KEEP_BACKUPS = 2


def sources():
    return {
        'data/files': DATA / 'files', 'data/versions': DATA / 'versions', 'data/trash': DATA / 'trash',
        'state/server.key': BASE / 'state/server.key', 'config/compose.yaml': BASE / 'compose.yaml',
        'config/gateway.conf': BASE / 'gateway.conf', 'config/private': BASE / 'private', 'config/tls': BASE / 'tls',
    }


def tree_state(roots):
    state = {}
    for root in roots.values():
        for path in chain((root,), root.rglob('*') if root.is_dir() else ()):
            stat = path.lstat()
            state[path] = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return state


def verify_snapshot(directory):
    database = directory / 'state' / 'drive.sqlite3'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Backup database failed integrity validation')
        counts = {}
        for table, folder, query in (
            ('versions', 'versions', 'SELECT coalesce(b.blob_id,v.id) FROM file_versions v LEFT JOIN version_blobs b ON b.version_id=v.id'),
            ('trash', 'trash', 'SELECT id FROM trash'),
        ):
            ids = {row[0] for row in db.execute(query)}
            if any(not (directory / 'data' / folder / uid).exists() for uid in ids):
                raise RuntimeError(f'Backup has missing {table} objects')
            counts[table] = len(ids)
        counts['libraries'] = db.execute('SELECT COUNT(*) FROM libraries').fetchone()[0]
    if len((directory / 'state/server.key').read_bytes()) != 32:
        raise RuntimeError('Backup server key is missing or invalid')
    return counts


def capture(destination):
    roots = sources()
    database = BASE / 'state/drive.sqlite3'
    output = destination / 'archive.tmp'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=10)) as live:
        revision = live.execute('PRAGMA data_version').fetchone()[0]
        before = tree_state(roots)
        size = sum(info[2] for info in before.values()) + database.stat().st_size
        if shutil.disk_usage(destination).free < 5 * 1024**3 + 3 * size:
            raise RuntimeError('Insufficient backup disk space for archive and restore verification')
        snapshot = destination / 'drive.sqlite3'
        with closing(sqlite3.connect(snapshot)) as db:
            live.backup(db)
            # Pending data transfers are deliberately expired on a restored instance.
            db.execute('DELETE FROM public_uploads')
            db.execute('DELETE FROM uploads')
            db.commit()
            db.execute('PRAGMA journal_mode=DELETE')
        with tarfile.open(output, 'w:gz', compresslevel=3, format=tarfile.PAX_FORMAT) as archive:
            archive.add(snapshot, arcname='state/drive.sqlite3')
            for name, path in roots.items():
                archive.add(path, arcname=name)
        if before != tree_state(roots) or revision != live.execute('PRAGMA data_version').fetchone()[0]:
            raise RuntimeError('Source changed during backup')
    return output


def main():
    os.umask(0o077)
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    if BACKUPS.stat().st_dev == DATA.stat().st_dev:
        raise RuntimeError('Backup and data are on the same filesystem')
    if shutil.disk_usage(BACKUPS).free < 5 * 1024**3:
        raise RuntimeError('Backup disk has less than 5 GiB available')
    for attempt in range(3):
        with TemporaryDirectory(prefix='.capture-', dir=BACKUPS) as work:
            scratch = Path(work)
            try:
                output = capture(scratch)
            except (RuntimeError, FileNotFoundError):
                if attempt == 2:
                    raise
                time.sleep(2)
                continue
            restored = scratch / 'restore-check'
            restored.mkdir()
            subprocess.run(['tar', '-xzf', str(output), '-C', str(restored)], check=True)
            counts = verify_snapshot(restored)
            now = datetime.now(SHANGHAI)
            target = BACKUPS / now.strftime('suyan-new-drive-%Y%m%d-%H%M%S.tar.gz')
            output.replace(target)
            archives = sorted(p for p in BACKUPS.iterdir() if p.is_file() and not p.is_symlink()
                              and re.fullmatch(r'suyan-new-drive-[0-9]{8}-[0-9]{6}\.tar\.gz', p.name))
            for old in archives[:-KEEP_BACKUPS]:
                old.unlink()
            print(f'Verified backup {target.name}, bytes={target.stat().st_size}, {counts}')
            return


if __name__ == '__main__':
    main()
