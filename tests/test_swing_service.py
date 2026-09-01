from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from etf_rotation.etf_metadata import EtfMetadataStore
from etf_rotation.swing_alerts import AlertInput, SwingAlertStore
from etf_rotation.swing_config import SwingWatchItem
from etf_rotation.swing_data import DailyBar, DailyHistoryStore
from etf_rotation.swing_portfolio import PortfolioLedger, TradeInput
from etf_rotation.swing_service import SwingPaths, SwingService
from etf_rotation.swing_strategy import (
    SwingState,
    evaluate_swing as real_evaluate_swing,
)

from tests.swing_helpers import (
    metadata_fixture,
    retime_daily_bars,
    swing_strategy_bars,
)


SHANGHAI = timezone(timedelta(hours=8))


class StaticDailyCollector:
    def __init__(self, bars: tuple[DailyBar, ...], *, delay: float = 0.0):
        self.bars = bars
        self.delay = delay
        self.calls = 0
        self.requested: list[date] = []

    def collect(
        self,
        watchlist: tuple[SwingWatchItem, ...],
        last_completed_date: date,
        count: int = 260,
    ) -> tuple[DailyBar, ...]:
        self.calls += 1
        self.requested.append(last_completed_date)
        if self.delay:
            time.sleep(self.delay)
        return self.bars


class FailingDailyCollector:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self, *args: object, **kwargs: object) -> tuple[DailyBar, ...]:
        self.calls += 1
        raise RuntimeError("collector exploded")


class SwingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = SwingPaths(
            watchlist=root / "watchlist.json",
            strategy=root / "strategy.json",
            daily_history=root / "daily.jsonl",
            portfolio_snapshot=root / "portfolio.json",
            trades=root / "trades.jsonl",
            alerts=root / "alerts.jsonl",
            metadata=root / "metadata.json",
            calendar=root / "calendar.json",
            backtests=root / "backtests",
        )
        self.paths.metadata.write_text(
            json.dumps(metadata_fixture(("510300",))), encoding="utf-8",
        )
        self.paths.watchlist.write_text(json.dumps({
            "schema_version": 1,
            "items": [{"symbol": "510300", "enabled": True}],
        }), encoding="utf-8")
        strategy = json.loads(
            (Path(__file__).parents[1] / "data/swing/strategy.json").read_text(
                encoding="utf-8",
            ),
        )
        self.paths.strategy.write_text(json.dumps(strategy), encoding="utf-8")
        self.paths.calendar.write_text(json.dumps({
            "schema_version": 1, "closed_dates": [],
        }), encoding="utf-8")
        self.metadata = EtfMetadataStore(self.paths.metadata).load()
        self.initial_bars = retime_daily_bars(
            swing_strategy_bars(70), ending_on=date(2026, 8, 31),
        )
        self.final_bars = retime_daily_bars(
            swing_strategy_bars(71), ending_on=date(2026, 9, 1),
        )
        DailyHistoryStore(
            self.paths.daily_history, self.metadata, frozenset(),
        ).upsert(self.initial_bars)
        PortfolioLedger(
            self.paths.trades,
            self.metadata,
            clock=lambda: datetime(2026, 8, 31, 16, tzinfo=SHANGHAI),
            closed_dates=frozenset(),
        ).initialize("test", 100_000.0, "init")

    def make_service(
        self,
        *,
        collector: object | None = None,
        intraday_provider: object | None = None,
        intraday_points_provider: object | None = None,
        event_limit: int = 4,
        clock: object | None = None,
        refresh_interval: float = 60.0,
    ) -> SwingService:
        return SwingService(
            self.paths,
            collector=collector,
            intraday_provider=(
                intraday_provider
                if intraday_provider is not None
                else lambda: {
                    "generated_at": "2026-09-01T14:00:00+08:00",
                    "items": [{
                        "symbol": "510300", "price": 106.0,
                        "timestamp": "2026-09-01T13:59:30+08:00",
                        "health_status": "REALTIME",
                    }],
                }
            ),
            intraday_points_provider=(
                intraday_points_provider
                if intraday_points_provider is not None
                else lambda _symbol: {"upserts": []}
            ),
            clock=(
                clock if clock is not None
                else lambda: datetime(2026, 9, 1, 14, 0, tzinfo=SHANGHAI)
            ),
            refresh_interval=refresh_interval,
            event_limit=event_limit,
        )

    @staticmethod
    def full_day_points(
        bar: DailyBar,
        *,
        mismatched_open: bool = False,
    ) -> dict[str, object]:
        timestamps: list[datetime] = []
        current = datetime.combine(
            bar.trading_date, datetime.min.time(), SHANGHAI,
        ).replace(hour=9, minute=30)
        morning_end = current.replace(hour=11, minute=30)
        while current <= morning_end:
            timestamps.append(current)
            current += timedelta(minutes=1)
        current = current.replace(hour=13, minute=1)
        afternoon_end = current.replace(hour=15, minute=0)
        while current <= afternoon_end:
            timestamps.append(current)
            current += timedelta(minutes=1)
        self_bar = bar
        points: list[dict[str, object]] = []
        for index, timestamp in enumerate(timestamps):
            price = self_bar.close
            high = self_bar.high if index == 1 else price
            low = self_bar.low if index == 2 else price
            points.append({
                "schema_version": 3,
                "trading_date": self_bar.trading_date.isoformat(),
                "timestamp": timestamp.isoformat(),
                "is_complete": True,
                "open": (
                    1.0 if index == 0 and mismatched_open
                    else self_bar.open if index == 0 else price
                ),
                "high": max(high, price),
                "low": min(low, price),
                "price": price,
            })
        return {"upserts": points}

    def test_bootstrap_reads_history_without_collecting_or_writing(self) -> None:
        before = self.paths.daily_history.read_bytes()
        service = self.make_service(collector=None)
        snapshot = service.snapshot()
        self.assertEqual(snapshot["revision"], 0)
        self.assertEqual(snapshot["as_of_trading_date"], "2026-08-31")
        self.assertEqual(self.paths.daily_history.read_bytes(), before)

    def test_post_close_refresh_publishes_once_after_atomic_history_commit(self) -> None:
        collector = StaticDailyCollector(self.final_bars)
        service = self.make_service(collector=collector)
        self.assertTrue(service.refresh_once(
            datetime(2026, 9, 1, 15, 10, tzinfo=SHANGHAI),
        ))
        self.assertEqual(collector.calls, 1)
        self.assertEqual(service.snapshot()["revision"], 1)
        self.assertEqual(service.snapshot()["as_of_trading_date"], "2026-09-01")
        self.assertFalse(service.refresh_once(
            datetime(2026, 9, 1, 15, 11, tzinfo=SHANGHAI),
        ))
        self.assertEqual(collector.calls, 1)

    def test_before_1510_never_collects_current_trading_day(self) -> None:
        collector = StaticDailyCollector(self.final_bars)
        service = self.make_service(collector=collector)
        self.assertFalse(service.refresh_once(
            datetime(2026, 9, 1, 15, 9, 59, tzinfo=SHANGHAI),
        ))
        self.assertEqual(collector.calls, 0)

    def test_missing_completed_date_backfills_during_lunch_and_closed_day(self) -> None:
        collector = StaticDailyCollector(self.final_bars)
        service = self.make_service(collector=collector)
        self.assertTrue(service.refresh_once(
            datetime(2026, 9, 2, 12, 0, tzinfo=SHANGHAI),
        ))
        self.assertEqual(collector.requested, [date(2026, 9, 1)])

    def test_failed_batch_preserves_history_and_formal_decisions(self) -> None:
        collector = FailingDailyCollector()
        service = self.make_service(collector=collector)
        before = self.paths.daily_history.read_bytes()
        formal = service.snapshot()["items"][0]["formal_state"]
        self.assertFalse(service.refresh_once(
            datetime(2026, 9, 1, 15, 10, tzinfo=SHANGHAI),
        ))
        snapshot = service.snapshot()
        self.assertEqual(self.paths.daily_history.read_bytes(), before)
        self.assertEqual(snapshot["items"][0]["formal_state"], formal)
        self.assertEqual(snapshot["health"]["daily"], "COLLECTION_FAILED")

        service.clock = lambda: datetime(2026, 9, 2, 10, 0, tzinfo=SHANGHAI)
        service.intraday_provider = lambda: {"items": [{
            "symbol": "510300", "price": 106.0,
            "timestamp": "2026-09-02T09:59:30+08:00",
            "health_status": "REALTIME",
        }]}
        realtime = service.refresh_intraday()["items"][0]
        self.assertEqual(realtime["formal_state"], formal)
        self.assertEqual(realtime["execution_status"], "PAUSED_DAILY_DATA")

    def test_available_minute_mismatch_blocks_entire_batch(self) -> None:
        target = self.final_bars[-1]
        def points(_symbol: str) -> dict[str, object]:
            return self.full_day_points(target, mismatched_open=True)

        service = self.make_service(
            collector=StaticDailyCollector(self.final_bars),
            intraday_points_provider=points,
        )
        before = self.paths.daily_history.read_bytes()
        self.assertFalse(service.refresh_once(
            datetime(2026, 9, 1, 15, 10, tzinfo=SHANGHAI),
        ))
        self.assertEqual(self.paths.daily_history.read_bytes(), before)
        self.assertEqual(service.snapshot()["health"]["daily"], "CROSSCHECK_FAILED")

    def test_partial_or_implicitly_complete_minutes_are_unavailable_not_mismatch(self) -> None:
        target = self.final_bars[-1]
        partial = self.full_day_points(target, mismatched_open=True)
        partial["upserts"] = partial["upserts"][:-1]
        partial["upserts"][0].pop("is_complete")
        service = self.make_service(
            collector=StaticDailyCollector(self.final_bars),
            intraday_points_provider=lambda _symbol: partial,
        )
        self.assertTrue(service.refresh_once(
            datetime(2026, 9, 1, 15, 10, tzinfo=SHANGHAI),
        ))
        self.assertEqual(
            service.snapshot()["health"]["minute_crosscheck"],
            "MINUTE_CROSSCHECK_UNAVAILABLE",
        )

    def test_duplicate_non_target_collector_key_blocks_batch_before_commit(self) -> None:
        duplicated = self.final_bars + (self.final_bars[0],)
        service = self.make_service(
            collector=StaticDailyCollector(duplicated),
        )
        before = self.paths.daily_history.read_bytes()
        self.assertFalse(service.refresh_once(
            datetime(2026, 9, 1, 15, 10, tzinfo=SHANGHAI),
        ))
        self.assertEqual(self.paths.daily_history.read_bytes(), before)
        self.assertIn("duplicate", service.snapshot()["errors"]["daily"])

    def test_absent_minute_crosscheck_is_degraded_but_does_not_block_commit(self) -> None:
        service = self.make_service(
            collector=StaticDailyCollector(self.final_bars),
            intraday_points_provider=lambda _symbol: {"upserts": []},
        )
        self.assertTrue(service.refresh_once(
            datetime(2026, 9, 1, 15, 10, tzinfo=SHANGHAI),
        ))
        self.assertEqual(
            service.snapshot()["health"]["minute_crosscheck"],
            "MINUTE_CROSSCHECK_UNAVAILABLE",
        )

    def test_intraday_failure_retracts_overlay_and_pauses_formal_plan(self) -> None:
        class FailingProvider:
            def __call__(self) -> dict[str, object]:
                raise RuntimeError("feed failed")

        service = self.make_service(intraday_provider=FailingProvider())
        snapshot = service.refresh_intraday()
        item = snapshot["items"][0]
        self.assertEqual(item["execution_status"], "PAUSED_MARKET_NOT_REALTIME")
        self.assertIsNone(item["intraday_overlay"])
        self.assertEqual(item["formal_state"], "TRIAL_ENTRY_CANDIDATE")

    def test_intraday_outage_never_retracts_persisted_formal_alert(self) -> None:
        class ToggleProvider:
            failed = False

            def __call__(self) -> dict[str, object]:
                if self.failed:
                    raise RuntimeError("outage")
                return {
                    "items": [{
                        "symbol": "510300", "price": 106.0,
                        "timestamp": "2026-09-01T10:00:00+08:00",
                        "health_status": "REALTIME",
                    }],
                }

        provider = ToggleProvider()
        service = self.make_service(
            collector=StaticDailyCollector(self.final_bars),
            intraday_provider=provider,
        )
        service.refresh_once(datetime(2026, 9, 1, 15, 10, tzinfo=SHANGHAI))
        formal_ids = {
            item["alert_id"] for item in service.alerts()["items"]
            if item["scope"] == "FORMAL"
        }
        self.assertTrue(formal_ids)
        provider.failed = True
        service.refresh_intraday()
        after = {item["alert_id"]: item for item in service.alerts()["items"]}
        self.assertTrue(formal_ids.issubset(after))
        self.assertTrue(all(not after[alert_id]["retracted"] for alert_id in formal_ids))

    def test_intraday_refresh_never_changes_formal_decision(self) -> None:
        service = self.make_service()
        before = service.snapshot()["items"][0]["formal_decision"]
        snapshot = service.refresh_intraday()
        self.assertEqual(snapshot["items"][0]["formal_decision"], before)
        self.assertEqual(
            snapshot["items"][0]["intraday_overlay"],
            "APPROACHING_ENTRY_ZONE",
        )

    def test_trial_overlay_is_suppressed_after_valid_session_close(self) -> None:
        service = self.make_service(clock=lambda: datetime(
            2026, 9, 1, 15, 1, tzinfo=SHANGHAI,
        ))
        item = service.refresh_intraday()["items"][0]
        self.assertEqual(item["formal_state"], "TRIAL_ENTRY_CANDIDATE")
        self.assertIsNone(item["intraday_overlay"])
        self.assertNotEqual(item["execution_status"], "READY_TO_EXECUTE")
        self.assertFalse(service.snapshot()["active_alerts"])

    def test_delayed_intraday_quote_is_visible_but_cannot_create_overlay(self) -> None:
        service = self.make_service(intraday_provider=lambda: {
            "items": [{
                "symbol": "510300", "price": 105.9,
                "timestamp": "2026-09-01T13:59:30+08:00",
                "health_status": "DELAYED",
            }],
        })
        snapshot = service.refresh_intraday()
        item = snapshot["items"][0]
        self.assertEqual(item["current_price"], 105.9)
        self.assertEqual(item["intraday_health_status"], "DELAYED")
        self.assertEqual(snapshot["health"]["intraday"], "DELAYED")
        self.assertIsNone(item["intraday_overlay"])
        self.assertEqual(item["execution_status"], "PAUSED_MARKET_NOT_REALTIME")

    def test_intraday_item_health_is_derived_instead_of_echoed(self) -> None:
        cases = (
            (
                "stale", datetime(2026, 9, 1, 14, 0, tzinfo=SHANGHAI),
                "2026-09-01T13:57:00+08:00", "REALTIME", "STALE",
            ),
            (
                "closed", datetime(2026, 9, 1, 15, 1, tzinfo=SHANGHAI),
                "2026-09-01T15:00:00+08:00", "REALTIME", "CLOSED",
            ),
            (
                "untrusted-source",
                datetime(2026, 9, 1, 14, 0, tzinfo=SHANGHAI),
                "2026-09-01T13:59:30+08:00", "CLOSED", "UNAVAILABLE",
            ),
            (
                "source-stale",
                datetime(2026, 9, 1, 14, 0, tzinfo=SHANGHAI),
                "2026-09-01T13:59:30+08:00", "STALE", "STALE",
            ),
            (
                "source-outage",
                datetime(2026, 9, 1, 14, 0, tzinfo=SHANGHAI),
                "2026-09-01T13:59:30+08:00", "OUTAGE", "OUTAGE",
            ),
            (
                "malformed", datetime(2026, 9, 1, 14, 0, tzinfo=SHANGHAI),
                "not-an-iso-time", "REALTIME", "UNAVAILABLE",
            ),
        )
        for label, now, timestamp, source_status, expected in cases:
            with self.subTest(label=label):
                service = self.make_service(
                    clock=lambda now=now: now,
                    intraday_provider=lambda timestamp=timestamp,
                    source_status=source_status: {"items": [{
                        "symbol": "510300", "price": 105.9,
                        "timestamp": timestamp,
                        "health_status": source_status,
                    }]},
                )
                snapshot = service.refresh_intraday()
                item = snapshot["items"][0]
                self.assertEqual(item["intraday_health_status"], expected)
                self.assertEqual(snapshot["health"]["intraday"], expected)
                self.assertEqual(
                    item["execution_status"], "PAUSED_MARKET_NOT_REALTIME",
                )

    def test_realtime_quote_requires_current_fresh_continuous_session(self) -> None:
        cases = (
            (
                "yesterday", datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
                "2026-08-31T10:00:00+08:00", "STALE",
            ),
            (
                "weekend", datetime(2026, 9, 5, 10, 0, tzinfo=SHANGHAI),
                "2026-09-05T10:00:00+08:00", "CLOSED",
            ),
            (
                "lunch", datetime(2026, 9, 1, 12, 0, tzinfo=SHANGHAI),
                "2026-09-01T11:59:30+08:00", "LUNCH_BREAK",
            ),
            (
                "stale", datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
                "2026-09-01T09:58:00+08:00", "STALE",
            ),
            (
                "future", datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
                "2026-09-01T10:01:00+08:00", "UNAVAILABLE",
            ),
        )
        for label, now, timestamp, expected_health in cases:
            with self.subTest(label=label):
                service = self.make_service(
                    clock=lambda now=now: now,
                    intraday_provider=lambda timestamp=timestamp: {
                        "items": [{
                            "symbol": "510300", "price": 106.0,
                            "timestamp": timestamp,
                            "health_status": "REALTIME",
                        }],
                    },
                )
                snapshot = service.refresh_intraday()
                item = snapshot["items"][0]
                self.assertEqual(
                    snapshot["health"]["intraday"], expected_health,
                )
                self.assertEqual(
                    item["intraday_health_status"], expected_health,
                )
                self.assertIsNone(item["intraday_overlay"])
                self.assertEqual(
                    item["execution_status"], "PAUSED_MARKET_NOT_REALTIME",
                )
                self.assertFalse(any(
                    alert["scope"] == "INTRADAY"
                    for alert in snapshot["active_alerts"]
                ))

    def test_explicit_nonrealtime_source_retracts_existing_overlay(self) -> None:
        service = self.make_service()
        self.assertTrue(any(
            item["scope"] == "INTRADAY"
            for item in service.refresh_intraday()["active_alerts"]
        ))
        service.intraday_provider = lambda: {"items": [{
            "symbol": "510300", "price": 105.9,
            "timestamp": "2026-09-01T13:59:30+08:00",
            "health_status": "STALE",
        }]}
        snapshot = service.refresh_intraday()
        self.assertEqual(snapshot["health"]["intraday"], "STALE")
        self.assertEqual(
            snapshot["items"][0]["intraday_health_status"], "STALE",
        )
        self.assertIsNone(snapshot["items"][0]["intraday_overlay"])
        self.assertFalse(any(
            item["scope"] == "INTRADAY"
            for item in snapshot["active_alerts"]
        ))

    def test_hostile_intraday_fields_withdraw_previous_overlay(self) -> None:
        class HostileStatus(str):
            def __eq__(self, other: object) -> bool:
                raise RuntimeError("hostile equality")

        class HostileItem(dict[str, object]):
            def get(self, key: str, default: object = None) -> object:
                if key == "price":
                    raise RuntimeError("hostile item")
                return super().get(key, default)

        service = self.make_service()
        self.assertTrue(service.refresh_intraday()["active_alerts"])
        service.intraday_provider = lambda: {"items": [{
            "symbol": "510300", "price": 106.0,
            "timestamp": "2026-09-01T13:59:30+08:00",
            "health_status": HostileStatus("REALTIME"),
        }]}
        hostile_status = service.refresh_intraday()
        self.assertEqual(hostile_status["health"]["intraday"], "UNAVAILABLE")
        self.assertFalse(any(
            item["scope"] == "INTRADAY"
            for item in hostile_status["active_alerts"]
        ))

        service.intraday_provider = lambda: {"items": [HostileItem({
            "symbol": "510300", "price": 106.0,
            "timestamp": "2026-09-01T13:59:30+08:00",
            "health_status": "REALTIME",
        })]}
        hostile_item = service.refresh_intraday()
        self.assertEqual(hostile_item["health"]["intraday"], "UNAVAILABLE")
        self.assertIsNone(hostile_item["items"][0]["intraday_overlay"])

    def test_intraday_evaluate_and_sync_exceptions_withdraw_overlay(self) -> None:
        for target in (
            "etf_rotation.swing_service.evaluate_intraday_overlay",
            "service-sync",
        ):
            with self.subTest(target=target):
                service = self.make_service()
                self.assertTrue(any(
                    item["scope"] == "INTRADAY"
                    for item in service.refresh_intraday()["active_alerts"]
                ))
                context = (
                    patch(target, side_effect=RuntimeError("intraday failed"))
                    if target != "service-sync"
                    else patch.object(
                        service, "_sync_overlay_alerts",
                        side_effect=RuntimeError("sync failed"),
                    )
                )
                with context:
                    withdrawn = service.refresh_intraday()
                self.assertEqual(
                    withdrawn["health"]["intraday"], "UNAVAILABLE",
                )
                self.assertIsNone(
                    withdrawn["items"][0]["intraday_overlay"],
                )
                self.assertFalse(any(
                    item["scope"] == "INTRADAY"
                    for item in withdrawn["active_alerts"]
                ))

    def test_refresh_is_single_producer_under_concurrency(self) -> None:
        collector = StaticDailyCollector(self.final_bars, delay=0.03)
        service = self.make_service(collector=collector)
        now = datetime(2026, 9, 1, 15, 10, tzinfo=SHANGHAI)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = tuple(pool.map(lambda _index: service.refresh_once(now), range(6)))
        self.assertEqual(collector.calls, 1)
        self.assertEqual(results.count(True), 1)

    def test_all_getters_read_one_published_revision_during_recompute(self) -> None:
        service = self.make_service()
        before = {
            "snapshot": service.snapshot(),
            "portfolio": service.portfolio(),
            "alerts": service.alerts(include_retracted=True),
            "watchlist": service.watchlist(),
        }
        entered = threading.Event()
        release = threading.Event()

        def blocking_evaluate(*args: object, **kwargs: object) -> object:
            entered.set()
            release.wait(2.0)
            return real_evaluate_swing(*args, **kwargs)

        errors: list[BaseException] = []
        with patch(
            "etf_rotation.swing_service.evaluate_swing",
            side_effect=blocking_evaluate,
        ):
            thread = threading.Thread(target=lambda: self._capture_error(
                errors,
                lambda: service.record_trade(TradeInput(
                    "510300", "BUY", 100, 100.0, 0.0,
                    datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
                    planned_risk_per_share=2.0,
                ), "blocked-read-buy"),
            ))
            thread.start()
            self.assertTrue(entered.wait(1.0))
            during = {
                "snapshot": service.snapshot(),
                "portfolio": service.portfolio(),
                "alerts": service.alerts(include_retracted=True),
                "watchlist": service.watchlist(),
            }
            self.assertEqual(during, before)
            release.set()
            thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        after = service.snapshot()
        self.assertGreater(after["revision"], before["snapshot"]["revision"])
        for getter in (
            service.portfolio(), service.alerts(include_retracted=True),
            service.watchlist(),
        ):
            self.assertEqual(getter["revision"], after["revision"])
        self.assertEqual(
            service.portfolio()["projection"], after["portfolio"],
        )

    @staticmethod
    def _capture_error(
        errors: list[BaseException], action: object,
    ) -> None:
        try:
            action()  # type: ignore[operator]
        except BaseException as error:
            errors.append(error)

    def test_clock_failure_is_published_and_later_success_clears_it(self) -> None:
        class FlakyClock:
            failed = True

            def __call__(self) -> datetime:
                if self.failed:
                    raise RuntimeError("clock unavailable")
                return datetime(2026, 9, 1, 14, 0, tzinfo=SHANGHAI)

        clock = FlakyClock()
        service = self.make_service(clock=clock)
        failed = service.snapshot()
        self.assertEqual(failed["health"]["service"], "CLOCK_FAILED")
        self.assertIn("service", failed["errors"])
        self.assertFalse(
            failed["health"]["service"] == "OK"
            and "service" in failed["errors"],
        )

        clock.failed = False
        recovered = service.refresh_intraday()
        self.assertEqual(recovered["health"]["service"], "OK")
        self.assertNotIn("service", recovered["errors"])

        clock.failed = True
        intraday_clock_failure = service.refresh_intraday()
        self.assertEqual(
            intraday_clock_failure["health"]["service"], "CLOCK_FAILED",
        )
        self.assertIn("service", intraday_clock_failure["errors"])

        clock.failed = False
        recovered_intraday = service.refresh_intraday()
        self.assertEqual(recovered_intraday["health"]["service"], "OK")
        self.assertNotIn("service", recovered_intraday["errors"])

        clock.failed = True
        self.assertFalse(service.refresh_once())
        failed_again = service.snapshot()
        self.assertEqual(failed_again["health"]["service"], "CLOCK_FAILED")
        self.assertIn("service", failed_again["errors"])
        failed_revision = failed_again["revision"]
        clock.failed = False
        self.assertFalse(service.refresh_once())
        recovered_again = service.snapshot()
        self.assertEqual(recovered_again["health"]["service"], "OK")
        self.assertNotIn("service", recovered_again["errors"])
        self.assertEqual(recovered_again["revision"], failed_revision + 1)

    def test_producer_failure_survives_clock_success_until_a_good_cycle(self) -> None:
        service = self.make_service(refresh_interval=0.01)
        service._publish_component_failure(
            "service", "PRODUCER_FAILED", RuntimeError("producer exploded"),
            now=None,
        )
        failed = service.snapshot()
        self.assertEqual(failed["health"]["service"], "PRODUCER_FAILED")
        self.assertIn("producer exploded", failed["errors"]["service"])

        service.start_refresh()
        deadline = time.monotonic() + 2.0
        with service.publish_condition:
            while (
                service.published["health"]["service"] == "PRODUCER_FAILED"
                and time.monotonic() < deadline
            ):
                service.publish_condition.wait(0.05)
        service.stop_refresh()
        recovered = service.snapshot()
        self.assertEqual(recovered["health"]["service"], "OK")
        self.assertNotIn("service", recovered["errors"])

    def test_snapshot_and_cursor_payloads_are_deep_copies(self) -> None:
        service = self.make_service()
        snapshot = service.snapshot()
        snapshot["items"][0]["formal_decision"]["evidence"]["changed"] = True
        self.assertNotIn(
            "changed",
            service.snapshot()["items"][0]["formal_decision"]["evidence"],
        )
        quotes = service.daily_quotes("510300", 0)
        quotes["upserts"][0]["close"] = 1.0
        self.assertNotEqual(service.daily_quotes("510300", 0)["upserts"][0]["close"], 1.0)

    def test_daily_quote_cursor_reset_and_delta_rules(self) -> None:
        collector = StaticDailyCollector(self.final_bars)
        service = self.make_service(collector=collector, event_limit=2)
        initial = service.daily_quotes("510300", 0)
        self.assertTrue(initial["reset"])
        self.assertEqual(len(initial["upserts"]), 70)
        service.refresh_once(datetime(2026, 9, 1, 15, 10, tzinfo=SHANGHAI))
        delta = service.daily_quotes("510300", 0)
        self.assertTrue(delta["reset"])
        current = service.daily_quotes("510300", 1)
        self.assertFalse(current["reset"])
        self.assertEqual(current["upserts"], [])
        self.assertTrue(service.daily_quotes("510300", 99)["reset"])

    def test_rollover_cursor_never_returns_prior_date_delta(self) -> None:
        service = self.make_service(collector=StaticDailyCollector(self.final_bars))
        service.refresh_once(datetime(2026, 9, 1, 15, 10, tzinfo=SHANGHAI))
        payload = service.daily_quotes("510300", 0)
        self.assertTrue(payload["reset"])
        self.assertEqual(payload["as_of_trading_date"], "2026-09-01")

    def test_wait_wakes_when_service_stops(self) -> None:
        service = self.make_service()
        result: list[object] = []
        thread = threading.Thread(
            target=lambda: result.append(service.wait_for_event(0, timeout=10.0)),
        )
        thread.start()
        time.sleep(0.03)
        service.stop_refresh()
        thread.join(1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [None])

    def test_event_queue_is_bounded_and_stale_cursor_resets(self) -> None:
        service = self.make_service(event_limit=2)
        service.refresh_intraday()
        service.refresh_intraday()
        service.refresh_intraday()
        event = service.wait_for_event(0, timeout=0.0)
        self.assertIsNotNone(event)
        self.assertTrue(event["reset"])

    def test_public_snapshots_stay_bounded_while_alert_history_is_revisioned(self) -> None:
        service = self.make_service(event_limit=4)
        store = service._alert_store
        self.assertIsNotNone(store)
        assert store is not None
        alert = AlertInput(
            trading_date=date(2026, 9, 1),
            symbol="510300",
            state="LOAD_TEST_OVERLAY",
            strategy_version="LOAD_TEST_V1",
            level="YELLOW",
            label="load test",
            evidence={},
        )
        baseline = len(service.alerts(include_retracted=True)["items"])

        def add_generations(count: int) -> None:
            for index in range(count):
                projection = store.publish_overlay(alert)
                store.retract_overlay(projection.alert_id, f"cycle-{index}")
            service._refresh_alert_snapshot(datetime(
                2026, 9, 1, 14, 0, tzinfo=SHANGHAI,
            ))

        add_generations(20)
        first = service.snapshot()
        first_event = service.wait_for_event(first["revision"] - 1, timeout=0.0)
        self.assertNotIn("alert_history", first)
        self.assertNotIn("alert_history", first_event)
        self.assertNotIn("_published_read_model", first)
        self.assertNotIn("_published_read_model", first_event)
        self.assertLessEqual(len(first["alerts"]), len(first["active_alerts"]))
        self.assertEqual(
            len(service.alerts(include_retracted=True)["items"]), baseline + 20,
        )
        self.assertEqual(
            service.alerts(include_retracted=True)["revision"], first["revision"],
        )
        first_size = len(json.dumps(first, ensure_ascii=False, sort_keys=True))

        add_generations(80)
        second = service.snapshot()
        second_event = service.wait_for_event(
            second["revision"] - 1, timeout=0.0,
        )
        self.assertNotIn("alert_history", second)
        self.assertNotIn("alert_history", second_event)
        self.assertNotIn("_published_read_model", second)
        self.assertNotIn("_published_read_model", second_event)
        self.assertLessEqual(len(second["alerts"]), len(second["active_alerts"]))
        self.assertEqual(
            len(service.alerts(include_retracted=True)["items"]), baseline + 100,
        )
        self.assertEqual(
            service.alerts(include_retracted=True)["revision"], second["revision"],
        )
        self.assertLess(
            len(json.dumps(second, ensure_ascii=False, sort_keys=True)),
            first_size + 128,
        )

    def test_unknown_overlay_state_is_history_but_never_publicly_active(self) -> None:
        service = self.make_service()
        store = service._alert_store
        self.assertIsNotNone(store)
        assert store is not None
        rogue = store.publish_overlay(AlertInput(
            trading_date=date(2026, 9, 1),
            symbol="510300",
            state="UNRECOGNIZED_OVERLAY",
            strategy_version="SWING_V1",
            level="RED",
            label="rogue",
            evidence={},
        ))
        service._health["intraday"] = "REALTIME"
        service._refresh_alert_snapshot(datetime(
            2026, 9, 1, 14, 0, tzinfo=SHANGHAI,
        ))
        self.assertIn(
            rogue.alert_id,
            {
                item["alert_id"]
                for item in service.alerts(include_retracted=True)["items"]
            },
        )
        self.assertNotIn(
            rogue.alert_id,
            {item["alert_id"] for item in service.snapshot()["active_alerts"]},
        )

    def test_invalid_calendar_fails_closed_for_portfolio_and_candidates(self) -> None:
        self.paths.calendar.write_text("{broken", encoding="utf-8")
        service = self.make_service()
        snapshot = service.snapshot()
        self.assertEqual(snapshot["health"]["calendar"], "BLOCKED")
        self.assertEqual(snapshot["health"]["portfolio"], "BLOCKED")
        self.assertNotEqual(
            snapshot["items"][0]["execution_status"], "READY_TO_EXECUTE",
        )

    def test_position_trailing_high_starts_at_current_open_lifecycle(self) -> None:
        bars = list(retime_daily_bars(
            swing_strategy_bars(80, pattern="rising"),
            ending_on=date(2026, 8, 31),
        ))
        old = bars[0]
        payload = old.to_dict()
        payload.update({
            "adjusted_open": old.adjusted_open * 10.0,
            "adjusted_high": old.adjusted_high * 10.0,
            "adjusted_low": old.adjusted_low * 10.0,
            "adjusted_close": old.adjusted_close * 10.0,
        })
        bars[0] = DailyBar.from_mapping(payload)
        self.paths.daily_history.unlink()
        DailyHistoryStore(
            self.paths.daily_history, self.metadata, frozenset(),
        ).upsert(tuple(bars))
        PortfolioLedger(
            self.paths.trades,
            self.metadata,
            clock=lambda: datetime(2026, 8, 31, 16, tzinfo=SHANGHAI),
            closed_dates=frozenset(),
        ).record_trade(TradeInput(
            symbol="510300", side="BUY", shares=100,
            price=bars[-1].close, fee=0.0,
            executed_at=datetime(2026, 8, 31, 14, 30, tzinfo=SHANGHAI),
            planned_risk_per_share=2.0,
        ), "late-entry")

        state = self.make_service().snapshot()["items"][0]["formal_state"]
        self.assertNotEqual(state, "EXIT_CANDIDATE")

    def test_initial_position_lifecycle_starts_at_initialization_session(self) -> None:
        bars = list(retime_daily_bars(
            swing_strategy_bars(80, pattern="rising"),
            ending_on=date(2026, 9, 4),
        ))
        old = bars[0]
        payload = old.to_dict()
        payload.update({
            "adjusted_open": old.adjusted_open * 10.0,
            "adjusted_high": old.adjusted_high * 10.0,
            "adjusted_low": old.adjusted_low * 10.0,
            "adjusted_close": old.adjusted_close * 10.0,
        })
        bars[0] = DailyBar.from_mapping(payload)
        self.paths.daily_history.unlink()
        DailyHistoryStore(
            self.paths.daily_history, self.metadata, frozenset(),
        ).upsert(tuple(bars))
        self.paths.trades.unlink()
        PortfolioLedger(
            self.paths.trades,
            self.metadata,
            clock=lambda: datetime(2026, 9, 6, 16, tzinfo=SHANGHAI),
            closed_dates=frozenset(),
        ).initialize(
            "existing", 1_000.0, "existing-init",
            initial_positions={
                "510300": {
                    "shares": 100,
                    "average_cost": bars[-1].close,
                    "planned_risk_per_share": 2.0,
                },
            },
        )
        state = self.make_service(clock=lambda: datetime(
            2026, 9, 7, 10, tzinfo=SHANGHAI,
        )).snapshot()["items"][0]["formal_state"]
        self.assertNotEqual(state, "EXIT_CANDIDATE")

    def test_trade_rebuilds_current_session_projection_before_publish(self) -> None:
        service = self.make_service()
        event = service.record_trade(TradeInput(
            symbol="510300", side="BUY", shares=100,
            price=100.0, fee=5.0,
            executed_at=datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
            planned_risk_per_share=2.0,
        ), "service-buy")
        snapshot = service.snapshot()
        self.assertEqual(event["event_type"], "BUY_CONFIRMED")
        self.assertEqual(snapshot["portfolio"]["as_of_trading_date"], "2026-09-01")
        self.assertEqual(snapshot["portfolio"]["positions"]["510300"]["shares"], 100)
        self.assertEqual(snapshot["revision"], 1)

    def test_stop_exit_cooldown_survives_restart_but_ordinary_exit_does_not(self) -> None:
        service = self.make_service()
        service.record_trade(TradeInput(
            "510300", "BUY", 100, 100.0, 0.0,
            datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
            planned_risk_per_share=2.0,
        ), "cooldown-buy")
        service.clock = lambda: datetime(2026, 9, 2, 10, tzinfo=SHANGHAI)
        service.record_trade(TradeInput(
            "510300", "SELL", 100, 98.0, 0.0,
            datetime(2026, 9, 2, 10, tzinfo=SHANGHAI),
            exit_reason="STOP_EXIT",
        ), "cooldown-stop")
        stop_log = self.paths.trades.read_bytes()
        restarted = self.make_service(clock=lambda: datetime(
            2026, 9, 3, 10, tzinfo=SHANGHAI,
        ))
        self.assertEqual(
            restarted.snapshot()["items"][0]["formal_state"], "COOLDOWN",
        )

        ordinary_path = Path(self.temporary.name) / "ordinary.jsonl"
        ordinary = PortfolioLedger(
            ordinary_path, self.metadata,
            clock=lambda: datetime(2026, 9, 2, 10, tzinfo=SHANGHAI),
            closed_dates=frozenset(),
        )
        ordinary.initialize("ordinary", 100_000.0, "ordinary-init")
        ordinary.record_trade(TradeInput(
            "510300", "BUY", 100, 100.0, 0.0,
            datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
        ), "ordinary-buy")
        ordinary.record_trade(TradeInput(
            "510300", "SELL", 100, 101.0, 0.0,
            datetime(2026, 9, 2, 10, tzinfo=SHANGHAI),
        ), "ordinary-sell")
        self.paths.trades.write_bytes(ordinary_path.read_bytes())
        not_stopped = self.make_service(clock=lambda: datetime(
            2026, 9, 3, 10, tzinfo=SHANGHAI,
        ))
        self.assertNotEqual(
            not_stopped.snapshot()["items"][0]["formal_state"], "COOLDOWN",
        )

        later_bars = retime_daily_bars(
            swing_strategy_bars(80, pattern="rising"),
            ending_on=date(2026, 9, 10),
        )
        self.paths.daily_history.unlink()
        DailyHistoryStore(
            self.paths.daily_history, self.metadata, frozenset(),
        ).upsert(later_bars)
        self.paths.trades.write_bytes(stop_log)
        released = self.make_service(clock=lambda: datetime(
            2026, 9, 11, 10, tzinfo=SHANGHAI,
        ))
        self.assertNotEqual(
            released.snapshot()["items"][0]["formal_state"], "COOLDOWN",
        )

        partial_path = Path(self.temporary.name) / "partial-stop.jsonl"
        partial = PortfolioLedger(
            partial_path, self.metadata,
            clock=lambda: datetime(2026, 9, 3, 10, tzinfo=SHANGHAI),
            closed_dates=frozenset(),
        )
        partial.initialize("partial", 100_000.0, "partial-init")
        partial.record_trade(TradeInput(
            "510300", "BUY", 200, 100.0, 0.0,
            datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
        ), "partial-buy")
        partial.record_trade(TradeInput(
            "510300", "SELL", 100, 98.0, 0.0,
            datetime(2026, 9, 2, 10, tzinfo=SHANGHAI),
            exit_reason="STOP_EXIT",
        ), "partial-stop")
        partial.record_trade(TradeInput(
            "510300", "SELL", 100, 101.0, 0.0,
            datetime(2026, 9, 3, 10, tzinfo=SHANGHAI),
        ), "partial-ordinary-close")
        self.paths.trades.write_bytes(partial_path.read_bytes())
        partial_restart = self.make_service(clock=lambda: datetime(
            2026, 9, 11, 10, tzinfo=SHANGHAI,
        ))
        self.assertNotEqual(
            partial_restart.snapshot()["items"][0]["formal_state"], "COOLDOWN",
        )

    def test_formal_recompute_failure_cannot_commit_or_publish_partial_batch(self) -> None:
        PortfolioLedger(
            self.paths.trades,
            self.metadata,
            clock=lambda: datetime(2026, 8, 31, 16, tzinfo=SHANGHAI),
            closed_dates=frozenset(),
        ).record_trade(TradeInput(
            "510300", "BUY", 100, self.initial_bars[-1].close, 0.0,
            datetime(2026, 8, 31, 10, tzinfo=SHANGHAI),
            planned_risk_per_share=2.0,
        ), "projection-counterexample-buy")
        service = self.make_service(collector=StaticDailyCollector(self.final_bars))
        before_file = self.paths.daily_history.read_bytes()
        before_portfolio = self.paths.portfolio_snapshot.read_bytes()
        before_snapshot = service.snapshot()
        with patch(
            "etf_rotation.swing_service.evaluate_swing",
            side_effect=RuntimeError("one symbol failed"),
        ):
            self.assertFalse(service.refresh_once(datetime(
                2026, 9, 1, 15, 10, tzinfo=SHANGHAI,
            )))
        self.assertEqual(self.paths.daily_history.read_bytes(), before_file)
        self.assertEqual(
            self.paths.portfolio_snapshot.read_bytes(), before_portfolio,
        )
        after = service.snapshot()
        self.assertEqual(after["items"], before_snapshot["items"])

    def test_new_trade_rejects_future_execution_but_retry_ignores_clock_rollback(
        self,
    ) -> None:
        service = self.make_service(clock=lambda: datetime(
            2026, 9, 1, 10, 1, tzinfo=SHANGHAI,
        ))
        before_trades = self.paths.trades.read_bytes()
        before_portfolio_file = self.paths.portfolio_snapshot.read_bytes()
        before_projection = service.portfolio()
        before_revision = service.snapshot()["revision"]
        for side in ("BUY", "SELL"):
            with self.subTest(side=side), self.assertRaisesRegex(
                Exception, "trusted clock",
            ):
                service.record_trade(TradeInput(
                    "510300", side, 100, 100.0, 0.0,
                    datetime(2026, 9, 1, 14, 0, tzinfo=SHANGHAI),
                    planned_risk_per_share=(2.0 if side == "BUY" else 0.0),
                ), f"far-future-{side.lower()}")
        self.assertEqual(self.paths.trades.read_bytes(), before_trades)
        self.assertEqual(
            self.paths.portfolio_snapshot.read_bytes(), before_portfolio_file,
        )
        self.assertEqual(service.portfolio(), before_projection)
        self.assertEqual(service.snapshot()["revision"], before_revision)

        boundary = TradeInput(
            "510300", "BUY", 100, 100.0, 0.0,
            datetime(2026, 9, 1, 10, 1, 5, tzinfo=SHANGHAI),
            planned_risk_per_share=2.0,
        )
        accepted = service.record_trade(boundary, "future-skew-boundary")
        accepted_revision = service.snapshot()["revision"]
        accepted_trades = self.paths.trades.read_bytes()
        accepted_portfolio = self.paths.portfolio_snapshot.read_bytes()

        with self.assertRaisesRegex(Exception, "trusted clock"):
            service.record_trade(replace(
                boundary,
                executed_at=datetime(
                    2026, 9, 1, 10, 1, 6, tzinfo=SHANGHAI,
                ),
            ), "future-skew-exceeded")
        self.assertEqual(service.snapshot()["revision"], accepted_revision)
        self.assertEqual(self.paths.trades.read_bytes(), accepted_trades)
        self.assertEqual(
            self.paths.portfolio_snapshot.read_bytes(), accepted_portfolio,
        )

        service.clock = lambda: datetime(
            2026, 9, 1, 9, 0, tzinfo=SHANGHAI,
        )
        self.assertEqual(
            service.record_trade(boundary, "future-skew-boundary"), accepted,
        )
        self.assertEqual(service.snapshot()["revision"], accepted_revision)
        self.assertEqual(self.paths.trades.read_bytes(), accepted_trades)
        self.assertEqual(
            self.paths.portfolio_snapshot.read_bytes(), accepted_portfolio,
        )

    def test_idempotent_retry_repairs_failed_trade_derivations_once(self) -> None:
        tracked_paths = (
            self.paths.trades,
            self.paths.portfolio_snapshot,
            self.paths.alerts,
        )
        baseline = {
            path: path.read_bytes() if path.exists() else None
            for path in tracked_paths
        }

        def restore() -> None:
            for path, content in baseline.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)

        for failure_point in ("evaluate", "build", "publish"):
            with self.subTest(failure_point=failure_point):
                restore()
                service = self.make_service()
                trade = TradeInput(
                    "510300", "BUY", 100, 100.0, 0.0,
                    datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
                    planned_risk_per_share=2.0,
                )
                if failure_point == "evaluate":
                    context = patch(
                        "etf_rotation.swing_service.evaluate_swing",
                        side_effect=RuntimeError("evaluate failed"),
                    )
                elif failure_point == "build":
                    context = patch.object(
                        service, "_build_snapshot",
                        side_effect=RuntimeError("build failed"),
                    )
                else:
                    context = patch.object(
                        service, "_publish",
                        side_effect=RuntimeError("publish failed"),
                    )
                with context, self.assertRaisesRegex(
                    RuntimeError, f"{failure_point} failed",
                ):
                    service.record_trade(trade, "repairable-buy")

                existing = next(
                    event for event in service._ledger.load_events()
                    if event.idempotency_key == "repairable-buy"
                )
                failed_revision = service.snapshot()["revision"]
                service.clock = lambda: datetime(
                    2026, 9, 1, 9, 0, tzinfo=SHANGHAI,
                )
                repaired = service.record_trade(trade, "repairable-buy")
                self.assertEqual(repaired, existing.to_dict())
                repaired_snapshot = service.snapshot()
                self.assertGreater(
                    repaired_snapshot["revision"], failed_revision,
                )
                repaired_position = (
                    repaired_snapshot["portfolio"]["positions"]["510300"]
                )
                self.assertEqual(
                    repaired_position["shares"], 100,
                )
                self.assertEqual(
                    service.portfolio()["projection"],
                    repaired_snapshot["portfolio"],
                )
                self.assertEqual(
                    repaired_snapshot["items"][0]["formal_decision"],
                    service._formal["510300"].to_dict(),
                )
                stable_revision = repaired_snapshot["revision"]
                self.assertEqual(
                    service.record_trade(trade, "repairable-buy"), repaired,
                )
                self.assertEqual(
                    service.snapshot()["revision"], stable_revision,
                )

    def test_idempotent_retry_strictly_repairs_corrupt_projection_json(self) -> None:
        tracked_paths = (
            self.paths.trades,
            self.paths.portfolio_snapshot,
            self.paths.alerts,
        )
        baseline = {
            path: path.read_bytes() if path.exists() else None
            for path in tracked_paths
        }
        for corruption in ("boolean-schema", "duplicate-key"):
            with self.subTest(corruption=corruption):
                for path, content in baseline.items():
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(content)
                service = self.make_service()
                trade = TradeInput(
                    "510300", "BUY", 100, 100.0, 0.0,
                    datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
                    planned_risk_per_share=2.0,
                )
                first = service.record_trade(trade, "corrupt-projection-buy")
                valid_text = self.paths.portfolio_snapshot.read_text(
                    encoding="utf-8",
                )
                if corruption == "boolean-schema":
                    payload = json.loads(valid_text)
                    payload["schema_version"] = True
                    corrupt_text = json.dumps(payload, ensure_ascii=False)
                else:
                    corrupt_text = '{"schema_version":1,' + valid_text.lstrip()[1:]
                self.paths.portfolio_snapshot.write_text(
                    corrupt_text, encoding="utf-8",
                )
                revision_before_repair = service.snapshot()["revision"]
                self.assertEqual(
                    service.record_trade(
                        trade, "corrupt-projection-buy",
                    ),
                    first,
                )
                self.assertEqual(
                    service.snapshot()["revision"], revision_before_repair + 1,
                )
                repaired = json.loads(
                    self.paths.portfolio_snapshot.read_text(encoding="utf-8"),
                )
                self.assertIs(type(repaired["schema_version"]), int)
                stable_revision = service.snapshot()["revision"]
                self.assertEqual(
                    service.record_trade(
                        trade, "corrupt-projection-buy",
                    ),
                    first,
                )
                self.assertEqual(
                    service.snapshot()["revision"], stable_revision,
                )

    def test_trade_clock_validation_never_mutates_unpublished_health(self) -> None:
        class ToggleClock:
            failed = False

            def __call__(self) -> datetime:
                if self.failed:
                    raise RuntimeError("trade clock failed")
                return datetime(2026, 9, 1, 14, 0, tzinfo=SHANGHAI)

        clock = ToggleClock()
        service = self.make_service(clock=clock)
        before = service.snapshot()
        before_health = dict(service._health)
        before_errors = dict(service._errors)
        clock.failed = True
        with self.assertRaisesRegex(Exception, "trusted clock"):
            service.record_trade(TradeInput(
                "510300", "BUY", 100, 100.0, 0.0,
                datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
                planned_risk_per_share=2.0,
            ), "failed-trade-clock")
        self.assertEqual(service._health, before_health)
        self.assertEqual(service._errors, before_errors)
        self.assertEqual(service.snapshot(), before)

        clock.failed = False
        service.record_trade(TradeInput(
            "510300", "BUY", 100, 100.0, 0.0,
            datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
            planned_risk_per_share=2.0,
        ), "recovered-trade-clock")
        recovered = service.snapshot()
        self.assertEqual(recovered["health"]["service"], "OK")
        self.assertNotIn("service", recovered["errors"])
        self.assertEqual(service._health, recovered["health"])
        self.assertEqual(service._errors, recovered["errors"])

    def test_inferred_stop_exit_retry_is_stable_and_still_checks_payload(self) -> None:
        service = self.make_service()
        service.record_trade(TradeInput(
            "510300", "BUY", 100, 100.0, 0.0,
            datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
            planned_risk_per_share=2.0,
        ), "stable-stop-buy")
        service.clock = lambda: datetime(2026, 9, 2, 10, tzinfo=SHANGHAI)
        with service.publish_condition:
            item = service.published["items"][0]
            item["intraday_overlay"] = "PREDEFINED_STOP_TOUCHED"
            item["intraday_health_status"] = "REALTIME"
            item["current_price_time"] = "2026-09-02T09:59:00+08:00"
        sell = TradeInput(
            "510300", "SELL", 100, 98.0, 0.0,
            datetime(2026, 9, 2, 10, tzinfo=SHANGHAI),
        )
        first = service.record_trade(sell, "stable-stop-sell")
        self.assertEqual(first["payload"]["exit_reason"], "STOP_EXIT")
        self.assertEqual(
            service.record_trade(sell, "stable-stop-sell"), first,
        )
        with self.assertRaisesRegex(Exception, "different request"):
            service.record_trade(replace(sell, price=97.0), "stable-stop-sell")

    def test_yesterday_overlay_does_not_reclassify_an_ordinary_exit(self) -> None:
        service = self.make_service()
        service.record_trade(TradeInput(
            "510300", "BUY", 100, 100.0, 0.0,
            datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
            planned_risk_per_share=2.0,
        ), "ordinary-after-old-overlay-buy")
        SwingAlertStore(self.paths.alerts).publish_overlay(AlertInput(
            trading_date=date(2026, 9, 1),
            symbol="510300",
            state="PREDEFINED_STOP_TOUCHED",
            strategy_version="SWING_V1",
            level="RED",
            label="old stop",
            evidence={},
        ))
        service.clock = lambda: datetime(2026, 9, 2, 10, tzinfo=SHANGHAI)
        event = service.record_trade(TradeInput(
            "510300", "SELL", 100, 101.0, 0.0,
            datetime(2026, 9, 2, 10, tzinfo=SHANGHAI),
        ), "ordinary-after-old-overlay-sell")
        self.assertNotIn("exit_reason", event["payload"])

    def test_current_stop_does_not_reclassify_a_historical_sell(self) -> None:
        service = self.make_service()
        service.record_trade(TradeInput(
            "510300", "BUY", 100, 100.0, 0.0,
            datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
            planned_risk_per_share=2.0,
        ), "historical-stop-buy")
        service.clock = lambda: datetime(2026, 9, 3, 10, tzinfo=SHANGHAI)
        with service.publish_condition:
            item = service.published["items"][0]
            item["intraday_overlay"] = "PREDEFINED_STOP_TOUCHED"
            item["intraday_health_status"] = "REALTIME"
            item["current_price_time"] = "2026-09-03T09:59:30+08:00"
        event = service.record_trade(TradeInput(
            "510300", "SELL", 100, 99.0, 0.0,
            datetime(2026, 9, 2, 10, tzinfo=SHANGHAI),
        ), "historical-stop-sell")
        self.assertNotIn("exit_reason", event["payload"])

    def test_overlay_stop_only_classifies_sales_after_the_trigger_time(self) -> None:
        original_trades = self.paths.trades.read_bytes()
        original_portfolio = (
            self.paths.portfolio_snapshot.read_bytes()
            if self.paths.portfolio_snapshot.exists() else None
        )
        for label, executed_at, expected_stop in (
            (
                "before-trigger",
                datetime(2026, 9, 2, 9, 35, tzinfo=SHANGHAI), False,
            ),
            (
                "after-trigger",
                datetime(2026, 9, 2, 10, 0, tzinfo=SHANGHAI), True,
            ),
        ):
            with self.subTest(label=label):
                self.paths.trades.write_bytes(original_trades)
                if original_portfolio is None:
                    self.paths.portfolio_snapshot.unlink(missing_ok=True)
                else:
                    self.paths.portfolio_snapshot.write_bytes(original_portfolio)
                service = self.make_service()
                service.record_trade(TradeInput(
                    "510300", "BUY", 100, 100.0, 0.0,
                    datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
                    planned_risk_per_share=2.0,
                ), f"{label}-buy")
                service.clock = lambda: datetime(
                    2026, 9, 2, 10, 1, tzinfo=SHANGHAI,
                )
                with service.publish_condition:
                    item = service.published["items"][0]
                    item["intraday_overlay"] = "PREDEFINED_STOP_TOUCHED"
                    item["intraday_health_status"] = "REALTIME"
                    item["current_price_time"] = (
                        "2026-09-02T09:59:30+08:00"
                    )
                event = service.record_trade(TradeInput(
                    "510300", "SELL", 100, 98.0, 0.0, executed_at,
                ), f"{label}-sell")
                if expected_stop:
                    self.assertEqual(
                        event["payload"]["exit_reason"], "STOP_EXIT",
                    )
                else:
                    self.assertNotIn("exit_reason", event["payload"])

    def test_explicit_historical_stop_exit_is_preserved(self) -> None:
        service = self.make_service()
        service.record_trade(TradeInput(
            "510300", "BUY", 100, 100.0, 0.0,
            datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
            planned_risk_per_share=2.0,
        ), "explicit-historical-buy")
        service.clock = lambda: datetime(2026, 9, 3, 10, tzinfo=SHANGHAI)
        event = service.record_trade(TradeInput(
            "510300", "SELL", 100, 98.0, 0.0,
            datetime(2026, 9, 2, 10, tzinfo=SHANGHAI),
            exit_reason="STOP_EXIT",
        ), "explicit-historical-sell")
        self.assertEqual(event["payload"]["exit_reason"], "STOP_EXIT")

    def test_current_healthy_formal_stop_can_classify_a_full_exit(self) -> None:
        service = self.make_service(
            collector=StaticDailyCollector(self.final_bars),
        )
        self.assertTrue(service.refresh_once(datetime(
            2026, 9, 1, 15, 10, tzinfo=SHANGHAI,
        )))
        service.clock = lambda: datetime(2026, 9, 2, 10, tzinfo=SHANGHAI)
        service.record_trade(TradeInput(
            "510300", "BUY", 100, 100.0, 0.0,
            datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
            planned_risk_per_share=2.0,
        ), "formal-stop-buy")
        formal = service._formal["510300"]
        evidence = dict(formal.evidence)
        evidence["exit_hard_stop"] = True
        service._formal["510300"] = replace(
            formal,
            state=SwingState.EXIT_CANDIDATE,
            evidence=evidence,
            blocked_reasons=(),
            valid_for_trading_date=None,
        )
        event = service.record_trade(TradeInput(
            "510300", "SELL", 100, 98.0, 0.0,
            datetime(2026, 9, 2, 10, tzinfo=SHANGHAI),
        ), "formal-stop-sell")
        self.assertEqual(event["payload"]["exit_reason"], "STOP_EXIT")

    def test_non_exit_formal_state_cannot_infer_stop_from_evidence_alone(self) -> None:
        service = self.make_service(
            collector=StaticDailyCollector(self.final_bars),
        )
        self.assertTrue(service.refresh_once(datetime(
            2026, 9, 1, 15, 10, tzinfo=SHANGHAI,
        )))
        service.clock = lambda: datetime(2026, 9, 2, 10, tzinfo=SHANGHAI)
        service.record_trade(TradeInput(
            "510300", "BUY", 100, 100.0, 0.0,
            datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
            planned_risk_per_share=2.0,
        ), "non-exit-evidence-buy")
        formal = service._formal["510300"]
        evidence = dict(formal.evidence)
        evidence["exit_hard_stop"] = True
        service._formal["510300"] = replace(
            formal, state=SwingState.HOLDING, evidence=evidence,
        )
        event = service.record_trade(TradeInput(
            "510300", "SELL", 100, 98.0, 0.0,
            datetime(2026, 9, 2, 10, tzinfo=SHANGHAI),
        ), "non-exit-evidence-sell")
        self.assertNotIn("exit_reason", event["payload"])

    def test_active_formal_alert_must_match_current_decision_identity(self) -> None:
        SwingAlertStore(self.paths.alerts).publish_formal(AlertInput(
            trading_date=date(2026, 8, 30),
            symbol="510300",
            state="ADD_CANDIDATE",
            strategy_version="SWING_V1",
            level="YELLOW",
            label="stale add",
            evidence={},
        ))
        service = self.make_service()
        self.assertEqual(service.snapshot()["active_alerts"], [])
        stale = next(
            item for item in service.alerts()["items"]
            if item["state"] == "ADD_CANDIDATE"
        )
        self.assertFalse(stale["active_notification"])

    def test_overlay_sync_retracts_by_full_lifecycle_without_churning_peer(self) -> None:
        service = self.make_service()
        store = SwingAlertStore(self.paths.alerts)
        stale = store.publish_overlay(AlertInput(
            trading_date=date(2026, 8, 31),
            symbol="510300",
            state="APPROACHING_ENTRY_ZONE",
            strategy_version="SWING_V1",
            level="BLUE",
            label="stale",
            evidence={},
        ))
        current_input = AlertInput(
            trading_date=date(2026, 9, 1),
            symbol="510300",
            state="APPROACHING_ENTRY_ZONE",
            strategy_version="SWING_V1",
            level="BLUE",
            label="current",
            evidence={},
        )
        current = store.publish_overlay(current_input)
        service._sync_overlay_alerts((current_input,))
        projected = {item.alert_id: item for item in store.current(
            include_retracted=True,
        )}
        self.assertTrue(projected[stale.alert_id].retracted)
        self.assertFalse(projected[current.alert_id].retracted)
        self.assertEqual(
            projected[current.alert_id].generation, current.generation,
        )

    def test_watchlist_update_is_atomic_validated_and_published(self) -> None:
        service = self.make_service()
        service.refresh_intraday()
        active_before = service.snapshot()["active_alerts"]
        self.assertTrue(active_before)
        old_overlay = active_before[0]
        before_revision = service.snapshot()["revision"]
        result = service.update_watchlist("510300", False)
        self.assertFalse(result["items"][0]["enabled"])
        self.assertEqual(service.snapshot()["items"], [])
        self.assertEqual(service.snapshot()["active_alerts"], [])
        self.assertEqual(service.snapshot()["revision"], before_revision + 1)
        persisted = json.loads(self.paths.watchlist.read_text(encoding="utf-8"))
        self.assertEqual(persisted, {
            "schema_version": 1,
            "items": [{"symbol": "510300", "enabled": False}],
        })
        service.update_watchlist("510300", True)
        self.assertFalse(any(
            item["scope"] == "INTRADAY"
            for item in service.snapshot()["active_alerts"]
        ))
        history = {
            item["alert_id"]: item
            for item in service.alerts(include_retracted=True)["items"]
        }
        self.assertTrue(history[old_overlay["alert_id"]]["retracted"])
        refreshed = service.refresh_intraday()
        replacement = next(
            item for item in refreshed["active_alerts"]
            if item["scope"] == "INTRADAY"
        )
        self.assertNotEqual(replacement["alert_id"], old_overlay["alert_id"])
        self.assertEqual(
            (
                replacement["trading_date"], replacement["symbol"],
                replacement["state"], replacement["strategy_version"],
            ),
            (
                old_overlay["trading_date"], old_overlay["symbol"],
                old_overlay["state"], old_overlay["strategy_version"],
            ),
        )
        self.assertGreater(
            replacement["generation"], old_overlay["generation"],
        )
        for symbol, enabled in (("999999", True), ("510300", 1)):
            with self.subTest(symbol=symbol, enabled=enabled):
                before = self.paths.watchlist.read_bytes()
                with self.assertRaises(Exception):
                    service.update_watchlist(symbol, enabled)
                self.assertEqual(self.paths.watchlist.read_bytes(), before)

        before = self.paths.watchlist.read_bytes()
        before_snapshot = service.snapshot()
        with patch(
            "etf_rotation.swing_service.os.replace",
            side_effect=OSError("replace failed"),
        ):
            with self.assertRaises(Exception):
                service.update_watchlist("510300", False)
        self.assertEqual(self.paths.watchlist.read_bytes(), before)
        failed = service.snapshot()
        self.assertEqual(service.watchlist()["items"], [
            {"symbol": "510300", "enabled": True},
        ])
        self.assertEqual(failed["active_alerts"], [])
        self.assertEqual(failed["health"]["alerts"], "BLOCKED")
        self.assertIsNone(failed["items"][0]["intraday_overlay"])
        self.assertEqual(
            failed["items"][0]["formal_decision"],
            before_snapshot["items"][0]["formal_decision"],
        )

    def test_watchlist_update_can_add_a_metadata_verified_symbol(self) -> None:
        self.paths.metadata.write_text(json.dumps(
            metadata_fixture(("510300", "159915")),
        ), encoding="utf-8")
        service = self.make_service()
        result = service.update_watchlist("159915", True)
        self.assertEqual(result["items"], [
            {"symbol": "510300", "enabled": True},
            {"symbol": "159915", "enabled": True},
        ])
        self.assertEqual(
            [item["symbol"] for item in service.snapshot()["items"]],
            ["510300", "159915"],
        )

    def test_enabling_existing_history_persists_one_current_formal_alert(self) -> None:
        self.paths.watchlist.write_text(json.dumps({
            "schema_version": 1,
            "items": [{"symbol": "510300", "enabled": False}],
        }), encoding="utf-8")
        service = self.make_service()
        self.assertEqual(service.snapshot()["items"], [])

        service.update_watchlist("510300", True)
        enabled = service.snapshot()
        self.assertEqual(
            enabled["items"][0]["formal_state"], "TRIAL_ENTRY_CANDIDATE",
        )
        formal = [
            item for item in service.alerts()["items"]
            if item["scope"] == "FORMAL"
        ]
        self.assertEqual(len(formal), 1)
        self.assertEqual(formal[0]["state"], "TRIAL_ENTRY_CANDIDATE")
        self.assertTrue(formal[0]["active_notification"])
        self.assertEqual(enabled["active_alerts"], formal)
        event_count = len(SwingAlertStore(self.paths.alerts).load_events())

        service.update_watchlist("510300", True)
        self.assertEqual(
            len(SwingAlertStore(self.paths.alerts).load_events()), event_count,
        )
        self.assertEqual(len(service.snapshot()["active_alerts"]), 1)

        service.update_watchlist("510300", False)
        disabled_history = service.alerts(include_retracted=True)["items"]
        self.assertEqual(len(disabled_history), 1)
        self.assertFalse(disabled_history[0]["retracted"])
        self.assertFalse(disabled_history[0]["active_notification"])
        self.assertEqual(service.snapshot()["active_alerts"], [])

    def test_same_watchlist_value_is_idempotent_without_overlay_churn(self) -> None:
        service = self.make_service()
        first_refresh = service.refresh_intraday()
        overlay = next(
            item for item in first_refresh["active_alerts"]
            if item["scope"] == "INTRADAY"
        )

        service.update_watchlist("510300", True)
        after_ensure = service.snapshot()
        retained = next(
            item for item in after_ensure["active_alerts"]
            if item["scope"] == "INTRADAY"
        )
        self.assertEqual(
            (retained["alert_id"], retained["generation"]),
            (overlay["alert_id"], overlay["generation"]),
        )
        self.assertTrue(any(
            item["scope"] == "FORMAL"
            for item in after_ensure["active_alerts"]
        ))
        store = SwingAlertStore(self.paths.alerts)
        ensured_event_count = len(store.load_events())
        ensured_revision = after_ensure["revision"]

        service.update_watchlist("510300", True)
        self.assertEqual(len(store.load_events()), ensured_event_count)
        self.assertEqual(service.snapshot()["revision"], ensured_revision)
        refreshed = service.refresh_intraday()
        still_active = next(
            item for item in refreshed["active_alerts"]
            if item["scope"] == "INTRADAY"
        )
        self.assertEqual(
            (still_active["alert_id"], still_active["generation"]),
            (overlay["alert_id"], overlay["generation"]),
        )
        self.assertEqual(len(store.load_events()), ensured_event_count)

        service.update_watchlist("510300", False)
        disabled_file = self.paths.watchlist.read_bytes()
        disabled_events = len(store.load_events())
        disabled_snapshot = service.snapshot()
        service.update_watchlist("510300", False)
        self.assertEqual(self.paths.watchlist.read_bytes(), disabled_file)
        self.assertEqual(len(store.load_events()), disabled_events)
        self.assertEqual(service.snapshot(), disabled_snapshot)

    def test_watchlist_retraction_failure_is_fail_closed(self) -> None:
        service = self.make_service()
        service.refresh_intraday()
        before = self.paths.watchlist.read_bytes()
        with patch.object(
            service._alert_store, "retract_overlay",
            side_effect=RuntimeError("retract failed"),
        ):
            with self.assertRaises(Exception):
                service.update_watchlist("510300", False)
        self.assertEqual(self.paths.watchlist.read_bytes(), before)
        failed = service.snapshot()
        self.assertEqual(failed["health"]["alerts"], "BLOCKED")
        self.assertEqual(failed["active_alerts"], [])
        self.assertIsNone(failed["items"][0]["intraday_overlay"])

    def test_blocking_producer_reference_survives_stop_timeout(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingCollector:
            def collect(self, *args: object, **kwargs: object) -> tuple[DailyBar, ...]:
                entered.set()
                release.wait(5.0)
                return self.final  # type: ignore[attr-defined]

        collector = BlockingCollector()
        collector.final = self.final_bars
        service = self.make_service(
            collector=collector,
            clock=lambda: datetime(2026, 9, 1, 15, 10, tzinfo=SHANGHAI),
            refresh_interval=0.01,
        )
        service.start_refresh()
        self.assertTrue(entered.wait(1.0))
        thread = service._refresh_thread
        service.stop_refresh()
        self.assertIs(service._refresh_thread, thread)
        self.assertTrue(thread.is_alive())
        service.start_refresh()
        self.assertIs(service._refresh_thread, thread)
        release.set()
        thread.join(1.0)
        service.stop_refresh()
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
