from __future__ import annotations

from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import unittest

from etf_rotation.etf_metadata import EtfMetadata, IndexMetadata, TradingMetadata
from etf_rotation.swing_v11 import (
    V11Context,
    calculate_relative_strength_20,
    calculate_v11_environment,
    calculate_v11_indicators,
    classify_v11_environment,
    evaluate_v11,
    load_v11_config,
    validate_v11_metadata,
)
from tests.swing_helpers import retime_daily_bars, swing_strategy_bars


class SwingV11P0Tests(unittest.TestCase):
    def test_metadata_environment_index_matches_handbook_side(self) -> None:
        payload = json.loads(
            (Path(__file__).parents[1] / "data" / "monitor" / "etf_metadata.json").read_text(encoding="utf-8")
        )
        by_symbol = {item["symbol"]: item for item in payload["items"]}
        for symbol in ("510500", "512100", "159915", "588000", "159781", "512480", "515880", "159995"):
            self.assertEqual(by_symbol[symbol]["environment_index"], "000852")
        for symbol in ("510300", "563360", "515180", "512170", "512010", "159928"):
            self.assertEqual(by_symbol[symbol]["environment_index"], "000300")
        for symbol in ("159792", "513050"):
            self.assertIsNone(by_symbol[symbol]["environment_index"])

    def test_trailing_holiday_week_is_completed_with_calendar(self) -> None:
        bars = retime_daily_bars(
            swing_strategy_bars(140), ending_on=date(2026, 9, 24),
        )
        indicators = calculate_v11_indicators(
            bars, closed_dates={date(2026, 9, 25)},
        )
        self.assertEqual(
            indicators["weekly"]["as_of_trading_date"], "2026-09-24",
        )

    def test_direct_evaluator_flattens_nested_indicator_contract(self) -> None:
        config = load_v11_config("data/swing/v11_strategy.json")
        bars = swing_strategy_bars(260)
        decision = evaluate_v11(
            bars, config=config, context=V11Context(
                environment_state="ATTACK", indicator={},
            ),
        )
        self.assertIn("price", decision.evidence)
        self.assertNotIn("INDICATOR_CONTEXT_UNAVAILABLE", decision.blocked_reasons)

    def test_market_environment_requires_two_day_confirmation_and_weekly_defense(self) -> None:
        attack = {
            "price": 105.0, "ma20": 103.0, "ma60": 100.0,
            "ma20_slope_pct_10d": 1.0, "weekly_close": 105.0,
            "weekly_ma20": 100.0,
        }
        defense = {
            "price": 95.0, "ma20": 97.0, "ma60": 100.0,
            "ma20_slope_pct_10d": -1.0, "weekly_close": 90.0,
            "weekly_ma20": 95.0, "weekly_ma20_prev": 96.0,
        }
        context = calculate_v11_environment(
            {"000300": (attack, attack), "000852": (attack, attack)},
        )
        self.assertEqual(context["state"], "ATTACK")
        self.assertEqual(context["health"], "OK")
        context = calculate_v11_environment(
            {"000300": (defense, defense), "000852": (attack, attack)},
        )
        self.assertEqual(context["state"], "DEFENSE")
        self.assertTrue(context["hard_defense"])

    def test_missing_explicit_index_history_fails_closed(self) -> None:
        attack = {
            "price": 105.0, "ma20": 103.0, "ma60": 100.0,
            "ma20_slope_pct_10d": 1.0, "weekly_close": 105.0,
            "weekly_ma20": 100.0,
        }
        context = calculate_v11_environment({"000300": (attack, attack)})
        self.assertEqual(context["state"], "UNKNOWN")
        self.assertEqual(context["health"], "UNAVAILABLE")

    def test_environment_health_allows_ma20_flat_without_ma20_above_ma60(self) -> None:
        self.assertEqual(classify_v11_environment({
            "price": 101.0, "ma20": 99.0, "ma60": 100.0,
            "ma20_slope_pct_10d": 0.4,
        }), "ATTACK")
        self.assertEqual(classify_v11_environment({
            "price": 99.0, "ma20": 101.0, "ma60": 100.0,
            "ma20_slope_pct_10d": -0.4,
        }), "DEFENSE")

    def test_weekly_hard_defense_requires_declining_csi300_weekly_ma20(self) -> None:
        attack = {
            "price": 105.0, "ma20": 103.0, "ma60": 100.0,
            "ma20_slope_pct_10d": 1.0, "weekly_close": 105.0,
            "weekly_ma20": 100.0, "weekly_ma20_prev": 100.0,
        }
        csi300_break = {
            "price": 101.0, "ma20": 99.0, "ma60": 100.0,
            "ma20_slope_pct_10d": 0.2, "weekly_close": 90.0,
            "weekly_ma20": 95.0, "weekly_ma20_prev": 96.0,
        }
        context = calculate_v11_environment({
            "000300": (csi300_break, csi300_break),
            "000852": (attack, attack),
        })
        self.assertTrue(context["hard_defense"])
        self.assertEqual(context["hard_defense_reason"], "CSI300_WEEKLY_BREAK")
        no_decline = dict(csi300_break, weekly_ma20_prev=95.0)
        context = calculate_v11_environment({
            "000300": (no_decline, no_decline), "000852": (attack, attack),
        })
        self.assertFalse(context["hard_defense"])

    def test_relative_strength_uses_twenty_day_return_difference(self) -> None:
        symbol = swing_strategy_bars(30)
        env = tuple(
            replace(bar, adjusted_open=bar.adjusted_open * 0.99,
                    adjusted_high=bar.adjusted_high * 0.99,
                    adjusted_low=bar.adjusted_low * 0.99,
                    adjusted_close=bar.adjusted_close * 0.99)
            for bar in swing_strategy_bars(30, symbol="000001")
        )
        env = (*env[:-1], replace(
            env[-1], adjusted_open=env[-1].adjusted_open * 0.98,
            adjusted_high=env[-1].adjusted_high * 0.98,
            adjusted_low=env[-1].adjusted_low * 0.98,
            adjusted_close=env[-1].adjusted_close * 0.98,
        ))
        value = calculate_relative_strength_20(symbol, env)
        self.assertIsNotNone(value)
        self.assertGreater(value, 0.0)

    def test_v11_metadata_requires_known_category_environment_and_group(self) -> None:
        trading = TradingMetadata("SSE", "DOMESTIC_EQUITY_ETF", False, 1, 100, .001, .1, 100)
        item = EtfMetadata(
            "510300", "ETF", IndexMetadata("000300", "CSI", "TEST"), trading,
            category="BROAD", environment_index="000300", correlation_group="BROAD_CN",
        )
        self.assertEqual(validate_v11_metadata({"510300": item}, ("510300",)), {})
        incomplete = EtfMetadata(
            item.symbol, item.name, item.index, item.trading,
            category=item.category, correlation_group=item.correlation_group,
        )
        self.assertIn("environment_index", validate_v11_metadata({"510300": incomplete}, ("510300",))["510300"])

    def test_cross_border_and_gold_may_omit_environment_index(self) -> None:
        trading = TradingMetadata("SSE", "DOMESTIC_EQUITY_ETF", False, 1, 100, .001, .1, 100)
        for category in ("CROSS_BORDER", "GOLD"):
            item = EtfMetadata(
                "510300", "ETF", IndexMetadata("000300", "CSI", "TEST"), trading,
                category=category, correlation_group="GROUP",
            )
            self.assertEqual(validate_v11_metadata({"510300": item}, ("510300",)), {})

    def test_canonical_categories_require_csi_environment_index(self) -> None:
        trading = TradingMetadata("SSE", "DOMESTIC_EQUITY_ETF", False, 1, 100, .001, .1, 100)
        for category in ("BROAD", "SECTOR"):
            item = EtfMetadata(
                "510300", "ETF", IndexMetadata("000300", "CSI", "TEST"), trading,
                category=category, correlation_group="GROUP",
            )
            self.assertIn("environment_index", validate_v11_metadata({"510300": item}, ("510300",))["510300"])
            complete = EtfMetadata(
                item.symbol, item.name, item.index, item.trading,
                category=category, environment_index="000300", correlation_group="GROUP",
            )
            self.assertEqual(validate_v11_metadata({"510300": complete}, ("510300",)), {})

        for legacy in ("GROWTH", "SMALL_CAP", "DIVIDEND"):
            item = EtfMetadata(
                "510300", "ETF", IndexMetadata("000300", "CSI", "TEST"), trading,
                category=legacy, environment_index="000300", correlation_group="GROUP",
            )
            self.assertIn("category", validate_v11_metadata({"510300": item}, ("510300",))["510300"])


if __name__ == "__main__":
    unittest.main()
