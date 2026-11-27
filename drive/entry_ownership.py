"""Recover existing ownership only when historical records distinguish creation from editing."""
from pathlib import PurePosixPath

from .store import DriveError


def backfill_entry_owners(store):
    with store.db() as db:
        if db.execute("SELECT 1 FROM settings WHERE key='entry_ownership_v1'").fetchone():
            return
        # An overwrite archives the previous file before recording the upload.
        # A later archive does not invalidate an earlier creation event.
        first_archive = {(r['library_id'], r['path']): r['first_saved'] for r in db.execute(
            'SELECT library_id,path,MIN(created) AS first_saved FROM file_versions GROUP BY library_id,path')}
        libraries = {r['id'] for r in db.execute('SELECT id FROM libraries')}
        users = {r['id'] for r in db.execute('SELECT id FROM users')}
        inferred, uncertain = {}, set()
        for row in db.execute("SELECT group_id,path,action,user_id,created FROM events ORDER BY id"):
            lid, rel, action, uid = row['group_id'], row['path'], row['action'], row['user_id']
            if lid not in libraries or uid not in users or not rel:
                continue
            key = (lid, rel)
            if action in ('upload', 'copy', 'create', 'mkdir'):
                if action in ('upload', 'copy') and key in first_archive and first_archive[key] <= row['created']:
                    if key not in inferred:
                        uncertain.add(key)
                    continue
                if not any(affected_lid == lid and (not path or rel == path or rel.startswith(path + '/'))
                           for affected_lid, path in uncertain):
                    if key in inferred and inferred[key] != uid:
                        inferred.pop(key)
                        uncertain.add(key)
                    else:
                        inferred[key] = uid
            elif action in ('delete', 'restore', 'rename', 'move', 'snapshot_restore', 'version_restore'):
                if action == 'snapshot_restore':
                    uncertain.add((lid, ''))
                    for existing in [p for p in inferred if p[0] == lid]:
                        inferred.pop(existing)
                        uncertain.add(existing)
                    continue
                if action == 'rename' and ' → ' in rel:
                    old, name = rel.rsplit(' → ', 1)
                    destination = (PurePosixPath(old).parent / name).as_posix()
                    for affected in (old, destination):
                        uncertain.add((lid, affected))
                        for existing in [p for p in inferred if p[0] == lid and (p[1] == affected or p[1].startswith(affected + '/'))]:
                            inferred.pop(existing)
                            uncertain.add(existing)
                else:
                    uncertain.add(key)
                    for existing in [p for p in inferred if p[0] == lid and (p[1] == rel or p[1].startswith(rel + '/'))]:
                        inferred.pop(existing)
                        uncertain.add(existing)
        eligible = []
        for (lid, rel), owner in inferred.items():
            try:
                path = store.path(lid, rel)
                if path.is_file() or path.is_dir():
                    eligible.append((lid, rel, owner))
            except (OSError, ValueError, DriveError):
                continue
        db.executemany('INSERT OR IGNORE INTO entry_owners VALUES(?,?,?)', eligible)
        db.execute("INSERT INTO settings VALUES('entry_ownership_v1','complete')")
