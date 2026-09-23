from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from etf_rotation.etf_metadata import EtfMetadataStore
from etf_rotation.swing_data import DailyHistoryStore
from etf_rotation.swing_portfolio import PortfolioLedger, TradeInput
from etf_rotation.swing_service import SwingPaths, SwingService

from tests.swing_helpers import metadata_fixture, retime_daily_bars, swing_strategy_bars


SHANGHAI = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]


class SwingV11ServiceWiringTests(unittest.TestCase):
    """The service feeds the V11 position evaluator and remembers its state."""

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
        fixture = metadata_fixture(("510300",))
        fixture["items"][0]["dividend_dates"] = ["2026-09-02"]
        self.paths.metadata.write_text(json.dumps(fixture), encoding="utf-8")
        self.paths.watchlist.write_text(json.dumps({
            "schema_version": 1,
            "items": [{"symbol": "510300", "enabled": True}],
        }), encoding="utf-8")
        strategy = json.loads(
            (ROOT / "data/swing/strategy.json").read_text(encoding="utf-8"),
        )
        self.paths.strategy.write_text(json.dumps(strategy), encoding="utf-8")
        self.closed_dates = [
            f"2026-10-{day:02d}" for day in range(1, 8)
        ]
        self.paths.calendar.write_text(json.dumps({
            "schema_version": 1, "closed_dates": self.closed_dates,
        }), encoding="utf-8")
        self.metadata = EtfMetadataStore(self.paths.metadata).load()
        self.bars = retime_daily_bars(
            swing_strategy_bars(70), ending_on=date(2026, 8, 31),
        )
        DailyHistoryStore(
            self.paths.daily_history, self.metadata, frozenset(),
        ).upsert(self.bars)
        self.ledger = PortfolioLedger(
            self.paths.trades,
            self.metadata,
            clock=lambda: datetime(2026, 8, 31, 16, tzinfo=SHANGHAI),
            closed_dates=frozenset(),
        )
        self.ledger.initialize("test", 100_000.0, "init")

    def make_service(self, *, clock: datetime | None = None) -> SwingService:
        moment = clock or datetime(2026, 9, 1, 14, 0, tzinfo=SHANGHAI)
        return SwingService(
            self.paths,
            collector=None,
            intraday_provider=lambda: {"generated_at": moment.isoformat(), "items": []},
            intraday_points_provider=lambda _symbol: {"upserts": []},
            clock=lambda: moment,
            refresh_interval=60.0,
            event_limit=4,
        )

    def buy(self, *, price: float, shares: int = 100, when: datetime | None = None) -> None:
        executed_at = when or datetime(2026, 8, 31, 14, 30, tzinfo=SHANGHAI)
        self.ledger.record_trade(TradeInput(
            symbol="510300", side="BUY", shares=shares, price=price, fee=0.0,
            executed_at=executed_at,
            planned_risk_per_share=2.0,
        ), f"buy-{price}-{shares}-{executed_at.isoformat()}")

    def state_path(self) -> Path:
        return self.paths.strategy.with_name("v11_positions.json")

    def test_held_position_is_evaluated_and_its_state_is_persisted(self) -> None:
        entry = self.bars[-1].close
        self.buy(price=entry)
        service = self.make_service()
        snapshot = service.snapshot()
        v11 = snapshot["items"][0]["v11"]
        position = v11["position_decision"]
        self.assertEqual(position["status"], "AVAILABLE")
        self.assertEqual(position["position"]["shares"], 100)
        self.assertEqual(position["position"]["holding_session"], 1)
        self.assertFalse(position["position"]["reduced"])
        self.assertEqual(position["position"]["setup"], "NONE")
        self.assertIn(position["decision"]["state"], {"OBSERVE", "POSITION_ACTION"})
        self.assertFalse(position["decision"]["executable"])
        # The published decision for a held ETF is the position decision;
        # the entry evaluation remains available as evidence.
        self.assertEqual(v11["decision"], position["decision"])
        self.assertIn("entry_decision", v11)
        self.assertIn("tracking_ma", v11["evidence"])
        self.assertEqual(v11["position_state"]["entry_trading_date"], "2026-08-31")
        self.assertTrue(self.state_path().exists())
        stored = json.loads(self.state_path().read_text(encoding="utf-8"))
        self.assertEqual(stored["positions"]["510300"]["entry_trading_date"], "2026-08-31")
        self.assertGreater(stored["positions"]["510300"]["stop_price_raw"], 0.0)
        self.assertIn("v11_environment_history", snapshot)
        self.assertIn("position_action_counts", snapshot["v11_summary"])

    def test_position_state_survives_restart_and_clears_after_exit(self) -> None:
        self.buy(price=self.bars[-1].close)
        self.make_service().snapshot()
        restarted = self.make_service()
        self.assertIn("510300", restarted._v11_state["positions"])
        self.ledger.clock = lambda: datetime(2026, 9, 1, 16, tzinfo=SHANGHAI)
        self.ledger.record_trade(TradeInput(
            symbol="510300", side="SELL", shares=100, price=self.bars[-1].close, fee=0.0,
            executed_at=datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
        ), "flat")
        after = self.make_service(
            clock=datetime(2026, 9, 1, 16, 30, tzinfo=SHANGHAI),
        )
        snapshot = after.snapshot()
        self.assertIsNone(snapshot["items"][0]["v11"]["position_decision"])
        stored = json.loads(self.state_path().read_text(encoding="utf-8"))
        self.assertEqual(stored["positions"], {})

    def test_stop_exit_feeds_the_reentry_cooldown_into_the_entry_context(self) -> None:
        self.buy(price=100.0, when=datetime(2026, 8, 27, 10, tzinfo=SHANGHAI))
        self.ledger.record_trade(TradeInput(
            symbol="510300", side="SELL", shares=100, price=98.0, fee=0.0,
            executed_at=datetime(2026, 8, 28, 10, 0, tzinfo=SHANGHAI),
            exit_reason="STOP_EXIT",
        ), "stop")
        service = self.make_service()
        bars = retime_daily_bars(swing_strategy_bars(260), ending_on=date(2026, 8, 31))
        result = service._v11_snapshot("510300", bars, self.metadata["510300"], {})
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertIn("REENTRY_COOLDOWN", result["blocked_reasons"])
        # One session (8/31) elapsed out of the configured five.
        self.assertEqual(service._v11_remaining_sessions(date(2026, 8, 28), date(2026, 8, 31), 5), 4)
        self.assertEqual(service._v11_remaining_sessions(date(2026, 8, 28), date(2026, 9, 4), 5), 0)

    def test_calendar_evidence_flags_long_holidays_and_ex_dividend_sessions(self) -> None:
        service = self.make_service()
        self.assertEqual(service._v11_long_holiday_sessions_ahead(date(2026, 9, 30)), 1)
        self.assertEqual(service._v11_long_holiday_sessions_ahead(date(2026, 9, 29)), 2)
        self.assertIsNone(service._v11_long_holiday_sessions_ahead(date(2026, 9, 25)))
        self.assertIsNone(service._v11_long_holiday_sessions_ahead(date(2026, 9, 4)))
        metadata = self.metadata["510300"]
        self.assertTrue(service._v11_ex_dividend_window(metadata, date(2026, 8, 31)))
        self.assertTrue(service._v11_ex_dividend_window(metadata, date(2026, 9, 2)))
        self.assertFalse(service._v11_ex_dividend_window(metadata, date(2026, 9, 3)))
        self.assertFalse(service._v11_ex_dividend_window(None, date(2026, 9, 1)))
        # 9/28-9/30 plus 10/8-10/9; the National Day closure is skipped.
        self.assertEqual(service._v11_sessions_between(date(2026, 9, 28), date(2026, 10, 9)), 5)

    def test_ledger_summary_counts_trailing_losing_cycles(self) -> None:
        moments = [
            datetime(2026, 8, 20, 10, tzinfo=SHANGHAI),
            datetime(2026, 8, 21, 10, tzinfo=SHANGHAI),
            datetime(2026, 8, 24, 10, tzinfo=SHANGHAI),
            datetime(2026, 8, 25, 10, tzinfo=SHANGHAI),
            datetime(2026, 8, 26, 10, tzinfo=SHANGHAI),
            datetime(2026, 8, 27, 10, tzinfo=SHANGHAI),
        ]
        prices = [(100.0, 105.0), (100.0, 97.0), (100.0, 99.0)]
        for index, (buy_price, sell_price) in enumerate(prices):
            self.buy(price=buy_price, when=moments[index * 2])
            self.ledger.record_trade(TradeInput(
                symbol="510300", side="SELL", shares=100, price=sell_price, fee=0.0,
                executed_at=moments[index * 2 + 1],
            ), f"sell-{index}")
        service = self.make_service()
        summary = service._v11_ledger_summary()
        self.assertEqual(summary["consecutive_losses"], 2)
        self.assertEqual(summary["last_loss_date"], date(2026, 8, 27))
        self.assertEqual(summary["open_cycle_buys"], {})
        self.buy(price=100.0, when=datetime(2026, 8, 28, 10, tzinfo=SHANGHAI))
        self.buy(price=101.0, when=datetime(2026, 8, 31, 10, tzinfo=SHANGHAI))
        summary = self.make_service()._v11_ledger_summary()
        self.assertEqual(summary["open_cycle_buys"], {"510300": 2})
        self.assertEqual(summary["consecutive_losses"], 2)


if __name__ == "__main__":
    unittest.main()
