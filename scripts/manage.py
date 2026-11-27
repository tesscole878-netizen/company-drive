"""Local server administration. Passwords are entered without echo."""
import argparse
import getpass
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drive.store import password_hash
from drive.workspace import WorkspaceStore
from drive.storage_config import configure_storage

if '--data' not in sys.argv:
    configure_storage(Path(__file__).resolve().parents[1])

parser = argparse.ArgumentParser()
parser.add_argument('command', choices=['create-admin', 'reset-password', 'cleanup-uploads', 'export-database'])
parser.add_argument('--data', default=os.environ.get('DRIVE_DATA_DIR', 'data'))
parser.add_argument('--state', default=os.environ.get('DRIVE_STATE_DIR'))
parser.add_argument('--username')
parser.add_argument('--output', help='Destination of export-database (must not exist)')
args = parser.parse_args()
store = WorkspaceStore(args.data, args.state)

if args.command in ('create-admin', 'reset-password'):
    username = args.username or input('Username: ').strip()
    password = getpass.getpass('New password (6+ characters): ')
    if password != getpass.getpass('Repeat new password: '):
        raise SystemExit('Passwords do not match.')
    if not 6 <= len(password) <= 200:
        raise SystemExit('Password must be 6–200 characters.')
    if args.command == 'create-admin':
        store.create_user(username, input('Display name: ').strip(), password, True)
        (store.state / 'setup.key').unlink(missing_ok=True)
    else:
        user = store.one('SELECT id FROM users WHERE username=? COLLATE NOCASE', (username,))
        if not user:
            raise SystemExit('User does not exist.')
        with store.db() as db:
            db.execute('UPDATE users SET password=? WHERE id=?', (password_hash(password), user['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
            db.execute('DELETE FROM tokens WHERE user_id=?', (user['id'],))
    print('Done. Existing sessions and device passwords were revoked for a password reset.')
elif args.command == 'cleanup-uploads':
    import time
    # Run only with the application stopped, so active requests cannot race this cleanup.
    rows = store.all('SELECT id,group_id FROM uploads WHERE updated<?', (time.time() - 7*86400,))
    for row in rows:
        store.upload_path(row['group_id'], row['id']).unlink(missing_ok=True)
        with store.db() as db:
            db.execute('DELETE FROM uploads WHERE id=?', (row['id'],))
            db.execute('DELETE FROM public_uploads WHERE upload_id=?', (row['id'],))
    print(f'Removed {len(rows)} unfinished uploads older than 7 days. DAV temporary files are not removed.')
elif args.command == 'export-database':
    import sqlite3
    if not args.output:
        raise SystemExit('--output is required.')
    target = Path(args.output).resolve()
    if target.exists():
        raise SystemExit('Output already exists; refusing to overwrite.')
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(store.db_path) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    print('Database snapshot exported. Files, trash and configuration must also be backed up.')
