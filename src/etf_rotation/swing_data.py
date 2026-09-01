"""Strict completed daily-bar schema and cross-record validation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import math
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .etf_metadata import EtfMetadata


SHANGHAI = ZoneInfo("Asia/Shanghai")
_FINAL_OBSERVATION_TIME = time(15, 10)
_ULP_MULTIPLIER = 4.0
_PRICE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "previous_close",
    "adjusted_open",
    "adjusted_high",
    "adjusted_low",
    "adjusted_close",
)
_RAW_OHLC_FIELDS = ("open", "high", "low", "close")
_ADJUSTED_OHLC_FIELDS = (
    "adjusted_open",
    "adjusted_high",
    "adjusted_low",
    "adjusted_close",
)
_DAILY_BAR_KEYS = frozenset((
    "schema_version",
    "symbol",
    "trading_date",
    "observed_at",
    "source",
    *_PRICE_FIELDS,
    "volume",
    "amount",
    "is_final",
))


class SwingDataError(ValueError):
    """Raised when swing-monitor daily data is malformed or inconsistent."""


@dataclass(frozen=True)
class DailyBar:
    schema_version: int
    symbol: str
    trading_date: date
    observed_at: datetime
    source: str
    open: float
    high: float
    low: float
    close: float
    previous_close: float
    volume: float
    amount: float
    adjusted_open: float
    adjusted_high: float
    adjusted_low: float
    adjusted_close: float
    is_final: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DailyBar:
        """Parse one exact schema-v1 completed daily bar."""
        if not isinstance(value, Mapping):
            raise SwingDataError("日线记录必须是映射")
        try:
            payload = dict(value)
        except Exception as error:
            raise SwingDataError(f"日线记录映射读取失败: {error}") from error

        if any(type(key) is not str for key in payload):
            raise SwingDataError("日线记录字段名必须是字符串")
        actual_keys = frozenset(payload)
        if actual_keys != _DAILY_BAR_KEYS:
            missing = sorted(_DAILY_BAR_KEYS - actual_keys)
            extra = sorted(actual_keys - _DAILY_BAR_KEYS)
            raise SwingDataError(
                f"日线记录字段无效: missing={missing}, extra={extra}",
            )
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise SwingDataError("日线schema_version必须是整数1")

        symbol = payload["symbol"]
        if (
            not isinstance(symbol, str)
            or len(symbol) != 6
            or not symbol.isascii()
            or not symbol.isdigit()
        ):
            raise SwingDataError("日线symbol必须是6位ASCII数字")
        trading_date = _iso_date(payload["trading_date"])
        observed_at = _iso_datetime(payload["observed_at"])
        source = payload["source"]
        if not isinstance(source, str) or not source.strip():
            raise SwingDataError("日线source不能为空")

        numbers = {
            field: _positive_number(payload[field], field)
            for field in _PRICE_FIELDS
        }
        volume = _nonnegative_number(payload["volume"], "volume")
        amount = _nonnegative_number(payload["amount"], "amount")
        is_final = payload["is_final"]
        if type(is_final) is not bool:
            raise SwingDataError("日线is_final必须是布尔值")
        if not is_final:
            raise SwingDataError("日线is_final必须为true")

        _validate_ohlc(
            numbers["open"], numbers["high"], numbers["low"], numbers["close"],
            "raw OHLC",
        )
        _validate_ohlc(
            numbers["adjusted_open"],
            numbers["adjusted_high"],
            numbers["adjusted_low"],
            numbers["adjusted_close"],
            "adjusted OHLC",
        )
        _validate_adjustment_scale(numbers)
        _validate_observation_time(trading_date, observed_at)

        return cls(
            schema_version=1,
            symbol=symbol,
            trading_date=trading_date,
            observed_at=observed_at,
            source=source,
            open=numbers["open"],
            high=numbers["high"],
            low=numbers["low"],
            close=numbers["close"],
            previous_close=numbers["previous_close"],
            volume=volume,
            amount=amount,
            adjusted_open=numbers["adjusted_open"],
            adjusted_high=numbers["adjusted_high"],
            adjusted_low=numbers["adjusted_low"],
            adjusted_close=numbers["adjusted_close"],
            is_final=True,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe primitives using ISO date/time strings."""
        observed_at = self.observed_at
        if observed_at.tzinfo is not None and observed_at.utcoffset() is not None:
            observed_at = observed_at.astimezone(SHANGHAI)
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "trading_date": self.trading_date.isoformat(),
            "observed_at": observed_at.isoformat(),
            "source": self.source,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "previous_close": self.previous_close,
            "volume": self.volume,
            "amount": self.amount,
            "adjusted_open": self.adjusted_open,
            "adjusted_high": self.adjusted_high,
            "adjusted_low": self.adjusted_low,
            "adjusted_close": self.adjusted_close,
            "is_final": self.is_final,
        }


class DailyBarValidator:
    """Validate completed daily bars against calendar and ETF metadata."""

    def __init__(self, closed_dates: Iterable[date]):
        try:
            closures = frozenset(closed_dates)
        except Exception as error:
            raise SwingDataError(f"休市日期集合无效: {error}") from error
        if any(type(item) is not date for item in closures):
            raise SwingDataError("休市日期必须是date")
        self.closed_dates = closures

    def validate(self, record: DailyBar, metadata: EtfMetadata) -> None:
        """Validate one record, rechecking its schema defensively."""
        normalized = self._normalize_record(record)
        trading = self._trading_metadata(normalized, metadata)

        if normalized.trading_date.weekday() >= 5:
            raise SwingDataError("日线trading_date不能是周末")
        if normalized.trading_date in self.closed_dates:
            raise SwingDataError("日线trading_date是配置休市日")

        tick = _positive_number(trading.price_tick, "price_tick")
        price_limit_pct = _positive_number(
            trading.price_limit_pct, "price_limit_pct",
        )
        previous = normalized.previous_close
        lower_factor = _safe_subtract(1.0, price_limit_pct, "涨跌幅下限因子")
        upper_factor = _safe_add(1.0, price_limit_pct, "涨跌幅上限因子")
        lower_limit = _safe_product(previous, lower_factor, "涨跌幅下限")
        upper_limit = _safe_product(previous, upper_factor, "涨跌幅上限")
        for field in _RAW_OHLC_FIELDS:
            value = getattr(normalized, field)
            tolerance = _one_tick_tolerance(
                tick, previous, value, lower_limit, upper_limit,
            )
            below = (
                value < lower_limit
                and _safe_subtract(lower_limit, value, "价格下限差") > tolerance
            )
            above = (
                value > upper_limit
                and _safe_subtract(value, upper_limit, "价格上限差") > tolerance
            )
            if below or above:
                raise SwingDataError(f"{field}价格越过涨跌幅限制")

        volume = normalized.volume
        amount = normalized.amount
        if (volume == 0.0) != (amount == 0.0):
            raise SwingDataError("成交量和成交额必须同时为零或同时非零")
        if volume > 0.0:
            unit_shares = trading.volume_unit_shares
            if type(unit_shares) is not int or unit_shares <= 0:
                raise SwingDataError("volume_unit_shares必须是正整数")
            low_denominator = _safe_product(
                _safe_add(volume, 1.0, "成交量上界"),
                unit_shares,
                "成交量单位换算上界",
            )
            high_denominator = max(
                _safe_product(
                    _safe_subtract(volume, 1.0, "成交量下界"),
                    unit_shares,
                    "成交量单位换算下界",
                ),
                1.0,
            )
            lowest_possible_price = _safe_divide(
                amount, low_denominator, "最低可能成交价",
            )
            highest_possible_price = _safe_divide(
                amount, high_denominator, "最高可能成交价",
            )
            low_tolerance = _one_tick_tolerance(
                tick, normalized.low, highest_possible_price,
            )
            high_tolerance = _one_tick_tolerance(
                tick, normalized.high, lowest_possible_price,
            )
            too_low = (
                highest_possible_price < normalized.low
                and _safe_subtract(
                    normalized.low, highest_possible_price, "量价下界差",
                ) > low_tolerance
            )
            too_high = (
                lowest_possible_price > normalized.high
                and _safe_subtract(
                    lowest_possible_price, normalized.high, "量价上界差",
                ) > high_tolerance
            )
            if too_low or too_high:
                raise SwingDataError("日线量价校验失败")

    def validate_sequence(
        self,
        records: Sequence[DailyBar],
        metadata_by_symbol: Mapping[str, EtfMetadata],
    ) -> None:
        """Validate bars already ordered strictly by ``(symbol, trading_date)``."""
        previous_key: tuple[str, date] | None = None
        previous_by_symbol: dict[str, DailyBar] = {}

        for record in records:
            normalized = self._normalize_record(record)
            key = (normalized.symbol, normalized.trading_date)
            if previous_key is not None:
                if key == previous_key:
                    raise SwingDataError(f"日线主键重复: {key[0]} {key[1]}")
                if key < previous_key:
                    raise SwingDataError("日线序列必须按(symbol, trading_date)严格排序")
            previous_key = key

            try:
                metadata = metadata_by_symbol.get(normalized.symbol)
            except Exception as error:
                raise SwingDataError(f"ETF元数据映射读取失败: {error}") from error
            if metadata is None:
                raise SwingDataError(f"缺少ETF元数据: {normalized.symbol}")
            self.validate(normalized, metadata)

            previous = previous_by_symbol.get(normalized.symbol)
            if previous is not None:
                self._validate_adjacent(previous, normalized, metadata)
            previous_by_symbol[normalized.symbol] = normalized

    @staticmethod
    def _normalize_record(record: DailyBar) -> DailyBar:
        if not isinstance(record, DailyBar):
            raise SwingDataError("日线record必须是DailyBar")
        try:
            payload = record.to_dict()
        except (AttributeError, TypeError, ValueError, OverflowError) as error:
            raise SwingDataError(f"日线record字段类型无效: {error}") from error
        return DailyBar.from_mapping(payload)

    def _validate_adjacent(
        self,
        previous: DailyBar,
        current: DailyBar,
        metadata: EtfMetadata,
    ) -> None:
        if current.trading_date <= previous.trading_date:
            raise SwingDataError("同一symbol的trading_date必须严格递增")
        candidate = previous.trading_date + timedelta(days=1)
        while candidate < current.trading_date:
            if candidate.weekday() < 5 and candidate not in self.closed_dates:
                raise SwingDataError(
                    f"{current.symbol}缺少交易日: {candidate.isoformat()}",
                )
            candidate += timedelta(days=1)

        tick = metadata.trading.price_tick
        tolerance = _one_tick_tolerance(
            tick, previous.close, current.previous_close,
        )
        difference = _safe_subtract(
            max(previous.close, current.previous_close),
            min(previous.close, current.previous_close),
            "昨收连续性差值",
        )
        if difference > tolerance:
            raise SwingDataError(
                f"{current.symbol}昨收与前一交易日收盘价不连续",
            )

    @staticmethod
    def _trading_metadata(record: DailyBar, metadata: EtfMetadata) -> Any:
        if not isinstance(metadata, EtfMetadata):
            raise SwingDataError("ETF元数据类型无效")
        if metadata.symbol != record.symbol:
            raise SwingDataError(
                f"ETF元数据symbol不匹配: {record.symbol}/{metadata.symbol}",
            )
        return metadata.trading


def _iso_date(value: object) -> date:
    if not isinstance(value, str):
        raise SwingDataError("日线trading_date必须是ISO日期")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise SwingDataError(f"日线trading_date无效: {value}") from error
    if parsed.isoformat() != value:
        raise SwingDataError(f"日线trading_date必须使用YYYY-MM-DD格式: {value}")
    return parsed


def _iso_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise SwingDataError("日线observed_at必须是ISO时间")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SwingDataError(f"日线observed_at无效: {value}") from error
    try:
        offset = parsed.utcoffset()
    except (ValueError, OverflowError) as error:
        raise SwingDataError("日线observed_at时区无效") from error
    if parsed.tzinfo is None or offset is None:
        raise SwingDataError("日线observed_at必须带时区")
    try:
        return parsed.astimezone(SHANGHAI)
    except Exception as error:
        raise SwingDataError("日线observed_at时区转换失败") from error


def _positive_number(value: object, field: str) -> float:
    number = _finite_number(value, field)
    if number <= 0:
        raise SwingDataError(f"日线{field}必须是有限正数")
    return number


def _nonnegative_number(value: object, field: str) -> float:
    number = _finite_number(value, field)
    if number < 0:
        raise SwingDataError(f"日线{field}必须是有限非负数")
    return number


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SwingDataError(f"日线{field}必须是有限数字")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise SwingDataError(f"日线{field}必须是有限数字") from error
    if not math.isfinite(number):
        raise SwingDataError(f"日线{field}必须是有限数字")
    return number


def _validate_ohlc(
    open_price: float,
    high: float,
    low: float,
    close: float,
    label: str,
) -> None:
    if low > min(open_price, close) or max(open_price, close) > high:
        raise SwingDataError(f"{label}关系无效")


def _validate_adjustment_scale(numbers: Mapping[str, float]) -> None:
    scale = numbers["adjusted_open"] / numbers["open"]
    if not math.isfinite(scale) or scale <= 0.0:
        raise SwingDataError("复权OHLC必须使用一致的有限正数复权比例")
    for raw_field, adjusted_field in zip(_RAW_OHLC_FIELDS, _ADJUSTED_OHLC_FIELDS):
        candidate = numbers[adjusted_field] / numbers[raw_field]
        if (
            not math.isfinite(candidate)
            or candidate <= 0.0
            or not math.isclose(candidate, scale, rel_tol=1e-6, abs_tol=0.0)
        ):
            raise SwingDataError("复权OHLC必须使用一致的正数复权比例")


def _validate_observation_time(trading_date: date, observed_at: datetime) -> None:
    try:
        local_observed = observed_at.astimezone(SHANGHAI)
    except (ValueError, OverflowError) as error:
        raise SwingDataError("日线observed_at时区无效") from error
    earliest = datetime.combine(
        trading_date,
        _FINAL_OBSERVATION_TIME,
        tzinfo=SHANGHAI,
    )
    if local_observed < earliest:
        raise SwingDataError("日线观测时间不得早于交易日15:10 Asia/Shanghai")


def _safe_add(left: object, right: object, label: str) -> float:
    return _safe_arithmetic(lambda: left + right, label)


def _safe_subtract(left: object, right: object, label: str) -> float:
    return _safe_arithmetic(lambda: left - right, label)


def _safe_product(left: object, right: object, label: str) -> float:
    return _safe_arithmetic(lambda: left * right, label)


def _safe_divide(left: object, right: object, label: str) -> float:
    return _safe_arithmetic(lambda: left / right, label)


def _safe_arithmetic(operation: Callable[[], object], label: str) -> float:
    try:
        result = operation()
        if isinstance(result, bool) or not isinstance(result, (int, float)):
            raise TypeError("结果不是数字")
        number = float(result)
    except Exception as error:
        raise SwingDataError(f"{label}数值运算失败: {error}") from error
    if not math.isfinite(number):
        raise SwingDataError(f"{label}数值运算结果必须有限")
    return number


def _one_tick_tolerance(tick: object, *operands: object) -> float:
    tick_value = _positive_number(tick, "price_tick")
    values = tuple(_finite_number(value, "价格边界") for value in operands)
    try:
        max_ulp = max(math.ulp(value) for value in values)
    except Exception as error:
        raise SwingDataError(f"价格边界精度计算失败: {error}") from error
    resolution = _safe_product(max_ulp, _ULP_MULTIPLIER, "价格边界ULP容差")
    if tick_value < resolution:
        raise SwingDataError("最小价位低于当前价格数量级的可表示精度")
    return _safe_add(tick_value, resolution, "一个最小价位容差")
