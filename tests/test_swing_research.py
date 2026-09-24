from __future__ import annotations

import unittest
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from etf_rotation.swing_research import ResearchStatus, assess_history
from scripts.build_swing_research_manifest import build_manifest
from tests.swing_helpers import swing_strategy_bars
from tests.swing_helpers import metadata_fixture


class ResearchAssessmentTests(unittest.TestCase):
    def test_history_with_unique_completed_bars_is_usable_with_warnings(self):
        result = assess_history(
            swing_strategy_bars(130),
            crosscheck_status="PENDING",
            adjustment_status="UNKNOWN",
            amount_quality="ESTIMATED",
        )
        self.assertIs(result.status, ResearchStatus.USABLE_WITH_WARNINGS)
        self.assertEqual(result.bar_count, 130)
        self.assertEqual(result.duplicate_dates, ())
        self.assertTrue(result.data_version.startswith("sha256:"))
        self.assertIn("CROSSCHECK_PENDING", result.warnings)

    def test_duplicate_date_is_excluded_and_is_reported(self):
        bars = list(swing_strategy_bars(130))
        bars[1] = bars[0]
        result = assess_history(
            bars,
            crosscheck_status="PASSED",
            adjustment_status="VERIFIED",
            amount_quality="PROVIDER_REPORTED",
        )
        self.assertIs(result.status, ResearchStatus.EXCLUDED)
        self.assertTrue(result.duplicate_dates)
        self.assertIn("DUPLICATE_TRADING_DATE", result.warnings)
        self.assertFalse(result.walk_forward_eligible)

    def test_short_history_is_not_a_walk_forward_sample(self):
        result = assess_history(
            swing_strategy_bars(257),
            crosscheck_status="PASSED",
            adjustment_status="VERIFIED",
            amount_quality="PROVIDER_REPORTED",
        )
        self.assertIs(result.status, ResearchStatus.SHORT_SAMPLE)
        self.assertFalse(result.walk_forward_eligible)

    def test_non_completed_bar_is_excluded(self):
        bars = list(swing_strategy_bars(130))
        result = assess_history(
            (*bars[:-1], replace(bars[-1], is_final=False)),
            crosscheck_status="PASSED",
            adjustment_status="VERIFIED",
            amount_quality="PROVIDER_REPORTED",
        )
        self.assertIs(result.status, ResearchStatus.EXCLUDED)
        self.assertIn("INVALID_DAILY_BAR", result.warnings)

    def test_manifest_refuses_to_overwrite_runtime_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "daily_quotes.jsonl"
            history.write_text("", encoding="utf-8")
            watchlist = root / "watchlist.json"
            watchlist.write_text(
                json.dumps({"schema_version": 1, "items": []}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                build_manifest(history, watchlist, output_path=history)

    def test_manifest_isolates_malformed_symbol_and_validates_metadata(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "daily_quotes.jsonl"
            valid = swing_strategy_bars(70, symbol="510300")
            history.write_text(
                "".join(
                    json.dumps(bar.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
                    for bar in valid
                ) + json.dumps({"symbol": "159915", "close": -1}) + "\n",
                encoding="utf-8",
            )
            watchlist = root / "watchlist.json"
            watchlist.write_text(
                json.dumps({
                    "schema_version": 1,
                    "items": [
                        {"symbol": "510300", "enabled": True},
                        {"symbol": "159915", "enabled": True},
                    ],
                }),
                encoding="utf-8",
            )
            metadata = root / "metadata.json"
            metadata.write_text(json.dumps(metadata_fixture(("510300",))), encoding="utf-8")
            manifest = build_manifest(
                history,
                watchlist,
                metadata_path=metadata,
            )
            items = {item["symbol"]: item for item in manifest["items"]}
            self.assertEqual(items["510300"]["metadata_status"], "VERIFIED")
            self.assertEqual(items["510300"]["sample_class"], "SHORT_SAMPLE")
            self.assertEqual(items["159915"]["research_status"], ResearchStatus.EXCLUDED.value)
            self.assertIn("NO_COMPLETED_BARS", items["159915"]["warnings"])
            self.assertEqual(manifest["invalid_history_records"], {"159915": 1})

    def test_manifest_covers_enabled_symbols_without_mutating_runtime_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "daily_quotes.jsonl"
            history.write_text(
                "".join(
                    json.dumps(bar.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
                    for bar in swing_strategy_bars(70)
                ),
                encoding="utf-8",
            )
            original = history.read_bytes()
            watchlist = root / "watchlist.json"
            watchlist.write_text(
                json.dumps({
                    "schema_version": 1,
                    "items": [
                        {"symbol": "510300", "enabled": True},
                        {"symbol": "159915", "enabled": True},
                    ],
                }),
                encoding="utf-8",
            )
            output = root / "research_manifest.json"

            manifest = build_manifest(
                history,
                watchlist,
                output_path=output,
                generated_at="2026-09-20T09:00:00+08:00",
            )

            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["generated_at"], "2026-09-20T09:00:00+08:00")
            self.assertTrue(manifest["runtime_history_unchanged"])
            items = {item["symbol"]: item for item in manifest["items"]}
            self.assertEqual(items["510300"]["bar_count"], 70)
            self.assertEqual(
                items["510300"]["research_status"],
                ResearchStatus.USABLE_WITH_WARNINGS.value,
            )
            self.assertEqual(
                items["159915"]["research_status"],
                ResearchStatus.EXCLUDED.value,
            )
            self.assertIn("NO_COMPLETED_BARS", items["159915"]["warnings"])
            self.assertEqual(history.read_bytes(), original)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), manifest)


    def test_manifest_applies_only_a_receipt_that_matches_the_current_history(self):
        from datetime import datetime, timedelta, timezone

        from etf_rotation.swing_crosscheck import (
            IndependentBar, crosscheck_history, write_receipts,
        )

        bars = tuple(
            replace(bar, source="东方财富 kline (push2his.eastmoney.com)")
            for bar in swing_strategy_bars(70)
        )
        checked_at = datetime(2026, 9, 23, 17, 0, tzinfo=timezone(timedelta(hours=8)))
        receipt = crosscheck_history(
            bars,
            tuple(
                IndependentBar(
                    trading_date=bar.trading_date, open=bar.open, high=bar.high,
                    low=bar.low, close=bar.close, volume=bar.volume,
                    adjusted_close=bar.adjusted_close,
                )
                for bar in bars
            ),
            source="腾讯 fqkline 独立交叉核验 (web.ifzq.gtimg.cn)",
            checked_at=checked_at, price_tick=0.001,
        )
        self.assertEqual(receipt.crosscheck_status, "PASSED")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "daily_quotes.jsonl"
            watchlist = root / "watchlist.json"
            watchlist.write_text(json.dumps({
                "schema_version": 1, "items": [{"symbol": "510300", "enabled": True}],
            }), encoding="utf-8")
            output = root / "research_manifest.json"
            write_receipts(root / "crosscheck_receipts.json", (receipt,), generated_at=checked_at)

            def write_history(records):
                history.write_text(
                    "".join(
                        json.dumps(bar.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
                        for bar in records
                    ),
                    encoding="utf-8",
                )

            write_history(bars)
            manifest = build_manifest(history, watchlist, output_path=output)
            item = manifest["items"][0]
            self.assertEqual(item["crosscheck_status"], "PASSED")
            self.assertEqual(item["adjustment_status"], "VERIFIED")
            self.assertEqual(item["amount_quality"], "PROVIDER_REPORTED")
            self.assertEqual(item["warnings"], [])
            self.assertEqual(item["research_status"], ResearchStatus.SHORT_SAMPLE.value)
            self.assertEqual(item["crosscheck_receipt"]["mismatch_count"], 0)
            self.assertEqual(item["crosscheck_receipt"]["checked_at"], receipt.checked_at)

            # One more completed bar changes the digest: the old receipt no
            # longer applies and the manifest falls back to PENDING.
            write_history(bars + swing_strategy_bars(71)[-1:])
            stale = build_manifest(history, watchlist, output_path=output)["items"][0]
            self.assertEqual(stale["crosscheck_status"], "PENDING")
            self.assertIn("CROSSCHECK_PENDING", stale["warnings"])
            self.assertIsNone(stale["crosscheck_receipt"])

            # An explicit receipts path that does not exist is simply no receipt.
            write_history(bars)
            missing = build_manifest(
                history, watchlist, output_path=output,
                receipts_path=root / "nowhere.json",
            )["items"][0]
            self.assertEqual(missing["crosscheck_status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
