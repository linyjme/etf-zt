"""Private, transactional notification history and single-owner runtime lock."""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import threading


STATUSES = frozenset({'OBSERVED', 'PENDING', 'SENDING', 'CONNECTION_OK', 'SERVER_ACCEPTED', 'FAILED', 'UNKNOWN', 'CANCELLED'})
TERMINAL = frozenset({'OBSERVED', 'CONNECTION_OK', 'SERVER_ACCEPTED', 'FAILED', 'UNKNOWN', 'CANCELLED'})
SCHEMA_MARKER = 0x4E4F5446
SCHEMA_VERSION = 1
SCHEMA = {
    'state': 'CREATE TABLE state (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)',
    'events': 'CREATE TABLE events (id TEXT PRIMARY KEY NOT NULL, data TEXT NOT NULL)',
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('通知记录包含重复字段')
        value[key] = item
    return value


def _loads(data):
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
        _json(value)
        return value
    except (TypeError, ValueError, RecursionError):
        raise ValueError('通知记录 JSON 无效，自动投递暂停') from None


class RuntimeLock:
    """OS-owned byte lock; stale PID files never masquerade as ownership."""
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        try:
            if self.path.stat().st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ValueError('通知服务已由其他进程运行') from None
        self.handle = handle

    def release(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            handle.close()


class NotificationStore:
    def __init__(self, path):
        self.path = Path(path)
        self._local = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Exclusively reserve a new path. Existing empty or partial files
            # must never be mistaken for a brand-new notification database.
            with self.path.open('xb'):
                pass
            initialize = True
        except FileExistsError:
            initialize = False
        with self._connection(initialize=initialize) as conn:
            self._validate_storage(conn)

    @contextmanager
    def _connection(self, *, initialize=False):
        existing = getattr(self._local, 'connection', None)
        if existing is not None:
            try:
                if self._local.rollback_only:
                    raise ValueError('通知事务已中止')
                yield existing
            except BaseException:
                self._local.rollback_only = True
                raise
            return
        conn = None
        try:
            # mode=rw prevents a deleted live database from being recreated.
            conn = sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True, timeout=3)
            conn.execute('PRAGMA busy_timeout=3000')
            conn.execute('BEGIN IMMEDIATE')
            self._local.connection = conn
            self._local.rollback_only = False
            if initialize:
                for statement in SCHEMA.values():
                    conn.execute(statement)
                conn.execute(f'PRAGMA application_id={SCHEMA_MARKER}')
                conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            self._validate_schema(conn)
            yield conn
            if self._local.rollback_only:
                raise ValueError('通知事务已中止')
            conn.commit()
        except BaseException as exc:
            if conn is not None: conn.rollback()
            if isinstance(exc, (sqlite3.Error, OSError, json.JSONDecodeError)):
                raise ValueError('通知记录不可用，自动投递暂停') from None
            raise
        finally:
            self._local.connection = None
            self._local.rollback_only = False
            if conn is not None: conn.close()

    @contextmanager
    def transaction(self):
        """Commit nested store calls together; any nested failure aborts all writes."""
        with self._connection():
            yield self

    @staticmethod
    def _validate_schema(conn):
        if (conn.execute('PRAGMA application_id').fetchone()[0] != SCHEMA_MARKER or
                conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
            raise ValueError('通知数据库标识或版本无效，自动投递暂停')
        objects = dict(conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall())
        if objects != SCHEMA:
            raise ValueError('通知数据库结构无效，自动投递暂停')

    @staticmethod
    def _validate_state(key, value):
        if type(key) is not str or not key:
            raise ValueError('通知状态标识无效')
        _json(value)

    def _validate_storage(self, conn):
        self._validate_schema(conn)
        if conn.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('通知记录校验失败')
        for key, encoded in conn.execute('SELECT key,value FROM state').fetchall():
            self._validate_state(key, _loads(encoded))
        for identity, encoded in conn.execute('SELECT id,data FROM events').fetchall():
            self._read_event(identity, encoded)

    def _read_event(self, identity, encoded):
        event = _loads(encoded)
        self._validate(event)
        if event['id'] != identity:
            raise ValueError('通知记录标识不一致，自动投递暂停')
        return event

    def state(self, key, default=None):
        with self._connection() as conn:
            row = conn.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
            if row is None:
                return default
            value = _loads(row[0])
            self._validate_state(key, value)
            return value

    def set_state(self, key, value):
        with self._connection() as conn:
            self._validate_state(key, value)
            conn.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, _json(value)))

    @staticmethod
    def _validate(event):
        required = {'id','kind','symbol','direction','created_at','expires_at','payload','status','attempts','next_attempt_at','reason'}
        if type(event) is not dict or required != event.keys():
            raise ValueError('通知记录字段无效')
        if any(type(event[field]) is not str for field in (
                'id','kind','symbol','direction','created_at','expires_at','status','next_attempt_at','reason')):
            raise ValueError('通知记录字段类型无效')
        if event['status'] not in STATUSES or not event['id']:
            raise ValueError('通知记录状态无效')
        if type(event['payload']) is not dict:
            raise ValueError('通知记录内容无效')
        for field in ('created_at','expires_at','next_attempt_at'):
            stamp = datetime.fromisoformat(event[field])
            if stamp.tzinfo is None: raise ValueError('通知时间必须有时区')
        if type(event['attempts']) is not int or event['attempts'] < 0:
            raise ValueError('投递次数无效')
        _json(event)

    def add_event(self, event):
        with self._connection() as conn:
            self._validate(event)
            result = conn.execute('INSERT OR IGNORE INTO events VALUES (?,?)', (event['id'], _json(event)))
            return result.rowcount == 1

    def get_event(self, identity):
        with self._connection() as conn:
            row = conn.execute('SELECT data FROM events WHERE id=?', (identity,)).fetchone()
            return None if row is None else self._read_event(identity, row[0])

    def events(self, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise ValueError('通知分页参数无效')
        with self._connection() as conn:
            rows = conn.execute('SELECT id,data FROM events ORDER BY rowid DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
            return [self._read_event(identity, data) for identity, data in rows]

    def update_event(self, identity, **fields):
        allowed = {'status','attempts','next_attempt_at','reason'}
        if not fields.keys() <= allowed: raise ValueError('不允许改写原始通知')
        with self._connection() as conn:
            row = conn.execute('SELECT data FROM events WHERE id=?', (identity,)).fetchone()
            if row is None: raise ValueError('通知不存在')
            event = self._read_event(identity, row[0])
            if event['status'] in TERMINAL and fields.get('status', event['status']) != event['status']:
                raise ValueError('已结束的投递不能重新发送')
            event.update(fields)
            self._validate(event)
            conn.execute('UPDATE events SET data=? WHERE id=?', (_json(event), identity))

    def claim(self, identity):
        with self._connection() as conn:
            row = conn.execute('SELECT data FROM events WHERE id=?', (identity,)).fetchone()
            if row is None: return False
            event = self._read_event(identity, row[0])
            if event['status'] != 'PENDING': return False
            event.update(status='SENDING', attempts=event['attempts']+1)
            conn.execute('UPDATE events SET data=? WHERE id=?', (_json(event), identity))
            return True

    def pending(self, now):
        # Read pending rows independently of history pagination; old records
        # must never hide a still-pending retry.
        with self._connection() as conn:
            rows = conn.execute("SELECT id,data FROM events WHERE json_extract(data,'$.status')='PENDING' ORDER BY rowid").fetchall()
            result = []
            for identity, data in rows:
                event = self._read_event(identity, data)
                if datetime.fromisoformat(event['next_attempt_at']) <= now:
                    result.append(event)
            return result

    def recover(self):
        with self._connection() as conn:
            self._validate_storage(conn)
            for identity, data in conn.execute('SELECT id,data FROM events').fetchall():
                event = self._read_event(identity, data)
                if event['status'] in {'PENDING','SENDING'}:
                    event['reason'] = '服务重启，不重发旧通知'
                    event['status'] = 'UNKNOWN' if event['status'] == 'SENDING' else 'CANCELLED'
                    conn.execute('UPDATE events SET data=? WHERE id=?', (_json(event), identity))

    def cancel_pending(self, reason):
        with self._connection() as conn:
            for identity, data in conn.execute("SELECT id,data FROM events WHERE json_extract(data,'$.status')='PENDING'").fetchall():
                event = self._read_event(identity, data)
                event.update(status='CANCELLED', reason=reason)
                self._validate(event)
                conn.execute('UPDATE events SET data=? WHERE id=?', (_json(event), identity))
