from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .constants import (
    RANGE_CONFIRMATIONS,
    RANGE_MAX_ER,
    RANGE_MAX_ONE_SIDE_RATIO,
    RANGE_MAX_VWAP_SLOPE,
    RANGE_WINDOW_MINUTES,
    TREND_CONFIRMATIONS,
    TREND_MIN_ER,
    TREND_MIN_ONE_SIDE_RATIO,
    TREND_MIN_VWAP_SLOPE,
    VWAP_NEUTRAL_BAND_PCT,
)

if TYPE_CHECKING:
    from .t_monitor import QuotePoint


SHANGHAI = ZoneInfo("Asia/Shanghai")
_MORNING_START = time(9, 30)
_MORNING_END = time(11, 30)
_AFTERNOON_START = time(13, 0)
_AFTERNOON_END = time(15, 0)


@dataclass(frozen=True)
class RegimeResult:
    state: str
    label: str
    sample_count: int
    path_efficiency: float | None
    one_side_ratio: float | None
    vwap_crossings: int | None
    vwap_slope: float | None
    above_vwap_count: int
    below_vwap_count: int
    range_confirmation_count: int
    trend_confirmation_count: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _WindowMetrics:
    path_efficiency: float
    one_side_ratio: float
    vwap_crossings: int
    vwap_slope: float
    above_vwap_count: int
    below_vwap_count: int
    range_ok: bool
    trend_direction: int
    range_reasons: tuple[str, ...]
    trend_reasons: tuple[str, ...]


class RegimeDetector:
    def evaluate(self, points: Sequence[QuotePoint]) -> RegimeResult:
        segment = current_continuous_segment(points)
        windows = [
            segment[end - RANGE_WINDOW_MINUTES:end]
            for end in range(RANGE_WINDOW_MINUTES, len(segment) + 1)
        ]
        if not windows:
            return _uncertain_result(len(segment), "INSUFFICIENT_SAMPLES")

        metrics = [self._metrics(window) for window in windows]
        current = metrics[-1]
        range_count = _trailing_count(metrics, lambda item: item.range_ok)
        trend_direction = current.trend_direction
        trend_count = _trailing_count(
            metrics,
            lambda item: trend_direction != 0 and item.trend_direction == trend_direction,
        )

        if range_count >= RANGE_CONFIRMATIONS:
            return _result_from(
                current,
                "RANGE",
                "震荡日，可等待反转确认后做T",
                range_count,
                0,
                ("RANGE_CONFIRMED",),
            )
        if trend_count >= TREND_CONFIRMATIONS:
            state = "UPTREND" if trend_direction > 0 else "DOWNTREND"
            label = "上涨趋势日，禁止逆势高抛" if trend_direction > 0 else "下跌趋势日，禁止逆势低吸"
            return _result_from(
                current,
                state,
                label,
                0,
                trend_count,
                (f"{state}_CONFIRMED",),
            )

        if current.range_ok:
            reasons = ("RANGE_CONFIRMATION_BELOW_3",)
        elif trend_direction != 0:
            state = "UPTREND" if trend_direction > 0 else "DOWNTREND"
            reasons = (f"{state}_CONFIRMATION_BELOW_2",)
        else:
            reasons = current.range_reasons + current.trend_reasons
        return _result_from(
            current,
            "UNCERTAIN",
            "状态未确认，暂停做T",
            range_count,
            trend_count,
            reasons,
        )

    def _metrics(self, window: Sequence[QuotePoint]) -> _WindowMetrics:
        prices = [point.price for point in window]
        averages = [point.average_price for point in window]
        path = sum(abs(current - previous) for previous, current in zip(prices, prices[1:]))
        path_efficiency = abs(prices[-1] - prices[0]) / path if path > 0 else 0.0
        average_baseline = sum(averages) / len(averages)
        vwap_slope = (averages[-1] - averages[0]) / average_baseline

        sides = [_vwap_side(price, average) for price, average in zip(prices, averages)]
        above_count = sides.count(1)
        below_count = sides.count(-1)
        one_side_ratio = max(above_count, below_count) / len(window)
        crossings = _side_to_side_crossings(sides)

        range_reasons: list[str] = []
        if crossings < 2:
            range_reasons.append("RANGE_VWAP_CROSSINGS_BELOW_2")
        if above_count < 2:
            range_reasons.append("RANGE_ABOVE_VWAP_COUNT_BELOW_2")
        if below_count < 2:
            range_reasons.append("RANGE_BELOW_VWAP_COUNT_BELOW_2")
        if one_side_ratio > RANGE_MAX_ONE_SIDE_RATIO:
            range_reasons.append("RANGE_ONE_SIDE_RATIO_ABOVE_0_70")
        if path_efficiency > RANGE_MAX_ER:
            range_reasons.append("RANGE_PATH_EFFICIENCY_ABOVE_0_30")
        if abs(vwap_slope) > RANGE_MAX_VWAP_SLOPE:
            range_reasons.append("RANGE_VWAP_SLOPE_ABOVE_0_001")

        price_direction = _direction(prices[-1] - prices[0])
        same_side_ratio = (
            above_count / len(window)
            if price_direction > 0
            else below_count / len(window)
            if price_direction < 0
            else 0.0
        )
        high_low_progress = _high_low_progress(window, price_direction)
        trend_reasons: list[str] = []
        if path_efficiency < TREND_MIN_ER:
            trend_reasons.append("TREND_PATH_EFFICIENCY_BELOW_0_55")
        if abs(vwap_slope) < TREND_MIN_VWAP_SLOPE:
            trend_reasons.append("TREND_VWAP_SLOPE_BELOW_0_001")
        if price_direction == 0:
            trend_reasons.append("TREND_PRICE_DIRECTION_FLAT")
        elif vwap_slope * price_direction <= 0:
            trend_reasons.append("TREND_PRICE_VWAP_DIRECTION_MISMATCH")
        if same_side_ratio < TREND_MIN_ONE_SIDE_RATIO:
            trend_reasons.append("TREND_ONE_SIDE_RATIO_BELOW_0_80")
        if not high_low_progress:
            trend_reasons.append("TREND_HIGH_LOW_NOT_ADVANCING")

        return _WindowMetrics(
            path_efficiency=path_efficiency,
            one_side_ratio=one_side_ratio,
            vwap_crossings=crossings,
            vwap_slope=vwap_slope,
            above_vwap_count=above_count,
            below_vwap_count=below_count,
            range_ok=not range_reasons,
            trend_direction=price_direction if not trend_reasons else 0,
            range_reasons=tuple(range_reasons),
            trend_reasons=tuple(trend_reasons),
        )


def current_continuous_segment(points: Sequence[QuotePoint]) -> tuple[QuotePoint, ...]:
    if not points:
        return ()
    latest = points[-1]
    session = _session_key(latest.timestamp)
    if session is None:
        return ()
    start = len(points) - 1
    for index in range(len(points) - 2, -1, -1):
        previous = points[index]
        current = points[index + 1]
        if _session_key(previous.timestamp) != session:
            break
        if current.timestamp - previous.timestamp != timedelta(minutes=1):
            break
        start = index
    return tuple(points[start:])


def _session_key(timestamp: datetime) -> tuple[object, str] | None:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return None
    local = timestamp.astimezone(SHANGHAI)
    local_time = local.time().replace(tzinfo=None)
    if _MORNING_START <= local_time <= _MORNING_END:
        return local.date(), "MORNING"
    if _AFTERNOON_START <= local_time <= _AFTERNOON_END:
        return local.date(), "AFTERNOON"
    return None


def _vwap_side(price: float, average: float) -> int:
    deviation = price / average - 1
    if deviation > VWAP_NEUTRAL_BAND_PCT:
        return 1
    if deviation < -VWAP_NEUTRAL_BAND_PCT:
        return -1
    return 0


def _side_to_side_crossings(sides: Sequence[int]) -> int:
    crossings = 0
    previous_side = 0
    for side in sides:
        if side == 0:
            continue
        if previous_side != 0 and side != previous_side:
            crossings += 1
        previous_side = side
    return crossings


def _high_low_progress(window: Sequence[QuotePoint], direction: int) -> bool:
    if direction == 0:
        return False
    midpoint = len(window) // 2
    first = window[:midpoint]
    second = window[midpoint:]
    first_high = max(point.high if point.high is not None else point.price for point in first)
    second_high = max(point.high if point.high is not None else point.price for point in second)
    first_low = min(point.low if point.low is not None else point.price for point in first)
    second_low = min(point.low if point.low is not None else point.price for point in second)
    if direction > 0:
        return second_high > first_high and second_low > first_low
    return second_high < first_high and second_low < first_low


def _direction(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _trailing_count(
    metrics: Sequence[_WindowMetrics],
    predicate: Callable[[_WindowMetrics], bool],
) -> int:
    count = 0
    for item in reversed(metrics):
        if not predicate(item):
            break
        count += 1
    return count


def _uncertain_result(sample_count: int, reason: str) -> RegimeResult:
    return RegimeResult(
        state="UNCERTAIN",
        label="样本不足，暂停做T",
        sample_count=sample_count,
        path_efficiency=None,
        one_side_ratio=None,
        vwap_crossings=None,
        vwap_slope=None,
        above_vwap_count=0,
        below_vwap_count=0,
        range_confirmation_count=0,
        trend_confirmation_count=0,
        reasons=(reason,),
    )


def _result_from(
    metrics: _WindowMetrics,
    state: str,
    label: str,
    range_count: int,
    trend_count: int,
    reasons: tuple[str, ...],
) -> RegimeResult:
    return RegimeResult(
        state=state,
        label=label,
        sample_count=RANGE_WINDOW_MINUTES,
        path_efficiency=round(metrics.path_efficiency, 8),
        one_side_ratio=round(metrics.one_side_ratio, 8),
        vwap_crossings=metrics.vwap_crossings,
        vwap_slope=round(metrics.vwap_slope, 8),
        above_vwap_count=metrics.above_vwap_count,
        below_vwap_count=metrics.below_vwap_count,
        range_confirmation_count=range_count,
        trend_confirmation_count=trend_count,
        reasons=reasons,
    )
