#!/usr/bin/env python3
"""Create independently restorable Seafile snapshots with shared unchanged files between backups."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import json
import os
from pathlib import Path
import re
import signal
import http.client
import ssl
import shutil
import subprocess
import time
import threading
import uuid

ROOT = Path('/data/suyan-backups/seafile')
SOURCE = Path('/data/seafile-data')
ZONE = timezone(timedelta(hours=8))
PATTERN = re.compile(r'seafile-[0-9]{8}-[0-9]{6}')
DATABASES = ['ccnet_db', 'seafile_db', 'seahub_db']
RESERVE = 256 * 1024**3
RATE_KIB = 24 * 1024


class DeferBackup(Exception):
    """Keep the incomplete staging directory for the next low-traffic window."""


def window_limit(now):
    start = now.replace(hour=0, minute=30, second=0, microsecond=0)
    end = start.replace(hour=6, minute=0)
    return max(0, int((end-now).total_seconds())) if start <= now < end else 0


def disk_sample():
    dev = os.stat('/data').st_dev
    sysdev = Path('/sys/dev/block') / f'{os.major(dev)}:{os.minor(dev)}'
    stats = (sysdev / 'stat').read_text().split()
    return time.monotonic(), tuple(map(int, stats))


def disk_overloaded(previous, current):
    t0, a = previous
    t1, b = current
    operations = b[0] - a[0] + b[4] - a[4]
    busy = (b[9] - a[9]) / max((t1-t0)*1000, 1)
    wait = (b[3]-a[3]+b[7]-a[7]) / max(operations, 1)
    return busy >= .85 and wait >= 25


def services_responsive():
    for port, path in ((443,'/api2/ping/'), (8443,'/health')):
        # Probe status only. No credentials or data are exchanged.
        connection = http.client.HTTPSConnection('192.168.80.252', port, timeout=2,
                                                  context=ssl._create_unverified_context())
        started = time.monotonic()
        try:
            connection.request('GET',path)
            response = connection.getresponse()
            if response.status != 200 or time.monotonic()-started > 1.0:
                return False
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()
    return True


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def completed(root):
    return sorted(p for p in root.iterdir() if PATTERN.fullmatch(p.name) and not p.is_symlink()
                  and p.is_dir() and (p / 'verified.json').is_file())


def due(snapshots, today):
    if not snapshots:
        return True
    published = datetime.strptime(snapshots[-1].name, 'seafile-%Y%m%d-%H%M%S').date()
    return (today - published).days >= 3


def copy_data(source, stage, previous=None):
    command = ['rsync', '-a', '--numeric-ids', '--delete-delay', '--bwlimit=' + str(RATE_KIB),
               '--exclude=/logs/', '--exclude=/seafile/logs/', '--exclude=/seafile/seafile-data/httptemp/',
               '--exclude=/seafile/seafile-data/tmpfiles/']
    if previous:
        command += ['--link-dest=' + str(previous / 'shared')]
    dest = stage / 'shared'
    dest.mkdir(exist_ok=True)
    proc = subprocess.Popen(command + [str(source) + '/', str(dest) + '/'],start_new_session=True)
    try:
        while proc.poll() is None:
            if shutil.disk_usage(stage).free < RESERVE:
                raise DeferBackup('Data volume reserve reached')
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                continue
        if proc.returncode:
            raise RuntimeError('rsync exited with status ' + str(proc.returncode))
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait()


def watch_load(finished):
    previous = disk_sample()
    strikes = 0
    while not finished.wait(10):
        current = disk_sample()
        overloaded = disk_overloaded(previous, current) or not services_responsive()
        strikes = strikes + 1 if overloaded else 0
        previous = current
        if strikes >= 3 or shutil.disk_usage(ROOT).free < RESERVE:
            os.kill(os.getpid(), signal.SIGUSR1)
            return


def dump_databases(stage):
    # Credentials come from the database container's existing runtime settings.
    command = ['docker','exec','seafile-mysql','sh','-ec',
               'export MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-$MYSQL_ROOT_PASSWORD}"; '
               'exec mariadb-dump --user=root --single-transaction --quick --hex-blob --routines --events '
               '--databases ccnet_db seafile_db seahub_db']
    with gzip.open(stage / 'databases.sql.gz', 'wb', compresslevel=1) as output:
        with subprocess.Popen(command, stdout=subprocess.PIPE) as proc:
            shutil.copyfileobj(proc.stdout, output, length=1024**2)
            if proc.wait():
                raise RuntimeError('Database dump failed')


@contextmanager
def restore_database(stage):
    image = subprocess.check_output(['docker','inspect','-f','{{.Image}}','seafile-mysql'], text=True).strip()
    name = 'suyan-verify-db-' + uuid.uuid4().hex[:12]
    run(['docker','run','-d','--network','none','--memory','1g','--cpus','1',
         '--name',name,'-e','MARIADB_ALLOW_EMPTY_ROOT_PASSWORD=1',image],stdout=subprocess.DEVNULL)
    try:
        for _ in range(120):
            # The image runs a temporary server with networking disabled during initialization.
            if subprocess.run(['docker','exec',name,'mariadb-admin','--protocol=tcp','-h127.0.0.1','ping'],
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Isolated verification database did not start')
        client = ['docker','exec','-i',name,'mariadb','-uroot','--batch','--skip-column-names']
        with gzip.open(stage / 'databases.sql.gz', 'rb') as source, subprocess.Popen(client, stdin=subprocess.PIPE) as proc:
            shutil.copyfileobj(source, proc.stdin, length=1024**2)
            proc.stdin.close()
            if proc.wait():
                raise RuntimeError('Database restore failed')
        yield client
    finally:
        run(['docker','rm','-fv',name], stdout=subprocess.DEVNULL)


def verify(stage):
    with restore_database(stage) as client:
        counts = {}
        for db in DATABASES:
            sql = f"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='{db}'"
            counts[db] = int(subprocess.check_output(client + ['-e',sql]))
            if not counts[db]:
                raise RuntimeError('Missing database tables: ' + db)
        refs = subprocess.check_output(client + ['-e',
            'SELECT b.repo_id,b.commit_id FROM seafile_db.Branch b JOIN seafile_db.Repo r ON r.repo_id=b.repo_id '
            "WHERE b.name='master'"], text=True).splitlines()
        if not refs:
            raise RuntimeError('Restored database has no library heads')
        for line in refs:
            repo, head = line.split('\t')
            if not re.fullmatch(r'[0-9a-f-]{36}',repo) or not re.fullmatch(r'[0-9a-f]{40}',head):
                raise RuntimeError('Invalid library reference')
            obj = stage / 'shared/seafile/seafile-data/storage/commits' / repo / head[:2] / head[2:]
            if not obj.is_file():
                raise RuntimeError('Missing library head in backup: ' + repo)
        counts['library_heads'] = len(refs)
        return counts


def prune_completed():
    for old in completed(ROOT)[:-2]:
        if old.parent != ROOT or old.resolve() != old:
            raise RuntimeError('Unexpected snapshot path')
        old.rename(ROOT / ('.pruning-' + old.name))
    for entry in ROOT.iterdir():
        if entry.name.startswith('.pruning-') and PATTERN.fullmatch(entry.name[len('.pruning-'):]):
            if entry.is_dir() and not entry.is_symlink() and entry.resolve() == entry:
                shutil.rmtree(entry)


def main():
    os.umask(0o077)
    if not Path('/data').is_mount() or ROOT.resolve() != ROOT or SOURCE in ROOT.parents:
        raise RuntimeError('Unexpected Seafile backup mount or path')
    ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (ROOT / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        snapshots = completed(ROOT)
        pending_prune = len(snapshots) > 2 or any(ROOT.glob('.pruning-seafile-*'))
        if not pending_prune and not due(snapshots,datetime.now(ZONE).date()):
            print('Recent verified snapshot is less than three calendar days old')
            return
        if shutil.disk_usage(ROOT).free < RESERVE:
            raise RuntimeError('Insufficient free space')
        stage = ROOT / '.incoming'
        if stage.is_symlink():
            raise RuntimeError('Unexpected staging link')
        stage.mkdir(mode=0o700,exist_ok=True)
        now = datetime.now(ZONE)
        remaining = window_limit(now)
        if not remaining or not services_responsive():
            print('Deferred until a healthy low-traffic window')
            return
        def stop(signum, frame):
            raise DeferBackup('End of low-traffic window or service stop')
        signal.signal(signal.SIGALRM, stop)
        signal.signal(signal.SIGTERM, stop)
        def overloaded(signum, frame):
            raise DeferBackup('Disk latency, free space or service response outside low-load limits')
        signal.signal(signal.SIGUSR1, overloaded)
        signal.alarm(remaining)
        finished = threading.Event()
        threading.Thread(target=watch_load,args=(finished,),daemon=True).start()
        if pending_prune:
            prune_completed()
            snapshots = completed(ROOT)
            if not due(snapshots,now.date()):
                return
        started = time.monotonic()
        if not snapshots and not (stage / '.seed-complete').exists():
            copy_data(SOURCE,stage)
            (stage / '.seed-complete').touch()
        if shutil.disk_usage('/var/lib/docker').free < 16 * 1024**3:
            raise DeferBackup('Insufficient space for the isolated database restore')
        dump_databases(stage)
        copy_data(SOURCE, stage, snapshots[-1] if snapshots else None)
        config = Path('/home/hv5/docker-compose.yml')
        shutil.copy2(config,stage / 'docker-compose.yml')
        if config.with_name('.env').is_file():
            shutil.copy2(config.with_name('.env'),stage / 'compose.env')
        if not services_responsive():
            raise DeferBackup('Deferred restore check to protect service response time')
        counts = verify(stage)
        counts['elapsed_seconds'] = round(time.monotonic()-started)
        target = ROOT / datetime.now(ZONE).strftime('seafile-%Y%m%d-%H%M%S')
        (stage / 'verified.json').write_text(json.dumps(counts),encoding='utf8')
        stage.rename(target)
        print('Verified',target.name,counts,flush=True)
        prune_completed()
        finished.set()
        signal.alarm(0)


if __name__ == '__main__':
    try:
        main()
    except DeferBackup as reason:
        print('Backup deferred; verified snapshots retained:',reason)
