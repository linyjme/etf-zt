from __future__ import annotations

from datetime import date, datetime, timedelta
from dataclasses import replace
import hashlib
import json
import unittest
from zoneinfo import ZoneInfo

from etf_rotation.swing_quality import assess_verified_quality
from tests.swing_helpers import retime_daily_bars, swing_strategy_bars


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _digest(bars):
    payload = [bar.to_dict() for bar in bars]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _receipt(bars):
    return {
        "source": "Wind",
        "checked_at": "2026-09-23T15:20:00+08:00",
        "sample_start": bars[0].trading_date.isoformat(),
        "sample_end": bars[-1].trading_date.isoformat(),
        "crosscheck_status": "PASSED",
        "adjustment_status": "VERIFIED",
        "calculation_version": _digest(bars),
        "warnings": [],
    }


class VerifiedQualityGateTests(unittest.TestCase):
    def setUp(self):
        bars = retime_daily_bars(swing_strategy_bars(250), ending_on=date(2026, 9, 22))
        bars = tuple(replace(bar, source="东方财富 kline (push2his.eastmoney.com)") for bar in bars)
        self.bars = bars
        self.kwargs = dict(
            today=datetime(2026, 9, 23, 14, 20, tzinfo=SHANGHAI),
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

    def test_receipt_for_other_bars_cannot_vouch_for_the_turnover_basis(self):
        unknown = tuple(replace(bar, source="unknown feed") for bar in self.bars)
        matching = {**_receipt(unknown), "amount_quality": "PROVIDER_REPORTED"}
        result = assess_verified_quality(unknown, **{**self.kwargs, "receipt": matching})
        self.assertEqual(result["amount_quality"], "PROVIDER_REPORTED")
        self.assertEqual(result["status"], "VERIFIED")

        stale = {**_receipt(self.bars), "amount_quality": "PROVIDER_REPORTED"}
        result = assess_verified_quality(unknown, **{**self.kwargs, "receipt": stale})
        self.assertEqual(result["amount_quality"], "UNKNOWN")
        self.assertIn("DATA_QUALITY_AMOUNT_NOT_PROVIDER_REPORTED", result["reasons"])
        self.assertIn("DATA_QUALITY_RECEIPT_SAMPLE_MISMATCH", result["reasons"])

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
        kwargs["today"] = datetime(2026, 9, 24, 14, 20, tzinfo=SHANGHAI)
        kwargs["closed_dates"] = frozenset({date(2026, 9, 23)})
        result = assess_verified_quality(self.bars, **kwargs)
        self.assertEqual(result["status"], "VERIFIED")

    def test_receipt_warning_never_upgrades_to_verified(self):
        kwargs = dict(self.kwargs)
        kwargs["receipt"] = {**_receipt(self.bars), "warnings": ["CROSSCHECK_PENDING"]}
        result = assess_verified_quality(self.bars, **kwargs)
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertIn("DATA_QUALITY_RECEIPT_WARNINGS", result["reasons"])

    def test_environment_history_must_be_long_and_current(self):
        cases = {
            "DATA_QUALITY_ENVIRONMENT_INSUFFICIENT_BARS": {
                "000300": self.bars[-1:], "000852": self.bars,
            },
            "DATA_QUALITY_ENVIRONMENT_LATEST_DATE_NOT_CURRENT": {
                "000300": self.bars[:-1], "000852": self.bars,
            },
        }
        for reason, histories in cases.items():
            with self.subTest(reason=reason):
                kwargs = dict(self.kwargs, environment_histories=histories)
                result = assess_verified_quality(self.bars, **kwargs)
                self.assertEqual(result["status"], "UNVERIFIED")
                self.assertIn(reason, result["reasons"])

    def test_environment_history_must_be_ordered_and_unique(self):
        histories = dict(self.kwargs["environment_histories"])
        histories["000300"] = (*self.bars[:-2], self.bars[-1], self.bars[-2])
        result = assess_verified_quality(
            self.bars, **dict(self.kwargs, environment_histories=histories),
        )
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertIn("DATA_QUALITY_ENVIRONMENT_INVALID_SEQUENCE", result["reasons"])

    def test_post_close_window_keeps_previous_trading_day(self):
        kwargs = dict(self.kwargs)
        kwargs["today"] = datetime(2026, 9, 23, 15, 30, tzinfo=SHANGHAI)
        result = assess_verified_quality(self.bars, **kwargs)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["expected_last_completed_date"], "2026-09-22")

    def test_after_daily_ready_time_expects_same_day_bar(self):
        kwargs = dict(self.kwargs)
        kwargs["today"] = datetime(2026, 9, 23, 16, 0, tzinfo=SHANGHAI)
        result = assess_verified_quality(self.bars, **kwargs)
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertIn("DATA_QUALITY_LATEST_DATE_NOT_CURRENT", result["reasons"])
        self.assertEqual(result["expected_last_completed_date"], "2026-09-23")

    def test_sample_end_may_lag_when_digest_still_matches(self):
        receipt = _receipt(self.bars)
        receipt["sample_end"] = self.bars[-2].trading_date.isoformat()
        result = assess_verified_quality(self.bars, **dict(self.kwargs, receipt=receipt))
        self.assertEqual(result["status"], "VERIFIED")
        receipt["calculation_version"] = "sha256:" + "ab" * 8
        mismatched = assess_verified_quality(self.bars, **dict(self.kwargs, receipt=receipt))
        self.assertIn("DATA_QUALITY_RECEIPT_SAMPLE_MISMATCH", mismatched["reasons"])


if __name__ == "__main__":
    unittest.main()
