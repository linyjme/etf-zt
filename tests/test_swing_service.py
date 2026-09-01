from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from etf_rotation.etf_metadata import EtfMetadataStore
from etf_rotation.swing_config import SwingWatchItem
from etf_rotation.swing_data import DailyBar, DailyHistoryStore
from etf_rotation.swing_portfolio import PortfolioLedger, TradeInput
from etf_rotation.swing_service import SwingPaths, SwingService

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
    ) -> SwingService:
        return SwingService(
            self.paths,
            collector=collector,
            intraday_provider=(
                intraday_provider
                if intraday_provider is not None
                else lambda: {
                    "generated_at": "2026-09-01T10:00:00+08:00",
                    "items": [{
                        "symbol": "510300", "price": 106.0,
                        "timestamp": "2026-09-01T10:00:00+08:00",
                        "health_status": "REALTIME",
                    }],
                }
            ),
            intraday_points_provider=(
                intraday_points_provider
                if intraday_points_provider is not None
                else lambda _symbol: {"upserts": []}
            ),
            clock=lambda: datetime(2026, 9, 1, 14, 0, tzinfo=SHANGHAI),
            event_limit=event_limit,
        )

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
        realtime = service.refresh_intraday()["items"][0]
        self.assertEqual(realtime["formal_state"], formal)
        self.assertEqual(realtime["execution_status"], "PAUSED_DAILY_DATA")

    def test_available_minute_mismatch_blocks_entire_batch(self) -> None:
        def points(_symbol: str) -> dict[str, object]:
            return {"upserts": [{
                "trading_date": "2026-09-01", "is_complete": True,
                "timestamp": "2026-09-01T15:00:00+08:00",
                "open": 1.0, "high": 1.0, "low": 1.0, "price": 1.0,
            }]}

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

    def test_delayed_intraday_quote_is_visible_but_cannot_create_overlay(self) -> None:
        service = self.make_service(intraday_provider=lambda: {
            "items": [{
                "symbol": "510300", "price": 105.9,
                "timestamp": "2026-09-01T09:31:00+08:00",
                "health_status": "DELAYED",
            }],
        })
        item = service.refresh_intraday()["items"][0]
        self.assertEqual(item["current_price"], 105.9)
        self.assertEqual(item["intraday_health_status"], "DELAYED")
        self.assertIsNone(item["intraday_overlay"])
        self.assertEqual(item["execution_status"], "PAUSED_MARKET_NOT_REALTIME")

    def test_refresh_is_single_producer_under_concurrency(self) -> None:
        collector = StaticDailyCollector(self.final_bars, delay=0.03)
        service = self.make_service(collector=collector)
        now = datetime(2026, 9, 1, 15, 10, tzinfo=SHANGHAI)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = tuple(pool.map(lambda _index: service.refresh_once(now), range(6)))
        self.assertEqual(collector.calls, 1)
        self.assertEqual(results.count(True), 1)

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
        scale = old.close / old.adjusted_close
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


if __name__ == "__main__":
    unittest.main()
