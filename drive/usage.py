"""Coarse activity and key operations for the system administrator."""
import logging
import sqlite3
import time

from fastapi import Depends, Query
from starlette.concurrency import run_in_threadpool

ACTIVE_SECONDS = 300
REFRESH_SECONDS = 60
PAGE_SIZE = 50
NATIVE_ACTIONS = {'upload': 'upload', 'share': 'share', 'link_create': 'share', 'publish': 'share'}
log = logging.getLogger(__name__)


class Usage:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS usage_web (
                    session_hash TEXT PRIMARY KEY REFERENCES sessions(hash) ON DELETE CASCADE,
                    ip TEXT NOT NULL, last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_accounts (
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT, username TEXT NOT NULL, name TEXT NOT NULL,
                    action TEXT NOT NULL, source TEXT NOT NULL, library_id TEXT NOT NULL,
                    library_name TEXT NOT NULL, path TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS usage_log_action ON usage_log(action, id DESC);
                CREATE INDEX IF NOT EXISTS usage_log_recent_user ON usage_log(user_id, action, created);
            """)

    @staticmethod
    def touch_account(db, user_id, now):
        db.execute("""
            INSERT INTO usage_accounts(user_id,last_seen) VALUES (?,?)
            ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen
            WHERE usage_accounts.last_seen<=excluded.last_seen-?
        """, (user_id, now, REFRESH_SECONDS))

    def touch_web(self, user_id, session_hash, ip, last_seen, last_ip):
        now = time.time()
        if ip and (last_ip != ip or last_seen is None or last_seen <= now - REFRESH_SECONDS):
            try:
                with self.store.db() as db:
                    touched = db.execute("""
                        INSERT INTO usage_web(session_hash,ip,last_seen)
                        SELECT hash,?,? FROM sessions WHERE hash=? AND expires>?
                        ON CONFLICT(session_hash) DO UPDATE SET ip=excluded.ip,last_seen=excluded.last_seen
                        WHERE usage_web.ip<>excluded.ip OR usage_web.last_seen<=?
                    """, (ip, now, session_hash, now, now - REFRESH_SECONDS))
                    if touched.rowcount:
                        self.touch_account(db, user_id, now)
            except sqlite3.Error:
                log.warning('Unable to update web activity', exc_info=True)

    def native_event(self, db, current, library, action, path, anonymous):
        if action in NATIVE_ACTIONS:
            self._insert(db, None if anonymous else current, NATIVE_ACTIONS[action], 'native', library, path)

    def record(self, current, action, source, library, path, *, name='', merge=False):
        try:
            with self.store.db() as db:
                self._insert(db, current, action, source, library, path, name=name, merge=merge)
        except sqlite3.Error:
            # An unavailable audit write must not change the outcome of a
            # completed upload or an already authorized download.
            log.warning('Unable to write cloud operation log', exc_info=True)

    @staticmethod
    def _insert(db, current, action, source, library, path, *, name='', merge=False):
        now = time.time()
        library = library or ''
        path = (path or '/')[:4000]
        user_id = current['id'] if current else None
        username = current.get('username', '') if current else ''
        actor = current.get('name', username) if current else '外链访客'
        if current and not username:
            row = db.execute('SELECT username,name FROM users WHERE id=?', (user_id,)).fetchone()
            if row:
                username, actor = row
        if source == 'native' and not name:
            row = db.execute('SELECT name FROM libraries WHERE id=?', (library,)).fetchone()
            name = row[0] if row else ''
        values = (user_id, username, actor, action, source, library, name, path, now)
        statement = """
            INSERT INTO usage_log(user_id,username,name,action,source,library_id,library_name,path,created)
            SELECT ?,?,?,?,?,?,?,?,?
        """
        if merge:
            statement += """
                WHERE NOT EXISTS(SELECT 1 FROM usage_log WHERE user_id=? AND action=? AND created>=?
                                 AND source=? AND library_id=? AND path=?)
            """
            values += (user_id, action, now - ACTIVE_SECONDS, source, library, path)
        db.execute(statement, values)

    def overview(self):
        now = time.time()
        cutoff = now - ACTIVE_SECONDS
        with self.store.reuse_read_connection():
            people = self.store.one('SELECT COUNT(*) AS total, COUNT(CASE WHEN active=1 THEN 1 END) AS enabled FROM users WHERE admin=0')
            recent = self.store.one("""
                SELECT COUNT(*) AS count FROM usage_accounts a JOIN users u ON u.id=a.user_id
                WHERE u.admin=0 AND a.last_seen>=?
            """, (now - 86400,))
            connections = self.store.all("""
                SELECT u.id,u.username,u.name,'web' AS channel,'' AS device,w.ip,w.last_seen
                FROM usage_web w JOIN sessions s ON s.hash=w.session_hash JOIN users u ON u.id=s.user_id
                WHERE w.last_seen>=? AND s.expires>? AND u.active=1
                UNION ALL
                SELECT u.id,u.username,u.name,'device',t.name,t.last_ip,t.last_seen
                FROM tokens t JOIN users u ON u.id=t.user_id
                WHERE t.last_seen>=? AND t.last_seen>t.created AND u.active=1
                ORDER BY last_seen DESC
            """, (cutoff, now, cutoff))
        return {'total': people['total'], 'enabled': people['enabled'], 'recent_accounts': recent['count'],
                'active_accounts': len({row['id'] for row in connections}),
                'active_window': ACTIVE_SECONDS, 'connections': connections}

    def operations(self, before, action):
        clauses, params = [], []
        if before:
            clauses.append('id<?')
            params.append(before)
        if action:
            clauses.append('action=?')
            params.append(action)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = self.store.all(
            'SELECT id,username,name,action,source,library_id,library_name,path,created FROM usage_log'
            + where + ' ORDER BY id DESC LIMIT ?', (*params, PAGE_SIZE + 1))
        return {'items': rows[:PAGE_SIZE], 'next': rows[PAGE_SIZE - 1]['id'] if len(rows) > PAGE_SIZE else None}


def note_download(request, current, source, library, path, *, merge=False):
    if request.method != 'HEAD':
        request.scope['usage_download'] = (current, source, library, path,
                                           merge or 'range' in request.headers)


class DownloadAudit:
    def __init__(self, app, usage):
        self.app = app
        self.usage = usage

    async def __call__(self, scope, receive, send):
        successful = False

        async def transmit(message):
            nonlocal successful
            await send(message)
            if message['type'] == 'http.response.start':
                successful = message['status'] in (200, 206)
            complete = message['type'] in ('http.response.pathsend', 'http.response.zerocopysend') or (
                message['type'] == 'http.response.body' and not message.get('more_body'))
            if successful and complete:
                item = scope.pop('usage_download', None)
                if item:
                    current, source, library, path, merge = item
                    await run_in_threadpool(self.usage.record, current, 'download', source, library, path, merge=merge)
        await self.app(scope, receive, transmit)


def register_usage(app, usage, admin):
    @app.get('/api/admin/usage')
    def usage_overview(current=Depends(admin)):
        return usage.overview()

    @app.get('/api/admin/usage/operations')
    def usage_operations(before: int = Query(0, ge=0, lt=2**63),
                         action: str = Query('', pattern='^(upload|download|share)?$'), current=Depends(admin)):
        return usage.operations(before, action)
