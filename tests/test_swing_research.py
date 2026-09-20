from __future__ import annotations

import unittest

from etf_rotation.swing_research import ResearchStatus, assess_history
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


if __name__ == "__main__":
    unittest.main()
