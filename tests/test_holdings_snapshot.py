from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import patch

from etf_rotation.holdings_snapshot import (
    HoldingsSnapshotError,
    import_holdings_snapshot,
    read_holdings_snapshot,
    validate_snapshot,
)


def snapshot_fixture() -> dict:
    """Synthetic account; never put a user's portfolio in test fixtures."""
    return {
        "schema_version": 1,
        "snapshot_id": "synthetic-account-1",
        "recorded_at": "2026-09-03T12:00:00+08:00",
        "reporting_date": "2026-09-03",
        "source": "synthetic user report",
        "positions_as_of": "2026-09-03T11:00:00+08:00",
        "account_as_of": None,
        "notes": ["Report and account timestamps may differ."],
        "account": {
            "reported_total_assets": 20000.0,
            "reported_securities_value": 14500.0,
            "available_cash": 5000.0,
            "other_assets": 500.0,
            "original_capital": 22000.0,
            "additional_loss_budget": 1000.0,
        },
        "positions": [
            {
                "symbol": "510300", "name": "Synthetic ETF",
                "asset_type": "ETF", "management_mode": "OBSERVE",
                "shares": 1000, "sellable_shares": 1000,
                "average_cost": 4.123, "reported_market_value": 4000.0,
                "reported_holding_pnl": -123.0,
                "entry_date": None, "stop_loss": None,
            },
            {
                "symbol": "600000", "name": "Synthetic stock",
                "asset_type": "STOCK", "management_mode": "LONG_TERM_ONLY",
                "shares": 100, "sellable_shares": None,
                "average_cost": 9.99, "reported_market_value": None,
                "reported_holding_pnl": None,
                "entry_date": None, "stop_loss": None,
            },
        ],
    }


class HoldingsSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "holdings_snapshot.json"
        self.metadata = {"510300": object()}

    def test_missing_is_absent_not_an_empty_strategy_account(self):
        view = read_holdings_snapshot(self.path, self.metadata)
        self.assertEqual(view["status"], "ABSENT")
        self.assertIsNone(view["snapshot"])
        self.assertFalse(view["strategy_ready"])

    def test_snapshot_preserves_unknowns_and_rounded_costs(self):
        result = import_holdings_snapshot(self.path, snapshot_fixture(), self.metadata)
        self.assertEqual(result["status"], "SNAPSHOT_ONLY")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["strategy_ready"])
        self.assertIsNone(result["snapshot"]["positions"][1]["reported_market_value"])
        self.assertIsNone(result["snapshot"]["positions"][1]["sellable_shares"])
        self.assertIsNone(result["snapshot"]["positions"][0]["entry_date"])
        self.assertIsNone(result["snapshot"]["positions"][0]["stop_loss"])
        self.assertEqual(result["snapshot"]["positions"][0]["average_cost"], 4.123)
        self.assertEqual(result["snapshot"]["account"]["available_cash"], 5000)
        self.assertFalse((self.path.parent / "trades.jsonl").exists())

    def test_identical_import_is_idempotent_but_different_import_does_not_overwrite(self):
        payload = snapshot_fixture()
        first = import_holdings_snapshot(self.path, payload, self.metadata)
        before = self.path.read_bytes()
        self.assertEqual(import_holdings_snapshot(self.path, payload, self.metadata), first)
        changed = copy.deepcopy(payload)
        changed["positions"][0]["shares"] = 1200
        changed["positions"][0]["sellable_shares"] = 1200
        with self.assertRaises(HoldingsSnapshotError):
            import_holdings_snapshot(self.path, changed, self.metadata)
        self.assertEqual(self.path.read_bytes(), before)

    def test_copies_cannot_change_saved_report(self):
        original = snapshot_fixture()
        result = import_holdings_snapshot(self.path, original, self.metadata)
        original["positions"][0]["shares"] = 1
        result["snapshot"]["positions"][0]["shares"] = 2
        self.assertEqual(read_holdings_snapshot(self.path, self.metadata)["snapshot"]["positions"][0]["shares"], 1000)

    def test_inconsistent_account_totals_rejected_without_adjusting_cash(self):
        payload = snapshot_fixture()
        payload["account"]["reported_total_assets"] += 5
        with self.assertRaises(HoldingsSnapshotError):
            validate_snapshot(payload, self.metadata)

    def test_incomplete_position_valuations_do_not_need_to_equal_account_total(self):
        payload = snapshot_fixture()
        self.assertEqual(validate_snapshot(payload, self.metadata), payload)

    def test_rejects_duplicates_unknown_etfs_and_non_ascii_security_codes(self):
        cases = []
        duplicate = snapshot_fixture()
        duplicate["positions"].append(copy.deepcopy(duplicate["positions"][0]))
        cases.append(duplicate)
        for symbol in ("510999", "５１０３００", "H30533"):
            payload = snapshot_fixture()
            payload["positions"][0]["symbol"] = symbol
            cases.append(payload)
        for payload in cases:
            with self.subTest(payload=payload["positions"][0]["symbol"]):
                with self.assertRaises(HoldingsSnapshotError):
                    validate_snapshot(payload, self.metadata)

    def test_rejects_invalid_position_numbers_and_invented_strategy_fields(self):
        cases = [("shares", True), ("shares", 1.5), ("shares", 0),
                 ("sellable_shares", 1001), ("sellable_shares", -1),
                 ("average_cost", float("nan")), ("average_cost", 0),
                 ("reported_market_value", float("inf")),
                 ("stop_loss", 3.9), ("entry_date", "2026-09-03")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                payload = snapshot_fixture()
                payload["positions"][0][key] = value
                with self.assertRaises(HoldingsSnapshotError):
                    validate_snapshot(payload, self.metadata)

    def test_stock_cannot_become_a_strategy_instrument(self):
        payload = snapshot_fixture()
        payload["positions"][1]["management_mode"] = "OBSERVE"
        with self.assertRaises(HoldingsSnapshotError):
            validate_snapshot(payload, self.metadata)

    def test_bad_timestamp_and_extra_fields_are_rejected(self):
        for key, value in (("recorded_at", "2026-09-03T12:00:00"),
                           ("reporting_date", "yesterday"),
                           ("positions_as_of", "2026-09-04T12:00:00+08:00"),
                           ("api_key", "not-a-real-key")):
            payload = snapshot_fixture()
            payload[key] = value
            with self.assertRaises(HoldingsSnapshotError):
                validate_snapshot(payload, self.metadata)

    def test_invalid_file_is_fail_closed_and_does_not_echo_private_text(self):
        self.path.write_text('{"private": "SENSITIVE_MARKER"}', encoding="utf-8")
        view = read_holdings_snapshot(self.path, self.metadata)
        self.assertEqual(view["status"], "INVALID")
        self.assertIsNone(view["snapshot"])
        self.assertNotIn("SENSITIVE_MARKER", json.dumps(view))
        with self.assertRaises(HoldingsSnapshotError):
            import_holdings_snapshot(self.path, snapshot_fixture(), self.metadata)

    def test_duplicate_json_keys_are_invalid(self):
        raw = json.dumps(snapshot_fixture()).replace('"schema_version": 1', '"schema_version": 0, "schema_version": 1')
        self.path.write_text(raw, encoding="utf-8")
        self.assertEqual(read_holdings_snapshot(self.path, self.metadata)["status"], "INVALID")

    def test_rejects_extreme_integer_with_domain_error(self):
        payload = snapshot_fixture()
        payload["account"]["available_cash"] = 10 ** 400
        with self.assertRaises(HoldingsSnapshotError):
            validate_snapshot(payload, self.metadata)

    def test_invalid_dates_do_not_leak_inputs_through_exception_chain(self):
        for field in ("recorded_at", "reporting_date", "positions_as_of"):
            payload = snapshot_fixture()
            payload[field] = "PRIVATE_TEST_MARKER"
            try:
                validate_snapshot(payload, self.metadata)
                self.fail("invalid date accepted")
            except HoldingsSnapshotError as error:
                self.assertNotIn("PRIVATE_TEST_MARKER", "".join(traceback.format_exception(error)))

    def test_unencodable_text_is_rejected_by_reader_and_import(self):
        payload = snapshot_fixture()
        payload["notes"] = ["synthetic-\ud800"]
        with self.assertRaises(HoldingsSnapshotError):
            validate_snapshot(payload, self.metadata)
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(read_holdings_snapshot(self.path, self.metadata)["status"], "INVALID")

    def test_asset_type_container_is_a_domain_error(self):
        payload = snapshot_fixture()
        payload["positions"][0]["asset_type"] = []
        with self.assertRaises(HoldingsSnapshotError):
            validate_snapshot(payload, self.metadata)

    def test_concurrent_identical_imports_produce_one_report(self):
        payload = snapshot_fixture()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                lambda _: import_holdings_snapshot(self.path, payload, self.metadata),
                range(8),
            ))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(read_holdings_snapshot(self.path, self.metadata)["snapshot"], payload)

    def test_failed_replace_leaves_no_partial_report(self):
        with patch("etf_rotation.holdings_snapshot.os.replace", side_effect=OSError("disk failed")):
            with self.assertRaises(OSError):
                import_holdings_snapshot(self.path, snapshot_fixture(), self.metadata)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob(".holdings-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
