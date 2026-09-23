from __future__ import annotations

import math

import pytest

from etf_rotation.swing_indicators import (
    IndicatorInputError,
    calculate_indicator_snapshot,
)
from tests.swing_helpers import swing_strategy_bars


def test_snapshot_exposes_macd_kdj_rsi_and_moving_averages() -> None:
    snapshot = calculate_indicator_snapshot(swing_strategy_bars(130))

    assert snapshot["schema_version"] == 1
    assert snapshot["status"] == "READY"
    assert snapshot["bar_count"] == 130
    assert snapshot["as_of_trading_date"] == "2026-07-03"
    assert set(snapshot["macd"]) == {"ema12", "ema26", "dif", "dea", "histogram"}
    assert set(snapshot["kdj"]) == {"k", "d", "j"}
    assert set(snapshot["rsi"]) == {"rsi14"}
    assert set(snapshot["moving_averages"]) == {"ma5", "ma10", "ma20", "ma60"}
    for section in ("macd", "kdj", "rsi", "moving_averages"):
        assert all(math.isfinite(value) for value in snapshot[section].values())


def test_snapshot_exposes_position_volume_and_weekly_context() -> None:
    snapshot = calculate_indicator_snapshot(swing_strategy_bars(130))

    assert set(snapshot["bias20"]) == {"value"}
    assert set(snapshot["bollinger"]) == {"middle", "upper", "lower", "stddev"}
    assert set(snapshot["volume"]) == {"ma20", "ratio20", "contraction"}
    assert set(snapshot["weekly"]) == {
        "status", "bar_count", "close", "ma10", "ma20", "as_of_trading_date",
    }
    assert snapshot["weekly"]["status"] == "READY"
    for section in ("bias20", "bollinger", "volume"):
        values = snapshot[section].values()
        for value in values:
            if value is not None and not isinstance(value, bool):
                assert math.isfinite(value)
    assert snapshot["volume"]["ratio20"] > 0.0


def test_flat_prices_have_neutral_rsi_and_stable_macd() -> None:
    snapshot = calculate_indicator_snapshot(swing_strategy_bars(130, pattern="flat"))

    assert snapshot["rsi"]["rsi14"] == pytest.approx(50.0)
    assert snapshot["macd"]["dif"] == pytest.approx(0.0)
    assert snapshot["macd"]["dea"] == pytest.approx(0.0)
    assert snapshot["macd"]["histogram"] == pytest.approx(0.0)
    assert snapshot["kdj"]["k"] == pytest.approx(50.0)
    assert snapshot["kdj"]["d"] == pytest.approx(50.0)
    assert snapshot["kdj"]["j"] == pytest.approx(50.0)


def test_insufficient_history_is_explicit_and_does_not_fill_missing_values() -> None:
    snapshot = calculate_indicator_snapshot(swing_strategy_bars(20))

    assert snapshot["status"] == "WARMUP"
    assert snapshot["reason"] == "INSUFFICIENT_COMPLETED_BARS"
    assert snapshot["bar_count"] == 20
    assert snapshot["macd"]["dif"] is not None
    assert snapshot["rsi"]["rsi14"] is not None


def test_empty_history_is_data_unavailable() -> None:
    snapshot = calculate_indicator_snapshot(())

    assert snapshot["status"] == "DATA_UNAVAILABLE"
    assert snapshot["reason"] == "NO_COMPLETED_BARS"
    assert snapshot["as_of_trading_date"] is None
    assert snapshot["macd"]["dif"] is None
    assert snapshot["kdj"]["k"] is None
    assert snapshot["rsi"]["rsi14"] is None


def test_rejects_duplicate_or_non_ascending_trading_dates() -> None:
    bars = list(swing_strategy_bars(5))
    bars[1] = bars[0]

    with pytest.raises(IndicatorInputError, match="trading_date"):
        calculate_indicator_snapshot(bars)
