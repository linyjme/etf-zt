from __future__ import annotations

import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from etf_rotation.swing_shadow import ShadowVariant
from etf_rotation.swing_shadow_backtest import (
    ExecutionCosts,
    replay_all_variants,
    replay_variant,
)
from tests.swing_helpers import swing_strategy_bars
from scripts.run_swing_shadow import run_shadow_report


class SwingShadowBacktestTests(unittest.TestCase):
    def test_shadow_replay_uses_next_trading_day_execution(self):
        result = replay_variant(
            {"510300": swing_strategy_bars(700)},
            variant=ShadowVariant.V2_A,
            costs=ExecutionCosts(),
        )
        self.assertTrue(all(
            trade.execution_date > trade.signal_date for trade in result.trades
        ))

    def test_all_variants_use_identical_cost_assumptions(self):
        results = replay_all_variants(
            {"510300": swing_strategy_bars(700)},
            costs=ExecutionCosts(),
        )
        self.assertEqual(
            {result.execution_assumptions for result in results.values()},
            {results["V1"].execution_assumptions},
        )

    def test_short_history_is_inconclusive_not_profitable(self):
        result = replay_variant(
            {"510300": swing_strategy_bars(257)},
            variant=ShadowVariant.V2_A,
            costs=ExecutionCosts(),
        )
        self.assertEqual(result.validation_status, "INSUFFICIENT_SAMPLE")
        self.assertFalse(result.performance_claim_allowed)

    def test_replay_rejects_symbol_key_mismatch(self):
        with self.assertRaises(ValueError):
            replay_variant(
                {"510500": swing_strategy_bars(700, symbol="510300")},
                variant=ShadowVariant.V2_A,
                costs=ExecutionCosts(),
            )

    def test_shadow_report_keeps_v11_shadow_only_when_data_quality_gate_is_missing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "daily_quotes.jsonl"
            history.write_text(
                "".join(
                    json.dumps(bar.to_dict(), ensure_ascii=False) + "\n"
                    for bar in swing_strategy_bars(260)
                ),
                encoding="utf-8",
            )
            manifest = root / "research_manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "generated_at": "2026-09-21T09:00:00+08:00",
                "history_path": str(history),
                "items": [{
                    "symbol": "510300",
                    "research_status": "USABLE_WITH_WARNINGS",
                    "sample_class": "FULL_SAMPLE",
                    "walk_forward_eligible": False,
                    "data_version": "sha256:test",
                    "crosscheck_status": "PENDING",
                    "adjustment_status": "REVIEW",
                    "amount_quality": "ESTIMATED",
                }],
            }), encoding="utf-8")
            _, report = run_shadow_report(
                manifest, output_root=root / "reports",
                strategy_versions=("SWING_V1", "SWING_V2_SHADOW", "SWING_V11_SHADOW"),
            )
            self.assertFalse(report["performance_claim_allowed"])
            self.assertIn("DATA_QUALITY", report["blocking_reasons"])
            variants = report["items"][0]["variants"]
            self.assertIn("SWING_V11_SHADOW", variants)
            self.assertFalse(variants["SWING_V11_SHADOW"]["performance_claim_allowed"])


if __name__ == "__main__":
    unittest.main()

