from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock

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
        self.metadata_path = self.root / "metadata.json"
        self.metadata_path.write_text(
            json.dumps(metadata_fixture(), ensure_ascii=False), encoding="utf-8",
        )
        self.metadata = EtfMetadataStore(self.metadata_path).load()
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

    def test_t_plus_one_uses_authoritative_trading_days_not_calendar_days(self) -> None:
        friday = datetime(2026, 9, 4, 10, 0, tzinfo=SHANGHAI)
        saturday = friday + timedelta(days=1)
        monday = friday + timedelta(days=3)
        tuesday = friday + timedelta(days=4)
        ledger = PortfolioLedger(
            self.path,
            self.metadata,
            closed_dates={monday.date()},
            clock=lambda: tuesday,
        )
        ledger.initialize("波段账户", 100_000.0, "init-calendar")
        ledger.record_trade(
            TradeInput("510300", "BUY", 100, 4.0, 0.0, friday), "friday-buy",
        )
        self.assertEqual(
            ledger.project(saturday.date(), {}).positions["510300"].sellable_shares,
            0,
        )
        self.assertEqual(
            ledger.project(monday.date(), {}).positions["510300"].sellable_shares,
            0,
        )
        self.assertEqual(
            ledger.project(tuesday.date(), {}).positions["510300"].sellable_shares,
            100,
        )
        for index, closed_time in enumerate((saturday, monday)):
            with self.subTest(closed_time=closed_time):
                with self.assertRaisesRegex(PortfolioLedgerError, "trading day"):
                    ledger.record_trade(
                        TradeInput(
                            "510300", "SELL", 100, 4.1, 0.0, closed_time,
                        ),
                        f"closed-sell-{index}",
                    )
        ledger.record_trade(
            TradeInput("510300", "SELL", 100, 4.1, 0.0, tuesday),
            "tuesday-sell",
        )

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

    def test_idempotency_key_reuse_requires_identical_operation_and_payload(self) -> None:
        self.initialize()
        original = TradeInput("510300", "BUY", 1000, 4.60, 5.0, self.tuesday)
        bought = self.ledger.record_trade(original, "trade-key")
        with self.assertRaisesRegex(PortfolioLedgerError, "different request"):
            self.ledger.record_trade(
                TradeInput("510300", "BUY", 1000, 4.61, 5.0, self.tuesday),
                "trade-key",
            )
        with self.assertRaisesRegex(PortfolioLedgerError, "different request"):
            self.ledger.record_trade(
                TradeInput("510300", "BUY", 900, 4.60, 5.0, self.tuesday),
                "trade-key",
            )
        with self.assertRaisesRegex(PortfolioLedgerError, "different request"):
            self.ledger.record_trade(original, "init-1")
        reversed_event = self.ledger.reverse(bought.event_id, "reverse-key")
        other = self.ledger.record_trade(original, "other-trade")
        with self.assertRaisesRegex(PortfolioLedgerError, "different request"):
            self.ledger.reverse(other.event_id, "reverse-key")
        self.assertEqual(
            self.ledger.reverse(bought.event_id, "reverse-key").event_id,
            reversed_event.event_id,
        )

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

    def test_subprocess_duplicate_idempotency_uses_cross_process_lock(self) -> None:
        self.initialize()
        start_flag = self.root / "subprocess-start.flag"
        project_root = Path(__file__).resolve().parents[1]
        child = """
import sys, time
from datetime import datetime
from pathlib import Path
from etf_rotation.etf_metadata import EtfMetadataStore
from etf_rotation.swing_portfolio import PortfolioLedger, TradeInput
path, metadata_path, start = map(Path, sys.argv[1:])
while not start.exists():
    time.sleep(0.001)
ledger = PortfolioLedger(path, EtfMetadataStore(metadata_path).load())
trade = TradeInput('510300', 'BUY', 100, 4.0, 0.0, datetime.fromisoformat('2026-09-01T10:00:00+08:00'))
print(ledger.record_trade(trade, 'subprocess-same-key').event_id, flush=True)
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(project_root / "src")
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child,
                    str(self.path),
                    str(self.metadata_path),
                    str(start_flag),
                ],
                cwd=project_root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        start_flag.write_text("go", encoding="ascii")
        outputs = [process.communicate(timeout=15) for process in processes]
        for process, (_, stderr) in zip(processes, outputs, strict=True):
            self.assertEqual(process.returncode, 0, stderr)
        event_ids = {stdout.strip() for stdout, _ in outputs}
        self.assertEqual(len(event_ids), 1)
        self.assertEqual(len(self.ledger.load_events()), 2)

    def test_event_ids_are_canonical_lowercase_uuid4_and_not_idempotency_keys(self) -> None:
        event = self.ledger.initialize(
            "波段账户", cash=100_000.0, idempotency_key="CALLER-Key/opaque",
        )
        parsed = uuid.UUID(event.event_id)
        self.assertEqual(parsed.version, 4)
        self.assertEqual(str(parsed), event.event_id)
        self.assertNotEqual(event.event_id, event.idempotency_key)

    def test_event_payload_is_recursively_snapshotted_frozen_and_deep_copied(self) -> None:
        event = self.ledger.initialize(
            "波段账户",
            1000.0,
            "deep-payload",
            initial_positions={
                "510300": {"shares": 100, "average_cost": 4.0},
            },
        )
        exported = event.to_dict()
        exported["payload"]["initial_positions"]["510300"]["shares"] = 999
        self.assertEqual(
            event.payload["initial_positions"]["510300"]["shares"], 100,
        )
        with self.assertRaises(TypeError):
            event.payload["initial_positions"]["510300"]["shares"] = 999

    def test_initialize_retry_is_exact_and_changed_payload_conflicts(self) -> None:
        first = self.ledger.initialize("波段账户", 100_000.0, "init-exact")
        second = self.ledger.initialize("波段账户", 100_000.0, "init-exact")
        self.assertEqual(first.event_id, second.event_id)
        with self.assertRaisesRegex(PortfolioLedgerError, "different request"):
            self.ledger.initialize("波段账户", 99_999.0, "init-exact")

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

    def test_each_sell_consumes_lots_sellable_on_its_own_execution_date(self) -> None:
        self.initialize(cash=100_000.0)
        tuesday_buy = self.ledger.record_trade(
            TradeInput("510300", "BUY", 100, 4.0, 0.0, self.tuesday), "tue-buy",
        )
        self.ledger.record_trade(
            TradeInput("510300", "BUY", 100, 4.1, 0.0, self.wednesday), "wed-buy",
        )
        self.ledger.record_trade(
            TradeInput("510300", "SELL", 100, 4.2, 0.0, self.wednesday), "wed-sell",
        )
        thursday = self.wednesday + timedelta(days=1)
        self.ledger.record_trade(
            TradeInput("510300", "BUY", 100, 4.3, 0.0, thursday), "thu-buy",
        )
        with self.assertRaisesRegex(PortfolioLedgerError, "invalidates"):
            self.ledger.reverse(tuesday_buy.event_id, "reverse-tue-buy")
        projected = self.ledger.project(thursday.date(), {"510300": 4.3})
        self.assertEqual(projected.positions["510300"].shares, 200)

    def test_reversed_trade_does_not_block_corrected_earlier_execution(self) -> None:
        self.initialize()
        late = self.ledger.record_trade(
            TradeInput("510300", "BUY", 100, 4.0, 0.0, self.wednesday), "late-buy",
        )
        self.ledger.reverse(late.event_id, "reverse-late")
        corrected = self.ledger.record_trade(
            TradeInput("510300", "BUY", 100, 4.0, 0.0, self.tuesday),
            "corrected-buy",
        )
        self.assertEqual(corrected.event_type, PortfolioEventType.BUY_CONFIRMED)
        self.assertEqual(
            self.ledger.project(self.wednesday.date(), {}).positions["510300"].shares,
            100,
        )

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

    def test_newer_projection_with_missing_or_wrong_fields_is_rebuilt(self) -> None:
        self.initialize()
        projection_path = self.root / "portfolio.json"
        valid = self.ledger.project(self.wednesday.date(), {}).to_dict()
        corruptions = (
            lambda payload: payload.pop("cash"),
            lambda payload: payload.__setitem__("schema_version", True),
            lambda payload: payload.__setitem__("warnings", "not-a-list"),
        )
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt):
                payload = dict(valid)
                payload["as_of_trading_date"] = "2026-09-03"
                corrupt(payload)
                projection_path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8",
                )
                rebuilt = self.ledger.load_or_rebuild_projection(
                    projection_path, self.tuesday.date(), {},
                )
                self.assertEqual(
                    json.loads(projection_path.read_text(encoding="utf-8")),
                    rebuilt.to_dict(),
                )

    def test_newer_projection_with_non_finite_numbers_is_rebuilt(self) -> None:
        self.ledger.initialize(
            "波段账户",
            cash=100_000.0,
            idempotency_key="init-finite-projection",
            initial_positions={"510300": InitialPositionInput(100, 4.0)},
        )
        projection_path = self.root / "portfolio.json"
        valid = self.ledger.project(
            self.wednesday.date(), {"510300": 4.1},
        ).to_dict()
        corruptions = (
            lambda payload: payload.__setitem__("cash", float("nan")),
            lambda payload: payload["positions"]["510300"].__setitem__(
                "market_value", float("inf"),
            ),
        )
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt):
                payload = json.loads(json.dumps(valid))
                payload["as_of_trading_date"] = "2026-09-03"
                corrupt(payload)
                projection_path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8",
                )
                rebuilt = self.ledger.load_or_rebuild_projection(
                    projection_path,
                    self.tuesday.date(),
                    {"510300": 4.1},
                )
                self.assertEqual(
                    json.loads(projection_path.read_text(encoding="utf-8")),
                    rebuilt.to_dict(),
                )

    def test_newer_projection_with_malformed_positions_is_rebuilt(self) -> None:
        self.ledger.initialize(
            "波段账户",
            cash=100_000.0,
            idempotency_key="init-position-projection",
            initial_positions={"510300": InitialPositionInput(100, 4.0)},
        )
        projection_path = self.root / "portfolio.json"
        valid = self.ledger.project(
            self.wednesday.date(), {"510300": 4.1},
        ).to_dict()
        corruptions = (
            lambda payload: payload.__setitem__("positions", []),
            lambda payload: payload["positions"]["510300"].__setitem__(
                "sellable_shares", 101,
            ),
            lambda payload: payload.__setitem__("etf_market_value", 999.0),
        )
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt):
                payload = json.loads(json.dumps(valid))
                payload["as_of_trading_date"] = "2026-09-03"
                corrupt(payload)
                projection_path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8",
                )
                rebuilt = self.ledger.load_or_rebuild_projection(
                    projection_path,
                    self.tuesday.date(),
                    {"510300": 4.1},
                )
                self.assertEqual(
                    json.loads(projection_path.read_text(encoding="utf-8")),
                    rebuilt.to_dict(),
                )

    def test_projection_path_must_not_alias_authoritative_event_log(self) -> None:
        self.initialize()
        with self.assertRaisesRegex(PortfolioLedgerError, "alias"):
            self.ledger.load_or_rebuild_projection(
                self.path, self.wednesday.date(), {},
            )
        hardlink = self.root / "portfolio-hardlink.json"
        os.link(self.path, hardlink)
        with self.assertRaisesRegex(PortfolioLedgerError, "alias"):
            self.ledger.load_or_rebuild_projection(
                hardlink, self.wednesday.date(), {},
            )
        symlink = self.root / "portfolio-symlink.json"
        try:
            symlink.symlink_to(self.path)
        except OSError:
            pass
        else:
            with self.assertRaisesRegex(PortfolioLedgerError, "alias"):
                self.ledger.load_or_rebuild_projection(
                    symlink, self.wednesday.date(), {},
                )

    def test_projection_rebuild_holds_event_snapshot_until_atomic_write(self) -> None:
        self.initialize()
        projection_path = self.root / "portfolio.json"
        old_writer_entered = threading.Event()
        allow_old_writer = threading.Event()
        writer_lock_attempted = threading.Event()
        writer_appended = threading.Event()
        module = __import__(
            "etf_rotation.swing_portfolio", fromlist=["_atomic_replace_json"],
        )
        original_atomic_write = module._atomic_replace_json
        original_lock_enter = module._SiblingFileLock.__enter__
        event_lock_name = f".{self.path.name}.lock"

        def tracking_lock_enter(lock: object) -> object:
            if (
                lock.path.name == event_lock_name
                and not lock.shared
                and old_writer_entered.is_set()
            ):
                writer_lock_attempted.set()
            return original_lock_enter(lock)

        def blocking_atomic_write(path: Path, payload: object) -> None:
            if not old_writer_entered.is_set():
                old_writer_entered.set()
                if not allow_old_writer.wait(15):
                    raise AssertionError("timed out waiting to release old writer")
            original_atomic_write(path, payload)

        def rebuild_old() -> None:
            self.ledger.load_or_rebuild_projection(
                projection_path, self.tuesday.date(), {},
            )

        def append_and_rebuild_new() -> None:
            self.ledger.record_trade(
                TradeInput("510300", "BUY", 100, 4.0, 0.0, self.tuesday),
                "concurrent-new-trade",
            )
            writer_appended.set()
            self.ledger.load_or_rebuild_projection(
                projection_path, self.tuesday.date(), {},
            )

        with (
            mock.patch(
                "etf_rotation.swing_portfolio._atomic_replace_json",
                side_effect=blocking_atomic_write,
            ),
            mock.patch.object(
                module._SiblingFileLock, "__enter__", tracking_lock_enter,
            ),
        ):
            old = threading.Thread(target=rebuild_old)
            new = threading.Thread(target=append_and_rebuild_new)
            old.start()
            try:
                self.assertTrue(old_writer_entered.wait(5))
                new.start()
                self.assertTrue(writer_lock_attempted.wait(5))
                self.assertFalse(writer_appended.is_set())
            finally:
                allow_old_writer.set()
                old.join(5)
                if new.ident is not None:
                    new.join(5)
        self.assertFalse(old.is_alive())
        self.assertFalse(new.is_alive())
        self.assertTrue(writer_appended.is_set())
        on_disk = json.loads(projection_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["positions"]["510300"]["shares"], 100)

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

    def test_trade_log_rejects_invalid_requested_risk_even_with_valid_effective_risk(
        self,
    ) -> None:
        self.initialize()
        self.ledger.record_trade(
            TradeInput(
                "510300", "BUY", 100, 4.0, 0.0, self.tuesday,
                planned_risk_per_share=1.0,
            ),
            "risk-event",
        )
        canonical = self.path.read_bytes()
        for invalid in ("1.0", float("nan")):
            with self.subTest(invalid=invalid):
                lines = canonical.decode("utf-8").splitlines()
                event = json.loads(lines[1])
                event["payload"]["planned_risk_per_share"] = invalid
                lines[1] = json.dumps(
                    event,
                    ensure_ascii=False,
                    allow_nan=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                self.path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
                with self.assertRaises(PortfolioLedgerError):
                    self.ledger.load_events()
                self.path.write_bytes(canonical)

    def test_trade_log_rejects_explicit_requested_and_effective_risk_mismatch(
        self,
    ) -> None:
        self.initialize()
        self.ledger.record_trade(
            TradeInput(
                "510300", "BUY", 100, 4.0, 0.0, self.tuesday,
                planned_risk_per_share=1.0,
            ),
            "risk-event",
        )
        lines = self.path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[1])
        event["payload"]["effective_planned_risk_per_share"] = 2.0
        lines[1] = json.dumps(
            event,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
        with self.assertRaisesRegex(PortfolioLedgerError, "risk fields"):
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

    def test_projection_rejects_decimal_results_not_representable_as_float(self) -> None:
        self.initialize()
        self.ledger.record_trade(
            TradeInput("510300", "BUY", 100, 4.0, 0.0, self.tuesday),
            "finite-buy",
        )
        with self.assertRaisesRegex(PortfolioLedgerError, "finite float"):
            self.ledger.project(self.tuesday.date(), {"510300": 1e308})

    def test_default_trade_risk_accumulates_into_portfolio_warning(self) -> None:
        self.ledger.initialize(
            "波段账户", 100_000.0, "init-risk", default_risk_per_trade=0.0075,
        )
        for index, symbol in enumerate(("510300", "510500", "563360")):
            self.ledger.record_trade(
                TradeInput(symbol, "BUY", 100, 4.0, 0.0, self.tuesday),
                f"default-risk-{index}",
            )
        projected = self.ledger.project(self.tuesday.date(), {})
        self.assertAlmostEqual(projected.planned_risk, 2250.0)
        self.assertIn("RISK_LIMIT_EXCEEDED", projected.warnings)

    def test_single_trade_risk_above_account_default_warns_below_portfolio_cap(
        self,
    ) -> None:
        self.initialize()
        self.ledger.record_trade(
            TradeInput(
                "510300", "BUY", 1000, 4.0, 0.0, self.tuesday,
                planned_risk_per_share=1.0,
            ),
            "single-risk-over-default",
        )
        projected = self.ledger.project(self.tuesday.date(), {})
        self.assertAlmostEqual(projected.planned_risk, 1000.0)
        self.assertLess(projected.planned_risk, projected.equity * 0.02)
        self.assertIn("RISK_LIMIT_EXCEEDED", projected.warnings)

    def test_sell_fee_cannot_make_unleveraged_cash_negative(self) -> None:
        self.ledger.initialize(
            "零现金持仓", 0.0, "init-position",
            initial_positions={"510300": {"shares": 100, "average_cost": 1.0}},
        )
        with self.assertRaisesRegex(PortfolioLedgerError, "negative cash"):
            self.ledger.record_trade(
                TradeInput("510300", "SELL", 100, 1.0, 101.0, self.tuesday),
                "fee-over-proceeds",
            )


if __name__ == "__main__":
    unittest.main()
