from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import importlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from etf_rotation.etf_metadata import EtfMetadata, IndexMetadata, TradingMetadata
from etf_rotation.market_data import MarketDataValidator
from etf_rotation.t_monitor import JsonQuoteAdapter, MarketDataError


TRADING = TradingMetadata("SSE", "DOMESTIC_EQUITY_ETF", False, 1, 100, 0.001, 0.10, 100)
METADATA = {
    symbol: EtfMetadata(symbol, "Synthetic ETF", IndexMetadata("000300", "Synthetic index", "TEST"), TRADING)
    for symbol in ("510300", "510500")
}


def minute(stamp: str = "11:20", price: float = 4.639) -> dict:
    return {"timestamp": f"2026-09-03T{stamp}:00+08:00", "price": price,
            "average_price": price, "open": price, "high": price, "low": price,
            "volume": 100.0, "amount": price * 10000.0}


def rejected_minute() -> dict:
    return {"timestamp": "2026-09-03T11:21:00+08:00", "price": 4.640,
            "average_price": 4.640, "open": 4.639, "high": 4.640, "low": 4.639,
            "volume": 8138.0, "amount": 3773740.0}


def batch(points: list[dict] | None = None, *, observed_at: str = "2026-09-03T11:24:00+08:00") -> dict:
    points = [minute(), rejected_minute(), minute("11:22", 4.641)] if points is None else points
    latest = points[-1]
    return {"schema_version": 2, "source": {"name": "TEST trends2"},
            "observed_at": observed_at, "collected_at": observed_at,
            "quotes": [{"schema_version": 2, "symbol": "510300", "name": "Synthetic ETF",
                        "source": "TEST trends2", "observed_at": observed_at,
                        "collected_at": observed_at, "previous_close": 4.62,
                        "timestamp": latest["timestamp"], "price": latest["price"],
                        "average_price": latest["average_price"], "points": points}]}


def quality_module():
    if importlib.util.find_spec("etf_rotation.quote_quality") is None:
        raise AssertionError("The quote quarantine module is missing")
    return importlib.import_module("etf_rotation.quote_quality")


class PrepareQuoteBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = quality_module()

    def test_source_failure_fixture_still_fails_the_unchanged_validator(self) -> None:
        quote = JsonQuoteAdapter().parse(batch())["510300"]
        with self.assertRaisesRegex(MarketDataError, "量价校验失败"):
            MarketDataValidator(TRADING).validate_point(quote.points[1], quote.previous_close)

    def test_only_bad_completed_point_is_removed_and_other_symbol_is_unchanged(self) -> None:
        payload = batch()
        second = deepcopy(batch([minute("11:22", 4.642)])["quotes"][0])
        second["symbol"] = "510500"
        payload["quotes"].append(second)
        before = deepcopy(payload)
        clean, quotes, issues = self.api.prepare_quote_batch(payload, METADATA)
        self.assertEqual(payload, before)
        self.assertEqual(clean["quotes"][0]["points"], [before["quotes"][0]["points"][0], before["quotes"][0]["points"][2]])
        self.assertEqual(clean["quotes"][1]["points"], second["points"])
        self.assertEqual(quotes["510300"].price, 4.641)
        self.assertEqual(quotes["510500"].price, 4.642)
        self.assertEqual(len(issues), 1)
        self.assertEqual(clean["validation_issues"], issues)

    def test_last_bad_minute_recalculates_quote_latest_fields(self) -> None:
        clean, quotes, _ = self.api.prepare_quote_batch(batch([minute(), rejected_minute()]), METADATA)
        row = clean["quotes"][0]
        self.assertEqual((row["timestamp"], row["price"], row["average_price"]), (minute()["timestamp"], 4.639, 4.639))
        self.assertEqual(quotes["510300"].timestamp.isoformat(), minute()["timestamp"])

    def test_all_bad_points_remove_only_affected_quote(self) -> None:
        payload = batch([rejected_minute()])
        second = batch([minute("11:22")])["quotes"][0]
        second["symbol"] = "510500"
        payload["quotes"].append(second)
        clean, quotes, issues = self.api.prepare_quote_batch(payload, METADATA)
        self.assertEqual(list(quotes), ["510500"])
        self.assertEqual([row["symbol"] for row in clean["quotes"]], ["510500"])
        self.assertEqual(issues[0]["symbol"], "510300")

    def test_entirely_bad_batch_has_parseable_empty_quotes(self) -> None:
        clean, quotes, issues = self.api.prepare_quote_batch(batch([rejected_minute()]), METADATA)
        self.assertEqual(clean["quotes"], [])
        self.assertEqual(dict(quotes), {})
        self.assertEqual(dict(JsonQuoteAdapter().parse(clean)), {})
        self.assertEqual(len(issues), 1)

    def test_unfinished_point_is_retained_without_faking_its_values(self) -> None:
        payload = batch([minute(), rejected_minute()], observed_at="2026-09-03T11:21:59+08:00")
        clean, quotes, issues = self.api.prepare_quote_batch(payload, METADATA)
        self.assertEqual(clean, payload)
        self.assertEqual(quotes["510300"].points[-1].amount, 3773740.0)
        self.assertEqual(issues, [])

    def test_exact_completion_boundary_enables_validation(self) -> None:
        payload = batch([minute(), rejected_minute()], observed_at="2026-09-03T11:22:00+08:00")
        _, quotes, issues = self.api.prepare_quote_batch(payload, METADATA)
        self.assertEqual(len(quotes["510300"].points), 1)
        self.assertEqual(len(issues), 1)

    def test_clean_payload_preserves_original_structure_and_is_deep_copied(self) -> None:
        payload = batch([minute()])
        payload["extra"] = {"test": [1]}
        clean, _, issues = self.api.prepare_quote_batch(payload, METADATA)
        self.assertEqual(clean, payload)
        self.assertEqual(issues, [])
        clean["extra"]["test"].append(2)
        self.assertEqual(payload["extra"]["test"], [1])

    def test_issue_preserves_normalized_source_evidence(self) -> None:
        _, _, issues = self.api.prepare_quote_batch(batch(), METADATA)
        self.assertEqual(issues, [{"schema_version": 1, "symbol": "510300",
                                  "timestamp": rejected_minute()["timestamp"], "trading_date": "2026-09-03",
                                  "observed_at": "2026-09-03T11:24:00+08:00", "source": "TEST trends2",
                                  "reason": "量价校验失败", "previous_close": 4.62,
                                  "volume_unit_shares": 100, "point": rejected_minute()}])

    def test_missing_metadata_and_malformed_quotes_still_raise(self) -> None:
        bad = batch()
        bad["quotes"][0]["points"][0]["amount"] = "invalid"
        for payload, metadata in ((batch(), {}), (bad, METADATA), ([], METADATA)):
            with self.subTest(payload_type=type(payload).__name__), self.assertRaises(MarketDataError):
                self.api.prepare_quote_batch(payload, metadata)

    def test_existing_issues_survive_reload_and_reader_needs_no_metadata(self) -> None:
        clean, _, issues = self.api.prepare_quote_batch(batch(), METADATA)
        restored, quotes, restored_issues = self.api.prepare_quote_batch(clean, METADATA)
        self.assertEqual(restored_issues, issues)
        self.assertEqual(restored["validation_issues"], issues)
        self.assertEqual(len(quotes["510300"].points), 2)
        self.assertEqual(self.api.read_validation_issues(clean), issues)
        restored_issues[0]["point"]["amount"] = 1.0
        self.assertEqual(clean["validation_issues"][0]["point"]["amount"], 3773740.0)
        self.assertEqual(self.api.read_validation_issues({}), [])

    def test_repeated_bad_batch_keeps_first_evidence_and_latest_observation(self) -> None:
        _, _, original = self.api.prepare_quote_batch(batch(), METADATA)
        later = batch(observed_at="2026-09-03T11:26:00+08:00")
        later["validation_issues"] = deepcopy(original)
        clean, _, issues = self.api.prepare_quote_batch(later, METADATA)
        expected = deepcopy(original)
        expected[0]["last_observed_at"] = "2026-09-03T11:26:00+08:00"
        self.assertEqual(issues, expected)
        self.assertEqual(clean["validation_issues"], expected)
        self.assertEqual(original[0]["observed_at"], "2026-09-03T11:24:00+08:00")
        self.assertNotIn("last_observed_at", original[0])

    def test_persisted_latest_observation_never_moves_backwards(self) -> None:
        _, _, original = self.api.prepare_quote_batch(batch(), METADATA)
        original[0]["last_observed_at"] = "2026-09-03T11:27:00+08:00"
        payload = batch(observed_at="2026-09-03T11:25:00+08:00")
        payload["validation_issues"] = original
        _, _, issues = self.api.prepare_quote_batch(payload, METADATA)
        self.assertEqual(issues, original)

    def test_optional_latest_observation_is_strictly_validated(self) -> None:
        _, _, original = self.api.prepare_quote_batch(batch(), METADATA)
        for stamp in (None, True, "", "bad", "2026-09-03T11:25:00", "2026-09-03T11:23:00+08:00"):
            invalid = deepcopy(original)
            invalid[0]["last_observed_at"] = stamp
            with self.subTest(stamp=stamp), self.assertRaises(MarketDataError):
                self.api.read_validation_issues({"validation_issues": invalid})
        for stamp in (original[0]["observed_at"], "2026-09-03T03:25:00+00:00"):
            valid = deepcopy(original)
            valid[0]["last_observed_at"] = stamp
            with self.subTest(stamp=stamp):
                self.assertEqual(self.api.read_validation_issues({"validation_issues": valid}), valid)

    def test_invalid_persisted_issue_shape_is_not_trusted(self) -> None:
        _, _, valid = self.api.prepare_quote_batch(batch(), METADATA)
        invalid = [None, {}, "issues", [{}]]
        for key, value in (("schema_version", True), ("symbol", "５１０３００"),
                           ("timestamp", "2026-09-03T11:21:00"), ("trading_date", "2026-09-02"),
                           ("observed_at", "bad"), ("source", ""), ("reason", ""),
                           ("volume_unit_shares", True), ("previous_close", float("nan")),
                           ("point", {})):
            row = deepcopy(valid[0]); row[key] = value
            invalid.append([row])
        missing = deepcopy(valid[0]); del missing["symbol"]
        invalid.append([missing])
        mismatched = deepcopy(valid[0]); mismatched["point"]["timestamp"] = minute()["timestamp"]
        invalid.append([mismatched])
        for value in invalid:
            payload = batch([minute()]); payload["validation_issues"] = value
            with self.subTest(value=value), self.assertRaises(MarketDataError):
                self.api.prepare_quote_batch(payload, METADATA)

    def test_nullable_ohlc_failure_can_be_preserved_without_inventing_price(self) -> None:
        raw = minute(); raw["open"] = None
        clean, _, issues = self.api.prepare_quote_batch(batch([raw]), METADATA)
        self.assertEqual(clean["quotes"], [])
        self.assertIsNone(issues[0]["point"]["open"])
        self.assertEqual(self.api.read_validation_issues(clean), issues)

    def test_issue_reader_rejects_unencodable_text(self) -> None:
        _, _, issues = self.api.prepare_quote_batch(batch(), METADATA)
        issues[0]["reason"] = "\ud800"
        with self.assertRaises(MarketDataError):
            self.api.read_validation_issues({"validation_issues": issues})

    def test_finite_outlier_keeps_full_source_value_in_quarantine(self) -> None:
        raw = minute()
        raw.update(price=1.5e308, high=1.5e308)
        clean, _, issues = self.api.prepare_quote_batch(batch([raw]), METADATA)
        self.assertEqual(clean["quotes"], [])
        self.assertEqual(issues[0]["point"]["price"], 1.5e308)

    def test_impossible_completion_at_datetime_limit_is_a_validation_error(self) -> None:
        _, _, issues = self.api.prepare_quote_batch(batch(), METADATA)
        stamp = "9999-12-31T23:59:00+08:00"
        issues[0].update(timestamp=stamp, observed_at=stamp, trading_date="9999-12-31")
        issues[0]["point"]["timestamp"] = stamp
        with self.assertRaises(MarketDataError):
            self.api.read_validation_issues({"validation_issues": issues})

    def test_legacy_symbol_map_is_unchanged_if_clean_and_canonical_when_isolated(self) -> None:
        raw = batch([minute()])["quotes"][0]
        del raw["symbol"]
        clean, _, issues = self.api.prepare_quote_batch({"510300": raw}, METADATA)
        self.assertEqual(clean, {"510300": raw})
        self.assertEqual(issues, [])
        raw = batch()["quotes"][0]
        del raw["symbol"]
        clean, quotes, issues = self.api.prepare_quote_batch({"510300": raw}, METADATA)
        self.assertNotIn("510300", clean)
        self.assertEqual(len(quotes["510300"].points), 2)
        self.assertEqual(len(issues), 1)


class MinuteQuarantineStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = quality_module()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "nested" / "quarantine.jsonl"
        self.store = self.api.MinuteQuarantineStore(self.path)
        _, _, self.issues = self.api.prepare_quote_batch(batch(), METADATA)

    def test_absent_read_and_empty_append_never_create_file(self) -> None:
        self.assertEqual(self.store.read(), [])
        self.assertEqual(self.store.append([]), 0)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_utf8_jsonl_roundtrip_flushes_and_fsyncs(self) -> None:
        with patch("etf_rotation.quote_quality.os.fsync", wraps=__import__("os").fsync) as sync:
            self.assertEqual(self.store.append(self.issues), 1)
        sync.assert_called()
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("量价校验失败", text)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual([json.loads(row) for row in text.splitlines()], self.issues)
        self.assertEqual(self.store.read(), self.issues)

    def test_content_dedup_preserves_first_evidence_and_advances_latest_watermark(self) -> None:
        later = deepcopy(self.issues)
        later[0]["observed_at"] = "2026-09-03T11:25:00+08:00"
        self.assertEqual(self.store.append(self.issues + later), 1)
        before = self.path.read_bytes()
        self.assertEqual(self.api.MinuteQuarantineStore(self.path).append(later), 0)
        self.assertEqual(self.path.read_bytes(), before)
        expected = deepcopy(self.issues)
        expected[0]["last_observed_at"] = later[0]["observed_at"]
        self.assertEqual(self.store.read(), expected)

    def test_later_recurrence_updates_one_row_with_zero_added_count(self) -> None:
        self.assertEqual(self.store.append(self.issues), 1)
        later = deepcopy(self.issues)
        later[0]["observed_at"] = "2026-09-03T11:26:00+08:00"
        self.assertEqual(self.api.MinuteQuarantineStore(self.path).append(later), 0)
        expected = deepcopy(self.issues)
        expected[0]["last_observed_at"] = later[0]["observed_at"]
        self.assertEqual(self.store.read(), expected)
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)
        before = self.path.read_bytes()
        self.assertEqual(self.store.append(self.issues), 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_watermark_only_update_is_atomic_on_replace_failure(self) -> None:
        self.store.append(self.issues)
        before = self.path.read_bytes()
        later = deepcopy(self.issues)
        later[0]["observed_at"] = "2026-09-03T11:26:00+08:00"
        with patch("etf_rotation.quote_quality.os.replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaises(MarketDataError):
                self.store.append(later)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_concurrent_watermark_updates_keep_maximum_without_adding_rows(self) -> None:
        self.store.append(self.issues)
        batches = []
        for minute in (28, 25, 27, 26):
            later = deepcopy(self.issues)
            later[0]["observed_at"] = f"2026-09-03T11:{minute}:00+08:00"
            batches.append(later)
        with ThreadPoolExecutor(max_workers=4) as pool:
            added = list(pool.map(lambda rows: self.api.MinuteQuarantineStore(self.path).append(rows), batches))
        self.assertEqual(sum(added), 0)
        expected = deepcopy(self.issues)
        expected[0]["last_observed_at"] = "2026-09-03T11:28:00+08:00"
        self.assertEqual(self.store.read(), expected)

    def test_different_bad_content_at_same_minute_is_another_evidence_record(self) -> None:
        self.store.append(self.issues)
        changed = deepcopy(self.issues)
        changed[0]["point"]["amount"] += 1
        self.assertEqual(self.store.append(changed), 1)
        self.assertEqual(self.store.read(), self.issues + changed)

    def test_read_returns_detached_records(self) -> None:
        self.store.append(self.issues)
        records = self.store.read(); records[0]["point"]["amount"] = 0
        self.assertEqual(self.store.read(), self.issues)

    def test_corrupt_existing_journal_fails_closed_without_overwrite(self) -> None:
        self.path.parent.mkdir()
        for content in ('{bad json\n', '{}\n', '\n', '{"schema_version":NaN}\n'):
            self.path.write_text(content, encoding="utf-8")
            before = self.path.read_bytes()
            with self.subTest(content=content):
                with self.assertRaises(MarketDataError):
                    self.store.read()
                with self.assertRaises(MarketDataError):
                    self.store.append(self.issues)
                self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_new_issue_is_rejected_before_any_write(self) -> None:
        with self.assertRaises(MarketDataError):
            self.store.append([{}])
        self.assertFalse(self.path.exists())

    def test_persistence_failure_is_raised(self) -> None:
        with patch("etf_rotation.quote_quality.os.fsync", side_effect=OSError("synthetic write failure")):
            with self.assertRaises((MarketDataError, OSError)):
                self.store.append(self.issues)

    def test_replace_failure_preserves_journal_and_removes_temporary_file(self) -> None:
        self.store.append(self.issues)
        before = self.path.read_bytes()
        changed = deepcopy(self.issues); changed[0]["point"]["amount"] += 1
        with patch("etf_rotation.quote_quality.os.replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaises(MarketDataError):
                self.store.append(changed)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_concurrent_store_instances_append_one_first_observation(self) -> None:
        stores = [self.api.MinuteQuarantineStore(self.path) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda store: store.append(self.issues), stores))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.store.read(), self.issues)

    def test_duplicate_json_keys_are_rejected_in_existing_evidence(self) -> None:
        self.path.parent.mkdir()
        raw = json.dumps(self.issues[0]).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
        self.path.write_text(raw + "\n", encoding="utf-8")
        with self.assertRaises(MarketDataError):
            self.store.read()


if __name__ == "__main__":
    unittest.main()
