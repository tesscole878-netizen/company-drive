"""Keep live path metadata separate from recycled entries and snapshot manifests."""

import json
import uuid


METADATA = {
    'file_tags': ('path', 'tag', 'color'),
    'comments': ('path', 'id', 'user_id', 'body', 'created'),
    'favorites': ('path', 'user_id', 'created'),
}
ACTIVE_VERSION = 'NOT EXISTS (SELECT 1 FROM detached_versions d WHERE d.version_id=file_versions.id)'


def path_scope(lid, path):
    return ('library_id=? AND (path=? OR substr(path,1,?)=?)', (lid, path, len(path) + 1, path + '/')) if path else ('library_id=?', (lid,))


def collaboration(db, lid, path=''):
    scope, args = path_scope(lid, path)
    return {table: [dict(row) for row in db.execute(f"SELECT {','.join(columns)} FROM {table} WHERE {scope}", args)]
            for table, columns in METADATA.items()}


def put_collaboration(db, lid, data, *, restore_snapshot=False):
    for table, rows in data.items():
        columns = METADATA[table]
        db.executemany(
            f"INSERT OR REPLACE INTO {table}(library_id,{','.join(columns)}) VALUES ({','.join('?' for _ in range(len(columns)+1))})",
            [(lid, *(uuid.uuid4().hex if column == 'id' and restore_snapshot else row[column] for column in columns))
             for row in rows],
        )


def detach_versions(db, lid, path='', trash_id=None):
    scope, args = path_scope(lid, path)
    db.execute(f"INSERT OR IGNORE INTO detached_versions SELECT id,? FROM file_versions WHERE {scope}", (trash_id, *args))


def revoke_shares(db, lid, path):
    scope, args = path_scope(lid, path)
    if not path:
        scope += " AND path!=''"
    db.execute(f"DELETE FROM library_shares WHERE {scope}", args)
    db.execute(f"UPDATE share_links SET active=0 WHERE {scope}", args)


def trash_metadata(db, tid, lid, path, deleted=None):
    scope, args = path_scope(lid, path)
    data = {}
    for table, columns in METADATA.items():
        condition = scope + (' AND created<=?' if deleted is not None and 'created' in columns else '')
        parameters = (*args, deleted) if condition != scope else args
        data[table] = [dict(row) for row in db.execute(f"SELECT {','.join(columns)} FROM {table} WHERE {condition}", parameters)]
        db.execute(f"DELETE FROM {table} WHERE {condition}", parameters)
    db.execute("INSERT INTO trash_metadata VALUES(?,?)", (tid, json.dumps(data, ensure_ascii=False)))
    if deleted is None:
        detach_versions(db, lid, path, tid)
        revoke_shares(db, lid, path)
    else:
        db.execute(f"INSERT OR IGNORE INTO detached_versions SELECT id,? FROM file_versions WHERE {scope} AND created<=?",
                   (tid, *args, deleted))


def restore_trash_metadata(db, tid, lid):
    row = db.execute("SELECT data FROM trash_metadata WHERE trash_id=?", (tid,)).fetchone()
    if row:
        put_collaboration(db, lid, json.loads(row['data']))
    db.execute("DELETE FROM detached_versions WHERE trash_id=?", (tid,))


def init_metadata(store):
    with store.db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS detached_versions (
                version_id TEXT PRIMARY KEY REFERENCES file_versions(id),
                trash_id TEXT);
            CREATE INDEX IF NOT EXISTS detached_versions_trash ON detached_versions(trash_id);
            CREATE TABLE IF NOT EXISTS trash_metadata (
                trash_id TEXT PRIMARY KEY REFERENCES trash(id) ON DELETE CASCADE,
                data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS version_blobs (
                version_id TEXT PRIMARY KEY REFERENCES file_versions(id),
                blob_id TEXT NOT NULL REFERENCES file_versions(id));
        """)
        if db.execute("SELECT 1 FROM settings WHERE key='file_metadata_v1'").fetchone():
            return
        for row in db.execute("SELECT * FROM trash ORDER BY deleted,id").fetchall():
            # The old store recorded file versions for a path even while it was in the trash.
            trash_metadata(db, row['id'], row['group_id'], row['path'], row['deleted'])
        db.execute("INSERT INTO settings VALUES ('file_metadata_v1','complete')")
