from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
import math
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .constants import DELAYED_MAX_AGE_SECONDS, REALTIME_MAX_AGE_SECONDS
from .etf_metadata import TradingMetadata
from .t_monitor import MarketDataError, QuotePoint


SHANGHAI = ZoneInfo("Asia/Shanghai")
_MORNING_START = time(9, 30)
_MORNING_END = time(11, 30)
_AFTERNOON_START = time(13, 0)
_AFTERNOON_END = time(15, 0)


@dataclass(frozen=True)
class MarketHealth:
    status: str
    quote_age_seconds: float | None
    reason: str


def finalized_points(
    points: Sequence[QuotePoint], observed_at: datetime,
) -> tuple[QuotePoint, ...]:
    observed = _aware_time(observed_at, "观测时间")
    result: list[QuotePoint] = []
    for item in points:
        timestamp = _aware_time(item.timestamp, "分钟时间")
        if observed >= timestamp + timedelta(minutes=1):
            result.append(item)
    return tuple(result)


class MarketHealthClassifier:
    def __init__(self, closed_dates: set[date] | None = None):
        self.closed_dates = frozenset(closed_dates or ())

    def classify(
        self,
        now: datetime,
        last_quote_at: datetime | None,
        error: str | None,
    ) -> MarketHealth:
        local = _aware_time(now, "当前时间").astimezone(SHANGHAI)
        local_time = local.time().replace(tzinfo=None)
        if (
            local.weekday() >= 5
            or local.date() in self.closed_dates
            or local_time < _MORNING_START
            or local_time > _AFTERNOON_END
        ):
            return MarketHealth("CLOSED", None, "非连续交易时段")
        if _MORNING_END < local_time < _AFTERNOON_START:
            return MarketHealth("LUNCH_BREAK", None, "午间休市")
        if error is not None:
            return MarketHealth("OUTAGE", None, error or "行情采集失败")
        if last_quote_at is None:
            return MarketHealth("OUTAGE", None, "缺少当日行情")
        quote_time = _aware_time(last_quote_at, "行情时间").astimezone(SHANGHAI)
        age = max(0.0, (local - quote_time).total_seconds())
        if age <= REALTIME_MAX_AGE_SECONDS:
            return MarketHealth("REALTIME", age, "行情实时")
        if age <= DELAYED_MAX_AGE_SECONDS:
            return MarketHealth("DELAYED", age, "行情延迟")
        return MarketHealth("OUTAGE", age, "行情断流")


class MarketDataValidator:
    def __init__(self, trading: TradingMetadata):
        self.trading = trading

    def validate_point(self, point: QuotePoint, previous_close: float) -> None:
        timestamp = _aware_time(point.timestamp, "分钟时间").astimezone(SHANGHAI)
        local_time = timestamp.time().replace(tzinfo=None)
        if timestamp.weekday() >= 5 or not (
            _MORNING_START <= local_time <= _MORNING_END
            or _AFTERNOON_START <= local_time <= _AFTERNOON_END
        ):
            raise MarketDataError("分钟时间不在连续交易时段")

        close = self._positive(point.price, "收盘价")
        average = self._positive(point.average_price, "均价")
        open_price = self._positive(point.open, "开盘价")
        high = self._positive(point.high, "最高价")
        low = self._positive(point.low, "最低价")
        previous = self._positive(previous_close, "昨收")
        if low > min(open_price, close) or high < max(open_price, close) or low > high:
            raise MarketDataError("OHLC关系无效")

        tick = self.trading.price_tick
        lower_limit = previous * (1.0 - self.trading.price_limit_pct) - tick
        upper_limit = previous * (1.0 + self.trading.price_limit_pct) + tick
        epsilon = max(1.0, previous) * 1e-12
        for value in (close, average, open_price, high, low):
            if value < lower_limit - epsilon or value > upper_limit + epsilon:
                raise MarketDataError("价格越过涨跌幅限制")

        volume = self._nonnegative(point.volume, "成交量")
        amount = self._nonnegative(point.amount, "成交额")
        if (volume == 0.0) != (amount == 0.0):
            raise MarketDataError("成交量和成交额必须同时为零或同时非零")
        if volume > 0.0:
            implied_price = amount / (volume * self.trading.volume_unit_shares)
            if implied_price < low - tick - epsilon or implied_price > high + tick + epsilon:
                raise MarketDataError("量价校验失败")

    @staticmethod
    def _positive(value: object, label: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise MarketDataError(f"{label}必须是有限正数")
        return float(value)

    @staticmethod
    def _nonnegative(value: object, label: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise MarketDataError(f"{label}必须是有限非负数")
        return float(value)


def load_closed_dates(path: Path) -> set[date]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MarketDataError(f"交易日历读取失败: {error}") from error
    if (
        not isinstance(payload, Mapping)
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
    ):
        raise MarketDataError("交易日历schema_version必须为1")
    values = payload.get("closed_dates")
    if not isinstance(values, list):
        raise MarketDataError("交易日历closed_dates必须是数组")
    result: set[date] = set()
    for value in values:
        if not isinstance(value, str):
            raise MarketDataError("交易日历休市日期必须是ISO日期")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise MarketDataError(f"交易日历休市日期无效: {value}") from error
        result.add(parsed)
    return result


def _aware_time(value: Any, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MarketDataError(f"{label}必须带时区")
    return value
