from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from etf_rotation.swing_config import load_strategy
from etf_rotation.swing_quality import (
    _history_digest,
    summarize_common_history,
    summarize_history_quality,
    summarize_strategy_diagnostics,
)
from tests.swing_helpers import swing_strategy_bars


TENCENT = (
    "腾讯 fqkline 原始+前复权 (web.ifzq.gtimg.cn); "
    "amount=OHLC均价×成交量(手)×100估算"
)
EASTMONEY = "东方财富 kline (push2his.eastmoney.com)"
WIND = (
    "Wind fund_data.get_fund_kline 512010.SH 原始/前复权；"
    "TURNOVER=元；VOLUME原值按接口样本校准折算为100份单位"
)


def _item(symbol, *, formal_state="WATCH", blocked_reasons=(), data_quality=None):
    return {
        "symbol": symbol,
        "formal_state": formal_state,
        "blocked_reasons": list(blocked_reasons),
        "data_quality": data_quality,
    }


COVERAGE = {
    "common_bar_count": 460,
    "walk_forward_required_bars": 630,
    "walk_forward_fold_count": 2,
    "warnings": [],
}


class SwingQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_strategy(Path(__file__).parents[1] / "data/swing/strategy.json")

    def test_estimated_amount_is_not_independently_verified(self) -> None:
        bars = tuple(replace(bar, source=TENCENT) for bar in swing_strategy_bars(70))
        result = summarize_history_quality(bars, self.config)
        self.assertEqual(result["amount_quality"], "ESTIMATED")
        self.assertEqual(result["crosscheck_status"], "NOT_RECORDED")
        self.assertIn("AMOUNT_ESTIMATED", result["warnings"])
        self.assertEqual(result["sources"], [TENCENT])
        self.assertEqual(result["bar_count"], 70)
        self.assertEqual(result["last_observed_at"], bars[-1].observed_at.isoformat())
        self.assertTrue(result["indicator_sample_ok"])
        self.assertFalse(result["backtest_sample_ok"])

    def test_supplier_reported_amount_is_not_crosscheck_success(self) -> None:
        bars = tuple(replace(bar, source=EASTMONEY) for bar in swing_strategy_bars(71))
        result = summarize_history_quality(bars, self.config)
        self.assertEqual(result["amount_quality"], "PROVIDER_REPORTED")
        self.assertEqual(result["crosscheck_status"], "NOT_RECORDED")
        self.assertTrue(result["backtest_sample_ok"])
        self.assertEqual(result["minimum_backtest_bars"], 71)

    def test_wind_reported_amount_with_explicit_unit_receipt_is_provider_reported(self) -> None:
        bars = tuple(replace(bar, source=WIND) for bar in swing_strategy_bars(71))
        result = summarize_history_quality(bars, self.config)
        self.assertEqual(result["amount_quality"], "PROVIDER_REPORTED")
        self.assertNotIn("UNKNOWN_SOURCE_CONTRACT", result["warnings"])
        self.assertIn("INDEPENDENT_CROSSCHECK_NOT_RECORDED", result["warnings"])

    def test_unknown_and_mixed_sources_are_explicit(self) -> None:
        bars = swing_strategy_bars(71)
        result = summarize_history_quality(bars, self.config)
        self.assertEqual(result["amount_quality"], "UNKNOWN")
        mixed = (replace(bars[0], source=TENCENT), replace(bars[1], source=EASTMONEY))
        result = summarize_history_quality(mixed, self.config)
        self.assertEqual(result["amount_quality"], "MIXED")
        self.assertIn("MIXED_SOURCES", result["warnings"])
        self.assertIn("AMOUNT_ESTIMATED", result["warnings"])

    def test_adjustment_change_is_review_warning_not_claimed_dividend(self) -> None:
        bars = swing_strategy_bars(71)
        last = bars[-1]
        changed = replace(last, **{
            field: getattr(last, field) * 0.9
            for field in ("adjusted_open", "adjusted_high", "adjusted_low", "adjusted_close")
        })
        result = summarize_history_quality((*bars[:-1], changed), self.config)
        self.assertEqual(result["adjustment_status"], "RATIO_CHANGED_REQUIRES_REVIEW")
        self.assertIn("ADJUSTMENT_BASIS_UNVERIFIED", result["warnings"])
        self.assertNotIn("CORPORATE_ACTION_CONFIRMED", result["warnings"])

    def test_matching_verified_receipt_replaces_legacy_not_recorded_statuses(self) -> None:
        bars = tuple(replace(bar, source=EASTMONEY) for bar in swing_strategy_bars(71))
        last = bars[-1]
        changed = replace(last, **{
            field: getattr(last, field) * 0.9
            for field in ("adjusted_open", "adjusted_high", "adjusted_low", "adjusted_close")
        })
        history = (*bars[:-1], changed)
        receipt = {
            "source": "腾讯 fqkline 独立交叉核验 (web.ifzq.gtimg.cn)",
            "checked_at": "2026-09-23T17:00:00+08:00",
            "sample_start": history[0].trading_date.isoformat(),
            "sample_end": history[-1].trading_date.isoformat(),
            "calculation_version": _history_digest(history),
            "crosscheck_status": "PASSED",
            "adjustment_status": "VERIFIED",
            "warnings": [],
        }
        result = summarize_history_quality(history, self.config, receipt=receipt)
        self.assertEqual(result["classification_basis"], "INDEPENDENT_RECEIPT")
        self.assertEqual(result["crosscheck_status"], "PASSED")
        self.assertEqual(result["adjustment_status"], "RATIO_CHANGED_VERIFIED")
        self.assertEqual(result["warnings"], [])

        # A receipt for a different history (digest mismatch) keeps the defaults.
        stale = {**receipt, "calculation_version": _history_digest(bars)}
        result = summarize_history_quality(history, self.config, receipt=stale)
        self.assertEqual(result["classification_basis"], "LEGACY_SOURCE_LABEL")
        self.assertEqual(result["crosscheck_status"], "NOT_RECORDED")
        self.assertEqual(result["adjustment_status"], "RATIO_CHANGED_REQUIRES_REVIEW")
        self.assertIn("INDEPENDENT_CROSSCHECK_NOT_RECORDED", result["warnings"])
        self.assertIn("ADJUSTMENT_BASIS_UNVERIFIED", result["warnings"])

        # A matching receipt that still needs review is reported as such.
        pending = {**receipt, "crosscheck_status": "FAILED", "adjustment_status": "REVIEW"}
        result = summarize_history_quality(history, self.config, receipt=pending)
        self.assertEqual(result["crosscheck_status"], "FAILED")
        self.assertEqual(result["adjustment_status"], "RATIO_CHANGED_REQUIRES_REVIEW")
        self.assertIn("INDEPENDENT_CROSSCHECK_NOT_RECORDED", result["warnings"])
        self.assertIn("ADJUSTMENT_BASIS_UNVERIFIED", result["warnings"])

    def test_empty_history_does_not_claim_readiness(self) -> None:
        result = summarize_history_quality((), self.config)
        self.assertEqual(result["bar_count"], 0)
        self.assertIsNone(result["start_date"])
        self.assertIsNone(result["last_observed_at"])
        self.assertFalse(result["indicator_sample_ok"])
        self.assertFalse(result["backtest_sample_ok"])
        self.assertEqual(result["walk_forward_fold_count"], 0)

    def test_window_counts_are_sample_coverage_not_performance(self) -> None:
        for count, expected in ((460, 0), (629, 0), (630, 1), (756, 2)):
            with self.subTest(count=count):
                result = summarize_history_quality(swing_strategy_bars(count), self.config)
                self.assertEqual(result["walk_forward_required_bars"], 630)
                self.assertEqual(result["walk_forward_fold_count"], expected)
                self.assertFalse(result["performance_validated"])

    def test_common_history_uses_overlap_and_keeps_individual_coverage(self) -> None:
        older = swing_strategy_bars(756)
        younger = tuple(replace(bar, symbol="563360") for bar in older[-460:])
        result = summarize_common_history({"510300": older, "563360": younger}, self.config)
        self.assertEqual(result["common_bar_count"], 460)
        self.assertTrue(result["aligned"])
        self.assertEqual(result["walk_forward_fold_count"], 0)
        self.assertEqual(result["start_date"], younger[0].trading_date.isoformat())
        self.assertEqual(summarize_history_quality(older, self.config)["walk_forward_fold_count"], 2)

    def test_gap_in_one_symbol_cannot_be_hidden_by_intersection(self) -> None:
        bars = swing_strategy_bars(756)
        gap = tuple(replace(bar, symbol="510500") for index, bar in enumerate(bars) if index != 700)
        result = summarize_common_history({"510300": bars, "510500": gap}, self.config)
        self.assertFalse(result["aligned"])
        self.assertEqual(result["walk_forward_fold_count"], 0)
        self.assertIn("NON_ALIGNED_COMMON_HISTORY", result["warnings"])

    def test_missing_symbol_cannot_be_silently_removed(self) -> None:
        result = summarize_common_history({"510300": swing_strategy_bars(756), "563360": ()}, self.config)
        self.assertEqual(result["common_bar_count"], 0)
        self.assertFalse(result["aligned"])
        self.assertIn("MISSING_SYMBOL_HISTORY", result["warnings"])

    def test_inputs_are_not_mutated_and_duplicates_do_not_count(self) -> None:
        bars = list(swing_strategy_bars(70))
        supplied = [bars[-1], *bars]
        original = supplied.copy()
        result = summarize_history_quality(supplied, self.config)
        self.assertEqual(supplied, original)
        self.assertEqual(result["bar_count"], 70)
        self.assertIn("DUPLICATE_TRADING_DATE", result["warnings"])
        self.assertEqual(result["walk_forward_fold_count"], 0)


class SwingStrategyDiagnosticsTests(unittest.TestCase):
    def test_duplicate_symbol_rows_do_not_inflate_counts(self):
        from etf_rotation.swing_quality import summarize_strategy_diagnostics
        item = {"symbol": "510300", "data_quality": {"warnings": ["AMOUNT_ESTIMATED"]},
                "blocked_reasons": ["trend_gate"], "formal_state": "TREND_BLOCKED"}
        result = summarize_strategy_diagnostics([item, dict(item)], None, {})
        self.assertEqual(result["warning_counts"], {"AMOUNT_ESTIMATED": 1})
        self.assertEqual(result["blocked_reason_counts"], {"trend_gate": 1})
        self.assertEqual(result["formal_state_counts"], {"TREND_BLOCKED": 1})
        self.assertEqual(result["warning_symbols"], ["510300"])


    def test_empty_items_report_no_data_not_healthy(self) -> None:
        result = summarize_strategy_diagnostics([], None, {})
        self.assertEqual(result["item_status"], "NO_DATA")
        self.assertTrue(result["snapshot_scope"])
        self.assertEqual(result["warning_counts"], {})
        self.assertEqual(result["warning_symbols"], [])
        self.assertEqual(result["blocked_reason_counts"], {})
        self.assertEqual(result["formal_state_counts"], {})
        self.assertEqual(result["layers"]["research_quality"], "UNVERIFIED")
        self.assertEqual(result["layers"]["performance"], "NOT_VALIDATED")
        self.assertTrue(result["coverage"]["sample_windows_are_not_validation"])
        self.assertEqual(result["coverage"]["common_bar_count"], 0)
        self.assertEqual(result["coverage"]["required"], 0)
        self.assertEqual(result["coverage"]["fold"], 0)

    def test_missing_quality_keeps_research_unverified(self) -> None:
        items = [_item("510300", formal_state="WATCH")]
        result = summarize_strategy_diagnostics(items, COVERAGE, {"daily": "OK"})
        self.assertEqual(result["layers"]["research_quality"], "UNVERIFIED")
        self.assertEqual(result["item_status"], "AVAILABLE")
        self.assertEqual(
            result["coverage"],
            {
                "common_bar_count": 460,
                "required": 630,
                "fold": 2,
                "sample_windows_are_not_validation": True,
            },
        )

    def test_unknown_quality_is_never_verified(self) -> None:
        items = [_item(
            "510300",
            data_quality={"amount_quality": "UNKNOWN", "warnings": ["NO_DAILY_HISTORY"]},
        )]
        result = summarize_strategy_diagnostics(items, COVERAGE, {})
        self.assertEqual(result["layers"]["research_quality"], "UNVERIFIED")

    def test_duplicate_warnings_count_each_symbol_once(self) -> None:
        items = [_item(
            "510300",
            data_quality={"amount_quality": "UNKNOWN", "warnings": [
                "A", "A", "B", "B", "B",
            ]},
        )]
        result = summarize_strategy_diagnostics(items, COVERAGE, {})
        self.assertEqual(result["warning_counts"], {"A": 1, "B": 1})
        self.assertEqual(result["warning_symbols"], ["510300"])

    def test_different_symbols_aggregate_warning_counts(self) -> None:
        items = [
            _item("510300", data_quality={"warnings": ["A", "B"]}),
            _item("510500", data_quality={"warnings": ["A", "C"]}),
        ]
        result = summarize_strategy_diagnostics(items, COVERAGE, {})
        self.assertEqual(result["warning_counts"], {"A": 2, "B": 1, "C": 1})
        self.assertEqual(set(result["warning_symbols"]), {"510300", "510500"})

    def test_blocked_reasons_and_formal_states_are_deduped_per_symbol(self) -> None:
        items = [
            _item("510300", formal_state="BLOCKED", blocked_reasons=["R1", "R1"]),
            _item("510500", formal_state="BLOCKED", blocked_reasons=["R1", "R2"]),
            _item("563360", formal_state="WATCH", blocked_reasons=[]),
        ]
        result = summarize_strategy_diagnostics(items, COVERAGE, {})
        self.assertEqual(result["blocked_reason_counts"], {"R1": 2, "R2": 1})
        self.assertEqual(result["formal_state_counts"], {"BLOCKED": 2, "WATCH": 1})

    def test_layers_map_health_components_without_historical_trigger_rate(self) -> None:
        items = [_item("510300", data_quality={"amount_quality": "PROVIDER_REPORTED"})]
        health = {"daily": "OK", "portfolio": "OK", "intraday": "REALTIME"}
        result = summarize_strategy_diagnostics(items, COVERAGE, health)
        self.assertEqual(result["layers"], {
            "daily_load": "OK",
            "account": "OK",
            "intraday": "REALTIME",
            "research_quality": "UNVERIFIED",
            "performance": "NOT_VALIDATED",
        })
        self.assertNotIn("trigger_rate", result)
        self.assertNotIn("historical", result)

    def test_inputs_are_not_mutated_and_result_is_deep_copy(self) -> None:
        health = {"daily": "OK"}
        reasons = ["R1", "R1"]
        warnings = ["A", "A"]
        items = [_item(
            "510300", formal_state="WATCH", blocked_reasons=reasons,
            data_quality={"amount_quality": "UNKNOWN", "warnings": warnings},
        )]
        coverage = dict(COVERAGE)
        original_items = [dict(items[0])]
        original_health = dict(health)
        result = summarize_strategy_diagnostics(items, coverage, health)
        self.assertEqual(items, original_items)
        self.assertEqual(health, original_health)
        self.assertEqual(coverage, COVERAGE)
        # Mutating the returned structure must not reach the inputs.
        result["warning_counts"]["A"] = 99
        result["warning_symbols"].append("X")
        result["layers"]["account"] = "BLOCKED"
        self.assertEqual(items[0]["blocked_reasons"], ["R1", "R1"])
        self.assertEqual(items[0]["data_quality"]["warnings"], ["A", "A"])
        self.assertEqual(health["daily"], "OK")
        self.assertIsNot(result["warning_symbols"], items[0].get("warning_symbols"))


if __name__ == "__main__":
    unittest.main()
