from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import unittest

from etf_rotation.market_data import SHANGHAI
from etf_rotation.notification_store import NotificationStore, RuntimeLock


class NotificationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'events.sqlite3'
        self.store = NotificationStore(self.path)
        self.now = datetime(2026, 9, 4, 10, 6, tzinfo=SHANGHAI)

    def event(self, identity='a', status='PENDING'):
        return dict(id=identity, kind='ANOMALY', symbol='510300', direction='UP',
                    created_at=self.now.isoformat(), expires_at=(self.now+timedelta(seconds=120)).isoformat(),
                    payload={'price': 4.0}, status=status, attempts=0,
                    next_attempt_at=self.now.isoformat(), reason='')

    def test_roundtrip_and_dedup(self):
        self.assertTrue(self.store.add_event(self.event()))
        self.assertFalse(self.store.add_event(self.event()))
        self.assertEqual(len(self.store.events()), 1)
        self.assertEqual(self.store.events()[0]['payload'], {'price': 4.0})

    def test_get_event_returns_detached_record_or_none_in_transaction(self):
        self.assertTrue(callable(getattr(self.store, 'get_event', None)))
        self.assertIsNone(self.store.get_event('missing'))
        with self.store.transaction():
            self.store.add_event(self.event())
            event = self.store.get_event('a')
            self.assertEqual(event, self.event())
            event['payload']['price'] = 999
            self.assertEqual(self.store.get_event('a')['payload'], {'price': 4.0})
            self.store.update_event('a', status='CANCELLED')
            self.assertEqual(self.store.get_event('a')['status'], 'CANCELLED')

    def test_get_event_rejects_corrupt_record_and_mismatched_identity(self):
        self.assertTrue(callable(getattr(self.store, 'get_event', None)))
        for encoded in ('{', json.dumps(self.event('different')),
                        json.dumps(self.event(status='DELIVERED'))):
            with self.subTest(encoded=encoded):
                with closing(sqlite3.connect(self.path)) as conn, conn:
                    conn.execute('INSERT OR REPLACE INTO events VALUES (?,?)', ('a', encoded))
                with self.assertRaises(ValueError):
                    self.store.get_event('a')

    def test_claim_is_atomic_and_terminal_is_not_reclaimed(self):
        self.store.add_event(self.event())
        self.assertTrue(self.store.claim('a'))
        self.assertFalse(self.store.claim('a'))
        self.store.update_event('a', status='SERVER_ACCEPTED')
        self.assertFalse(self.store.claim('a'))
        with self.assertRaises(ValueError):
            self.store.update_event('a', status='PENDING')

    def test_restart_does_not_replay_and_preserves_unknown(self):
        self.store.add_event(self.event('queued'))
        self.store.add_event(self.event('sending'))
        self.store.claim('sending')
        NotificationStore(self.path).recover()
        statuses = {e['id']: e['status'] for e in self.store.events()}
        self.assertEqual(statuses, {'queued':'CANCELLED', 'sending':'UNKNOWN'})

    def test_pending_respects_next_attempt_time(self):
        self.store.add_event(self.event())
        self.store.update_event('a', next_attempt_at=(self.now+timedelta(seconds=30)).isoformat())
        self.assertEqual(self.store.pending(self.now), [])
        self.assertEqual(len(self.store.pending(self.now+timedelta(seconds=31))), 1)

    def test_state_detached_and_persistent(self):
        value = {'x':[1]}
        self.store.set_state('test', value)
        value['x'].append(2)
        self.assertEqual(NotificationStore(self.path).state('test'), {'x':[1]})

    def test_transaction_commits_state_and_event_together(self):
        self.assertTrue(callable(getattr(self.store, 'transaction', None)))
        with self.store.transaction():
            self.store.set_state('checkpoint', {'seen': True})
            self.store.add_event(self.event())
            self.assertEqual(self.store.state('checkpoint'), {'seen': True})
            self.assertEqual(len(self.store.events()), 1)
        reopened = NotificationStore(self.path)
        self.assertEqual(reopened.state('checkpoint'), {'seen': True})
        self.assertEqual(len(reopened.events()), 1)

    def test_transaction_rolls_back_state_and_event_on_any_exception(self):
        self.assertTrue(callable(getattr(self.store, 'transaction', None)))
        self.store.set_state('checkpoint', {'seen': False})
        with self.assertRaisesRegex(RuntimeError, 'abort'):
            with self.store.transaction():
                self.store.set_state('checkpoint', {'seen': True})
                self.store.add_event(self.event())
                raise RuntimeError('abort')
        self.assertEqual(self.store.state('checkpoint'), {'seen': False})
        self.assertEqual(self.store.events(), [])

    def test_nested_transactions_commit_only_with_outer_transaction(self):
        self.assertTrue(callable(getattr(self.store, 'transaction', None)))
        with self.assertRaisesRegex(RuntimeError, 'abort'):
            with self.store.transaction():
                self.store.set_state('checkpoint', 'outer')
                with self.store.transaction():
                    self.store.add_event(self.event())
                raise RuntimeError('abort')
        self.assertIsNone(self.store.state('checkpoint'))
        self.assertEqual(self.store.events(), [])
        with self.store.transaction():
            with self.store.transaction():
                self.store.add_event(self.event())
        self.assertEqual(len(self.store.events()), 1)

    def test_caught_nested_exception_still_aborts_outer_transaction(self):
        self.assertTrue(callable(getattr(self.store, 'transaction', None)))
        with self.assertRaises(ValueError):
            with self.store.transaction():
                self.store.set_state('checkpoint', 'outer')
                try:
                    with self.store.transaction():
                        self.store.add_event(self.event())
                        raise RuntimeError('abort')
                except RuntimeError:
                    pass
        self.assertIsNone(self.store.state('checkpoint'))
        self.assertEqual(self.store.events(), [])

    def test_concurrent_threads_have_independent_atomic_transactions(self):
        self.assertTrue(callable(getattr(self.store, 'transaction', None)))
        started = threading.Barrier(2)

        def increment():
            started.wait(timeout=5)
            for _ in range(10):
                with self.store.transaction():
                    self.store.set_state('counter', self.store.state('counter', 0) + 1)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(increment) for _ in range(2)]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(self.store.state('counter'), 20)

    def test_pagination_and_invalid_status(self):
        for i in range(5):
            self.store.add_event(self.event(str(i)))
        self.assertEqual(len(self.store.events(limit=2, offset=2)), 2)
        with self.assertRaises(ValueError): self.store.events(limit=1001)
        with self.assertRaises(ValueError): self.store.add_event(self.event('bad','DELIVERED'))

    def test_corrupt_storage_fails_closed(self):
        bad = self.path.with_name('bad.sqlite3')
        bad.write_bytes(b'not a database')
        with self.assertRaises(ValueError): NotificationStore(bad)

    def test_existing_empty_database_is_not_initialized(self):
        bad = self.path.with_name('empty.sqlite3')
        bad.touch()
        with self.assertRaises(ValueError):
            NotificationStore(bad)

    def test_reopen_rejects_each_missing_required_table_without_rebuilding(self):
        for table in ('state', 'events'):
            with self.subTest(table=table):
                path = self.path.with_name(table + '.sqlite3')
                NotificationStore(path)
                with closing(sqlite3.connect(path)) as conn, conn:
                    conn.execute('DROP TABLE ' + table)
                with self.assertRaises(ValueError):
                    NotificationStore(path)
                with closing(sqlite3.connect(path)) as conn, conn:
                    self.assertIsNone(conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone())

    def test_reopen_requires_exact_schema_marker_and_version(self):
        for pragma in ('application_id', 'user_version'):
            for invalid in (0, 999):
                with self.subTest(pragma=pragma, invalid=invalid):
                    path = self.path.with_name(f'{pragma}-{invalid}.sqlite3')
                    NotificationStore(path)
                    with closing(sqlite3.connect(path)) as conn, conn:
                        conn.execute(f'PRAGMA {pragma}={invalid}')
                    with self.assertRaises(ValueError):
                        NotificationStore(path)

    def test_reopen_rejects_changed_table_schema(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('ALTER TABLE state ADD COLUMN unexpected TEXT')
        with self.assertRaises(ValueError):
            NotificationStore(self.path)

    def test_reopen_and_recover_reject_invalid_state_json(self):
        for encoded in ('{', 'NaN', '{"x":1,"x":2}'):
            with self.subTest(encoded=encoded):
                with closing(sqlite3.connect(self.path)) as conn, conn:
                    conn.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('test', encoded))
                with self.assertRaises(ValueError):
                    NotificationStore(self.path)
                with self.assertRaises(ValueError):
                    self.store.recover()

    def test_reopen_and_recover_reject_invalid_event_records(self):
        valid = self.event()
        invalid_records = [
            '{', '[]',
            json.dumps({**valid, 'id': 'different'}),
            json.dumps({**valid, 'status': 'DELIVERED'}),
            json.dumps({**valid, 'status': []}),
            json.dumps({**valid, 'unrecognized': True}),
            json.dumps({**valid, 'payload': []}),
            json.dumps({**valid, 'created_at': 123}),
            json.dumps({**valid, 'attempts': True}),
        ]
        for encoded in invalid_records:
            with self.subTest(encoded=encoded):
                with closing(sqlite3.connect(self.path)) as conn, conn:
                    conn.execute('INSERT OR REPLACE INTO events VALUES (?,?)', ('a', encoded))
                with self.assertRaises(ValueError):
                    NotificationStore(self.path)
                with self.assertRaises(ValueError):
                    self.store.recover()

    def test_recovery_validates_all_data_before_changing_pending_events(self):
        self.store.add_event(self.event())
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('INSERT INTO state VALUES (?,?)', ('test', '{'))
        with self.assertRaises(ValueError):
            self.store.recover()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            data = conn.execute('SELECT data FROM events WHERE id=?', ('a',)).fetchone()[0]
        self.assertEqual(json.loads(data)['status'], 'PENDING')

    def test_live_store_rejects_missing_database_or_lost_schema(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('DROP TABLE state')
        with self.assertRaises(ValueError):
            self.store.events()
        self.path.unlink()
        with self.assertRaises(ValueError):
            self.store.events()
        self.assertFalse(self.path.exists())

    def test_runtime_lock_is_exclusive_and_reusable(self):
        first = RuntimeLock(self.path.with_suffix('.lock'))
        second = RuntimeLock(self.path.with_suffix('.lock'))
        first.acquire()
        try:
            with self.assertRaises(ValueError): second.acquire()
        finally: first.release()
        second.acquire()
        second.release()


if __name__ == '__main__': unittest.main()
