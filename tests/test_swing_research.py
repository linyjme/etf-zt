from __future__ import annotations

import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from etf_rotation.swing_research import ResearchStatus, assess_history
from scripts.build_swing_research_manifest import build_manifest
from tests.swing_helpers import swing_strategy_bars


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


if __name__ == "__main__":
    unittest.main()
