from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta
import inspect
import json
from pathlib import Path
import tempfile
import unittest

from etf_rotation.market_data import MarketHealthClassifier
from etf_rotation.t_monitor import TMonitorEngine, WatchItem, snapshot_to_dict
from etf_rotation.t_web import MonitorApplication
from tests.regime_fixtures import confirmed_range_quote
from tests import test_swing_service as swing_fixtures


class CompletedMinuteHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = MarketHealthClassifier()
        self.start = datetime.fromisoformat("2026-09-03T09:51:00+08:00")
        self.end = self.start + timedelta(minutes=1)

    def classify(self, now: datetime, stamp: datetime | None, error: str | None = None):
        self.assertIn("completed_minute", inspect.signature(self.classifier.classify).parameters)
        return self.classifier.classify(now, stamp, error, completed_minute=True)

    def test_age_and_boundaries_start_when_the_minute_finishes(self) -> None:
        for seconds, expected in ((0, "REALTIME"), (1, "REALTIME"), (59, "REALTIME"), (74, "REALTIME"),
                                  (75, "REALTIME"), (76, "DELAYED"),
                                  (180, "DELAYED"), (181, "OUTAGE")):
            with self.subTest(seconds=seconds):
                health = self.classify(self.end + timedelta(seconds=seconds), self.start)
                self.assertEqual(health.status, expected)
                self.assertEqual(health.quote_age_seconds, seconds)

    def test_unfinished_and_future_minutes_never_become_realtime(self) -> None:
        for stamp in (self.start, self.start + timedelta(minutes=1)):
            with self.subTest(stamp=stamp):
                health = self.classify(self.end - timedelta(seconds=1), stamp)
                self.assertEqual(health.status, "OUTAGE")

    def test_errors_missing_data_and_closed_sessions_remain_safe(self) -> None:
        self.assertEqual(self.classify(self.end, self.start, "feed failed").status, "OUTAGE")
        self.assertEqual(self.classify(self.end, None).status, "OUTAGE")
        for value, expected in (("2026-09-03T12:00:00+08:00", "LUNCH_BREAK"),
                                ("2026-09-03T15:01:00+08:00", "CLOSED"),
                                ("2026-09-05T10:00:00+08:00", "CLOSED")):
            self.assertEqual(self.classify(datetime.fromisoformat(value), self.start).status, expected)

    def test_legacy_instant_quotes_do_not_receive_a_minute_extension(self) -> None:
        now = self.start + timedelta(seconds=76)
        self.assertEqual(self.classifier.classify(now, self.start, None).status, "DELAYED")


class MinuteMonitorIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.quote = confirmed_range_quote(-0.008, -0.007)
        self.watch = (WatchItem("510300", "沪深300ETF", 0.002),)

    def test_candidate_remains_available_during_normal_minute_refresh_cycle(self) -> None:
        now = self.quote.observed_at + timedelta(seconds=59)
        signal = TMonitorEngine().evaluate(self.watch, {self.quote.symbol: self.quote}, now).signals[0]
        self.assertEqual(signal.health_status, "REALTIME")
        self.assertEqual(signal.action, "BUY_CANDIDATE")
        self.assertEqual(signal.regime_state, "RANGE")

    def test_repeated_collection_of_an_old_minute_does_not_renew_it(self) -> None:
        now = self.quote.observed_at + timedelta(seconds=181)
        old_quote = replace(self.quote, observed_at=now)
        signal = TMonitorEngine().evaluate(self.watch, {old_quote.symbol: old_quote}, now).signals[0]
        self.assertEqual(signal.health_status, "OUTAGE")
        self.assertNotIn(signal.action, {"BUY_CANDIDATE", "SELL_CANDIDATE"})
        self.assertIn("MARKET_NOT_REALTIME", signal.blocked_reasons)

    def test_snapshot_marks_minute_basis_without_changing_the_chart_timestamp(self) -> None:
        item = snapshot_to_dict(TMonitorEngine().evaluate(
            self.watch, {self.quote.symbol: self.quote}, self.quote.observed_at,
        ))["items"][0]
        self.assertEqual(item.get("timestamp_basis"), "MINUTE_START")
        self.assertEqual(item["timestamp"], self.quote.timestamp.isoformat())

    def test_application_publishes_the_same_minute_health_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quotes_path, watch_path, calendar_path = (root / name for name in (
                "quotes.json", "watchlist.json", "calendar.json",
            ))
            quotes_path.write_text(json.dumps({"quotes": [asdict(self.quote)]},
                                             default=lambda value: value.isoformat()), encoding="utf-8")
            watch_path.write_text(json.dumps([asdict(self.watch[0])]), encoding="utf-8")
            calendar_path.write_text(json.dumps({"schema_version": 1, "closed_dates": []}), encoding="utf-8")
            application = MonitorApplication(
                quotes_path, watch_path, calendar_path=calendar_path,
                clock=lambda: self.quote.observed_at + timedelta(seconds=59),
            )
            item = application.snapshot()["items"][0]
            self.assertEqual(item["health_status"], "REALTIME")
            self.assertEqual(item["action"], "BUY_CANDIDATE")
            self.assertEqual(item.get("timestamp_basis"), "MINUTE_START")


class SwingMinuteConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reuse the existing isolated ledger/data fixture; never touches runtime data.
        self.fixture = swing_fixtures.SwingServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.make_service()
        self.now = datetime.fromisoformat("2026-09-01T14:00:00+08:00")
        self.raw = {
            "symbol": "510300", "price": 105.9,
            "timestamp": "2026-09-01T13:58:00+08:00",
            "timestamp_basis": "MINUTE_START", "health_status": "REALTIME",
        }

    def test_swing_consumer_uses_minute_end_but_retains_the_original_display_time(self) -> None:
        healthy, stamp, status = self.service._validated_realtime_quote(self.raw, self.now)
        self.assertTrue(healthy)
        self.assertEqual(status, "REALTIME")
        self.assertEqual(stamp, self.raw["timestamp"])

    def test_swing_consumer_does_not_extend_unmarked_legacy_quotes(self) -> None:
        raw = dict(self.raw)
        raw.pop("timestamp_basis")
        healthy, _, status = self.service._validated_realtime_quote(raw, self.now)
        self.assertFalse(healthy)
        self.assertEqual(status, "STALE")

    def test_swing_consumer_rejects_unfinished_or_unknown_minute_basis(self) -> None:
        cases = (
            {**self.raw, "timestamp": "2026-09-01T14:00:00+08:00"},
            {**self.raw, "timestamp": "2026-09-01T13:59:59+08:00", "timestamp_basis": "UNKNOWN"},
        )
        for raw in cases:
            with self.subTest(raw=raw):
                healthy, _, _ = self.service._validated_realtime_quote(raw, self.now)
                self.assertFalse(healthy)

    def test_swing_consumer_never_promotes_an_explicitly_failed_source(self) -> None:
        for status in ("DELAYED", "STALE", "OUTAGE"):
            with self.subTest(status=status):
                healthy, _, result = self.service._validated_realtime_quote(
                    {**self.raw, "timestamp": "2026-09-01T13:59:00+08:00", "health_status": status}, self.now,
                )
                self.assertFalse(healthy)
                self.assertEqual(result, status)
