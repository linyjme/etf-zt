from __future__ import annotations

import json
from pathlib import Path
import unittest

from etf_rotation.swing_shadow import (
    ShadowConfig,
    ShadowContext,
    ShadowState,
    ShadowVariant,
    evaluate_hybrid_shadow,
    evaluate_shadow,
    load_shadow_config,
)
from tests.swing_helpers import swing_strategy_bars


class SwingShadowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_shadow_config(
            Path(__file__).parents[1] / "data/swing/shadow_strategy.json"
        )

    def test_v2a_uses_one_momentum_trigger_and_one_confirmation(self):
        decision = evaluate_shadow(
            swing_strategy_bars(140, pattern="pullback_reclaim"),
            variant=ShadowVariant.V2_A,
            config=self.config,
            context=ShadowContext(),
        )
        self.assertEqual(decision.strategy_version, "SWING_V2_SHADOW")
        self.assertFalse(decision.executable)
        self.assertLessEqual(decision.evidence["momentum_trigger_count"], 1)
        self.assertLessEqual(decision.evidence["confirmation_count"], 1)

    def test_v2a_does_not_require_macd_rsi_and_kdj_simultaneously(self):
        decision = evaluate_shadow(
            swing_strategy_bars(140, pattern="rising"),
            variant=ShadowVariant.V2_A,
            config=self.config,
            context=ShadowContext(),
        )
        self.assertIn(
            decision.state,
            {ShadowState.TECHNICAL_CANDIDATE, ShadowState.OBSERVE},
        )
        self.assertFalse(decision.evidence["all_three_indicators_required"])

    def test_v2a_requires_a_confirmed_opportunity_event(self):
        decision = evaluate_shadow(
            swing_strategy_bars(140, pattern="pullback_reclaim"),
            variant=ShadowVariant.V2_A,
            config=self.config,
            context=ShadowContext(
                opportunity_id="op-1",
                opportunity_status="PULLBACK_WATCH",
            ),
        )
        self.assertIn("OPPORTUNITY_NOT_CONFIRMED", decision.blocked_reasons)
        self.assertNotEqual(decision.state, ShadowState.TECHNICAL_CANDIDATE)

    def test_snapshot_only_and_unknown_quality_are_never_executable(self):
        decision = evaluate_shadow(
            swing_strategy_bars(140),
            variant=ShadowVariant.V2_A,
            config=self.config,
            context=ShadowContext(snapshot_only=True, data_quality="UNKNOWN"),
        )
        self.assertFalse(decision.executable)
        self.assertIn("SNAPSHOT_ONLY", decision.blocked_reasons)
        self.assertIn("DATA_QUALITY_UNKNOWN", decision.blocked_reasons)

    def test_hybrid_shadow_reports_multi_factor_evidence_without_becoming_executable(self):
        decision = evaluate_hybrid_shadow(
            swing_strategy_bars(140, pattern="pullback_reclaim"),
            context=ShadowContext(data_quality="VERIFIED"),
        )
        self.assertEqual(decision.strategy_version, "SWING_HYBRID_SHADOW")
        self.assertEqual(decision.variant, ShadowVariant.HYBRID)
        self.assertFalse(decision.executable)
        self.assertIn("momentum_score", decision.evidence)
        self.assertIn("weekly_context", decision.evidence)

    def test_hybrid_shadow_keeps_unknown_quality_blocked(self):
        decision = evaluate_hybrid_shadow(
            swing_strategy_bars(140),
            context=ShadowContext(data_quality="UNKNOWN"),
        )
        self.assertFalse(decision.executable)
        self.assertIn("DATA_QUALITY_UNKNOWN", decision.blocked_reasons)

    def test_shadow_config_rejects_unknown_keys(self):
        payload = {
            "schema_version": 1,
            "strategy_version": "SWING_V2_SHADOW",
            "event_window_sessions": 5,
            "rsi_lower": 45.0,
            "rsi_upper": 65.0,
            "kdj_j_max": 85.0,
            "atr_distance_max": 1.0,
            "macd_histogram_rising_days": 2,
            "min_walk_forward_bars": 630,
            "variants": ["V2_A", "V2_B", "V2_C"],
            "unexpected": True,
        }
        path = Path(__file__).with_name("_invalid_shadow_strategy.json")
        try:
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_shadow_config(path)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
