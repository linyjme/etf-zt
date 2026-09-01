from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError, asdict
import json
from pathlib import Path
import tempfile
import unittest

from etf_rotation.etf_metadata import MetadataError
from etf_rotation.swing_config import (
    SwingConfigError,
    SwingStrategyConfig,
    SwingWatchItem,
    load_strategy,
    load_watchlist,
)
from tests.swing_helpers import (
    SWING_SYMBOLS,
    completed_daily_bars,
    metadata_fixture,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

SWING_V1_DEFAULTS = {
    "schema_version": 1,
    "strategy_version": "SWING_V1",
    "minimum_daily_bars": 70,
    "short_ma_days": 20,
    "long_ma_days": 60,
    "long_ma_slope_lookback": 10,
    "atr_days": 14,
    "pullback_atr_distance": 0.5,
    "entry_zone_atr_half_width": 0.25,
    "anti_chase_atr_distance": 1.5,
    "breakout_days": 20,
    "add_profit_r": 1.0,
    "reduce_profit_r": 2.0,
    "risk_per_trade": 0.0075,
    "max_symbol_weight": 0.40,
    "max_equity_weight": 0.80,
    "max_portfolio_risk": 0.02,
    "initial_stop_atr": 2.0,
    "trailing_stop_atr": 3.0,
    "cooldown_days": 5,
    "walk_forward_train_days": 504,
    "walk_forward_test_days": 126,
    "walk_forward_step_days": 126,
    "max_volume_participation": 0.10,
}

DAY_FIELDS = (
    "minimum_daily_bars",
    "short_ma_days",
    "long_ma_days",
    "long_ma_slope_lookback",
    "atr_days",
    "breakout_days",
    "cooldown_days",
    "walk_forward_train_days",
    "walk_forward_test_days",
    "walk_forward_step_days",
)
RATE_FIELDS = (
    "risk_per_trade",
    "max_symbol_weight",
    "max_equity_weight",
    "max_portfolio_risk",
    "max_volume_participation",
)
POSITIVE_NUMBER_FIELDS = (
    "pullback_atr_distance",
    "entry_zone_atr_half_width",
    "anti_chase_atr_distance",
    "add_profit_r",
    "reduce_profit_r",
    "initial_stop_atr",
    "trailing_stop_atr",
)


class SwingConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.metadata_path = self.root / "metadata.json"
        self.metadata_path.write_text(
            json.dumps(metadata_fixture()), encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, name: str, payload: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def configuration_error_from(
        self, action: Callable[[], object],
    ) -> SwingConfigError:
        try:
            action()
        except Exception as error:
            self.assertIsInstance(error, SwingConfigError)
            return error  # type: ignore[return-value]
        self.fail("SwingConfigError not raised")

    def assert_watchlist_rejected(self, payload: object) -> None:
        self.configuration_error_from(lambda: load_watchlist(
            self.write("watchlist.json", payload), self.metadata_path,
        ))

    def assert_strategy_rejected(self, payload: object) -> None:
        self.configuration_error_from(
            lambda: load_strategy(self.write("strategy.json", payload)),
        )

    def test_public_configuration_error_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(SwingConfigError, ValueError))

    def test_loaders_normalize_json_decode_errors_with_cause(self) -> None:
        for loader, arguments in (
            (load_strategy, (self.root / "strategy.json",)),
            (load_watchlist, (self.root / "watchlist.json", self.metadata_path)),
        ):
            path = arguments[0]
            path.write_text("{", encoding="utf-8")
            with self.subTest(loader=loader.__name__):
                error = self.configuration_error_from(lambda: loader(*arguments))
                self.assertIsInstance(error.__cause__, json.JSONDecodeError)

    def test_watchlist_normalizes_metadata_validation_errors_with_cause(self) -> None:
        watchlist_path = self.write("watchlist.json", {
            "schema_version": 1,
            "items": [{"symbol": "510300", "enabled": True}],
        })
        self.metadata_path.write_text("{", encoding="utf-8")

        error = self.configuration_error_from(
            lambda: load_watchlist(watchlist_path, self.metadata_path),
        )

        self.assertIn("ETF元数据", str(error))
        self.assertIsInstance(error.__cause__, MetadataError)

    def test_repository_defaults_are_exact_and_independent(self) -> None:
        watchlist_path = PROJECT_ROOT / "data" / "swing" / "watchlist.json"
        strategy_path = PROJECT_ROOT / "data" / "swing" / "strategy.json"
        metadata_path = PROJECT_ROOT / "data" / "monitor" / "etf_metadata.json"

        raw_watchlist = json.loads(watchlist_path.read_text(encoding="utf-8"))
        self.assertEqual(set(raw_watchlist), {"schema_version", "items"})
        self.assertEqual(
            tuple(raw_watchlist["items"]),
            tuple({"symbol": symbol, "enabled": True} for symbol in SWING_SYMBOLS),
        )
        self.assertEqual(
            load_watchlist(watchlist_path, metadata_path),
            tuple(SwingWatchItem(symbol, True) for symbol in SWING_SYMBOLS),
        )
        self.assertEqual(asdict(load_strategy(strategy_path)), SWING_V1_DEFAULTS)

    def test_loaded_models_are_frozen(self) -> None:
        watchlist = load_watchlist(
            self.write("watchlist.json", {
                "schema_version": 1,
                "items": [{"symbol": "510300", "enabled": True}],
            }),
            self.metadata_path,
        )
        strategy = load_strategy(self.write("strategy.json", SWING_V1_DEFAULTS))

        with self.assertRaises(FrozenInstanceError):
            watchlist[0].enabled = False  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            strategy.cooldown_days = 10  # type: ignore[misc]

    def test_watchlist_requires_exact_top_level_shape_and_schema(self) -> None:
        valid_items = [{"symbol": "510300", "enabled": True}]
        cases = (
            [],
            {"items": valid_items},
            {"schema_version": 1},
            {"schema_version": True, "items": valid_items},
            {"schema_version": 2, "items": valid_items},
            {"schema_version": 1, "items": "510300"},
            {"schema_version": 1, "items": valid_items, "unknown": None},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.assert_watchlist_rejected(payload)

    def test_watchlist_items_require_exact_fields_and_strict_types(self) -> None:
        invalid_items = (
            "510300",
            {"enabled": True},
            {"symbol": "510300"},
            {"symbol": "510300", "enabled": True, "name": "ETF"},
            {"symbol": 510300, "enabled": True},
            {"symbol": "510300", "enabled": 1},
            {"symbol": "５１０３００", "enabled": True},
            {"symbol": "51030", "enabled": True},
            {"symbol": "51030A", "enabled": True},
        )
        for item in invalid_items:
            with self.subTest(item=item):
                self.assert_watchlist_rejected({"schema_version": 1, "items": [item]})

    def test_watchlist_rejects_unknown_metadata_and_duplicate_symbols(self) -> None:
        self.assert_watchlist_rejected({
            "schema_version": 1,
            "items": [{"symbol": "510301", "enabled": True}],
        })
        self.assert_watchlist_rejected({
            "schema_version": 1,
            "items": [
                {"symbol": "510300", "enabled": True},
                {"symbol": "510300", "enabled": False},
            ],
        })

    def test_strategy_requires_exact_fields_and_versions(self) -> None:
        missing = dict(SWING_V1_DEFAULTS)
        missing.pop("atr_days")
        extra = {**SWING_V1_DEFAULTS, "unknown": 1}
        wrong_schema_type = {**SWING_V1_DEFAULTS, "schema_version": True}
        wrong_schema = {**SWING_V1_DEFAULTS, "schema_version": 2}
        wrong_strategy = {**SWING_V1_DEFAULTS, "strategy_version": "SWING_V2"}
        for payload in ([], missing, extra, wrong_schema_type, wrong_schema, wrong_strategy):
            with self.subTest(payload=payload):
                self.assert_strategy_rejected(payload)

    def test_strategy_rejects_boolean_and_wrong_numeric_types(self) -> None:
        cases = (
            {**SWING_V1_DEFAULTS, "short_ma_days": True},
            {**SWING_V1_DEFAULTS, "short_ma_days": 20.0},
            {**SWING_V1_DEFAULTS, "risk_per_trade": False},
            {**SWING_V1_DEFAULTS, "risk_per_trade": "0.1"},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.assert_strategy_rejected(payload)

    def test_strategy_requires_positive_day_counts(self) -> None:
        for field in DAY_FIELDS:
            with self.subTest(field=field):
                self.assert_strategy_rejected({**SWING_V1_DEFAULTS, field: 0})

    def test_strategy_requires_finite_rates_in_closed_upper_unit_interval(self) -> None:
        for field in RATE_FIELDS:
            for value in (0, -0.1, 1.0001, float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    self.assert_strategy_rejected({**SWING_V1_DEFAULTS, field: value})

    def test_strategy_requires_finite_positive_distances_and_r_multiples(self) -> None:
        for field in POSITIVE_NUMBER_FIELDS:
            for value in (0, -0.1, float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    self.assert_strategy_rejected({**SWING_V1_DEFAULTS, field: value})

    def test_strategy_rejects_incoherent_relationships(self) -> None:
        overrides = (
            {"short_ma_days": 60},
            {"long_ma_days": 71},
            {"entry_zone_atr_half_width": 0.75},
            {"anti_chase_atr_distance": 0.5},
            {"add_profit_r": 2.0},
            {"initial_stop_atr": 3.0, "trailing_stop_atr": 2.0},
            {"walk_forward_train_days": 100},
            {"walk_forward_step_days": 127},
            {"max_portfolio_risk": 0.81},
            {"max_symbol_weight": 0.81},
        )
        for override in overrides:
            with self.subTest(override=override):
                self.assert_strategy_rejected({**SWING_V1_DEFAULTS, **override})

    def test_strategy_rejects_trade_risk_above_portfolio_risk(self) -> None:
        self.assert_strategy_rejected({
            **SWING_V1_DEFAULTS,
            "risk_per_trade": 0.03,
            "max_portfolio_risk": 0.02,
        })

    def test_minimum_history_covers_long_ma_slope_at_boundary(self) -> None:
        accepted = load_strategy(self.write("strategy.json", {
            **SWING_V1_DEFAULTS,
            "minimum_daily_bars": 70,
            "long_ma_days": 60,
            "long_ma_slope_lookback": 10,
        }))
        self.assertEqual(accepted.minimum_daily_bars, 70)
        self.assert_strategy_rejected({
            **SWING_V1_DEFAULTS,
            "minimum_daily_bars": 69,
            "long_ma_days": 60,
            "long_ma_slope_lookback": 10,
        })

    def test_minimum_history_covers_breakout_window_at_boundary(self) -> None:
        accepted = load_strategy(self.write("strategy.json", {
            **SWING_V1_DEFAULTS,
            "minimum_daily_bars": 70,
            "breakout_days": 69,
        }))
        self.assertEqual(accepted.breakout_days, 69)
        self.assert_strategy_rejected({
            **SWING_V1_DEFAULTS,
            "minimum_daily_bars": 70,
            "breakout_days": 70,
        })

    def test_minimum_history_covers_true_range_at_boundary(self) -> None:
        accepted = load_strategy(self.write("strategy.json", {
            **SWING_V1_DEFAULTS,
            "minimum_daily_bars": 70,
            "atr_days": 69,
        }))
        self.assertEqual(accepted.atr_days, 69)
        self.assert_strategy_rejected({
            **SWING_V1_DEFAULTS,
            "minimum_daily_bars": 70,
            "atr_days": 70,
        })

    def test_walk_forward_training_covers_minimum_history_at_boundary(self) -> None:
        boundary = {
            **SWING_V1_DEFAULTS,
            "minimum_daily_bars": 70,
            "walk_forward_test_days": 60,
            "walk_forward_step_days": 60,
        }
        accepted = load_strategy(self.write("strategy.json", {
            **boundary,
            "walk_forward_train_days": 70,
        }))
        self.assertEqual(accepted.walk_forward_train_days, 70)
        self.assert_strategy_rejected({
            **boundary,
            "walk_forward_train_days": 69,
        })

    def test_huge_json_integer_is_a_configuration_error(self) -> None:
        self.assert_strategy_rejected({
            **SWING_V1_DEFAULTS,
            "risk_per_trade": 10**400,
        })

    def test_daily_bar_fixture_is_deterministic_and_timezone_aware(self) -> None:
        first = completed_daily_bars(3)
        self.assertEqual(first, completed_daily_bars(3))
        self.assertEqual(len(first), 3)
        self.assertTrue(all(str(bar["timestamp"]).endswith("+08:00") for bar in first))


if __name__ == "__main__":
    unittest.main()
