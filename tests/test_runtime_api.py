from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from etf_rotation.t_monitor import MarketDataError
from etf_rotation.t_web import MonitorApplication, MonitorRequestHandler, MonitorServer
from tests.regime_fixtures import confirmed_range_quote


def test_metadata_document(*symbols: str) -> dict[str, object]:
    symbols = symbols or ("510300",)
    return {
        "schema_version": 2,
        "items": [{
            "symbol": symbol,
            "name": f"ETF-{symbol}",
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
        } for symbol in symbols],
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


def quote_payload_for_date(value: date | str) -> dict[str, object]:
    payload = copy.deepcopy(valid_completed_quote_payload())
    target_date = value if isinstance(value, date) else date.fromisoformat(value)
    quote = payload["quotes"][0]
    source_date = datetime.fromisoformat(quote["timestamp"]).date()
    offset = target_date - source_date
    for field in ("timestamp", "observed_at"):
        quote[field] = (datetime.fromisoformat(quote[field]) + offset).isoformat()
    for point in quote["points"]:
        point["timestamp"] = (
            datetime.fromisoformat(point["timestamp"]) + offset
        ).isoformat()
    payload["collected_at"] = quote["observed_at"]
    return payload


def quote_payload_ending_at(
    value: date | str, ending_at: str,
) -> dict[str, object]:
    payload = quote_payload_for_date(value)
    quote = payload["quotes"][0]
    latest = datetime.fromisoformat(quote["timestamp"])
    hour, minute = (int(part) for part in ending_at.split(":"))
    target = latest.replace(hour=hour, minute=minute)
    offset = target - latest
    for field in ("timestamp", "observed_at"):
        quote[field] = (datetime.fromisoformat(quote[field]) + offset).isoformat()
    for point in quote["points"]:
        point["timestamp"] = (
            datetime.fromisoformat(point["timestamp"]) + offset
        ).isoformat()
    payload["collected_at"] = quote["observed_at"]
    return payload


def mixed_health_quote_payload() -> dict[str, object]:
    payload = valid_completed_quote_payload()
    stale = copy.deepcopy(payload["quotes"][0])
    stale["symbol"] = "510500"
    stale["name"] = "中证500ETF"
    fresh = payload["quotes"][0]
    fresh["timestamp"] = (
        datetime.fromisoformat(fresh["timestamp"]) + timedelta(minutes=8)
    ).isoformat()
    fresh["observed_at"] = (
        datetime.fromisoformat(fresh["observed_at"]) + timedelta(minutes=8)
    ).isoformat()
    for point in fresh["points"]:
        point["timestamp"] = (
            datetime.fromisoformat(point["timestamp"]) + timedelta(minutes=8)
        ).isoformat()
    payload["collected_at"] = fresh["observed_at"]
    payload["quotes"].append(stale)
    return payload


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


class LifecycleCollector(StaticCollector):
    def __init__(self, payload: dict[str, object]):
        super().__init__(payload)
        self.entered = threading.Event()
        self.release = threading.Event()

    def collect_to_file(self, watchlist: object, path: Path) -> dict[str, object]:
        self.entered.set()
        if not self.release.wait(8):
            raise TimeoutError("test did not release collector")
        return super().collect_to_file(watchlist, path)


class FailingCollector:
    def __init__(self, message: str):
        self.message = message

    def collect_to_file(self, watchlist: object, path: Path) -> dict[str, object]:
        raise MarketDataError(self.message)


class RecordingWaitEvent:
    def __init__(self, *, stop_after: int):
        self.stop_after = stop_after
        self.waits: list[float] = []
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def set(self) -> None:
        self.stopped = True

    def clear(self) -> None:
        self.stopped = False

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        if len(self.waits) >= self.stop_after:
            self.stopped = True
        return self.stopped


class ObservableWaitEvent:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.wait_entered = threading.Event()
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        self.wait_entered.set()
        return self._event.wait(timeout)


class SequenceClock:
    def __init__(self, *values: str):
        self.values = [datetime.fromisoformat(value) for value in values]
        self.calls = 0

    def __call__(self) -> datetime:
        index = min(self.calls, len(self.values) - 1)
        self.calls += 1
        return self.values[index]


class MutableClock:
    def __init__(self, value: str):
        self.current = datetime.fromisoformat(value)

    def __call__(self) -> datetime:
        return self.current


class AdvancingWaitEvent(RecordingWaitEvent):
    def __init__(self, clock: MutableClock, *, stop_after: int):
        super().__init__(stop_after=stop_after)
        self.clock = clock

    def wait(self, timeout: float) -> bool:
        result = super().wait(timeout)
        self.clock.current += timedelta(seconds=timeout)
        return result


class AbortingRequestLine(io.BytesIO):
    def __init__(self, entered: threading.Event):
        super().__init__()
        self.entered = entered

    def readline(self, size: int = -1) -> bytes:
        self.entered.set()
        raise ConnectionAbortedError(10053, "request-line connection aborted")


class FailingStore:
    def __init__(self, message: str, method: str):
        self.message = message
        self.method = method
        self.calls = 0

    def upsert(self, quotes: object, metadata: object) -> int:
        self.calls += 1
        if self.method == "upsert":
            raise OSError(self.message)
        return 0

    def append_candidates(self, payload: object) -> None:
        self.calls += 1
        if self.method == "append_candidates":
            raise OSError(self.message)


class ScriptedEventApplication:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self.snapshot_calls = 0
        self.waits: list[int] = []
        self.script: list[dict[str, object] | None | str] = [
            {"event": "delta", "revision": 8, "items": [], "upserts": {}},
            None,
            {"event": "delta", "revision": 9, "items": [], "upserts": {}},
            "stop",
        ]

    def snapshot(self) -> dict[str, object]:
        self.snapshot_calls += 1
        return {"revision": 7, "items": []}

    def wait_for_revision(
        self, after_revision: int, timeout: float,
    ) -> dict[str, object] | None:
        self.waits.append(after_revision)
        item = self.script.pop(0)
        if item == "stop":
            self._stop_event.set()
            return None
        return item

    def is_stopping(self) -> bool:
        return self._stop_event.is_set()


class AbortedStream:
    def __init__(self) -> None:
        self.flush_calls = 0

    def write(self, value: bytes) -> None:
        raise ConnectionAbortedError(10053, "已建立的连接被主机中的软件中止")

    def flush(self) -> None:
        self.flush_calls += 1


class ExplodingStream:
    def write(self, value: bytes) -> None:
        raise RuntimeError("unexpected stream failure")

    def flush(self) -> None:
        raise AssertionError("flush must not run after write fails")


class FlushAbortedStream:
    def __init__(self) -> None:
        self.flush_calls = 0
        self.writes: list[bytes] = []

    def write(self, value: bytes) -> None:
        self.writes.append(value)

    def flush(self) -> None:
        self.flush_calls += 1
        raise ConnectionAbortedError(10053, "已建立的连接被主机中的软件中止")


class SnapshotAbortedApplication(ScriptedEventApplication):
    def snapshot(self) -> dict[str, object]:
        raise ConnectionAbortedError(10053, "application failure")


class RestartCursorApplication:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self.waits: list[int] = []

    def snapshot(self) -> dict[str, object]:
        return {"revision": 1, "items": []}

    def wait_for_revision(
        self, after_revision: int, timeout: float,
    ) -> dict[str, object] | None:
        self.waits.append(after_revision)
        self._stop_event.set()
        return {
            **self.snapshot(),
            "event": "reset",
            "reset": True,
        }

    def is_stopping(self) -> bool:
        return self._stop_event.is_set()


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
        self,
        collector: object | None = _DEFAULT_COLLECTOR,
        *,
        revision_event_limit: int = 128,
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
            calendar_path=self.paths.calendar,
            clock=lambda: datetime.fromisoformat("2026-08-28T10:02:00+08:00"),
            revision_event_limit=revision_event_limit,
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

    def test_read_paths_never_expose_a_previous_market_date(self) -> None:
        cases = (
            ("2026-08-29T10:02:00+08:00", [], "CLOSED"),
            ("2026-08-31T09:00:00+08:00", [], "CLOSED"),
            ("2026-08-31T10:02:00+08:00", ["2026-08-31"], "CLOSED"),
        )
        for collector in (StaticCollector(quote_payload_for_date("2026-08-28")), None):
            for current_time, closed_dates, expected_health in cases:
                with self.subTest(
                    collector=type(collector).__name__, current_time=current_time,
                ):
                    self.paths.quotes.write_text(
                        json.dumps(quote_payload_for_date("2026-08-28")),
                        encoding="utf-8",
                    )
                    self.paths.calendar.write_text(json.dumps({
                        "schema_version": 1, "closed_dates": closed_dates,
                    }), encoding="utf-8")
                    clock = MutableClock("2026-08-28T10:02:00+08:00")
                    app = self.make_runtime_fixture(collector)
                    app.clock = clock
                    app._bootstrap(increment_revision=True)
                    revision = app._revision
                    history_before = self.paths.history.read_bytes()
                    quotes_before = self.paths.quotes.read_bytes()

                    clock.current = datetime.fromisoformat(current_time)
                    self.assertFalse(app.collection_due())
                    snapshot = app.snapshot()
                    delta = app.quotes("510300", since=0)

                    self.assertEqual(app._revision, revision)
                    self.assertEqual(snapshot["revision"], revision)
                    self.assertEqual(snapshot["generated_at"], current_time)
                    self.assertEqual(snapshot["items"][0]["status"], "MISSING_QUOTE")
                    self.assertEqual(
                        snapshot["items"][0]["health_status"], expected_health,
                    )
                    for key in (
                        "price", "average_price", "previous_close", "timestamp",
                        "path_efficiency", "one_side_ratio", "vwap_slope",
                    ):
                        self.assertIsNone(snapshot["items"][0][key], key)
                    self.assertEqual(snapshot["items"][0]["trade_markers"], [])
                    self.assertTrue(delta["reset"])
                    self.assertEqual(delta["revision"], revision)
                    self.assertEqual(delta["upserts"], [])
                    self.assertEqual(self.paths.history.read_bytes(), history_before)
                    self.assertEqual(self.paths.quotes.read_bytes(), quotes_before)

    def test_producer_publishes_exactly_one_authoritative_cross_date_reset(self) -> None:
        cases = (
            ("2026-08-29T10:02:00+08:00", []),
            ("2026-08-31T09:00:00+08:00", []),
            ("2026-08-31T10:02:00+08:00", ["2026-08-31"]),
        )
        for current_time, closed_dates in cases:
            with self.subTest(current_time=current_time):
                payload = quote_payload_for_date("2026-08-28")
                self.paths.quotes.write_text(json.dumps(payload), encoding="utf-8")
                self.paths.calendar.write_text(json.dumps({
                    "schema_version": 1, "closed_dates": closed_dates,
                }), encoding="utf-8")
                collector = StaticCollector(payload)
                clock = MutableClock("2026-08-28T10:02:00+08:00")
                app = self.make_runtime_fixture(collector)
                app.clock = clock
                app._bootstrap(increment_revision=False)
                clock.current = datetime.fromisoformat(current_time)
                history_before = self.paths.history.read_bytes()
                quotes_before = self.paths.quotes.read_bytes()
                waits = RecordingWaitEvent(stop_after=2)
                app._stop_event = waits
                app._generation = 1
                app._refresh_thread = threading.current_thread()

                app._refresh_loop(1)

                self.assertEqual(collector.calls, 0)
                self.assertEqual(app._revision, 1)
                self.assertEqual(app._published["generated_at"], current_time)
                self.assertEqual(
                    app._published["items"][0]["status"], "MISSING_QUOTE",
                )
                self.assertEqual(app._revision_events[-1]["resets"], ["510300"])
                self.assertEqual(self.paths.history.read_bytes(), history_before)
                self.assertEqual(self.paths.quotes.read_bytes(), quotes_before)

    def test_cancelled_generation_cannot_publish_a_cross_date_reset(self) -> None:
        payload = quote_payload_for_date("2026-08-28")
        self.paths.quotes.write_text(json.dumps(payload), encoding="utf-8")
        app = self.make_runtime_fixture(StaticCollector(payload))
        before = copy.deepcopy(app._published)
        app._generation = 2
        app._refresh_thread = threading.current_thread()

        published = app._publish_cross_date_reset(
            datetime.fromisoformat("2026-08-29T10:02:00+08:00"), generation=1,
        )

        self.assertFalse(published)
        self.assertEqual(app._revision, 0)
        self.assertEqual(app._published, before)

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

    def test_refresh_classifies_health_per_symbol_and_blocks_only_stale_quote(self) -> None:
        self.paths.metadata.write_text(
            json.dumps(test_metadata_document("510300", "510500")), encoding="utf-8",
        )
        self.paths.watchlist.write_text(json.dumps({"watchlist": [
            {"symbol": "510300", "name": "fresh", "grid_width_pct": 0.002},
            {"symbol": "510500", "name": "stale", "grid_width_pct": 0.002},
        ]}), encoding="utf-8")
        app = self.make_runtime_fixture(StaticCollector(mixed_health_quote_payload()))
        app.clock = lambda: datetime.fromisoformat("2026-08-28T10:10:00+08:00")
        self.assertTrue(app.refresh_once(), app.snapshot())
        items = {item["symbol"]: item for item in app.snapshot()["items"]}
        self.assertEqual(items["510300"]["health_status"], "REALTIME")
        self.assertEqual(items["510300"]["action"], "BUY_CANDIDATE")
        self.assertEqual(items["510500"]["health_status"], "OUTAGE")
        self.assertNotIn(items["510500"]["action"], {"BUY_CANDIDATE", "SELL_CANDIDATE"})

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

    def test_closed_session_with_complete_current_day_never_collects(self) -> None:
        payload = quote_payload_ending_at("2026-08-28", "15:00")
        self.paths.quotes.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8",
        )
        collector = StaticCollector(payload)
        app = self.make_runtime_fixture(collector)
        app.clock = lambda: datetime.fromisoformat("2026-08-28T15:10:00+08:00")
        app._bootstrap(increment_revision=True)

        self.assertFalse(app.collection_due())
        app.start_refresh()
        threading.Event().wait(0.05)
        app.stop_refresh()

        self.assertEqual(collector.calls, 0)

    def test_closed_session_without_current_day_data_catches_up_once(self) -> None:
        payload = quote_payload_ending_at("2026-08-28", "15:00")
        collector = StaticCollector(payload)
        app = self.make_runtime_fixture(collector)
        app.clock = lambda: datetime.fromisoformat("2026-08-28T15:10:00+08:00")

        self.assertTrue(app.collection_due())
        app.start_refresh()
        revision = app.wait_for_revision(0, timeout=1.0)
        app.stop_refresh()

        self.assertIsNotNone(revision)
        self.assertEqual(collector.calls, 1)
        self.assertFalse(app.collection_due())

    def test_lunch_catch_up_requires_the_morning_session_endpoint(self) -> None:
        for ending_at, expected_due in (("11:29", True), ("11:30", False)):
            with self.subTest(ending_at=ending_at):
                payload = quote_payload_ending_at("2026-08-28", ending_at)
                self.paths.quotes.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8",
                )
                app = self.make_runtime_fixture(StaticCollector(payload))
                app.clock = lambda: datetime.fromisoformat(
                    "2026-08-28T12:00:00+08:00"
                )
                app._bootstrap(increment_revision=True)

                self.assertEqual(app.collection_due(), expected_due)

    def test_close_catch_up_requires_the_afternoon_session_endpoint(self) -> None:
        cases = (("11:30", True), ("14:59", True), ("15:00", False))
        for ending_at, expected_due in cases:
            with self.subTest(ending_at=ending_at):
                payload = quote_payload_ending_at("2026-08-28", ending_at)
                self.paths.quotes.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8",
                )
                app = self.make_runtime_fixture(StaticCollector(payload))
                app.clock = lambda: datetime.fromisoformat(
                    "2026-08-28T15:10:00+08:00"
                )
                app._bootstrap(increment_revision=True)

                self.assertEqual(app.collection_due(), expected_due)

    def test_invalid_or_naive_completion_timestamp_is_not_complete(self) -> None:
        payload = quote_payload_ending_at("2026-08-28", "15:00")
        self.paths.quotes.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8",
        )
        app = self.make_runtime_fixture(StaticCollector(payload))
        app.clock = lambda: datetime.fromisoformat("2026-08-28T15:10:00+08:00")
        app._bootstrap(increment_revision=True)

        for timestamp in ("not-a-time", "2026-08-28T15:00:00"):
            with self.subTest(timestamp=timestamp):
                with app._publish_condition:
                    app._published["items"][0]["timestamp"] = timestamp
                self.assertTrue(app.collection_due())

    def test_empty_or_disabled_watchlist_never_collects(self) -> None:
        cases = (
            ("2026-08-28T10:02:00+08:00", {"watchlist": []}),
            ("2026-08-28T15:10:00+08:00", {"watchlist": [{
                "symbol": "510300", "name": "ETF", "grid_width_pct": 0.002,
                "enabled": False,
            }]}),
        )
        for current_time, watchlist in cases:
            with self.subTest(current_time=current_time):
                self.paths.watchlist.write_text(
                    json.dumps(watchlist), encoding="utf-8",
                )
                collector = StaticCollector(valid_completed_quote_payload())
                app = self.make_runtime_fixture(collector)
                app.clock = lambda value=current_time: datetime.fromisoformat(value)
                wait_event = ObservableWaitEvent()
                app._stop_event = wait_event

                self.assertFalse(app.collection_due())
                app.start_refresh()
                try:
                    self.assertTrue(wait_event.wait_entered.wait(0.5))
                finally:
                    app.stop_refresh()
                self.assertEqual(collector.calls, 0)

    def test_weekend_without_current_day_data_never_collects(self) -> None:
        collector = StaticCollector(quote_payload_for_date("2026-08-29"))
        app = self.make_runtime_fixture(collector)
        app.clock = lambda: datetime.fromisoformat("2026-08-29T10:02:00+08:00")

        self.assertFalse(app.collection_due())
        app.start_refresh()
        threading.Event().wait(0.05)
        app.stop_refresh()

        self.assertEqual(collector.calls, 0)

    def test_collection_failure_uses_current_market_session_health(self) -> None:
        cases = (
            ("2026-08-28T10:02:00+08:00", "OUTAGE", ["采集失败"]),
            ("2026-08-28T15:10:00+08:00", "CLOSED", []),
        )
        for current_time, health_status, errors in cases:
            with self.subTest(current_time=current_time):
                self.paths.quotes.write_text(
                    json.dumps(
                        quote_payload_for_date("2026-08-28"), ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                app = self.make_runtime_fixture(FailingCollector("采集失败"))
                app.clock = lambda value=current_time: datetime.fromisoformat(value)
                app._bootstrap(increment_revision=True)

                self.assertFalse(app.refresh_once())

                snapshot = app.snapshot()
                self.assertEqual(snapshot["items"][0]["health_status"], health_status)
                self.assertEqual(snapshot["errors"], errors)

    def test_collection_failure_uses_one_completion_time_across_boundaries(self) -> None:
        cases = (
            (
                quote_payload_ending_at("2026-08-28", "11:30"),
                ("2026-08-28T12:59:59+08:00", "2026-08-28T13:00:00+08:00"),
                "LUNCH_BREAK",
            ),
            (
                quote_payload_ending_at("2026-08-28", "15:00"),
                ("2026-08-28T15:00:00+08:00", "2026-08-28T15:00:01+08:00"),
                "OUTAGE",
            ),
        )
        for payload, clock_values, expected_health in cases:
            with self.subTest(clock_values=clock_values):
                self.paths.quotes.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8",
                )
                app = self.make_runtime_fixture(FailingCollector("采集失败"))
                clock = SequenceClock(*clock_values)
                app.clock = clock

                app._publish_collection_failure("采集失败")
                self.assertEqual(clock.calls, 1)

                snapshot = app.snapshot()
                self.assertEqual(snapshot["generated_at"], clock_values[0])
                self.assertEqual(
                    snapshot["items"][0]["health_status"], expected_health,
                )
                self.assertEqual(clock.calls, 2)

    def test_inactive_catch_up_failures_back_off_without_revision_churn(self) -> None:
        cases = (
            ("11:29", "2026-08-28T12:00:00+08:00", "LUNCH_BREAK"),
            ("14:59", "2026-08-28T15:10:00+08:00", "CLOSED"),
        )
        for ending_at, current_time, expected_health in cases:
            with self.subTest(current_time=current_time):
                payload = quote_payload_ending_at("2026-08-28", ending_at)
                self.paths.quotes.write_text(json.dumps(payload), encoding="utf-8")
                app = self.make_runtime_fixture(FailingCollector("采集失败"))
                clock = MutableClock(current_time)
                app.clock = clock
                app.refresh_interval = 60.0
                waits = AdvancingWaitEvent(clock, stop_after=2)
                app._stop_event = waits
                app._generation = 1
                app._refresh_thread = threading.current_thread()
                event_count = len(app._revision_events)

                app._refresh_loop(1)

                self.assertEqual(waits.waits, [60.0, 120.0])
                self.assertEqual(app._revision, 1)
                self.assertEqual(len(app._revision_events), event_count + 1)
                snapshot = app.snapshot()
                self.assertEqual(snapshot["errors"], [])
                self.assertIsNone(snapshot["refresh_error"])
                self.assertEqual(
                    snapshot["items"][0]["health_status"], expected_health,
                )

    def test_collection_due_accepts_one_authoritative_cycle_time(self) -> None:
        payload = quote_payload_ending_at("2026-08-28", "15:00")
        self.paths.quotes.write_text(json.dumps(payload), encoding="utf-8")
        app = self.make_runtime_fixture(StaticCollector(payload))

        def unexpected_clock() -> datetime:
            raise AssertionError("collection_due must use the injected cycle time")

        app.clock = unexpected_clock

        self.assertFalse(app.collection_due(
            datetime.fromisoformat("2026-08-28T15:10:00+08:00"),
        ))

    def test_cycle_does_not_cross_midnight_or_opening_boundary_mid_decision(self) -> None:
        cases = (
            (
                "2026-08-28T23:59:59+08:00",
                "2026-08-31T09:30:00+08:00",
            ),
            (
                "2026-08-31T09:29:59+08:00",
                "2026-08-31T09:30:00+08:00",
            ),
        )
        for first, second in cases:
            with self.subTest(first=first, second=second):
                payload = quote_payload_ending_at(first[:10], "15:00")
                self.paths.quotes.write_text(json.dumps(payload), encoding="utf-8")
                collector = StaticCollector(payload)
                app = self.make_runtime_fixture(collector)
                clock = SequenceClock(first, second)
                app.clock = clock
                waits = RecordingWaitEvent(stop_after=1)
                app._stop_event = waits
                app._generation = 1
                app._refresh_thread = threading.current_thread()

                app._refresh_loop(1)

                self.assertEqual(clock.calls, 1)
                self.assertEqual(collector.calls, 0)

    def test_refresh_delay_is_bounded_and_success_resets_loop_backoff(self) -> None:
        app = self.make_runtime_fixture()
        app.refresh_interval = 60.0
        self.assertEqual(
            [app.refresh_delay(count) for count in range(1, 6)],
            [60.0, 120.0, 240.0, 300.0, 300.0],
        )

        waits = RecordingWaitEvent(stop_after=2)
        outcomes = iter((False, True))
        app._stop_event = waits
        app._generation = 1
        app._refresh_thread = threading.current_thread()
        app.collection_due = lambda now=None: True
        app._refresh_once = lambda generation: next(outcomes)

        app._refresh_loop(1)

        self.assertEqual(waits.waits, [60.0, 60.0])

    def test_delay_until_next_minute_uses_completion_time(self) -> None:
        cases = (
            ("2026-08-28T10:02:05+08:00", 55.0),
            ("2026-08-28T10:03:00+08:00", 60.0),
            ("2026-08-28T10:03:59.500000+08:00", 0.5),
        )
        for completed_at, expected in cases:
            with self.subTest(completed_at=completed_at):
                self.assertEqual(
                    MonitorApplication.delay_until_next_minute(
                        datetime.fromisoformat(completed_at),
                    ),
                    expected,
                )

    def test_successful_five_second_batches_do_not_drift_from_minute_boundaries(self) -> None:
        app = self.make_runtime_fixture()
        clock = MutableClock("2026-08-28T10:02:00+08:00")
        waits = AdvancingWaitEvent(clock, stop_after=3)
        app.clock = clock
        app._stop_event = waits
        app._generation = 1
        app._refresh_thread = threading.current_thread()
        app.collection_due = lambda now=None: True

        def collect(generation: int) -> bool:
            clock.current += timedelta(seconds=5)
            return True

        app._refresh_once = collect

        app._refresh_loop(1)

        self.assertEqual(waits.waits, [55.0, 55.0, 55.0])
        self.assertEqual(clock.current.second, 0)

    def test_scheduling_error_publishes_outage_without_stopping_producer(self) -> None:
        app = self.make_runtime_fixture()
        app.clock = lambda: datetime.fromisoformat("2026-08-28T15:10:00+08:00")
        app.refresh_interval = 60.0
        wait_event = ObservableWaitEvent()
        app._stop_event = wait_event
        self.paths.watchlist.write_text("{", encoding="utf-8")

        app.start_refresh()
        try:
            revision = app.wait_for_revision(0, timeout=0.5)
            self.assertIsNotNone(revision)
            snapshot = app.snapshot()
            self.assertGreaterEqual(snapshot["revision"], 1)
            self.assertIsNotNone(snapshot["refresh_error"])
            self.assertEqual(snapshot["items"][0]["health_status"], "OUTAGE")
            self.assertTrue(wait_event.wait_entered.wait(0.5))
            self.assertEqual(wait_event.waits, [60.0])
            thread = app._refresh_thread
            self.assertIsNotNone(thread)
            self.assertTrue(thread.is_alive())
        finally:
            stop_started = time.monotonic()
            app.stop_refresh()
            stop_elapsed = time.monotonic() - stop_started

        self.assertLess(stop_elapsed, 0.5)
        self.assertIsNone(app._refresh_thread)

    def test_stop_cancels_blocked_generation_without_any_commit_side_effect(self) -> None:
        collector = LifecycleCollector(valid_completed_quote_payload())
        app = self.make_runtime_fixture(collector)
        app.refresh_interval = 0.01
        before = app.snapshot()
        app.start_refresh()
        self.assertTrue(collector.entered.wait(1))
        thread = app._refresh_thread
        app.stop_refresh()
        self.assertIs(app._refresh_thread, thread)
        self.assertTrue(thread.is_alive())
        app.start_refresh()
        self.assertIs(app._refresh_thread, thread)
        collector.release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(app._refresh_thread)
        self.assertEqual(collector.calls, 1)
        self.assertEqual(app.snapshot(), before)
        self.assertFalse(self.paths.quotes.exists())
        self.assertEqual(self.paths.history.read_bytes(), b"")
        self.assertEqual(self.paths.alerts.read_bytes(), b"")
        self.assertEqual(list(self.paths.quotes.parent.glob("*.staging")), [])

        app.collector = StaticCollector(valid_completed_quote_payload())
        app.refresh_interval = 5.0
        app.start_refresh()
        for _ in range(50):
            if app.snapshot()["revision"] == 1:
                break
            threading.Event().wait(0.01)
        app.stop_refresh()
        self.assertEqual(app.snapshot()["revision"], 1)
        self.assertTrue(self.paths.quotes.exists())
        self.assertTrue(self.paths.history.read_bytes())
        self.assertTrue(self.paths.alerts.read_bytes())

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

    def test_derived_failure_publishes_primary_and_retries_without_duplicates(self) -> None:
        for attribute, method in (("history_store", "upsert"), ("alert_store", "append_candidates")):
            with self.subTest(attribute=attribute):
                root = self.paths.quotes.parent / attribute
                root.mkdir()
                quotes = root / "quotes.json"
                history = root / "quotes.jsonl"
                alerts = root / "alerts.jsonl"
                history.write_bytes(b"")
                alerts.write_bytes(b"")
                old_payload = valid_completed_quote_payload()
                old_payload["source"] = {"name": "OLD"}
                quotes.write_text(json.dumps(old_payload, ensure_ascii=False), encoding="utf-8")
                current_payload = valid_completed_quote_payload()
                current_payload["source"] = {"name": "CURRENT"}
                app = MonitorApplication(
                    quotes_path=quotes,
                    watchlist_path=self.paths.watchlist,
                    history_path=history,
                    collector=StaticCollector(current_payload),
                    alert_history_path=alerts,
                    metadata_path=self.paths.metadata,
                    calendar_path=self.paths.calendar,
                    clock=lambda: datetime.fromisoformat("2026-08-28T10:02:00+08:00"),
                )
                original_store = getattr(app, attribute)
                failing = FailingStore(f"{attribute} failed", method)
                setattr(app, attribute, failing)
                self.assertTrue(app.refresh_once())
                snapshot = app.snapshot()
                self.assertEqual(snapshot["revision"], 1)
                self.assertEqual(snapshot["errors"], [f"{attribute} failed"])
                self.assertEqual(snapshot["persistence_errors"], [f"{attribute} failed"])
                self.assertIsNone(snapshot["refresh_error"])
                self.assertTrue(any(
                    item["action"] in {"BUY_CANDIDATE", "SELL_CANDIDATE"}
                    for item in snapshot["items"]
                ))
                self.assertEqual(
                    json.loads(quotes.read_text(encoding="utf-8"))["source"]["name"],
                    "CURRENT",
                )

                setattr(app, attribute, original_store)
                self.assertTrue(app.refresh_once())
                records = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
                alert_records = [json.loads(line) for line in alerts.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(
                    len({(item["symbol"], item["timestamp"]) for item in records}),
                    len(records),
                )
                self.assertEqual(len(records), len(current_payload["quotes"][0]["points"]))
                self.assertEqual(len(alert_records), 1)
                self.assertEqual(app.snapshot()["persistence_errors"], [])

    def test_failed_validation_never_replaces_official_quotes(self) -> None:
        old_payload = valid_completed_quote_payload()
        old_bytes = json.dumps(old_payload, ensure_ascii=False).encode("utf-8")
        rejected_payload = copy.deepcopy(old_payload)
        rejected_payload["source"] = {"name": "REJECTED"}
        self.paths.quotes.write_bytes(old_bytes)
        app = self.make_runtime_fixture(StaticCollector(rejected_payload))
        self.paths.metadata.write_text(
            json.dumps({"schema_version": 2, "items": []}), encoding="utf-8",
        )
        self.assertFalse(app.refresh_once())
        self.assertEqual(self.paths.quotes.read_bytes(), old_bytes)
        self.assertEqual(list(self.paths.quotes.parent.glob("*.staging")), [])

        self.paths.metadata.write_text(
            json.dumps(test_metadata_document()), encoding="utf-8",
        )
        restarted = self.make_runtime_fixture(collector=None)
        self.assertEqual(restarted.snapshot()["revision"], 1)
        self.assertEqual(
            restarted.snapshot()["items"][0]["price"],
            old_payload["quotes"][0]["price"],
        )

    def test_primary_replace_failure_skips_all_derived_stores(self) -> None:
        app = self.make_runtime_fixture()
        history = FailingStore("must not run history", "none")
        alerts = FailingStore("must not run alerts", "none")
        app.history_store = history
        app.alert_store = alerts
        with patch("etf_rotation.t_web.os.replace", side_effect=OSError("replace failed")):
            self.assertFalse(app.refresh_once())
        self.assertEqual(history.calls, 0)
        self.assertEqual(alerts.calls, 0)
        self.assertFalse(self.paths.quotes.exists())
        self.assertEqual(app.snapshot()["errors"], ["replace failed"])

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

    def test_collector_free_bootstrap_hides_quotes_from_an_older_trading_day(self) -> None:
        self.paths.quotes.write_text(
            json.dumps(quote_payload_for_date("2026-08-28"), ensure_ascii=False),
            encoding="utf-8",
        )
        app = MonitorApplication(
            quotes_path=self.paths.quotes,
            watchlist_path=self.paths.watchlist,
            history_path=self.paths.history,
            collector=None,
            alert_history_path=self.paths.alerts,
            metadata_path=self.paths.metadata,
            calendar_path=self.paths.calendar,
            clock=lambda: datetime.fromisoformat("2026-08-31T10:02:00+08:00"),
        )

        item = app.snapshot()["items"][0]
        self.assertEqual(item["status"], "MISSING_QUOTE")
        self.assertIsNone(item["price"])
        self.assertIsNone(item["timestamp"])
        quotes = app.quotes("510300", since=0)
        self.assertTrue(quotes["reset"])
        self.assertEqual(quotes["upserts"], [])

    def test_bootstrap_quote_cursor_uses_generated_day_not_latest_point_day(self) -> None:
        now = [datetime.fromisoformat("2026-08-28T10:02:00+08:00")]
        app = MonitorApplication(
            quotes_path=self.paths.quotes,
            watchlist_path=self.paths.watchlist,
            history_path=self.paths.history,
            collector=StaticCollector(quote_payload_for_date("2026-08-28")),
            alert_history_path=self.paths.alerts,
            metadata_path=self.paths.metadata,
            calendar_path=self.paths.calendar,
            clock=lambda: now[0],
        )
        self.assertTrue(app.refresh_once())
        self.assertTrue(app.quotes("510300", since=0)["upserts"])

        now[0] = datetime.fromisoformat("2026-08-31T10:02:00+08:00")
        app._bootstrap(increment_revision=True)

        quotes = app.quotes("510300", since=0)
        self.assertTrue(quotes["reset"])
        self.assertEqual(quotes["upserts"], [])

    def test_collector_free_add_rebuilds_published_watchlist_without_data_writes(self) -> None:
        self.paths.quotes.write_text(
            json.dumps(valid_completed_quote_payload(), ensure_ascii=False),
            encoding="utf-8",
        )
        self.paths.metadata.write_text(
            json.dumps(test_metadata_document("510300", "159915")),
            encoding="utf-8",
        )
        app = self.make_runtime_fixture(collector=None)
        before = {
            "quotes": self.paths.quotes.read_bytes(),
            "history": self.paths.history.read_bytes(),
            "alerts": self.paths.alerts.read_bytes(),
        }
        previous_revision = app.snapshot()["revision"]
        app.add_watch_item("159915", "创业板ETF")
        snapshot = app.snapshot()
        items = {item["symbol"]: item for item in snapshot["items"]}
        self.assertEqual(snapshot["revision"], previous_revision + 1)
        self.assertEqual(items["159915"]["status"], "MISSING_QUOTE")
        self.assertEqual(self.paths.quotes.read_bytes(), before["quotes"])
        self.assertEqual(self.paths.history.read_bytes(), before["history"])
        self.assertEqual(self.paths.alerts.read_bytes(), before["alerts"])

    def test_history_disabled_never_requires_or_loads_metadata(self) -> None:
        missing_metadata = self.paths.metadata.with_name("missing-metadata.json")
        app = MonitorApplication(
            quotes_path=self.paths.quotes,
            watchlist_path=self.paths.watchlist,
            history_path=None,
            collector=StaticCollector(valid_completed_quote_payload()),
            alert_history_path=None,
            metadata_path=missing_metadata,
            calendar_path=self.paths.calendar,
            clock=lambda: datetime.fromisoformat("2026-08-28T10:02:00+08:00"),
        )
        self.assertTrue(app.refresh_once())
        self.assertEqual(app.snapshot()["items"][0]["health_status"], "REALTIME")

    def test_history_enabled_bootstrap_validates_finalized_points_read_only(self) -> None:
        payload = valid_completed_quote_payload()
        payload["quotes"][0]["points"][0]["amount"] = 0.0
        self.paths.quotes.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8",
        )
        original = self.paths.quotes.read_bytes()
        app = self.make_runtime_fixture(collector=None)
        snapshot = app.snapshot()
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(snapshot["items"][0]["health_status"], "OUTAGE")
        self.assertTrue(snapshot["errors"])
        self.assertEqual(self.paths.quotes.read_bytes(), original)
        self.assertEqual(self.paths.history.read_bytes(), b"")
        self.assertEqual(self.paths.alerts.read_bytes(), b"")

    def test_revision_queue_preserves_fast_outage_recovery_and_wakes_on_stop(self) -> None:
        app = self.make_runtime_fixture()
        self.assertTrue(app.refresh_once())
        app.collector = FailingCollector("断流")
        self.assertFalse(app.refresh_once())
        app.collector = StaticCollector(valid_completed_quote_payload())
        self.assertTrue(app.refresh_once())

        outage = app.wait_for_revision(1, timeout=0.01)
        recovery = app.wait_for_revision(2, timeout=0.01)
        self.assertEqual(outage["revision"], 2)
        self.assertEqual(outage["refresh_error"], "断流")
        self.assertEqual(recovery["revision"], 3)
        self.assertIsNone(recovery["refresh_error"])
        self.assertIsNone(app.wait_for_revision(3, timeout=0.01))
        self.assertLessEqual(len(app._revision_events), app.revision_event_limit)

        with ThreadPoolExecutor(max_workers=1) as executor:
            waiting = executor.submit(app.wait_for_revision, 3, 5.0)
            app.stop_refresh()
            self.assertIsNone(waiting.result(timeout=0.5))

    def test_cross_day_outage_clears_previous_day_live_view(self) -> None:
        now = [datetime.fromisoformat("2026-08-28T10:02:00+08:00")]
        app = MonitorApplication(
            quotes_path=self.paths.quotes,
            watchlist_path=self.paths.watchlist,
            history_path=self.paths.history,
            collector=StaticCollector(quote_payload_for_date("2026-08-28")),
            alert_history_path=self.paths.alerts,
            metadata_path=self.paths.metadata,
            calendar_path=self.paths.calendar,
            clock=lambda: now[0],
        )
        self.assertTrue(app.refresh_once())
        self.assertIsNotNone(app.snapshot()["items"][0]["price"])
        quotes_before = self.paths.quotes.read_bytes()
        history_before = self.paths.history.read_bytes()

        now[0] = datetime.fromisoformat("2026-08-31T10:02:00+08:00")
        app.collector = FailingCollector("次日采集失败")
        self.assertFalse(app.refresh_once())

        item = app.snapshot()["items"][0]
        self.assertEqual(item["status"], "MISSING_QUOTE")
        self.assertIsNone(item["price"])
        self.assertIsNone(item["timestamp"])
        self.assertEqual(item["health_status"], "OUTAGE")
        self.assertEqual(item["health_reason"], "次日采集失败")
        self.assertNotIn(item["action"], {"BUY_CANDIDATE", "SELL_CANDIDATE"})
        self.assertEqual(app.snapshot()["refresh_error"], "次日采集失败")
        self.assertEqual(app.snapshot()["source"], {"name": "TEST"})
        self.assertEqual(app._published["items"][0]["points"], [])
        quotes = app.quotes("510300", since=0)
        self.assertTrue(quotes["reset"])
        self.assertEqual(quotes["upserts"], [])
        self.assertEqual(self.paths.quotes.read_bytes(), quotes_before)
        self.assertEqual(self.paths.history.read_bytes(), history_before)

    def test_snapshot_is_lightweight_and_quote_cursor_returns_revised_minutes(self) -> None:
        collector = StaticCollector(valid_completed_quote_payload())
        app = self.make_runtime_fixture(collector)
        self.assertTrue(app.refresh_once())

        summary = app.snapshot()
        self.assertNotIn("points", summary["items"][0])
        initial = app.quotes("510300", since=0)
        self.assertEqual(initial["symbol"], "510300")
        self.assertEqual(initial["revision"], 1)
        self.assertEqual(
            len(initial["upserts"]),
            len(valid_completed_quote_payload()["quotes"][0]["points"]),
        )
        self.assertTrue(all(
            point["schema_version"] == 3
            and point["trading_date"] == "2026-08-28"
            and point["observed_at"]
            and point["is_complete"] is True
            for point in initial["upserts"]
        ))

        revised = valid_completed_quote_payload()
        latest = revised["quotes"][0]["points"][-1]
        latest["price"] = 9.931
        latest["open"] = 9.931
        latest["amount"] = 993_100.0
        revised["quotes"][0]["price"] = 9.931
        collector.payload = revised
        self.assertTrue(app.refresh_once())

        delta = app.quotes("510300", since=1)
        self.assertEqual(delta["revision"], 2)
        self.assertEqual(len(delta["upserts"]), 1)
        self.assertEqual(delta["upserts"][0]["timestamp"], latest["timestamp"])
        self.assertEqual(delta["upserts"][0]["price"], 9.931)

    def test_quote_cursor_excludes_the_observation_current_minute(self) -> None:
        payload = valid_completed_quote_payload()
        quote = payload["quotes"][0]
        current_timestamp = quote["observed_at"]
        quote["points"].append({
            "timestamp": current_timestamp,
            "price": quote["price"],
            "average_price": quote["average_price"],
            "open": quote["price"],
            "high": quote["price"],
            "low": quote["price"],
            "volume": 100.0,
            "amount": quote["price"] * 10_000.0,
        })
        quote["timestamp"] = current_timestamp
        collector = StaticCollector(payload)
        app = self.make_runtime_fixture(collector)

        self.assertTrue(app.refresh_once())
        result = app.quotes("510300", since=0)

        self.assertNotIn(
            current_timestamp,
            {point["timestamp"] for point in result["upserts"]},
        )
        self.assertTrue(all(point["is_complete"] for point in result["upserts"]))

    def test_since_zero_is_an_initializing_full_reset_at_revision_zero_and_later(self) -> None:
        payload = valid_completed_quote_payload()
        self.paths.quotes.write_text(json.dumps(payload), encoding="utf-8")
        collector = StaticCollector(payload)
        app = self.make_runtime_fixture(collector)

        at_bootstrap = app.quotes("510300", since=0)
        self.assertEqual(at_bootstrap["revision"], 0)
        self.assertTrue(at_bootstrap["reset"])
        self.assertEqual(len(at_bootstrap["upserts"]), len(payload["quotes"][0]["points"]))

        self.assertTrue(app.refresh_once())
        after_identical_refresh = app.quotes("510300", since=0)
        self.assertEqual(after_identical_refresh["revision"], 1)
        self.assertTrue(after_identical_refresh["reset"])
        self.assertEqual(
            len(after_identical_refresh["upserts"]),
            len(payload["quotes"][0]["points"]),
        )

    def test_quote_shrink_and_missing_quote_force_authoritative_resets(self) -> None:
        collector = StaticCollector(valid_completed_quote_payload())
        app = self.make_runtime_fixture(collector)
        self.assertTrue(app.refresh_once())

        shortened = valid_completed_quote_payload()
        shortened_points = shortened["quotes"][0]["points"][:-1]
        shortened["quotes"][0]["points"] = shortened_points
        shortened["quotes"][0]["timestamp"] = shortened_points[-1]["timestamp"]
        shortened["quotes"][0]["price"] = shortened_points[-1]["price"]
        shortened["quotes"][0]["average_price"] = shortened_points[-1]["average_price"]
        collector.payload = shortened
        self.assertTrue(app.refresh_once())

        shrink = app.quotes("510300", since=1)
        self.assertTrue(shrink["reset"])
        self.assertEqual(len(shrink["upserts"]), len(shortened_points))
        self.assertNotIn(valid_completed_quote_payload()["quotes"][0]["points"][-1], shrink["upserts"])

        collector.payload = {
            "collected_at": shortened["collected_at"],
            "source": {"name": "EMPTY"},
            "quotes": [],
        }
        self.assertTrue(app.refresh_once())
        missing = app.quotes("510300", since=2)
        self.assertTrue(missing["reset"])
        self.assertEqual(missing["upserts"], [])

    def test_trading_day_change_resets_without_mixing_days(self) -> None:
        collector = StaticCollector(valid_completed_quote_payload())
        app = self.make_runtime_fixture(collector)
        self.assertTrue(app.refresh_once())

        next_day = valid_completed_quote_payload()
        for point in next_day["quotes"][0]["points"]:
            point["timestamp"] = (
                datetime.fromisoformat(point["timestamp"]) + timedelta(days=3)
            ).isoformat()
        quote = next_day["quotes"][0]
        quote["timestamp"] = next_day["quotes"][0]["points"][-1]["timestamp"]
        quote["observed_at"] = (
            datetime.fromisoformat(quote["observed_at"]) + timedelta(days=3)
        ).isoformat()
        next_day["collected_at"] = (
            datetime.fromisoformat(next_day["collected_at"]) + timedelta(days=3)
        ).isoformat()
        collector.payload = next_day
        app.clock = lambda: datetime.fromisoformat("2026-08-31T10:02:00+08:00")
        self.assertTrue(app.refresh_once())

        result = app.quotes("510300", since=1)
        self.assertTrue(result["reset"])
        self.assertEqual(
            {point["timestamp"][:10] for point in result["upserts"]},
            {"2026-08-31"},
        )

    def test_equivalent_offsets_use_one_shanghai_minute_primary_key(self) -> None:
        collector = StaticCollector(valid_completed_quote_payload())
        app = self.make_runtime_fixture(collector)
        self.assertTrue(app.refresh_once())

        equivalent = valid_completed_quote_payload()
        quote = equivalent["quotes"][0]
        for point in quote["points"]:
            point["timestamp"] = datetime.fromisoformat(
                point["timestamp"],
            ).astimezone(timezone.utc).isoformat()
        quote["timestamp"] = datetime.fromisoformat(
            quote["timestamp"],
        ).astimezone(timezone.utc).isoformat()
        quote["observed_at"] = datetime.fromisoformat(
            quote["observed_at"],
        ).astimezone(timezone.utc).isoformat()
        equivalent["collected_at"] = datetime.fromisoformat(
            equivalent["collected_at"],
        ).astimezone(timezone.utc).isoformat()
        collector.payload = equivalent
        self.assertTrue(app.refresh_once())

        delta = app.quotes("510300", since=1)
        self.assertFalse(delta["reset"])
        self.assertEqual(delta["upserts"], [])
        initial = app.quotes("510300", since=0)
        self.assertTrue(initial["reset"])
        self.assertTrue(all(point["timestamp"].endswith("+08:00") for point in initial["upserts"]))

    def test_quote_cursor_falls_back_to_current_day_after_delta_eviction(self) -> None:
        app = self.make_runtime_fixture(revision_event_limit=2)
        self.assertTrue(app.refresh_once())
        app.collector = FailingCollector("断流")
        self.assertFalse(app.refresh_once())
        app.collector = StaticCollector(valid_completed_quote_payload())
        self.assertTrue(app.refresh_once())

        result = app.quotes("510300", since=0)
        self.assertEqual(result["revision"], 3)
        self.assertTrue(result["reset"])
        self.assertTrue(result["upserts"])
        self.assertEqual(
            {point["timestamp"][:10] for point in result["upserts"]},
            {"2026-08-28"},
        )

    def test_quote_cursor_rejects_disabled_unicode_or_invalid_arguments(self) -> None:
        app = self.make_runtime_fixture()
        with self.assertRaisesRegex(ValueError, "启用"):
            app.quotes("159915", since=0)
        with self.assertRaisesRegex(ValueError, "6位"):
            app.quotes("５１０３００", since=0)
        with self.assertRaisesRegex(ValueError, "非负整数"):
            app.quotes("510300", since=-1)

    def test_revision_cursor_outside_retained_range_returns_current_snapshot(self) -> None:
        app = self.make_runtime_fixture(revision_event_limit=2)
        self.assertTrue(app.refresh_once())
        ahead = app.wait_for_revision(999, timeout=0.01)
        self.assertEqual(ahead["revision"], 1)
        self.assertEqual(ahead["event"], "reset")
        self.assertTrue(ahead["reset"])
        self.assertNotIn("points", ahead["items"][0])
        app.collector = FailingCollector("断流")
        self.assertFalse(app.refresh_once())
        app.collector = StaticCollector(valid_completed_quote_payload())
        self.assertTrue(app.refresh_once())
        old = app.wait_for_revision(0, timeout=0.01)
        self.assertEqual(old["revision"], 3)
        self.assertEqual(old["event"], "reset")
        self.assertTrue(old["reset"])
        self.assertNotIn("points", old["items"][0])

    def test_revision_wait_replaces_retained_previous_day_delta_with_reset(self) -> None:
        clock = MutableClock("2026-08-28T10:02:00+08:00")
        app = self.make_runtime_fixture()
        app.clock = clock
        self.assertTrue(app.refresh_once())
        clock.current = datetime.fromisoformat("2026-08-31T09:00:00+08:00")

        reset = app.wait_for_revision(0, timeout=0.01)

        self.assertEqual(reset["event"], "reset")
        self.assertTrue(reset["reset"])
        self.assertEqual(reset["revision"], 2)
        self.assertEqual(reset.get("upserts", {}), {})
        self.assertEqual(reset["generated_at"], clock.current.isoformat())
        self.assertEqual(reset["items"][0]["status"], "MISSING_QUOTE")
        self.assertIsNone(reset["items"][0]["price"])
        self.assertIsNone(reset["items"][0]["timestamp"])
        self.assertIsNone(app.wait_for_revision(2, timeout=0.01))

    def test_sse_rollover_reserves_revision_before_outage_and_recovery(self) -> None:
        old_payload = quote_payload_for_date("2026-08-28")
        clock = MutableClock("2026-08-28T10:02:00+08:00")
        collector = StaticCollector(old_payload)
        app = self.make_runtime_fixture(collector)
        app.clock = clock
        app._bootstrap(increment_revision=False)
        self.assertEqual(app._revision, 0)
        clock.current = datetime.fromisoformat("2026-08-31T10:02:00+08:00")

        rollover = app.wait_for_revision(0, timeout=0.01)

        self.assertEqual(rollover["event"], "reset")
        self.assertEqual(rollover["revision"], 1)
        self.assertEqual(app._revision, 1)
        self.assertEqual(app._published["generated_at"], clock.current.isoformat())

        app._publish_outage("断流", now=clock.current)
        outage = app.wait_for_revision(rollover["revision"], timeout=0.01)
        self.assertEqual(outage["revision"], 2)
        self.assertEqual(outage["refresh_error"], "断流")

        collector.payload = quote_payload_for_date("2026-08-31")
        self.assertTrue(app.refresh_once())
        recovery = app.wait_for_revision(outage["revision"], timeout=0.01)
        self.assertEqual(recovery["revision"], 3)
        self.assertIsNone(recovery["refresh_error"])
        self.assertEqual(
            [rollover["revision"], outage["revision"], recovery["revision"]],
            [1, 2, 3],
        )

    def test_collector_free_sse_rollover_reserves_one_revision_per_date(self) -> None:
        payload = quote_payload_for_date("2026-08-28")
        self.paths.quotes.write_text(json.dumps(payload), encoding="utf-8")
        clock = MutableClock("2026-08-28T10:02:00+08:00")
        app = self.make_runtime_fixture(collector=None)
        app.clock = clock
        app._bootstrap(increment_revision=False)
        cursor = app._revision
        persisted_before = {
            path: path.read_bytes()
            for path in (
                self.paths.quotes, self.paths.history, self.paths.alerts,
            )
        }

        clock.current = datetime.fromisoformat("2026-08-31T09:00:00+08:00")
        monday = app.wait_for_revision(cursor, timeout=0.01)
        self.assertEqual(monday["revision"], cursor + 1)
        self.assertEqual(app._revision, monday["revision"])
        self.assertIsNone(
            app.wait_for_revision(monday["revision"], timeout=0.01),
        )

        clock.current = datetime.fromisoformat("2026-09-01T09:00:00+08:00")
        tuesday = app.wait_for_revision(monday["revision"], timeout=0.01)
        self.assertEqual(tuesday["revision"], monday["revision"] + 1)
        self.assertEqual(app._revision, tuesday["revision"])
        self.assertEqual(tuesday["generated_at"], clock.current.isoformat())
        self.assertIsNone(
            app.wait_for_revision(tuesday["revision"], timeout=0.01),
        )
        self.assertEqual(
            {path: path.read_bytes() for path in persisted_before},
            persisted_before,
        )

    def test_concurrent_sse_rollover_clients_share_one_reserved_revision(self) -> None:
        payload = quote_payload_for_date("2026-08-28")
        self.paths.quotes.write_text(json.dumps(payload), encoding="utf-8")
        clock = MutableClock("2026-08-28T10:02:00+08:00")
        app = self.make_runtime_fixture(StaticCollector(payload))
        app.clock = clock
        app._bootstrap(increment_revision=False)
        clock.current = datetime.fromisoformat("2026-08-31T09:00:00+08:00")

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(app.wait_for_revision, 0, 0.1)
                for _ in range(2)
            ]
            results = [future.result(timeout=1) for future in futures]

        self.assertEqual([result["revision"] for result in results], [1, 1])
        self.assertTrue(all(result["event"] == "reset" for result in results))
        self.assertEqual(app._revision, 1)
        self.assertEqual(len([
            event for event in app._revision_events
            if event.get("event") == "reset" and event.get("revision") == 1
        ]), 1)

    def test_producer_and_sse_race_publish_only_one_rollover_reset(self) -> None:
        payload = quote_payload_for_date("2026-08-28")
        clock = MutableClock("2026-08-28T10:02:00+08:00")
        app = self.make_runtime_fixture(StaticCollector(payload))
        app.clock = clock
        app._bootstrap(increment_revision=False)
        clock.current = datetime.fromisoformat("2026-08-31T09:00:00+08:00")
        producer_building = threading.Event()
        allow_producer = threading.Event()
        original_empty_view = app._empty_current_date_view

        def interleaved_empty_view(
            published: object, now: datetime,
        ) -> dict[str, object]:
            if threading.current_thread().name == "rollover-producer":
                producer_building.set()
                self.assertTrue(allow_producer.wait(timeout=1))
            return original_empty_view(published, now)

        producer_result: list[bool] = []

        def publish_from_producer() -> None:
            producer_result.append(
                app._publish_cross_date_reset(clock.current, generation=None),
            )

        with patch.object(
            app, "_empty_current_date_view", side_effect=interleaved_empty_view,
        ):
            producer = threading.Thread(
                target=publish_from_producer, name="rollover-producer",
            )
            producer.start()
            self.assertTrue(producer_building.wait(timeout=1))
            sse_reset = app.wait_for_revision(0, timeout=0.1)
            allow_producer.set()
            producer.join(timeout=1)

        self.assertFalse(producer.is_alive())
        self.assertEqual(producer_result, [False])
        self.assertEqual(sse_reset["revision"], 1)
        self.assertEqual(app._revision, 1)
        self.assertEqual(len([
            event for event in app._revision_events
            if event.get("event") == "reset" and event.get("revision") == 1
        ]), 1)

    def test_previous_day_wait_resets_ahead_evicted_and_collector_free_cursors(self) -> None:
        cases = ("ahead", "evicted", "collector-free")
        for case in cases:
            with self.subTest(case=case):
                payload = quote_payload_for_date("2026-08-28")
                self.paths.quotes.write_text(json.dumps(payload), encoding="utf-8")
                clock = MutableClock("2026-08-28T10:02:00+08:00")
                if case == "collector-free":
                    app = self.make_runtime_fixture(collector=None)
                    cursor = app._revision
                else:
                    app = self.make_runtime_fixture(
                        StaticCollector(payload), revision_event_limit=2,
                    )
                    app.clock = clock
                    self.assertTrue(app.refresh_once())
                    if case == "evicted":
                        self.assertTrue(app.refresh_once())
                        self.assertTrue(app.refresh_once())
                        cursor = 0
                    else:
                        cursor = 999
                app.clock = clock
                clock.current = datetime.fromisoformat(
                    "2026-08-31T09:00:00+08:00"
                )

                reset = app.wait_for_revision(cursor, timeout=0.01)

                self.assertEqual(reset["event"], "reset")
                self.assertEqual(reset["items"][0]["status"], "MISSING_QUOTE")
                self.assertIsNone(reset["items"][0]["price"])
                self.assertEqual(reset.get("upserts", {}), {})
                self.assertIsNone(
                    app.wait_for_revision(reset["revision"], timeout=0.01),
                )

    def test_valid_last_event_id_never_streams_a_previous_day_delta(self) -> None:
        clock = MutableClock("2026-08-28T10:02:00+08:00")
        app = self.make_runtime_fixture()
        app.clock = clock
        self.assertTrue(app.refresh_once())
        clock.current = datetime.fromisoformat("2026-08-31T09:00:00+08:00")
        original_wait = app.wait_for_revision

        def one_wait(after_revision: int, timeout: float) -> dict[str, object] | None:
            payload = original_wait(after_revision, timeout)
            app._stop_event.set()
            return payload

        app.wait_for_revision = one_wait
        handler = object.__new__(MonitorRequestHandler)
        handler.server = SimpleNamespace(application=app)
        handler.headers = {"Last-Event-ID": "0"}
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None

        handler._events()

        stream = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("event: reset\n", stream)
        self.assertIn('"generated_at":"2026-08-31T09:00:00+08:00"', stream)
        self.assertNotIn("2026-08-28T", stream)
        self.assertNotIn('"trading_date":"2026-08-28"', stream)

    def test_sse_uses_revision_ids_cursor_and_heartbeat_without_sleeping(self) -> None:
        application = ScriptedEventApplication()
        handler = object.__new__(MonitorRequestHandler)
        handler.server = SimpleNamespace(application=application)
        handler.headers = {}
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None

        with patch("etf_rotation.t_web.time.sleep"):
            handler._events()

        stream = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(application.waits, [7, 8, 8, 9])
        self.assertEqual(stream.count("id: 7\n"), 1)
        self.assertEqual(stream.count("id: 8\n"), 1)
        self.assertEqual(stream.count("id: 9\n"), 1)
        self.assertEqual(stream.count("event: snapshot\n"), 1)
        self.assertEqual(stream.count("event: delta\n"), 2)
        self.assertIn(": heartbeat\n\n", stream)

    def test_sse_restart_cursor_ahead_of_current_gets_full_snapshot(self) -> None:
        application = RestartCursorApplication()
        handler = object.__new__(MonitorRequestHandler)
        handler.server = SimpleNamespace(application=application)
        handler.headers = {"Last-Event-ID": "999"}
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler._events()
        stream = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(application.waits, [999])
        self.assertIn("id: 1\n", stream)
        self.assertIn("event: reset\n", stream)
        self.assertNotIn('"points"', stream)
        self.assertNotIn(": heartbeat", stream)

    def test_sse_ignores_windows_connection_abort_without_flushing(self) -> None:
        application = ScriptedEventApplication()
        handler = object.__new__(MonitorRequestHandler)
        handler.server = SimpleNamespace(application=application)
        handler.headers = {}
        handler.wfile = AbortedStream()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None

        handler._events()

        self.assertEqual(handler.wfile.flush_calls, 0)

    def test_sse_propagates_unexpected_stream_errors(self) -> None:
        application = ScriptedEventApplication()
        handler = object.__new__(MonitorRequestHandler)
        handler.server = SimpleNamespace(application=application)
        handler.headers = {}
        handler.wfile = ExplodingStream()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None

        with self.assertRaisesRegex(RuntimeError, "unexpected stream failure"):
            handler._events()

    def test_sse_ignores_windows_connection_abort_while_ending_headers(self) -> None:
        application = ScriptedEventApplication()
        handler = object.__new__(MonitorRequestHandler)
        handler.server = SimpleNamespace(application=application)
        handler.headers = {}
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None

        def abort_headers() -> None:
            raise ConnectionAbortedError(10053, "header connection aborted")

        handler.end_headers = abort_headers

        handler._events()

        self.assertEqual(application.snapshot_calls, 0)
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_sse_ignores_windows_connection_abort_while_flushing(self) -> None:
        application = ScriptedEventApplication()
        handler = object.__new__(MonitorRequestHandler)
        handler.server = SimpleNamespace(application=application)
        handler.headers = {}
        handler.wfile = FlushAbortedStream()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None

        handler._events()

        self.assertEqual(len(handler.wfile.writes), 1)
        self.assertEqual(handler.wfile.flush_calls, 1)
        self.assertEqual(application.waits, [])

    def test_sse_propagates_connection_abort_from_application_logic(self) -> None:
        handler = object.__new__(MonitorRequestHandler)
        handler.server = SimpleNamespace(application=SnapshotAbortedApplication())
        handler.headers = {}
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None

        with self.assertRaisesRegex(ConnectionAbortedError, "application failure"):
            handler._events()

    def test_http_send_ignores_windows_connection_abort_while_ending_headers(self) -> None:
        handler = object.__new__(MonitorRequestHandler)
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None

        def abort_headers() -> None:
            raise ConnectionAbortedError(10053, "header connection aborted")

        handler.end_headers = abort_headers

        handler._send(HTTPStatus.OK, b"payload", "application/json")

        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_http_send_ignores_windows_connection_abort_while_writing_body(self) -> None:
        handler = object.__new__(MonitorRequestHandler)
        handler.wfile = AbortedStream()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None

        handler._send(HTTPStatus.OK, b"payload", "application/json")

        self.assertEqual(handler.wfile.flush_calls, 0)

    def test_http_send_propagates_unexpected_header_and_body_errors(self) -> None:
        handler = object.__new__(MonitorRequestHandler)
        handler.wfile = ExplodingStream()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None

        with self.assertRaisesRegex(RuntimeError, "unexpected stream failure"):
            handler._send(HTTPStatus.OK, b"payload", "application/json")

        def explode_headers() -> None:
            raise RuntimeError("unexpected header failure")

        handler.end_headers = explode_headers
        with self.assertRaisesRegex(RuntimeError, "unexpected header failure"):
            handler._send(HTTPStatus.OK, b"payload", "application/json")

    def test_real_server_quiets_abort_only_while_reading_request_line(self) -> None:
        application = SimpleNamespace(stop_refresh=lambda: None)
        server = MonitorServer(("127.0.0.1", 0), application)
        server.timeout = 0.5
        entered = threading.Event()
        finished = threading.Event()
        process_request = server.process_request_thread

        def completed_request(request: object, address: object) -> None:
            try:
                process_request(request, address)
            finally:
                finished.set()

        server.process_request_thread = completed_request

        def aborting_setup(handler: MonitorRequestHandler) -> None:
            handler.connection = handler.request
            handler.rfile = AbortingRequestLine(entered)
            handler.wfile = handler.request.makefile("wb")

        try:
            with patch.object(MonitorRequestHandler, "setup", aborting_setup), patch.object(
                ThreadingHTTPServer, "handle_error",
            ) as parent_handle:
                client = socket.create_connection(server.server_address, timeout=1)
                try:
                    server.handle_request()
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(finished.wait(1))
                finally:
                    client.close()
            parent_handle.assert_not_called()
        finally:
            server.server_close()

    def test_real_server_delegates_application_connection_abort_to_parent(self) -> None:
        application = SnapshotAbortedApplication()
        application.stop_refresh = lambda: None
        server = MonitorServer(("127.0.0.1", 0), application)
        server.timeout = 0.5
        handled = threading.Event()
        finished = threading.Event()
        process_request = server.process_request_thread

        def completed_request(request: object, address: object) -> None:
            try:
                process_request(request, address)
            finally:
                finished.set()

        server.process_request_thread = completed_request

        def record_error(*args: object, **kwargs: object) -> None:
            handled.set()

        try:
            with patch.object(
                ThreadingHTTPServer, "handle_error", side_effect=record_error,
            ) as parent_handle:
                client = socket.create_connection(server.server_address, timeout=1)
                try:
                    client.sendall(
                        b"GET /api/snapshot HTTP/1.1\r\nHost: localhost\r\n\r\n"
                    )
                    server.handle_request()
                    self.assertTrue(handled.wait(1))
                    self.assertTrue(finished.wait(1))
                finally:
                    client.close()
            parent_handle.assert_called_once()
        finally:
            server.server_close()

    def test_health_summary_tracks_latest_outage_revision(self) -> None:
        app = self.make_runtime_fixture()
        self.assertTrue(app.refresh_once())
        self.assertTrue(app.health()["ok"])
        app.collector = FailingCollector("断流")
        self.assertFalse(app.refresh_once())
        health = app.health()
        self.assertFalse(health["ok"])
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["revision"], 2)
        self.assertEqual(health["errors"], ["断流"])

    def test_history_and_alert_handlers_return_json_for_read_errors(self) -> None:
        handler = object.__new__(MonitorRequestHandler)
        handler.server = SimpleNamespace(application=SimpleNamespace(
            history_path=self.paths.history,
            alert_history_path=self.paths.alerts,
        ))
        handler.path = "/api/history/dates"
        responses: list[tuple[HTTPStatus, dict[str, object]]] = []
        handler._json = lambda status, payload: responses.append((status, payload))
        with patch(
            "etf_rotation.t_web.QuoteHistoryStore.available_dates",
            side_effect=OSError("history unavailable"),
        ):
            handler._history_dates()
        self.assertEqual(responses.pop(0), (
            HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "history unavailable"},
        ))

        handler.path = "/api/alerts"
        with patch(
            "etf_rotation.t_web.AlertHistoryStore.query",
            side_effect=ValueError("alerts invalid"),
        ):
            handler._alerts()
        self.assertEqual(responses.pop(0), (
            HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "alerts invalid"},
        ))


if __name__ == "__main__":
    unittest.main()
