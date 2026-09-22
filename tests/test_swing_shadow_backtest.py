from __future__ import annotations

import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
from unittest.mock import patch

from etf_rotation.swing_shadow import ShadowVariant
from etf_rotation.swing_shadow_backtest import (
    ExecutionCosts,
    replay_all_variants,
    replay_variant,
)
from tests.swing_helpers import swing_strategy_bars
from scripts.run_swing_shadow import run_shadow_report, _v11_shadow_result
from etf_rotation.swing_v11 import load_v11_config


class SwingShadowBacktestTests(unittest.TestCase):
    def test_v11_report_supplies_ma20_slope_evidence(self):
        config = load_v11_config(Path(__file__).parents[1] / "data/swing/v11_strategy.json")
        result = _v11_shadow_result("510300", swing_strategy_bars(260), {
            "crosscheck_status": "PASSED", "amount_quality": "PROVIDER_REPORTED",
            "adjustment_status": "VERIFIED",
        }, config)
        self.assertIsNotNone(result["decision"]["evidence"].get("ma20_slope_pct_10d"))

    def test_shadow_candidate_can_enter_without_v1_technical_candidate(self):
        from types import SimpleNamespace
        from etf_rotation import swing_shadow_backtest as module
        from etf_rotation.swing_strategy import PortfolioContext, SwingState
        from etf_rotation.swing_shadow import ShadowState, load_shadow_config
        from etf_rotation.swing_config import load_strategy
        from etf_rotation.etf_metadata import EtfMetadataStore
        bars = swing_strategy_bars(140, pattern="rising", raw_scale=0.03)
        config = load_strategy(module.ROOT / "data/swing/strategy.json")
        context = PortfolioContext.empty(100000, next_trading_date=bars[-1].trading_date.replace(day=bars[-1].trading_date.day + 1))
        formal = module.evaluate_swing(bars, config, context)
        self.assertEqual(formal.planned_shares, 0)
        self.assertTrue(formal.evidence["entry_hard_gates_ok"])
        runner = module._ResearchRunner(config,
            EtfMetadataStore(module.ROOT / "data/monitor/etf_metadata.json").load()["510300"].trading,
            variant=ShadowVariant.V2_B, costs=ExecutionCosts(),
            shadow_config=load_shadow_config(module.ROOT / "data/swing/shadow_strategy.json"), prepared={})
        with patch.object(module, "_evaluate_shadow_context", return_value=SimpleNamespace(
                state=ShadowState.TECHNICAL_CANDIDATE, blocked_reasons=())):
            decision = runner._evaluate_signal(bars, context)
        self.assertEqual(decision.state, SwingState.TRIAL_ENTRY_CANDIDATE)
        self.assertGreater(decision.planned_shares, 0)

    def test_missing_metadata_fails_closed(self):
        result = replay_variant({"510300": swing_strategy_bars(756)},
            variant=ShadowVariant.V1, costs=ExecutionCosts(), trading_by_symbol={})
        self.assertEqual(result.validation_status, "BLOCKED_METADATA")
        self.assertFalse(result.trades)

    def test_v1_uses_formal_evaluator_and_folds_do_not_cross_their_end(self):
        from etf_rotation import swing_shadow_backtest as module
        bars = swing_strategy_bars(756, raw_scale=0.03)
        with patch.object(module, "evaluate_swing", wraps=module.evaluate_swing) as evaluator:
            result = replay_variant({"510300": bars}, variant=ShadowVariant.V1, costs=ExecutionCosts())
        self.assertTrue(evaluator.called)
        for fold in result.folds:
            for trade in fold["trades"]:
                self.assertGreaterEqual(trade["execution_date"], fold["test_start_date"])
                self.assertLessEqual(trade["execution_date"], fold["test_end_date"])
                self.assertLess(trade["signal_date"], trade["execution_date"])
                if trade["side"] == "BUY":
                    self.assertLessEqual(trade["shares"] * trade["execution_price"], 5000)
        self.assertFalse(result.performance_claim_allowed)

    def test_end_price_changes_cannot_change_first_test_window(self):
        bars = swing_strategy_bars(756, raw_scale=0.03)
        first = replay_variant({"510300": bars}, variant=ShadowVariant.V1, costs=ExecutionCosts())
        changed_list = list(bars)
        for index in range(630, len(changed_list)):
            bar = changed_list[index]
            scale = 1.001
            changed_list[index] = replace(bar, close=bar.close * scale,
                high=bar.high * scale, low=bar.low * scale,
                adjusted_close=bar.adjusted_close * scale,
                adjusted_high=bar.adjusted_high * scale,
                adjusted_low=bar.adjusted_low * scale,
                previous_close=changed_list[index - 1].close)
        changed = tuple(changed_list)
        second = replay_variant({"510300": changed}, variant=ShadowVariant.V1, costs=ExecutionCosts())
        self.assertEqual(first.folds[0], second.folds[0])

    def test_different_symbol_calendars_cannot_be_aligned_by_row_number(self):
        first = swing_strategy_bars(756)
        other = swing_strategy_bars(756, symbol="510500")[1:]
        result = replay_variant({"510300": first, "510500": other},
            variant=ShadowVariant.V1, costs=ExecutionCosts())
        self.assertEqual(len(result.folds), 1)

    def test_execution_costs_model_waives_minimum_commission(self):
        self.assertEqual(ExecutionCosts().minimum_fee, 0.0)

    def test_replay_exposes_fixed_walk_forward_folds_and_marked_metrics(self):
        bars = swing_strategy_bars(756)
        result = replay_variant(
            {"510300": bars}, variant=ShadowVariant.V1, costs=ExecutionCosts(),
        )
        self.assertEqual(len(result.folds), 2)
        self.assertEqual(result.folds[0]["train_bar_count"], 504)
        self.assertEqual(result.folds[0]["test_bar_count"], 126)
        self.assertEqual(
            result.folds[0]["test_start_date"], bars[504].trading_date.isoformat(),
        )
        for key in (
            "completed_round_trips", "uncompleted_leg_count", "fees",
            "spread_cost", "slippage", "average_holding_days", "baseline_policy",
        ):
            self.assertIn(key, result.metrics)

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
        self.assertIn("HYBRID", results)
        self.assertFalse(results["HYBRID"].performance_claim_allowed)

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
            self.assertIn("outcome", variants["SWING_V11_SHADOW"])
            self.assertIn("shadow_outcome_counts", report)


if __name__ == "__main__":
    unittest.main()
