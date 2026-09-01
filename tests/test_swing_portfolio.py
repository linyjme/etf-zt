from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from etf_rotation.etf_metadata import EtfMetadataStore
from etf_rotation.swing_portfolio import (
    InitialPositionInput,
    PortfolioEventType,
    PortfolioLedger,
    PortfolioLedgerError,
    TradeInput,
)
from tests.swing_helpers import metadata_fixture


SHANGHAI = timezone(timedelta(hours=8))


class SwingPortfolioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        metadata_path = self.root / "metadata.json"
        metadata_path.write_text(
            json.dumps(metadata_fixture(), ensure_ascii=False), encoding="utf-8",
        )
        self.metadata = EtfMetadataStore(metadata_path).load()
        self.path = self.root / "trades.jsonl"
        self.tuesday = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)
        self.wednesday = datetime(2026, 9, 2, 10, 0, tzinfo=SHANGHAI)
        self.clock_time = datetime(2026, 9, 2, 15, 30, tzinfo=SHANGHAI)
        self.ledger = PortfolioLedger(
            self.path, self.metadata, clock=lambda: self.clock_time,
        )

    def initialize(self, cash: float = 100_000.0) -> None:
        self.ledger.initialize("波段账户", cash=cash, idempotency_key="init-1")

    def test_buy_rollover_sell_and_rebuild_are_exact(self) -> None:
        self.initialize()
        self.ledger.record_trade(
            TradeInput("510300", "BUY", 1000, 4.60, 5.0, self.tuesday),
            idempotency_key="buy-1",
        )
        same_day = self.ledger.project(self.tuesday.date(), {"510300": 4.60})
        self.assertEqual(same_day.positions["510300"].sellable_shares, 0)
        self.assertEqual(same_day.positions["510300"].today_bought_shares, 1000)
        next_day = self.ledger.project(self.wednesday.date(), {"510300": 4.70})
        self.assertEqual(next_day.positions["510300"].sellable_shares, 1000)
        self.ledger.record_trade(
            TradeInput("510300", "SELL", 500, 4.80, 5.0, self.wednesday),
            idempotency_key="sell-1",
        )
        rebuilt = PortfolioLedger(self.path, self.metadata).project(
            self.wednesday.date(), {"510300": 4.80},
        )
        self.assertEqual(rebuilt.positions["510300"].shares, 500)
        self.assertEqual(rebuilt.positions["510300"].sellable_shares, 500)
        self.assertAlmostEqual(rebuilt.positions["510300"].average_cost, 4.605)
        self.assertAlmostEqual(rebuilt.cash, 97_790.0)
        self.assertAlmostEqual(rebuilt.realized_pnl, 92.5)
        self.assertAlmostEqual(rebuilt.equity, 100_190.0)

    def test_weighted_average_cost_includes_buy_fees(self) -> None:
        self.initialize()
        self.ledger.record_trade(
            TradeInput("510300", "BUY", 1000, 4.0, 5.0, self.tuesday), "buy-1",
        )
        self.ledger.record_trade(
            TradeInput("510300", "BUY", 1000, 5.0, 5.0, self.wednesday), "buy-2",
        )
        position = self.ledger.project(date(2026, 9, 2), {}).positions["510300"]
        self.assertAlmostEqual(position.average_cost, 4.505)

    def test_initial_holdings_are_sellable_assets_independent_of_cash(self) -> None:
        event = self.ledger.initialize(
            "既有组合",
            cash=1_000.0,
            idempotency_key="init-existing",
            initial_positions={
                "510300": InitialPositionInput(150, 10.0),
                "159915": {"shares": 200, "average_cost": 2.5},
            },
            default_risk_per_trade=0.01,
        )
        projected = self.ledger.project(
            self.tuesday.date(), {"510300": 11.0, "159915": 3.0},
        )
        self.assertEqual(projected.cash, 1_000.0)
        self.assertEqual(projected.positions["510300"].shares, 150)
        self.assertEqual(projected.positions["510300"].sellable_shares, 150)
        self.assertEqual(projected.positions["510300"].today_bought_shares, 0)
        self.assertEqual(projected.default_risk_per_trade, 0.01)
        self.assertAlmostEqual(projected.etf_market_value, 2_250.0)
        self.assertAlmostEqual(projected.equity, 3_250.0)
        self.assertEqual(event.payload["default_risk_per_trade"], 0.01)

        rebuilt = PortfolioLedger(self.path, self.metadata).project(
            self.wednesday.date(), {"510300": 11.0, "159915": 3.0},
        )
        self.assertEqual(rebuilt.to_dict(), projected.to_dict() | {
            "as_of_trading_date": self.wednesday.date().isoformat(),
        })

    def test_zero_cash_is_valid_for_a_fully_invested_existing_account(self) -> None:
        self.ledger.initialize(
            "满仓账户",
            cash=0.0,
            idempotency_key="init-zero-cash",
            initial_positions={"510300": {"shares": 100, "average_cost": 4.0}},
        )
        projected = self.ledger.project(
            self.tuesday.date(), {"510300": 4.5},
        )
        self.assertEqual(projected.cash, 0.0)
        self.assertEqual(projected.equity, 450.0)

    def test_initial_holdings_and_default_risk_are_strictly_validated(self) -> None:
        invalid_cases = (
            ({"999999": {"shares": 100, "average_cost": 4.0}}, 0.0075),
            ({"510300": {"shares": -1, "average_cost": 4.0}}, 0.0075),
            ({"510300": {"shares": 100, "average_cost": 0.0}}, 0.0075),
            ({"510300": {"shares": 100, "average_cost": 4.0}}, 0.0),
            ({"510300": {"shares": 100, "average_cost": 4.0}}, float("inf")),
        )
        for index, (positions, risk_rate) in enumerate(invalid_cases):
            with self.subTest(index=index):
                ledger = PortfolioLedger(self.root / f"invalid-{index}.jsonl", self.metadata)
                with self.assertRaises(PortfolioLedgerError):
                    ledger.initialize(
                        "波段账户", 1000.0, f"invalid-{index}",
                        initial_positions=positions,
                        default_risk_per_trade=risk_rate,
                    )
                self.assertFalse(ledger.path.exists())

    def test_duplicate_idempotency_key_is_not_appended_twice(self) -> None:
        self.initialize()
        trade = TradeInput("510300", "BUY", 1000, 4.60, 5.0, self.tuesday)
        first = self.ledger.record_trade(trade, idempotency_key="trade-1")
        second = self.ledger.record_trade(trade, idempotency_key="trade-1")
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(len(self.ledger.load_events()), 2)

    def test_concurrent_duplicate_idempotency_is_atomic(self) -> None:
        self.initialize()
        trade = TradeInput("510300", "BUY", 1000, 4.60, 5.0, self.tuesday)

        def append(_: int) -> str:
            ledger = PortfolioLedger(self.path, self.metadata)
            return ledger.record_trade(trade, "concurrent-trade").event_id

        with ThreadPoolExecutor(max_workers=8) as executor:
            event_ids = tuple(executor.map(append, range(24)))
        self.assertEqual(len(set(event_ids)), 1)
        self.assertEqual(len(self.ledger.load_events()), 2)

    def test_concurrent_distinct_appends_do_not_lose_events(self) -> None:
        self.initialize()

        def append(index: int) -> str:
            ledger = PortfolioLedger(self.path, self.metadata)
            return ledger.record_trade(
                TradeInput("510300", "BUY", 100, 4.0, 0.0, self.tuesday),
                f"distinct-{index}",
            ).event_id

        with ThreadPoolExecutor(max_workers=8) as executor:
            event_ids = tuple(executor.map(append, range(8)))
        self.assertEqual(len(set(event_ids)), 8)
        self.assertEqual(len(self.ledger.load_events()), 9)
        self.assertEqual(
            self.ledger.project(self.tuesday.date(), {}).positions["510300"].shares,
            800,
        )

    def test_event_ids_are_canonical_lowercase_uuid4_and_not_idempotency_keys(self) -> None:
        event = self.ledger.initialize(
            "波段账户", cash=100_000.0, idempotency_key="CALLER-Key/opaque",
        )
        parsed = uuid.UUID(event.event_id)
        self.assertEqual(parsed.version, 4)
        self.assertEqual(str(parsed), event.event_id)
        self.assertNotEqual(event.event_id, event.idempotency_key)

    def test_reversal_restores_projection_and_cannot_be_reversed_twice(self) -> None:
        self.initialize()
        bought = self.ledger.record_trade(
            TradeInput("510300", "BUY", 1000, 4.60, 5.0, self.tuesday), "buy-1",
        )
        reversal = self.ledger.reverse(bought.event_id, "reverse-1")
        projected = self.ledger.project(self.wednesday.date(), {})
        self.assertEqual(projected.cash, 100_000.0)
        self.assertNotIn("510300", projected.positions)
        self.assertEqual(reversal.event_type, PortfolioEventType.TRADE_REVERSED)
        with self.assertRaisesRegex(PortfolioLedgerError, "already reversed"):
            self.ledger.reverse(bought.event_id, "reverse-2")

    def test_reversal_requires_canonical_uuid4_trade_target(self) -> None:
        self.initialize()
        invalid_ids = (
            "not-a-uuid",
            str(uuid.uuid1()),
            str(uuid.uuid4()).upper(),
            self.ledger.load_events()[0].event_id,
        )
        for index, event_id in enumerate(invalid_ids):
            with self.subTest(event_id=event_id):
                with self.assertRaises(PortfolioLedgerError):
                    self.ledger.reverse(event_id, f"reverse-{index}")

    def test_reversal_rejects_removing_trade_needed_by_later_sale(self) -> None:
        self.initialize()
        bought = self.ledger.record_trade(
            TradeInput("510300", "BUY", 1000, 4.60, 5.0, self.tuesday), "buy-1",
        )
        self.ledger.record_trade(
            TradeInput("510300", "SELL", 500, 4.80, 5.0, self.wednesday), "sell-1",
        )
        with self.assertRaisesRegex(PortfolioLedgerError, "invalidates"):
            self.ledger.reverse(bought.event_id, "reverse-buy")

    def test_missing_or_corrupt_projection_is_rebuilt_from_events(self) -> None:
        self.initialize()
        self.ledger.record_trade(
            TradeInput("510300", "BUY", 1000, 4.60, 5.0, self.tuesday), "buy-1",
        )
        projection_path = self.root / "portfolio.json"
        projection_path.write_text("broken", encoding="utf-8")
        rebuilt = self.ledger.load_or_rebuild_projection(
            projection_path, self.wednesday.date(), {"510300": 4.80},
        )
        self.assertEqual(rebuilt.positions["510300"].shares, 1000)
        on_disk = json.loads(projection_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["positions"]["510300"]["shares"], 1000)

    def test_corrupt_event_log_is_never_silently_recovered(self) -> None:
        self.path.write_text("{broken\n", encoding="utf-8")
        with self.assertRaisesRegex(PortfolioLedgerError, "event log"):
            self.ledger.load_or_rebuild_projection(
                self.root / "portfolio.json", self.wednesday.date(), {},
            )
        self.assertFalse((self.root / "portfolio.json").exists())

    def test_noncanonical_or_semantically_invalid_event_log_is_rejected(self) -> None:
        self.initialize(cash=1_000.0)
        canonical = self.path.read_bytes()
        self.path.write_bytes(canonical.rstrip(b"\n"))
        with self.assertRaisesRegex(PortfolioLedgerError, "incomplete"):
            self.ledger.load_events()

        self.path.write_bytes(canonical)
        event = self.ledger.record_trade(
            TradeInput("510300", "BUY", 100, 4.0, 0.0, self.tuesday), "buy-1",
        )
        lines = self.path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[1])
        self.assertEqual(payload["event_id"], event.event_id)
        payload["payload"]["price"] = 100.0
        lines[1] = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        self.path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
        with self.assertRaisesRegex(PortfolioLedgerError, "available cash"):
            self.ledger.load_events()

    def test_rejects_buy_beyond_cash_sell_beyond_sellable_and_same_day_sell(self) -> None:
        self.initialize(cash=5_000.0)
        with self.assertRaisesRegex(PortfolioLedgerError, "cash"):
            self.ledger.record_trade(
                TradeInput("510300", "BUY", 1100, 4.60, 5.0, self.tuesday),
                "too-expensive",
            )
        self.ledger.record_trade(
            TradeInput("510300", "BUY", 1000, 4.60, 5.0, self.tuesday), "buy-1",
        )
        with self.assertRaisesRegex(PortfolioLedgerError, "sellable"):
            self.ledger.record_trade(
                TradeInput("510300", "SELL", 100, 4.80, 5.0, self.tuesday),
                "same-day-sell",
            )
        with self.assertRaisesRegex(PortfolioLedgerError, "sellable"):
            self.ledger.record_trade(
                TradeInput("510300", "SELL", 1100, 4.80, 5.0, self.wednesday),
                "too-many",
            )

    def test_rejects_invalid_trade_account_and_idempotency_inputs(self) -> None:
        bad_trades = (
            TradeInput("999999", "BUY", 100, 4.0, 0.0, self.tuesday),
            TradeInput("510300", "BUY", 99, 4.0, 0.0, self.tuesday),
            TradeInput("510300", "HOLD", 100, 4.0, 0.0, self.tuesday),
        )
        self.initialize()
        for index, trade in enumerate(bad_trades):
            with self.subTest(trade=trade):
                with self.assertRaises(PortfolioLedgerError):
                    self.ledger.record_trade(trade, f"bad-{index}")
        with self.assertRaises(PortfolioLedgerError):
            self.ledger.initialize("second", 1.0, "init-2")
        with self.assertRaises(PortfolioLedgerError):
            self.ledger.record_trade(
                TradeInput("510300", "BUY", 100, 4.0, 0.0, self.tuesday), " ",
            )

    def test_strategy_risk_warning_does_not_reject_legal_trade(self) -> None:
        self.initialize()
        event = self.ledger.record_trade(
            TradeInput(
                "510300", "BUY", 1000, 4.60, 5.0, self.tuesday,
                planned_risk_per_share=3.0,
            ),
            "risk-over-limit",
        )
        self.assertEqual(event.event_type, PortfolioEventType.BUY_CONFIRMED)
        projected = self.ledger.project(self.tuesday.date(), {"510300": 4.60})
        self.assertIn("RISK_LIMIT_EXCEEDED", projected.warnings)
        self.assertAlmostEqual(projected.planned_risk, 3000.0)


if __name__ == "__main__":
    unittest.main()
