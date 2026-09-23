import copy
from datetime import datetime, timedelta
import json
import importlib.util
import unittest
from unittest.mock import patch

from etf_rotation.t_monitor import JsonQuoteAdapter
from tests import test_runtime_api as runtime_fixtures
from tests.test_runtime_api import (
    StaticCollector, test_metadata_document,
    valid_completed_quote_payload,
)


class RuntimeQualityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_fixtures.RuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.paths = self.fixture.paths
        self.paths.watchlist.write_text(json.dumps({'watchlist': [
            {'symbol': symbol, 'name': symbol, 'grid_width_pct': 0.002}
            for symbol in ('510300', '510500')
        ]}), encoding='utf-8')
        self.paths.metadata.write_text(json.dumps(
            test_metadata_document('510300', '510500')
        ), encoding='utf-8')
        self.good = valid_completed_quote_payload()
        second = copy.deepcopy(self.good['quotes'][0])
        second['symbol'] = '510500'
        self.good['quotes'].append(second)
        self.bad = copy.deepcopy(self.good)
        self.bad_point = self.bad['quotes'][0]['points'][3]
        self.bad_point['amount'] *= 1.05
        self.bad_at = self.bad_point['timestamp']

    def make_app(self, payload=None):
        return self.fixture.make_runtime_fixture(
            StaticCollector(self.bad if payload is None else payload)
        )

    def test_one_bad_minute_does_not_freeze_other_symbol_or_valid_later_prices(self):
        app = self.make_app()
        self.assertTrue(app.refresh_once())
        snapshot = app.snapshot()
        items = {item['symbol']: item for item in snapshot['items']}
        self.assertIsNone(snapshot['refresh_error'])
        self.assertEqual(snapshot['errors'], [])
        self.assertEqual(items['510500']['health_status'], 'REALTIME')
        self.assertEqual(items['510300']['health_status'], 'DATA_ERROR')
        self.assertEqual(items['510300']['timestamp'], self.good['quotes'][0]['timestamp'])
        self.assertNotIn(items['510300']['action'], ('BUY_CANDIDATE', 'SELL_CANDIDATE'))
        self.assertIn('MARKET_DATA_INVALID', items['510300']['blocked_reasons'])
        self.assertEqual(snapshot['validation_issues'][0]['timestamp'], self.bad_at)
        self.assertFalse(app.health()['ok'])
        official = JsonQuoteAdapter().load(self.paths.quotes)
        self.assertNotIn(self.bad_at, [p.timestamp.isoformat() for p in official['510300'].points])
        self.assertEqual(len(official['510500'].points), len(self.good['quotes'][1]['points']))
        history = [json.loads(line) for line in self.paths.history.read_text(encoding='utf-8').splitlines()]
        self.assertFalse(any(row['symbol'] == '510300' and row['timestamp'] == self.bad_at for row in history))
        alerts = [json.loads(line) for line in self.paths.alerts.read_text(encoding='utf-8').splitlines()]
        self.assertFalse(any(row['symbol'] == '510300' for row in alerts))

    def test_bootstrap_restores_quality_lock_without_writing(self):
        app = self.make_app()
        self.assertTrue(app.refresh_once())
        paths = [self.paths.quotes, self.paths.history, self.paths.alerts,
                 self.paths.quotes.with_name('quarantine.jsonl')]
        before = {path: path.read_bytes() for path in paths}
        restarted = self.fixture.make_runtime_fixture(collector=None)
        affected = next(item for item in restarted.snapshot()['items'] if item['symbol'] == '510300')
        self.assertEqual(affected['health_status'], 'DATA_ERROR')
        self.assertNotIn(affected['action'], ('BUY_CANDIDATE', 'SELL_CANDIDATE'))
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_closure_bootstrap_does_not_erase_quality_error(self):
        app = self.make_app()
        self.assertTrue(app.refresh_once())
        app._bootstrap(increment_revision=True, now=datetime.fromisoformat('2026-08-28T15:30:00+08:00'))
        items = {item['symbol']: item for item in app.snapshot()['items']}
        self.assertEqual(items['510300']['health_status'], 'DATA_ERROR')
        self.assertEqual(items['510500']['health_status'], 'CLOSED')

    def test_repeated_failure_evidence_is_deduplicated(self):
        app = self.make_app()
        self.assertTrue(app.refresh_once())
        quarantine = self.paths.quotes.with_name('quarantine.jsonl')
        original = quarantine.read_bytes()
        self.assertTrue(app.refresh_once())
        self.assertEqual(quarantine.read_bytes(), original)
        row = json.loads(original.decode('utf-8'))
        self.assertEqual(row['point']['amount'], self.bad_point['amount'])
        self.assertEqual(row['symbol'], '510300')
        self.assertEqual(row['timestamp'], self.bad_at)

    def test_quarantine_write_failure_never_promotes_batch(self):
        app = self.make_app(self.good)
        self.assertTrue(app.refresh_once())
        before = self.paths.quotes.read_bytes()
        app.collector = StaticCollector(self.bad)
        self.assertIsNotNone(importlib.util.find_spec('etf_rotation.quote_quality'))
        with patch('etf_rotation.quote_quality.MinuteQuarantineStore.append', side_effect=OSError('audit unavailable')):
            self.assertFalse(app.refresh_once())
        self.assertEqual(self.paths.quotes.read_bytes(), before)
        self.assertIsNotNone(app.snapshot()['refresh_error'])

    def test_bad_latest_point_does_not_retimestamp_old_price(self):
        payload = copy.deepcopy(self.good)
        payload['quotes'][0]['points'][-1]['amount'] *= 1.1
        app = self.make_app(payload)
        self.assertTrue(app.refresh_once())
        affected = next(item for item in app.snapshot()['items'] if item['symbol'] == '510300')
        self.assertEqual(affected['timestamp'], payload['quotes'][0]['points'][-2]['timestamp'])
        self.assertEqual(affected['health_status'], 'DATA_ERROR')

    def test_all_points_rejected_symbol_is_not_given_a_healthy_missing_state(self):
        payload = copy.deepcopy(self.good)
        for point in payload['quotes'][0]['points']:
            point['amount'] *= 1.1
        app = self.make_app(payload)
        self.assertTrue(app.refresh_once())
        items = {item['symbol']: item for item in app.snapshot()['items']}
        self.assertEqual(items['510300']['health_status'], 'DATA_ERROR')
        self.assertIsNone(items['510300']['price'])
        self.assertEqual(items['510500']['health_status'], 'REALTIME')

    def test_unresolved_quality_issues_block_backtest_and_replay(self):
        app = self.make_app()
        self.assertTrue(app.refresh_once())
        before = self.paths.quotes.with_name('quarantine.jsonl').read_bytes()
        for result in (app.t_backtest(), app.signal_replay()):
            items = {item['symbol']: item for item in result['items']}
            self.assertEqual(items['510300']['status'], 'INVALID_DATA')
            self.assertIn('隔离', items['510300']['reason'])
            self.assertNotEqual(items['510500']['status'], 'INVALID_DATA')
        self.assertEqual(before, self.paths.quotes.with_name('quarantine.jsonl').read_bytes())

    def test_source_correction_restores_symbol_without_changing_validation_rules(self):
        app = self.make_app()
        self.assertTrue(app.refresh_once())
        corrected = copy.deepcopy(self.good)
        for quote in corrected['quotes']:
            quote['observed_at'] = (datetime.fromisoformat(quote['observed_at']) + timedelta(seconds=5)).isoformat()
        corrected['collected_at'] = corrected['quotes'][0]['observed_at']
        app.collector = StaticCollector(corrected)
        self.assertTrue(app.refresh_once())
        self.assertFalse(app.snapshot().get('validation_issues'))
        self.assertTrue(all(item['health_status'] == 'REALTIME' for item in app.snapshot()['items']))
        for result in (app.t_backtest(), app.signal_replay()):
            self.assertNotEqual(result['items'][0]['status'], 'INVALID_DATA')
        self.assertTrue(self.paths.quotes.with_name('quarantine.jsonl').exists())

    def test_next_day_snapshot_cannot_hide_unresolved_historical_gap(self):
        app = self.make_app()
        self.assertTrue(app.refresh_once())
        tomorrow = copy.deepcopy(self.good)
        for quote in tomorrow['quotes']:
            for key in ('timestamp', 'observed_at'):
                quote[key] = (datetime.fromisoformat(quote[key]) + timedelta(days=3)).isoformat()
            for point in quote['points']:
                point['timestamp'] = (datetime.fromisoformat(point['timestamp']) + timedelta(days=3)).isoformat()
        tomorrow['collected_at'] = tomorrow['quotes'][0]['observed_at']
        app.clock = lambda: datetime.fromisoformat('2026-08-31T10:02:00+08:00')
        app.collector = StaticCollector(tomorrow)
        self.assertTrue(app.refresh_once())
        self.assertFalse(app.snapshot().get('validation_issues'))
        for result in (app.t_backtest(), app.signal_replay()):
            affected = next(item for item in result['items'] if item['symbol'] == '510300')
            self.assertEqual(affected['status'], 'INVALID_DATA')

    def test_removing_previously_published_minute_emits_chart_reset(self):
        app = self.make_app(self.good)
        self.assertTrue(app.refresh_once())
        old_revision = app.snapshot()['revision']
        app.collector = StaticCollector(self.bad)
        self.assertTrue(app.refresh_once())
        event = app.wait_for_revision(old_revision, timeout=0)
        self.assertIn('510300', event['resets'])
        for result in (app.t_backtest(), app.signal_replay()):
            self.assertEqual(result['items'][0]['status'], 'INVALID_DATA')

    def shifted(self, payload, *, seconds=0, days=0):
        result = copy.deepcopy(payload)
        for quote in result['quotes']:
            quote['observed_at'] = (datetime.fromisoformat(quote['observed_at']) + timedelta(seconds=seconds, days=days)).isoformat()
            if days:
                quote['timestamp'] = (datetime.fromisoformat(quote['timestamp']) + timedelta(days=days)).isoformat()
                for point in quote['points']:
                    point['timestamp'] = (datetime.fromisoformat(point['timestamp']) + timedelta(days=days)).isoformat()
        result['collected_at'] = result['quotes'][0]['observed_at']
        return result

    def test_omitted_bad_minute_does_not_resolve_current_quality_lock(self):
        app = self.make_app()
        self.assertTrue(app.refresh_once())
        incomplete = self.shifted(self.good, seconds=5)
        incomplete['quotes'][0]['points'].pop(3)
        app.collector = StaticCollector(incomplete)
        self.assertTrue(app.refresh_once())
        affected = app.snapshot()['items'][0]
        self.assertEqual(affected['health_status'], 'DATA_ERROR')
        self.assertNotIn(affected['action'], ('BUY_CANDIDATE', 'SELL_CANDIDATE'))
        self.assertEqual(app.snapshot()['validation_issues'][0]['timestamp'], self.bad_at)
        persisted = json.loads(self.paths.quotes.read_text(encoding='utf-8'))
        self.assertEqual(persisted['validation_issues'][0]['timestamp'], self.bad_at)

    def test_bootstrap_reconciles_audit_after_canonical_commit_failure(self):
        app = self.make_app(self.good)
        self.assertTrue(app.refresh_once())
        before = self.paths.quotes.read_bytes()
        app.collector = StaticCollector(self.shifted(self.bad, seconds=5))
        with patch.object(app, '_commit_staged_quotes', side_effect=OSError('commit unavailable')):
            self.assertFalse(app.refresh_once())
        self.assertEqual(before, self.paths.quotes.read_bytes())
        restarted = self.fixture.make_runtime_fixture(collector=None)
        self.assertEqual(restarted.snapshot()['items'][0]['health_status'], 'DATA_ERROR')
        self.assertEqual(before, self.paths.quotes.read_bytes())

    def test_entirely_isolated_first_day_still_blocks_whole_history_replay(self):
        bad = copy.deepcopy(self.good)
        for point in bad['quotes'][0]['points']:
            point['amount'] *= 1.1
        app = self.make_app(bad)
        self.assertTrue(app.refresh_once())
        app.clock = lambda: datetime.fromisoformat('2026-08-31T10:02:00+08:00')
        app.collector = StaticCollector(self.shifted(self.good, days=3))
        self.assertTrue(app.refresh_once())
        for result in (app.t_backtest(), app.signal_replay()):
            self.assertEqual(result['items'][0]['status'], 'INVALID_DATA')

    def test_repeated_bad_value_after_correction_requires_a_new_correction(self):
        app = self.make_app()
        self.assertTrue(app.refresh_once())
        app.collector = StaticCollector(self.shifted(self.good, seconds=5))
        self.assertTrue(app.refresh_once())
        self.assertEqual(app.snapshot()['items'][0]['health_status'], 'REALTIME')
        app.collector = StaticCollector(self.shifted(self.bad, seconds=10))
        self.assertTrue(app.refresh_once())
        app.clock = lambda: datetime.fromisoformat('2026-08-31T10:02:00+08:00')
        app.collector = StaticCollector(self.shifted(self.good, days=3))
        self.assertTrue(app.refresh_once())
        for result in (app.t_backtest(), app.signal_replay()):
            self.assertEqual(result['items'][0]['status'], 'INVALID_DATA')


if __name__ == '__main__':
    unittest.main()
