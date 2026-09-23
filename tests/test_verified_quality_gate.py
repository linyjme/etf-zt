from __future__ import annotations

from datetime import date, datetime, timedelta
from dataclasses import replace
import unittest

from etf_rotation.swing_quality import assess_verified_quality
from tests.swing_helpers import retime_daily_bars, swing_strategy_bars


SHANGHAI = timedelta(hours=8)


def _receipt(bars):
    return {
        "source": "Wind",
        "checked_at": "2026-09-23T15:20:00+08:00",
        "sample_start": bars[0].trading_date.isoformat(),
        "sample_end": bars[-1].trading_date.isoformat(),
        "crosscheck_status": "PASSED",
        "adjustment_status": "VERIFIED",
        "calculation_version": "research-v1",
        "warnings": [],
    }


class VerifiedQualityGateTests(unittest.TestCase):
    def setUp(self):
        bars = retime_daily_bars(swing_strategy_bars(250), ending_on=date(2026, 9, 22))
        bars = tuple(replace(bar, source="东方财富 kline (push2his.eastmoney.com)") for bar in bars)
        self.bars = bars
        self.kwargs = dict(
            today=datetime(2026, 9, 23, 14, 20, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Shanghai")),
            closed_dates=frozenset(),
            metadata_status="PASSED",
            environment_histories={"000300": bars, "000852": bars},
            receipt=_receipt(bars),
        )

    def test_complete_receipt_and_history_is_verified(self):
        result = assess_verified_quality(self.bars, **self.kwargs)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["last_completed_date"], "2026-09-22")
        self.assertEqual(result["amount_quality"], "PROVIDER_REPORTED")
        self.assertEqual(result["environment_history_status"], "PASSED")

    def test_each_missing_requirement_fails_closed(self):
        cases = {
            "DATA_QUALITY_INSUFFICIENT_BARS": dict(bars=self.bars[-249:]),
            "DATA_QUALITY_LATEST_DATE_NOT_CURRENT": dict(
                bars=retime_daily_bars(swing_strategy_bars(250), ending_on=date(2026, 9, 18)),
            ),
            "DATA_QUALITY_AMOUNT_NOT_PROVIDER_REPORTED": dict(
                receipt={**_receipt(self.bars), "amount_quality": "ESTIMATED"},
            ),
            "DATA_QUALITY_METADATA_INVALID": dict(metadata_status="FAILED"),
            "DATA_QUALITY_ENVIRONMENT_MISSING": dict(environment_histories={"000300": self.bars}),
            "DATA_QUALITY_RECEIPT_MISSING": dict(receipt=None),
        }
        for reason, overrides in cases.items():
            with self.subTest(reason=reason):
                kwargs = dict(self.kwargs)
                kwargs.update(overrides)
                result = assess_verified_quality(kwargs.pop("bars", self.bars), **kwargs)
                self.assertEqual(result["status"], "UNVERIFIED")
                self.assertIn(reason, result["reasons"])

    def test_weekend_and_closed_day_are_resolved_by_calendar(self):
        kwargs = dict(self.kwargs)
        kwargs["today"] = datetime(2026, 9, 24, 14, 20, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Shanghai"))
        kwargs["closed_dates"] = frozenset({date(2026, 9, 23)})
        result = assess_verified_quality(self.bars, **kwargs)
        self.assertEqual(result["status"], "VERIFIED")

    def test_receipt_warning_never_upgrades_to_verified(self):
        kwargs = dict(self.kwargs)
        kwargs["receipt"] = {**_receipt(self.bars), "warnings": ["CROSSCHECK_PENDING"]}
        result = assess_verified_quality(self.bars, **kwargs)
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertIn("DATA_QUALITY_RECEIPT_WARNINGS", result["reasons"])


if __name__ == "__main__":
    unittest.main()
