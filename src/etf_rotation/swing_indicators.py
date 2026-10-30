"""Pure, read-only technical indicators for the swing monitor.

The formal swing state machine intentionally remains in ``swing_strategy``.
This module only derives an explainable indicator snapshot from completed daily
bars so the UI can show the inputs without turning them into a new signal.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from hashlib import sha256
import json
import math
from typing import Any

from .swing_data import DailyBar


INDICATOR_SCHEMA_VERSION = 1
INDICATOR_VERSION = "INDICATORS_V1"
DEFAULT_MINIMUM_BARS = 120


class IndicatorInputError(ValueError):
    """Raised when a daily-bar sequence cannot be used for indicators."""


def _number(value: object, field: str) -> float:
    if type(value) not in (int, float):
        raise IndicatorInputError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise IndicatorInputError(f"{field} must be a finite positive number")
    return result


def _ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    if len(values) < period:
        seed_index = 0
        result = [float(values[0])]
    else:
        seed_index = period - 1
        seed = _mean(values[:period])
        result = [float(seed)] * period
    for value in values[seed_index + 1:]:
        result.append(result[-1] + alpha * (float(value) - result[-1]))
    return result


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else math.nan


def _stddev(values: Sequence[float]) -> float:
    if not values:
        return math.nan
    average = _mean(values)
    return math.sqrt(_mean([(value - average) ** 2 for value in values]))


def _latest_average(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return _mean(values[-period:])


def _rsi_wilder(values: Sequence[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = _mean(gains[:period])
    average_loss = _mean(losses[:period])
    for index in range(period, len(changes)):
        average_gain = ((average_gain * (period - 1)) + gains[index]) / period
        average_loss = ((average_loss * (period - 1)) + losses[index]) / period
    return _rsi_from_averages(average_gain, average_loss)


def _rsi_from_averages(average_gain: float, average_loss: float) -> float:
    if average_gain == 0.0 and average_loss == 0.0:
        return 50.0
    if average_loss == 0.0:
        return 100.0
    if average_gain == 0.0:
        return 0.0
    relative_strength = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def _positive_period(period: int, field: str) -> int:
    if type(period) is not int or period <= 0:
        raise IndicatorInputError(f"{field} must be a positive integer")
    return period


def rsi_series(closes: Sequence[float], period: int = 14) -> list[float | None]:
    """Wilder RSI aligned to closes. Warmup bars are None.

    The last finite value matches ``_rsi_wilder`` for the same period.
    """
    period = _positive_period(period, "period")
    count = len(closes)
    result: list[float | None] = [None] * count
    if count <= period:
        return result
    changes = [closes[index] - closes[index - 1] for index in range(1, count)]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = _mean(gains[:period])
    average_loss = _mean(losses[:period])
    result[period] = _rsi_from_averages(average_gain, average_loss)
    for index in range(period, len(changes)):
        average_gain = ((average_gain * (period - 1)) + gains[index]) / period
        average_loss = ((average_loss * (period - 1)) + losses[index]) / period
        result[index + 1] = _rsi_from_averages(average_gain, average_loss)
    return result


def kdj_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    rsv_period: int = 9,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> tuple[list[float], list[float], list[float]]:
    """Return K, D and J for every bar, seeded at 50 like the snapshot KDJ."""
    rsv_period = _positive_period(rsv_period, "rsv_period")
    k_smooth = _positive_period(k_smooth, "k_smooth")
    d_smooth = _positive_period(d_smooth, "d_smooth")
    if not (len(highs) == len(lows) == len(closes)):
        raise IndicatorInputError("KDJ inputs must have the same length")
    k_values: list[float] = []
    d_values: list[float] = []
    j_values: list[float] = []
    k = 50.0
    d = 50.0
    k_weight = (k_smooth - 1) / k_smooth
    d_weight = (d_smooth - 1) / d_smooth
    for index, close in enumerate(closes):
        start = max(0, index - rsv_period + 1)
        window_high = max(highs[start:index + 1])
        window_low = min(lows[start:index + 1])
        span = window_high - window_low
        rsv = 50.0 if span <= 0.0 else (close - window_low) / span * 100.0
        k = k_weight * k + rsv / k_smooth
        d = d_weight * d + k / d_smooth
        k_values.append(k)
        d_values.append(d)
        j_values.append(3.0 * k - 2.0 * d)
    return k_values, d_values, j_values


def dmi_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> tuple[list[float | None], list[float | None]]:
    """Wilder +DI and -DI aligned to closes. Warmup bars are None."""
    period = _positive_period(period, "period")
    if not (len(highs) == len(lows) == len(closes)):
        raise IndicatorInputError("DMI inputs must have the same length")
    count = len(closes)
    plus_di: list[float | None] = [None] * count
    minus_di: list[float | None] = [None] * count
    if count <= period:
        return plus_di, minus_di
    true_ranges: list[float] = []
    plus_moves: list[float] = []
    minus_moves: list[float] = []
    for index in range(1, count):
        up_move = highs[index] - highs[index - 1]
        down_move = lows[index - 1] - lows[index]
        plus_moves.append(up_move if up_move > down_move and up_move > 0.0 else 0.0)
        minus_moves.append(down_move if down_move > up_move and down_move > 0.0 else 0.0)
        true_ranges.append(max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        ))
    smooth_range = sum(true_ranges[:period])
    smooth_plus = sum(plus_moves[:period])
    smooth_minus = sum(minus_moves[:period])

    def _write(bar_index: int) -> None:
        if smooth_range <= 0.0:
            plus_di[bar_index] = 0.0
            minus_di[bar_index] = 0.0
            return
        plus_di[bar_index] = 100.0 * smooth_plus / smooth_range
        minus_di[bar_index] = 100.0 * smooth_minus / smooth_range

    _write(period)
    for index in range(period, len(true_ranges)):
        smooth_range = smooth_range - (smooth_range / period) + true_ranges[index]
        smooth_plus = smooth_plus - (smooth_plus / period) + plus_moves[index]
        smooth_minus = smooth_minus - (smooth_minus / period) + minus_moves[index]
        _write(index + 1)
    return plus_di, minus_di


def adx_series(
    plus_di: Sequence[float | None],
    minus_di: Sequence[float | None],
    period: int = 14,
) -> list[float | None]:
    """Wilder ADX aligned to the DI series. Warmup bars are None."""
    period = _positive_period(period, "period")
    count = len(plus_di)
    if count != len(minus_di):
        raise IndicatorInputError("ADX inputs must have the same length")
    directional = [None] * count
    for index, (plus, minus) in enumerate(zip(plus_di, minus_di)):
        if plus is None or minus is None or plus + minus <= 0.0:
            continue
        directional[index] = 100.0 * abs(plus - minus) / (plus + minus)
    result: list[float | None] = [None] * count
    finite = [index for index, value in enumerate(directional) if value is not None]
    if len(finite) < period:
        return result
    seed_indexes = finite[:period]
    smoothed = sum(float(directional[index]) for index in seed_indexes) / period
    result[seed_indexes[-1]] = smoothed
    for index in finite[period:]:
        smoothed = ((smoothed * (period - 1)) + float(directional[index])) / period
        result[index] = smoothed
    return result


def macd_series(
    closes: Sequence[float],
    fast: int = 6,
    slow: int = 13,
    signal: int = 5,
) -> tuple[list[float], list[float], list[float]]:
    """DIF, DEA and histogram. Histogram is DIF minus DEA, so its sign is the cross."""
    fast = _positive_period(fast, "fast")
    slow = _positive_period(slow, "slow")
    signal = _positive_period(signal, "signal")
    if fast >= slow:
        raise IndicatorInputError("MACD fast period must be shorter than slow period")
    dif = [
        short - long
        for short, long in zip(_ema(closes, fast), _ema(closes, slow))
    ]
    dea = _ema(dif, signal)
    histogram = [value - average for value, average in zip(dif, dea)]
    return dif, dea, histogram


def classify_regimes(closes: Sequence[float]) -> list[str | None]:
    """Label each day UP, DOWN or RANGE from its own close and MA60.

    A day is UP only when close is above MA60 and MA60 is above its value 10
    days earlier. DOWN is the mirror. Everything else that has both averages
    is RANGE. Earlier days stay None. The three labels do not overlap.
    """
    labels: list[str | None] = [None] * len(closes)
    for index in range(69, len(closes)):
        average = _mean(closes[index - 59:index + 1])
        previous_average = _mean(closes[index - 69:index - 9])
        close = closes[index]
        if close > average and average > previous_average:
            labels[index] = "UP"
        elif close < average and average < previous_average:
            labels[index] = "DOWN"
        else:
            labels[index] = "RANGE"
    return labels


def _kdj(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> dict[str, float] | None:
    if len(closes) < 9:
        return None
    k_values, d_values, j_values = kdj_series(highs, lows, closes)
    return {"k": k_values[-1], "d": d_values[-1], "j": j_values[-1]}


def _none_sections() -> dict[str, dict[str, None]]:
    return {
        "macd": {"ema12": None, "ema26": None, "dif": None, "dea": None, "histogram": None},
        "kdj": {"k": None, "d": None, "j": None},
        "rsi": {"rsi14": None},
        "moving_averages": {"ma5": None, "ma10": None, "ma20": None, "ma60": None},
        "bias20": {"value": None},
        "bollinger": {"middle": None, "upper": None, "lower": None, "stddev": None},
        "volume": {"ma20": None, "ratio20": None, "contraction": None},
    }


def _weekly_context(
    bars: Sequence[DailyBar], closes: Sequence[float],
) -> dict[str, object]:
    weekly: list[tuple[date, float]] = []
    for bar, close in zip(bars, closes):
        week = bar.trading_date.isocalendar()
        key = (week.year, week.week)
        if weekly and weekly[-1][0].isocalendar()[:2] == key:
            weekly[-1] = (bar.trading_date, close)
        else:
            weekly.append((bar.trading_date, close))
    values = [value for _, value in weekly]
    status = "DATA_UNAVAILABLE" if not values else (
        "READY" if len(values) >= 20 else "WARMUP"
    )
    return {
        "status": status,
        "bar_count": len(values),
        "close": values[-1] if values else None,
        "ma10": _latest_average(values, 10),
        "ma20": _latest_average(values, 20),
        "as_of_trading_date": weekly[-1][0].isoformat() if weekly else None,
    }


def calculate_indicator_snapshot(
    bars: Sequence[DailyBar],
    *,
    minimum_bars: int = DEFAULT_MINIMUM_BARS,
) -> dict[str, Any]:
    """Return the latest MACD/KDJ/RSI snapshot for completed bars.

    Values are computed from adjusted OHLC data.  A warmup snapshot still
    exposes values that have enough local history, while its status prevents
    callers from treating it as a fully validated strategy sample.
    """
    if type(minimum_bars) is not int or minimum_bars <= 0:
        raise IndicatorInputError("minimum_bars must be a positive integer")
    try:
        materialized = tuple(bars)
    except Exception as error:
        raise IndicatorInputError("bars must be a sequence") from error

    previous_date: date | None = None
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    symbol: str | None = None
    for bar in materialized:
        if type(bar) is not DailyBar:
            raise IndicatorInputError("bars must contain DailyBar values")
        if bar.is_final is not True:
            raise IndicatorInputError("bars must contain completed DailyBar values")
        if symbol is None:
            symbol = bar.symbol
        elif bar.symbol != symbol:
            raise IndicatorInputError("bars must contain one symbol")
        if previous_date is not None and bar.trading_date <= previous_date:
            raise IndicatorInputError("trading_date must be strictly ascending")
        previous_date = bar.trading_date
        high = _number(bar.adjusted_high, "adjusted_high")
        low = _number(bar.adjusted_low, "adjusted_low")
        close = _number(bar.adjusted_close, "adjusted_close")
        if low > high or close < low or close > high:
            raise IndicatorInputError("adjusted OHLC is inconsistent")
        highs.append(high)
        lows.append(low)
        closes.append(close)

    sections: dict[str, dict[str, float | None]] = _none_sections()  # type: ignore[assignment]
    status = "DATA_UNAVAILABLE" if not closes else (
        "READY" if len(closes) >= minimum_bars else "WARMUP"
    )
    reason = None if status == "READY" else (
        "NO_COMPLETED_BARS" if not closes else "INSUFFICIENT_COMPLETED_BARS"
    )
    if closes:
        ema12 = _ema(closes, 12)[-1]
        ema26_series = _ema(closes, 26)
        dif_series = [short - long for short, long in zip(_ema(closes, 12), ema26_series)]
        dea_series = _ema(dif_series, 9)
        dif = dif_series[-1]
        dea = dea_series[-1]
        sections["macd"] = {
            "ema12": ema12,
            "ema26": ema26_series[-1],
            "dif": dif,
            "dea": dea,
            "histogram": 2.0 * (dif - dea),
        }
        kdj = _kdj(highs, lows, closes)
        if kdj is not None:
            sections["kdj"] = kdj
        rsi = _rsi_wilder(closes)
        if rsi is not None:
            sections["rsi"] = {"rsi14": rsi}
        sections["moving_averages"] = {
            "ma5": _latest_average(closes, 5),
            "ma10": _latest_average(closes, 10),
            "ma20": _latest_average(closes, 20),
            "ma60": _latest_average(closes, 60),
        }
        ma20 = sections["moving_averages"]["ma20"]
        if ma20 is not None:
            sections["bias20"] = {"value": (closes[-1] / ma20 - 1.0) * 100.0}
        window = closes[-20:]
        middle = _mean(window) if len(window) == 20 else None
        stddev = _stddev(window) if middle is not None else None
        sections["bollinger"] = {
            "middle": middle,
            "upper": middle + 2.0 * stddev
            if middle is not None and stddev is not None else None,
            "lower": middle - 2.0 * stddev
            if middle is not None and stddev is not None else None,
            "stddev": stddev,
        }
        volumes = [float(bar.volume) for bar in materialized]
        volume_ma20 = _latest_average(volumes, 20)
        sections["volume"] = {
            "ma20": volume_ma20,
            "ratio20": volumes[-1] / volume_ma20
            if volume_ma20 is not None and volume_ma20 > 0.0 else None,
            "contraction": (
                volumes[-1] < volume_ma20
                if volume_ma20 is not None and volume_ma20 > 0.0 else None
            ),
        }

    return {
        "schema_version": INDICATOR_SCHEMA_VERSION,
        "symbol": symbol,
        "as_of_trading_date": previous_date.isoformat() if previous_date else None,
        "bar_count": len(closes),
        "minimum_bars": minimum_bars,
        "status": status,
        "reason": reason,
        "weekly": _weekly_context(materialized, closes),
        **sections,
    }


def _validated_price_series(
    bars: Sequence[DailyBar],
) -> tuple[tuple[DailyBar, ...], str | None, list[float], list[float], list[float]]:
    """Validate completed bars and return adjusted OHLC series."""
    try:
        materialized = tuple(bars)
    except Exception as error:
        raise IndicatorInputError("bars must be a sequence") from error
    previous_date: date | None = None
    symbol: str | None = None
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    for bar in materialized:
        if type(bar) is not DailyBar:
            raise IndicatorInputError("bars must contain DailyBar values")
        if bar.is_final is not True:
            raise IndicatorInputError("bars must contain completed DailyBar values")
        if symbol is None:
            symbol = bar.symbol
        elif bar.symbol != symbol:
            raise IndicatorInputError("bars must contain one symbol")
        if previous_date is not None and bar.trading_date <= previous_date:
            raise IndicatorInputError("trading_date must be strictly ascending")
        previous_date = bar.trading_date
        high = _number(bar.adjusted_high, "adjusted_high")
        low = _number(bar.adjusted_low, "adjusted_low")
        close = _number(bar.adjusted_close, "adjusted_close")
        if low > high or close < low or close > high:
            raise IndicatorInputError("adjusted OHLC is inconsistent")
        highs.append(high)
        lows.append(low)
        closes.append(close)
    return materialized, symbol, closes, highs, lows


def _direction(previous: float | None, current: float | None) -> str | None:
    if previous is None or current is None:
        return None
    if current > previous:
        return "UP"
    if current < previous:
        return "DOWN"
    return "FLAT"


def _macd_cross_age(
    difs: Sequence[float], deas: Sequence[float], index: int,
) -> tuple[str | None, int | None]:
    for candidate in range(index, 0, -1):
        prior = difs[candidate - 1] - deas[candidate - 1]
        current = difs[candidate] - deas[candidate]
        if prior <= 0.0 < current:
            return "BULLISH", index - candidate
        if prior >= 0.0 > current:
            return "BEARISH", index - candidate
    return None, None


def _histogram_rising_days(histograms: Sequence[float], index: int) -> int:
    count = 0
    for candidate in range(index, 0, -1):
        if histograms[candidate] <= histograms[candidate - 1]:
            break
        count += 1
    return count


def _kdj_cross_age(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
) -> int | None:
    if len(closes) < 9:
        return None
    # Build the same 9/3/3 stream once.  Re-slicing and re-running _kdj for
    # every prefix made a three-point context quadratic in history length.
    values: list[tuple[float, float]] = []
    k = 50.0
    d = 50.0
    for index, close in enumerate(closes):
        start = max(0, index - 8)
        window_high = max(highs[start:index + 1])
        window_low = min(lows[start:index + 1])
        span = window_high - window_low
        rsv = 50.0 if span <= 0.0 else (close - window_low) / span * 100.0
        k = (2.0 * k + rsv) / 3.0
        d = (2.0 * d + k) / 3.0
        values.append((k, d))
    for index in range(len(values) - 1, 0, -1):
        current = values[index]
        previous = values[index - 1]
        prior_diff = previous[0] - previous[1]
        current_diff = current[0] - current[1]
        if prior_diff <= 0.0 < current_diff or prior_diff >= 0.0 > current_diff:
            return len(values) - 1 - index
    return None


def calculate_indicator_context(
    bars: Sequence[DailyBar], *, lookback: int = 3,
) -> dict[str, object]:
    """Return latest and recent completed-bar indicator evidence.

    Every point is computed from bars up to that point, so the trailing context
    cannot accidentally use a later completed bar as look-ahead information.
    Values use adjusted OHLC and retain the same MACD/KDJ/RSI formulas as the
    existing snapshot function.
    """
    if type(lookback) is not int or lookback <= 0:
        raise IndicatorInputError("lookback must be a positive integer")
    materialized, symbol, closes, highs, lows = _validated_price_series(bars)
    count = len(materialized)
    status = "DATA_UNAVAILABLE" if not count else (
        "READY" if count >= DEFAULT_MINIMUM_BARS else "WARMUP"
    )
    reason = None if status == "READY" else (
        "NO_COMPLETED_BARS" if not count else "INSUFFICIENT_COMPLETED_BARS"
    )
    if not count:
        return {
            "status": status,
            "reason": reason,
            "indicator_version": INDICATOR_VERSION,
            "price_basis": "adjusted_ohlc",
            "data_version": "sha256:" + sha256(b"[]").hexdigest(),
            "symbol": symbol,
            "as_of_trading_date": None,
            "bar_count": 0,
            "recent": [],
            "latest": None,
        }

    points: list[dict[str, object]] = []
    start_index = max(0, count - lookback)
    for index in range(start_index, count):
        bar = materialized[index]
        prefix_closes = closes[:index + 1]
        prefix_highs = highs[:index + 1]
        prefix_lows = lows[:index + 1]
        prefix_snapshot = calculate_indicator_snapshot(prefix_closes and materialized[:index + 1])
        macd = prefix_snapshot["macd"]
        kdj = prefix_snapshot["kdj"]
        rsi_section = prefix_snapshot["rsi"]
        ma = prefix_snapshot["moving_averages"]
        ema12_series = _ema(prefix_closes, 12)
        ema26_series = _ema(prefix_closes, 26)
        difs = [short - long for short, long in zip(ema12_series, ema26_series)]
        deas = _ema(difs, 9)
        histograms = [2.0 * (dif - dea) for dif, dea in zip(difs, deas)]
        cross, cross_age = _macd_cross_age(difs, deas, index)
        previous_histogram = histograms[index - 1] if index else None
        previous_rsi = _rsi_wilder(prefix_closes[:-1]) if index else None
        rsi = rsi_section["rsi14"]
        kdj_k = kdj["k"]
        kdj_d = kdj["d"]
        kdj_cross_age = _kdj_cross_age(prefix_highs, prefix_lows, prefix_closes)
        rsi_values = [
            _rsi_wilder(prefix_closes[:candidate + 1])
            for candidate in range(max(0, index - 12), index + 1)
        ]
        rsi_rising_days = 0
        for candidate in range(len(rsi_values) - 1, 0, -1):
            current_rsi = rsi_values[candidate]
            prior_rsi = rsi_values[candidate - 1]
            if current_rsi is None or prior_rsi is None or current_rsi <= prior_rsi:
                break
            rsi_rising_days += 1
        ma20 = ma["ma20"]
        ma60 = ma["ma60"]
        ma20_prior = _latest_average(prefix_closes[:-5], 20) if index >= 5 else None
        ma60_prior = _latest_average(prefix_closes[:-10], 60) if index >= 10 else None
        close_ma20_atr_distance = None
        true_ranges: list[float] = []
        if ma20 is not None and index >= 1:
            for candidate in range(max(1, index - 13), index + 1):
                prior_close = prefix_closes[candidate - 1]
                true_ranges.append(max(
                    prefix_highs[candidate] - prefix_lows[candidate],
                    abs(prefix_highs[candidate] - prior_close),
                    abs(prefix_lows[candidate] - prior_close),
                ))
            atr = _mean(true_ranges)
            if math.isfinite(atr) and atr > 0.0:
                close_ma20_atr_distance = abs(prefix_closes[-1] - ma20) / atr
        point: dict[str, object] = {
            "trading_date": bar.trading_date.isoformat(),
            "as_of_trading_date": bar.trading_date.isoformat(),
            "bar_count": index + 1,
            "macd": macd,
            "kdj": kdj,
            "rsi": rsi_section,
            "moving_averages": ma,
            "bias20": prefix_snapshot["bias20"],
            "bollinger": prefix_snapshot["bollinger"],
            "volume": prefix_snapshot["volume"],
            "weekly": prefix_snapshot["weekly"],
            "macd_cross": cross,
            "macd_cross_age": cross_age,
            "macd_histogram_rising_days": _histogram_rising_days(histograms, index),
            "macd_histogram_direction": _direction(previous_histogram, histograms[index]),
            "rsi_direction": _direction(previous_rsi, rsi),
            "macd_histogram_previous_1": histograms[index - 1] if index >= 1 else None,
            "macd_histogram_previous_2": histograms[index - 2] if index >= 2 else None,
            "macd_dif_above_dea": macd["dif"] > macd["dea"],
            "rsi_previous_1": previous_rsi,
            "rsi_previous_2": (
                _rsi_wilder(prefix_closes[:-2]) if index >= 2 else None
            ),
            "rsi_rising_days": rsi_rising_days,
            "kdj_k_above_d": (
                kdj_k > kdj_d if kdj_k is not None and kdj_d is not None else None
            ),
            "kdj_cross_age": kdj_cross_age,
            "ma20_slope_pct_5d": (
                ((ma20 / ma20_prior) - 1.0) * 100.0
                if ma20 is not None and ma20_prior not in (None, 0.0) else None
            ),
            "ma60_slope_pct_10d": (
                ((ma60 / ma60_prior) - 1.0) * 100.0
                if ma60 is not None and ma60_prior not in (None, 0.0) else None
            ),
            "close_ma20_atr_distance": close_ma20_atr_distance,
        }
        points.append(point)

    recent = points
    latest = points[-1]
    return {
        "status": status,
        "reason": reason,
        "indicator_version": INDICATOR_VERSION,
        "price_basis": "adjusted_ohlc",
        "data_version": "sha256:" + sha256(json.dumps(
            [bar.to_dict() for bar in materialized],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest(),
        "symbol": symbol,
        "as_of_trading_date": materialized[-1].trading_date.isoformat(),
        "bar_count": count,
        "recent": recent,
        "latest": latest,
    }


__all__ = [
    "DEFAULT_MINIMUM_BARS",
    "INDICATOR_VERSION",
    "INDICATOR_SCHEMA_VERSION",
    "IndicatorInputError",
    "calculate_indicator_context",
    "calculate_indicator_snapshot",
]
