from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest

from etf_rotation.t_monitor import MarketDataError
from etf_rotation.t_web import MonitorApplication
from tests.regime_fixtures import confirmed_range_quote


def test_metadata_document() -> dict[str, object]:
    return {
        "schema_version": 2,
        "items": [{
            "symbol": "510300",
            "name": "沪深300ETF",
            "index": {"code": "000300", "name": "沪深300", "provider": "中证指数"},
            "trading": {
                "exchange": "SSE",
                "asset_type": "DOMESTIC_EQUITY_ETF",
                "intraday_turnaround": False,
                "sellable_delay_days": 1,
                "lot_size": 100,
                "price_tick": 0.001,
                "price_limit_pct": 0.10,
                "volume_unit_shares": 100,
            },
        }],
    }


def valid_completed_quote_payload() -> dict[str, object]:
    quote = confirmed_range_quote(-0.008, -0.007)
    return {
        "collected_at": quote.observed_at.isoformat(),
        "source": {"name": "TEST"},
        "quotes": [{
            "symbol": quote.symbol,
            "name": quote.name,
            "price": quote.price,
            "average_price": quote.average_price,
            "previous_close": quote.previous_close,
            "timestamp": quote.timestamp.isoformat(),
            "observed_at": quote.observed_at.isoformat(),
            "source": quote.source,
            "points": [{
                "timestamp": point.timestamp.isoformat(),
                "price": point.price,
                "average_price": point.average_price,
                "open": point.open,
                "high": point.high,
                "low": point.low,
                "volume": point.volume,
                "amount": point.amount,
            } for point in quote.points],
        }],
    }


class StaticCollector:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.calls = 0

    def collect_to_file(self, watchlist: object, path: Path) -> dict[str, object]:
        self.calls += 1
        path.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")
        return self.payload


class BlockingCollector(StaticCollector):
    def __init__(self, payload: dict[str, object]):
        super().__init__(payload)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.overlap = threading.Event()
        self._active = 0
        self._active_lock = threading.Lock()

    def collect_to_file(self, watchlist: object, path: Path) -> dict[str, object]:
        with self._active_lock:
            self._active += 1
            if self._active > 1:
                self.overlap.set()
        try:
            self.entered.set()
            if not self.release.wait(2):
                raise TimeoutError("test did not release collector")
            return super().collect_to_file(watchlist, path)
        finally:
            with self._active_lock:
                self._active -= 1


class FailingCollector:
    def __init__(self, message: str):
        self.message = message

    def collect_to_file(self, watchlist: object, path: Path) -> dict[str, object]:
        raise MarketDataError(self.message)


class FailingStore:
    def __init__(self, message: str, method: str):
        self.message = message
        self.method = method

    def upsert(self, quotes: object, metadata: object) -> int:
        if self.method == "upsert":
            raise OSError(self.message)
        return 0

    def append_candidates(self, payload: object) -> None:
        if self.method == "append_candidates":
            raise OSError(self.message)


_DEFAULT_COLLECTOR = object()


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = SimpleNamespace(
            quotes=root / "quotes.json",
            watchlist=root / "watchlist.json",
            history=root / "quotes.jsonl",
            alerts=root / "alerts.jsonl",
            metadata=root / "etf_metadata.json",
            calendar=root / "market_calendar.json",
        )
        self.paths.watchlist.write_text(json.dumps({
            "watchlist": [{
                "symbol": "510300", "name": "ETF", "grid_width_pct": 0.002,
            }],
        }), encoding="utf-8")
        self.paths.metadata.write_text(
            json.dumps(test_metadata_document()), encoding="utf-8",
        )
        self.paths.calendar.write_text(json.dumps({
            "schema_version": 1, "closed_dates": [],
        }), encoding="utf-8")
        self.paths.history.write_bytes(b"")
        self.paths.alerts.write_bytes(b"")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_runtime_fixture(
        self, collector: object | None = _DEFAULT_COLLECTOR,
    ) -> MonitorApplication:
        if collector is _DEFAULT_COLLECTOR:
            collector = StaticCollector(valid_completed_quote_payload())
        return MonitorApplication(
            quotes_path=self.paths.quotes,
            watchlist_path=self.paths.watchlist,
            history_path=self.paths.history,
            collector=collector,
            refresh_interval=5.0,
            alert_history_path=self.paths.alerts,
            metadata_path=self.paths.metadata,
            valuation_path=None,
            calendar_path=self.paths.calendar,
            clock=lambda: datetime.fromisoformat("2026-08-28T10:02:00+08:00"),
        )

    def test_snapshot_reads_published_state_without_writing_or_revising(self) -> None:
        collector = StaticCollector(valid_completed_quote_payload())
        app = self.make_runtime_fixture(collector)
        self.assertTrue(app.refresh_once())
        history_before = self.paths.history.read_bytes()
        alerts_before = self.paths.alerts.read_bytes()
        first = app.snapshot()
        first["items"][0]["price"] = -1
        second = app.snapshot()
        self.assertEqual(second["revision"], 1)
        self.assertNotEqual(second["items"][0]["price"], -1)
        self.assertEqual(self.paths.history.read_bytes(), history_before)
        self.assertEqual(self.paths.alerts.read_bytes(), alerts_before)
        self.assertEqual(collector.calls, 1)

    def test_successful_refresh_persists_each_minute_and_candidate_once(self) -> None:
        app = self.make_runtime_fixture()
        self.assertTrue(app.refresh_once())
        first = app.snapshot()
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["items"][0]["health_status"], "REALTIME")
        self.assertEqual(first["items"][0]["action"], "BUY_CANDIDATE")
        history = [json.loads(line) for line in self.paths.history.read_text(encoding="utf-8").splitlines()]
        alerts = [json.loads(line) for line in self.paths.alerts.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(history), len(valid_completed_quote_payload()["quotes"][0]["points"]))
        self.assertEqual(len({(item["symbol"], item["timestamp"]) for item in history}), len(history))
        self.assertEqual(len(alerts), 1)

    def test_snapshot_returns_previous_revision_during_concurrent_refresh(self) -> None:
        collector = BlockingCollector(valid_completed_quote_payload())
        app = self.make_runtime_fixture(collector)
        with ThreadPoolExecutor(max_workers=3) as executor:
            refresh = executor.submit(app.refresh_once)
            self.assertTrue(collector.entered.wait(1))
            snapshots = [executor.submit(app.snapshot) for _ in range(2)]
            results = [item.result(timeout=1) for item in snapshots]
            self.assertEqual([item["revision"] for item in results], [0, 0])
            self.assertEqual(self.paths.history.read_bytes(), b"")
            self.assertEqual(self.paths.alerts.read_bytes(), b"")
            collector.release.set()
            self.assertTrue(refresh.result(timeout=2))
        self.assertEqual(app.snapshot()["revision"], 1)
        records = self.paths.history.read_text(encoding="utf-8").splitlines()
        alerts = self.paths.alerts.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(records), len(valid_completed_quote_payload()["quotes"][0]["points"]))
        self.assertEqual(len(alerts), 1)

    def test_concurrent_refreshes_are_serialized_without_duplicate_records(self) -> None:
        collector = BlockingCollector(valid_completed_quote_payload())
        app = self.make_runtime_fixture(collector)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(app.refresh_once)
            self.assertTrue(collector.entered.wait(1))
            second = executor.submit(app.refresh_once)
            self.assertFalse(collector.overlap.wait(0.1))
            collector.release.set()
            self.assertTrue(first.result(timeout=2))
            self.assertTrue(second.result(timeout=2))
        self.assertEqual(app.snapshot()["revision"], 2)
        records = [json.loads(line) for line in self.paths.history.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len({(item["symbol"], item["timestamp"]) for item in records}), len(records))
        self.assertEqual(len(self.paths.alerts.read_text(encoding="utf-8").splitlines()), 1)

    def test_start_refresh_is_idempotent(self) -> None:
        collector = BlockingCollector(valid_completed_quote_payload())
        app = self.make_runtime_fixture(collector)
        app.start_refresh()
        try:
            self.assertTrue(collector.entered.wait(1))
            thread = app._refresh_thread
            app.start_refresh()
            self.assertIs(app._refresh_thread, thread)
            self.assertFalse(collector.overlap.is_set())
        finally:
            collector.release.set()
            app.stop_refresh()
        self.assertEqual(collector.calls, 1)

    def test_failed_refresh_immediately_revokes_published_candidate(self) -> None:
        app = self.make_runtime_fixture()
        self.assertTrue(app.refresh_once())
        alerts_before = self.paths.alerts.read_bytes()
        previous = app.snapshot()
        app.collector = FailingCollector("断流")
        self.assertFalse(app.refresh_once())
        failed = app.snapshot()
        item = failed["items"][0]
        self.assertEqual(failed["revision"], previous["revision"] + 1)
        self.assertEqual(item["price"], previous["items"][0]["price"])
        self.assertEqual(item["health_status"], "OUTAGE")
        self.assertEqual(item["action"], "DEVIATION_OBSERVE")
        self.assertIn("MARKET_NOT_REALTIME", item["blocked_reasons"])
        self.assertEqual(failed["errors"], ["断流"])
        self.assertEqual(self.paths.alerts.read_bytes(), alerts_before)

    def test_history_or_alert_failure_never_publishes_success_state(self) -> None:
        for attribute, method in (("history_store", "upsert"), ("alert_store", "append_candidates")):
            with self.subTest(attribute=attribute):
                app = self.make_runtime_fixture()
                setattr(app, attribute, FailingStore(f"{attribute} failed", method))
                self.assertFalse(app.refresh_once())
                snapshot = app.snapshot()
                self.assertEqual(snapshot["revision"], 1)
                self.assertEqual(snapshot["errors"], [f"{attribute} failed"])
                self.assertFalse(any(
                    item["action"] in {"BUY_CANDIDATE", "SELL_CANDIDATE"}
                    for item in snapshot["items"]
                ))

    def test_invalid_calendar_fails_application_startup(self) -> None:
        self.paths.calendar.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(MarketDataError, "schema_version"):
            self.make_runtime_fixture()

    def test_collector_free_bootstrap_is_read_only_and_safe_without_quotes(self) -> None:
        history_before = self.paths.history.read_bytes()
        alerts_before = self.paths.alerts.read_bytes()
        app = MonitorApplication(
            quotes_path=self.paths.quotes,
            watchlist_path=self.paths.watchlist,
            history_path=self.paths.history,
            collector=None,
            alert_history_path=self.paths.alerts,
            metadata_path=self.paths.metadata,
            calendar_path=self.paths.calendar,
            clock=lambda: datetime.fromisoformat("2026-08-28T10:02:00+08:00"),
        )
        snapshot = app.snapshot()
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(snapshot["items"][0]["status"], "MISSING_QUOTE")
        self.assertTrue(snapshot["errors"])
        self.assertEqual(self.paths.history.read_bytes(), history_before)
        self.assertEqual(self.paths.alerts.read_bytes(), alerts_before)
        self.assertFalse(self.paths.quotes.exists())

    def test_collector_free_bootstrap_reads_existing_quotes_without_writes(self) -> None:
        payload = valid_completed_quote_payload()
        self.paths.quotes.write_text(json.dumps(payload), encoding="utf-8")
        quotes_before = self.paths.quotes.read_bytes()
        history_before = self.paths.history.read_bytes()
        alerts_before = self.paths.alerts.read_bytes()
        app = self.make_runtime_fixture(collector=None)
        snapshot = app.snapshot()
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(snapshot["items"][0]["price"], payload["quotes"][0]["price"])
        self.assertEqual(self.paths.quotes.read_bytes(), quotes_before)
        self.assertEqual(self.paths.history.read_bytes(), history_before)
        self.assertEqual(self.paths.alerts.read_bytes(), alerts_before)


if __name__ == "__main__":
    unittest.main()
