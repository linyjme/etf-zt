from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_swing_shadow import run_shadow_report
from tests.swing_helpers import swing_strategy_bars


class SwingShadowReportTests(unittest.TestCase):
    def test_cutoff_is_applied_and_repeated_runs_do_not_overwrite(self):
        bars = swing_strategy_bars(140)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "history.jsonl"
            history.write_text("".join(json.dumps(b.to_dict()) + "\n" for b in bars), encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"schema_version": 1, "history_path": str(history),
                "items": [{"symbol": "510300", "data_version": "sha256:stale"}]}), encoding="utf-8")
            with patch("scripts.run_swing_shadow.replay_all_variants", return_value={}) as replay:
                first, report = run_shadow_report(manifest, output_root=root / "out", end_date=bars[100].trading_date.isoformat())
                self.assertEqual(len(replay.call_args.args[0]["510300"]), 101)
            before = first.read_bytes()
            with patch("scripts.run_swing_shadow.replay_all_variants", return_value={}):
                second, _ = run_shadow_report(manifest, output_root=root / "out", end_date=bars[100].trading_date.isoformat())
            self.assertNotEqual(first, second)
            self.assertEqual(before, first.read_bytes())
            self.assertIn("input_digest", report)

    def test_quality_warnings_block_performance_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "history.jsonl"
            history.write_text(
                "".join(json.dumps(bar.to_dict()) + "\n" for bar in swing_strategy_bars(700)),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "generated_at": "2026-09-20T00:00:00+08:00",
                "history_path": str(history),
                "items": [{
                    "symbol": "510300",
                    "research_status": "USABLE_WITH_WARNINGS",
                    "sample_class": "FULL_SAMPLE",
                    "crosscheck_status": "PENDING",
                    "adjustment_status": "REVIEW",
                    "data_version": "sha256:test",
                }],
            }), encoding="utf-8")
            output, report = run_shadow_report(
                manifest, history_path=history, output_root=root / "reports",
            )
            variant = report["items"][0]["variants"]["V2_A"]
            self.assertEqual(variant["validation_status"], "BLOCKED_DATA_QUALITY")
            self.assertFalse(variant["performance_claim_allowed"])
            self.assertEqual(report["validation"]["walk_forward"]["train_sessions"], 504)
            self.assertEqual(report["validation"]["walk_forward"]["test_sessions"], 126)
            self.assertTrue(output.exists())

    def test_malformed_symbol_history_isolated_in_report(self) -> None:
        bars = swing_strategy_bars(20)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "history.jsonl"
            malformed = json.dumps({"symbol": "510500", "trading_date": "bad"})
            history.write_text(
                "".join(json.dumps(bar.to_dict()) + "\n" for bar in bars)
                + malformed + "\n",
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "history_path": str(history),
                "items": [
                    {"symbol": "510300"},
                    {"symbol": "510500"},
                ],
            }), encoding="utf-8")
            with patch("scripts.run_swing_shadow.replay_all_variants", return_value={}):
                _, report = run_shadow_report(manifest, output_root=root / "out")
            item = next(value for value in report["items"] if value["symbol"] == "510500")
            self.assertTrue(item["invalid_history"])
            self.assertEqual(item["invalid_history_reason"], "INVALID_DAILY_HISTORY")
            self.assertEqual(
                item["variants"]["V2_A"]["validation_status"],
                "BLOCKED_INVALID_HISTORY",
            )
            self.assertEqual(len(report["invalid_history_lines"]), 1)


if __name__ == "__main__":
    unittest.main()
