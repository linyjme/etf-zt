"""Strict, versioned configuration for the swing monitor."""

from __future__ import annotations

from dataclasses import dataclass, fields
import json
import math
from pathlib import Path

from etf_rotation.etf_metadata import EtfMetadataStore, MetadataError


class SwingConfigError(ValueError):
    """Raised when swing-monitor configuration cannot be loaded or validated."""


@dataclass(frozen=True)
class SwingWatchItem:
    symbol: str
    enabled: bool


@dataclass(frozen=True)
class SwingStrategyConfig:
    schema_version: int
    strategy_version: str
    minimum_daily_bars: int
    short_ma_days: int
    long_ma_days: int
    long_ma_slope_lookback: int
    atr_days: int
    pullback_atr_distance: float
    entry_zone_atr_half_width: float
    anti_chase_atr_distance: float
    breakout_days: int
    add_profit_r: float
    reduce_profit_r: float
    risk_per_trade: float
    max_symbol_weight: float
    max_equity_weight: float
    max_portfolio_risk: float
    initial_stop_atr: float
    trailing_stop_atr: float
    cooldown_days: int
    walk_forward_train_days: int
    walk_forward_test_days: int
    walk_forward_step_days: int
    max_volume_participation: float


_WATCHLIST_KEYS = frozenset(("schema_version", "items"))
_WATCH_ITEM_KEYS = frozenset(("symbol", "enabled"))
_STRATEGY_KEYS = frozenset(field.name for field in fields(SwingStrategyConfig))
_DAY_FIELDS = (
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
_RATE_FIELDS = (
    "risk_per_trade",
    "max_symbol_weight",
    "max_equity_weight",
    "max_portfolio_risk",
    "max_volume_participation",
)
_POSITIVE_NUMBER_FIELDS = (
    "pullback_atr_distance",
    "entry_zone_atr_half_width",
    "anti_chase_atr_distance",
    "add_profit_r",
    "reduce_profit_r",
    "initial_stop_atr",
    "trailing_stop_atr",
)


def _load_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SwingConfigError(f"{label}读取失败: {error}") from error
    if not isinstance(payload, dict):
        raise SwingConfigError(f"{label}必须是JSON对象")
    return payload


def _require_exact_keys(
    payload: dict[str, object], expected: frozenset[str], label: str,
) -> None:
    actual = frozenset(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise SwingConfigError(
            f"{label}字段无效: missing={missing}, extra={extra}",
        )


def load_watchlist(
    path: Path, metadata_path: Path,
) -> tuple[SwingWatchItem, ...]:
    """Load a strict swing watchlist and cross-check its ETF symbols."""
    payload = _load_json_object(path, "波段监控列表")
    _require_exact_keys(payload, _WATCHLIST_KEYS, "波段监控列表")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise SwingConfigError("波段监控列表schema_version必须是整数1")
    raw_items = payload["items"]
    if not isinstance(raw_items, list):
        raise SwingConfigError("波段监控列表items必须是数组")

    try:
        metadata = EtfMetadataStore(metadata_path).load()
    except MetadataError as error:
        raise SwingConfigError(
            f"波段监控列表ETF元数据加载失败: {error}",
        ) from error
    result: list[SwingWatchItem] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise SwingConfigError("波段监控项必须是对象")
        _require_exact_keys(raw_item, _WATCH_ITEM_KEYS, "波段监控项")
        symbol = raw_item["symbol"]
        if (
            not isinstance(symbol, str)
            or len(symbol) != 6
            or not symbol.isascii()
            or not symbol.isdigit()
        ):
            raise SwingConfigError("波段监控代码必须是6位ASCII数字")
        enabled = raw_item["enabled"]
        if type(enabled) is not bool:
            raise SwingConfigError(f"{symbol}.enabled必须是布尔值")
        if symbol in seen:
            raise SwingConfigError(f"波段监控代码重复: {symbol}")
        if symbol not in metadata:
            raise SwingConfigError(f"波段监控代码缺少ETF元数据: {symbol}")
        seen.add(symbol)
        result.append(SwingWatchItem(symbol, enabled))
    return tuple(result)


def _positive_integer(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise SwingConfigError(f"{field}必须是正整数")
    return value


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SwingConfigError(f"{field}必须是有限数字")
    try:
        number = float(value)
    except OverflowError as error:
        raise SwingConfigError(f"{field}必须是有限数字") from error
    if not math.isfinite(number):
        raise SwingConfigError(f"{field}必须是有限数字")
    return number


def _positive_number(value: object, field: str) -> float:
    number = _finite_number(value, field)
    if number <= 0:
        raise SwingConfigError(f"{field}必须是正数")
    return number


def _rate(value: object, field: str) -> float:
    number = _finite_number(value, field)
    if not 0 < number <= 1:
        raise SwingConfigError(f"{field}必须在(0,1]范围内")
    return number


def load_strategy(path: Path) -> SwingStrategyConfig:
    """Load a strict SWING_V1 strategy configuration."""
    payload = _load_json_object(path, "波段策略配置")
    _require_exact_keys(payload, _STRATEGY_KEYS, "波段策略配置")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise SwingConfigError("波段策略schema_version必须是整数1")
    if payload["strategy_version"] != "SWING_V1":
        raise SwingConfigError("波段策略strategy_version必须是SWING_V1")

    values = dict(payload)
    for field in _DAY_FIELDS:
        values[field] = _positive_integer(values[field], field)
    for field in _RATE_FIELDS:
        values[field] = _rate(values[field], field)
    for field in _POSITIVE_NUMBER_FIELDS:
        values[field] = _positive_number(values[field], field)

    if values["short_ma_days"] >= values["long_ma_days"]:
        raise SwingConfigError("short_ma_days必须小于long_ma_days")
    if values["minimum_daily_bars"] < (
        values["long_ma_days"] + values["long_ma_slope_lookback"]
    ):
        raise SwingConfigError(
            "minimum_daily_bars必须覆盖long_ma_days和long_ma_slope_lookback",
        )
    if values["minimum_daily_bars"] < values["breakout_days"] + 1:
        raise SwingConfigError("minimum_daily_bars必须至少为breakout_days + 1")
    if values["minimum_daily_bars"] < values["atr_days"] + 1:
        raise SwingConfigError("minimum_daily_bars必须至少为atr_days + 1")
    if values["entry_zone_atr_half_width"] > values["pullback_atr_distance"]:
        raise SwingConfigError("entry_zone_atr_half_width不能超过pullback_atr_distance")
    if values["pullback_atr_distance"] >= values["anti_chase_atr_distance"]:
        raise SwingConfigError("pullback_atr_distance必须小于anti_chase_atr_distance")
    if values["add_profit_r"] >= values["reduce_profit_r"]:
        raise SwingConfigError("add_profit_r必须小于reduce_profit_r")
    if values["initial_stop_atr"] > values["trailing_stop_atr"]:
        raise SwingConfigError("initial_stop_atr不能超过trailing_stop_atr")
    if values["walk_forward_test_days"] > values["walk_forward_train_days"]:
        raise SwingConfigError("walk_forward_test_days不能超过walk_forward_train_days")
    if values["walk_forward_step_days"] > values["walk_forward_test_days"]:
        raise SwingConfigError("walk_forward_step_days不能超过walk_forward_test_days")
    if values["walk_forward_train_days"] < values["minimum_daily_bars"]:
        raise SwingConfigError(
            "walk_forward_train_days不能小于minimum_daily_bars",
        )
    if values["risk_per_trade"] > values["max_portfolio_risk"]:
        raise SwingConfigError("risk_per_trade不能超过max_portfolio_risk")
    if values["max_portfolio_risk"] > values["max_equity_weight"]:
        raise SwingConfigError("max_portfolio_risk不能超过max_equity_weight")
    if values["max_symbol_weight"] > values["max_equity_weight"]:
        raise SwingConfigError("max_symbol_weight不能超过max_equity_weight")

    return SwingStrategyConfig(**values)
