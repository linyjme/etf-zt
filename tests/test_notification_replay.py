from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from etf_rotation.etf_metadata import EtfMetadata, IndexMetadata, TradingMetadata
from etf_rotation.market_data import MarketHealthClassifier, MinuteHistoryStore, SHANGHAI
from etf_rotation.notification_replay import audited_replay
from etf_rotation.quote_quality import MinuteQuarantineStore
from etf_rotation.t_monitor import Quote, QuotePoint


class AuditedReplayTests(unittest.TestCase):
    def setUp(self):
        self.replay = audited_replay
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 9, 4, 10, 10, tzinfo=SHANGHAI)
        self.metadata = {'510300': EtfMetadata(
            '510300', 'Synthetic ETF', IndexMetadata('000300', 'Synthetic index', 'Synthetic'),
            TradingMetadata('SSE', 'DOMESTIC_EQUITY_ETF', False, 1, 100, 0.001, 0.10, 100),
        )}
        self.store = MinuteHistoryStore(self.root / 'quotes.jsonl')
        self.monitor = SimpleNamespace(
            history_store=self.store,
            metadata_store=SimpleNamespace(load=lambda: self.metadata),
            health_classifier=MarketHealthClassifier(),
            quotes_path=self.root / 'quotes.json',
            clock=lambda: self.now,
        )
        self.write_quote()

    def write_quote(self, *, day='2026-09-04', count=8):
        start = datetime.fromisoformat(day + 'T10:00:00+08:00')
        prices = [1.0] * min(5, count) + [1.02] * max(0, count - 5)
        points = tuple(QuotePoint(
            start + timedelta(minutes=index), price, price, price, price, price, 0.0, 0.0,
        ) for index, price in enumerate(prices))
        last = points[-1]
        self.store.upsert({'510300': Quote(
            '510300', 'Synthetic ETF', last.price, last.price, 1.0,
            last.timestamp, points, start + timedelta(minutes=count), 'SYNTHETIC_ONLY',
        )}, self.metadata)

    def rows(self):
        return [json.loads(line) for line in self.store.path.read_text(encoding='utf-8').splitlines()]

    def save_rows(self, rows):
        self.store.path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def files(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob('*') if path.is_file()}

    def assert_rejected_without_changes(self, pattern, symbol='510300', day='2026-09-04'):
        before = self.files()
        with self.assertRaisesRegex(ValueError, pattern):
            self.replay(self.monitor, symbol, day)
        self.assertEqual(self.files(), before)

    def issue(self, *, observed='2026-09-04T10:08:00+08:00', day='2026-09-04'):
        row = self.rows()[0]
        timestamp = day + 'T10:00:00+08:00'
        return {
            'schema_version': 1, 'symbol': '510300', 'timestamp': timestamp,
            'trading_date': day, 'observed_at': observed,
            'source': 'SYNTHETIC_ONLY', 'reason': 'Synthetic OHLC quarantine',
            'previous_close': 1.0, 'volume_unit_shares': 100,
            'point': {**{key: row[key] for key in (
                'price', 'average_price', 'open', 'high', 'low', 'volume', 'amount',
            )}, 'timestamp': timestamp},
        }

    def test_actual_history_store_replays_without_file_changes(self):
        before = self.files()
        result = self.replay(self.monitor, '510300', '2026-09-04')
        self.assertEqual(result['sample_count'], 8)
        self.assertEqual(result['raw_crossings'], 3)
        self.assertEqual(result['merged_events'], 1)
        self.assertEqual(result['invalid_samples'], 5)
        self.assertEqual(result['events'][0]['direction'], 'UP')
        self.assertEqual(self.files(), before)

    def test_explicit_threshold_is_forwarded(self):
        result = self.replay(self.monitor, '510300', '2026-09-04', threshold_pct=3)
        self.assertEqual(result['raw_crossings'], 0)

    def test_missing_and_noncanonical_dates_are_rejected(self):
        for value in (None, '', '20260904', '2026-02-30', date(2026, 9, 4)):
            with self.subTest(value=value):
                self.assert_rejected_without_changes('日期', day=value)

    def test_no_records_for_requested_day_are_rejected(self):
        self.assert_rejected_without_changes('没有.*分钟历史', day='2026-09-03')

    def test_missing_store_and_missing_history_file_are_rejected(self):
        self.monitor.history_store = None
        self.assert_rejected_without_changes('分钟历史')
        self.monitor.history_store = MinuteHistoryStore(self.root / 'missing.jsonl')
        self.assert_rejected_without_changes('分钟历史')

    def test_missing_metadata_is_rejected(self):
        self.metadata.clear()
        self.assert_rejected_without_changes('元数据')

    def test_strict_schema_does_not_repair_missing_ohlc(self):
        rows = self.rows()
        del rows[0]['open']
        self.save_rows(rows)
        self.assert_rejected_without_changes('缺少字段')

    def test_invalid_schema_version_is_rejected(self):
        rows = self.rows()
        rows[0]['schema_version'] = 99
        self.save_rows(rows)
        self.assert_rejected_without_changes('schema_version')

    def test_ohlc_audit_is_not_replaced_by_close_only_calculation(self):
        rows = self.rows()
        rows[0]['high'] = 0.99
        self.save_rows(rows)
        self.assert_rejected_without_changes('OHLC')

    def test_inconsistent_previous_close_is_rejected(self):
        rows = self.rows()
        rows[0]['previous_close'] = 1.02
        self.save_rows(rows)
        self.assert_rejected_without_changes('昨收不一致')

    def test_invalid_volume_amount_relationship_is_rejected(self):
        rows = self.rows()
        rows[0]['volume'] = 1.0
        self.save_rows(rows)
        self.assert_rejected_without_changes('成交量和成交额')

    def test_incomplete_record_is_rejected_not_dropped(self):
        rows = self.rows()
        rows[0]['is_complete'] = False
        self.save_rows(rows)
        self.assert_rejected_without_changes('未完成分钟')

    def test_duplicate_and_reordered_records_are_not_repaired(self):
        rows = self.rows()
        for changed in ([rows[0], *rows], [rows[1], rows[0], *rows[2:]]):
            with self.subTest(changed=changed[:2]):
                self.save_rows(changed)
                self.assert_rejected_without_changes('重复|顺序|乱序')

    def test_gap_is_reported_as_unusable_samples_not_filled(self):
        rows = self.rows()
        self.save_rows(rows[:3] + rows[4:])
        before = self.files()
        result = self.replay(self.monitor, '510300', '2026-09-04')
        self.assertEqual(result['sample_count'], 7)
        self.assertEqual(result['invalid_samples'], 7)
        self.assertEqual(result['merged_events'], 0)
        self.assertEqual(self.files(), before)

    def test_future_day_and_not_yet_completed_minutes_are_rejected(self):
        self.assert_rejected_without_changes('未来|日期', day='2026-09-07')
        self.now = self.now.replace(minute=7)
        self.assert_rejected_without_changes('未来|未完成|观测时间')

    def test_configured_closed_trading_date_is_rejected(self):
        self.monitor.health_classifier = MarketHealthClassifier({date(2026, 9, 4)})
        self.assert_rejected_without_changes('交易日|休市')

    def test_transaction_journal_is_left_untouched_and_blocks_replay(self):
        self.store._journal_path().write_text('not a recovered transaction', encoding='utf-8')
        self.assert_rejected_without_changes('事务')

    def test_unresolved_quarantine_blocks_replay(self):
        MinuteQuarantineStore(self.root / 'quarantine.jsonl').append([self.issue()])
        self.assert_rejected_without_changes('质量|隔离')

    def test_latest_quarantine_observation_must_be_resolved(self):
        issue = self.issue(observed='2026-09-04T10:01:00+08:00')
        issue['last_observed_at'] = '2026-09-04T10:09:00+08:00'
        MinuteQuarantineStore(self.root / 'quarantine.jsonl').append([issue])
        self.assert_rejected_without_changes('质量|隔离')

    def test_later_audited_history_resolves_quarantine_without_editing_evidence(self):
        MinuteQuarantineStore(self.root / 'quarantine.jsonl').append([
            self.issue(observed='2026-09-04T10:01:00+08:00'),
        ])
        before = self.files()
        result = self.replay(self.monitor, '510300', '2026-09-04')
        self.assertEqual(result['merged_events'], 1)
        self.assertEqual(self.files(), before)

    def test_quarantine_on_a_different_day_does_not_block_requested_day(self):
        MinuteQuarantineStore(self.root / 'quarantine.jsonl').append([
            self.issue(day='2026-09-03', observed='2026-09-03T10:01:00+08:00'),
        ])
        self.assertEqual(self.replay(self.monitor, '510300', '2026-09-04')['merged_events'], 1)

    def test_corrupt_quarantine_is_fail_closed(self):
        (self.root / 'quarantine.jsonl').write_text('{broken', encoding='utf-8')
        self.assert_rejected_without_changes('隔离')

    def test_quote_envelope_quality_evidence_is_respected(self):
        self.monitor.quotes_path.write_text(json.dumps({
            'quotes': [], 'validation_issues': [self.issue()],
        }), encoding='utf-8')
        self.assert_rejected_without_changes('质量|隔离')


if __name__ == '__main__':
    unittest.main()
