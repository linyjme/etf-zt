from __future__ import annotations

import unittest

from etf_rotation.swing_shadow import ShadowVariant
from etf_rotation.swing_shadow_backtest import (
    ExecutionCosts,
    replay_all_variants,
    replay_variant,
)
from tests.swing_helpers import swing_strategy_bars


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


if __name__ == "__main__":
    unittest.main()

