from __future__ import annotations

from etf_rotation.swing_indicators import (
    _kdj,
    _rsi_wilder,
    classify_regimes,
    dmi_series,
    kdj_series,
    rsi_series,
)
from scripts.backtest_dmi_kdj_rsi import Config, simulate
from tests.swing_helpers import swing_strategy_bars


def test_rising_market_has_plus_di_above_minus_di() -> None:
    bars = swing_strategy_bars(40)
    plus, minus = dmi_series(
        [bar.adjusted_high for bar in bars],
        [bar.adjusted_low for bar in bars],
        [bar.adjusted_close for bar in bars],
        14,
    )

    assert plus[13] is None
    assert plus[-1] is not None and minus[-1] is not None
    assert plus[-1] > minus[-1]


def test_falling_market_has_minus_di_above_plus_di() -> None:
    bars = swing_strategy_bars(40, pattern="falling_ma60")
    plus, minus = dmi_series(
        [bar.adjusted_high for bar in bars],
        [bar.adjusted_low for bar in bars],
        [bar.adjusted_close for bar in bars],
        14,
    )

    assert minus[-1] is not None and plus[-1] is not None
    assert minus[-1] > plus[-1]


def test_kdj_series_matches_snapshot_function() -> None:
    bars = swing_strategy_bars(40)
    highs = [bar.adjusted_high for bar in bars]
    lows = [bar.adjusted_low for bar in bars]
    closes = [bar.adjusted_close for bar in bars]
    k_values, d_values, j_values = kdj_series(highs, lows, closes)
    latest = _kdj(highs, lows, closes)

    assert latest is not None
    assert k_values[-1] == latest["k"]
    assert d_values[-1] == latest["d"]
    assert j_values[-1] == latest["j"]


def test_rsi_series_matches_wilder_helper() -> None:
    closes = [100.0 + (index % 7) - 3 for index in range(30)]
    series = rsi_series(closes, 14)

    assert series[14] is not None
    assert series[-1] == _rsi_wilder(closes, 14)


def test_regimes_are_exclusive_once_ma60_exists() -> None:
    price = 100.0
    closes: list[float] = []
    for index in range(120):
        if index < 80:
            price += 0.4
        elif index < 95:
            price += 0.05 if index % 2 == 0 else -0.05
        else:
            price -= 0.5
        closes.append(price)
    labels = classify_regimes(closes)

    assert all(label is None for label in labels[:69])
    assert labels[80] == "UP"
    assert labels[-1] == "DOWN"
    assert set(labels[69:]) <= {"UP", "DOWN", "RANGE"}


def test_assisted_round_trip_ends_within_three_holding_days() -> None:
    bars = swing_strategy_bars(180)
    trades = simulate(bars, Config(14, 9, 3, 3, 6))
    index = {bar.trading_date.isoformat(): position for position, bar in enumerate(bars)}

    for trade in trades:
        assert index[trade.exit_date] - index[trade.entry_date] <= 3


def test_completed_trades_do_not_use_future_bars() -> None:
    bars = swing_strategy_bars(180)
    config = Config(14, 9, 3, 3, 14)
    full = simulate(bars, config)
    shorter = simulate(bars[:-10], config)
    last_date = bars[-11].trading_date.isoformat()

    def identity(trade):
        return (
            trade.signal_date, trade.entry_date, trade.exit_date,
            trade.entry_price, trade.exit_price, trade.regime,
        )

    expected = {identity(trade) for trade in full if trade.exit_date <= last_date}
    actual = {identity(trade) for trade in shorter if trade.exit_date <= last_date}
    assert expected == actual
