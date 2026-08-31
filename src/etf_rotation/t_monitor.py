from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .constants import DEFAULT_GRID_WIDTH_PCT
from .regime import RegimeDetector
from .t_strategy import (
    CURRENT_STRATEGY_VERSION,
    CandidateContext,
    TStrategy,
    fast_rise_grids,
)


_CURRENT_CANDIDATE_ACTIONS = frozenset({"BUY_CANDIDATE", "SELL_CANDIDATE"})


def _is_current_candidate(item: object) -> bool:
    return (
        isinstance(item, dict)
        and item.get("action") in _CURRENT_CANDIDATE_ACTIONS
        and item.get("strategy_version") == CURRENT_STRATEGY_VERSION
    )


class QuoteHistoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        # market_data imports the public Quote models from this module, so the
        # compatibility facade resolves the concrete store only after import.
        from .market_data import MinuteHistoryStore

        self._store = MinuteHistoryStore(self.path)

    @property
    def daily_root(self) -> Path:
        return self._store.daily_root

    def append(self, quotes: Mapping[str, Quote]) -> None:
        self._store.append_legacy(quotes)

    def upsert(self, quotes: Mapping[str, Quote], metadata: Mapping[str, Any]) -> int:
        return self._store.upsert(quotes, metadata)

    def available_dates(self) -> list[str]:
        return self._store.available_dates()

    def query(self, trading_date: str, symbol: str | None = None) -> list[dict[str, Any]]:
        return self._store.query(trading_date, symbol)

    def merge(self, quotes: Mapping[str, Quote]) -> Mapping[str, Quote]:
        return self._store.merge(quotes)


class MarketDataError(ValueError):
    pass


class AlertHistoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    @property
    def daily_root(self) -> Path:
        return self.path.parent / "history"

    def append(self, payload: Mapping[str, Any]) -> None:
        records = payload.get("items", [])
        if not isinstance(records, list):
            return
        with self._lock:
            existing = self._read()
            self._write_daily(existing)
            keys = {(item.get("symbol"), item.get("timestamp"), item.get("action"), item.get("strategy_version")) for item in existing}
            pending = []
            for item in records:
                if not _is_current_candidate(item):
                    continue
                key = (item.get("symbol"), item.get("timestamp"), item.get("action"), item.get("strategy_version"))
                if key in keys:
                    continue
                event = dict(item)
                event["event_type"] = "MONITOR_SIGNAL"
                event["recorded_at"] = payload.get("generated_at")
                event["trading_date"] = str(item.get("timestamp", ""))[:10]
                pending.append(event)
                keys.add(key)
            if not pending:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                for item in pending:
                    handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._write_daily(pending)

    def append_candidates(self, payload: Mapping[str, Any]) -> None:
        self.append(payload)

    def _write_daily(self, records: Sequence[Mapping[str, Any]]) -> None:
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for item in records:
            if not _is_current_candidate(item):
                continue
            trading_date = str(item.get("trading_date") or "")
            if trading_date:
                grouped.setdefault(trading_date, []).append(item)
        for trading_date, items in grouped.items():
            daily_path = self.daily_root / trading_date / "alerts.jsonl"
            existing = self._read_path(daily_path)
            keys = {(item.get("symbol"), item.get("timestamp"), item.get("action"), item.get("strategy_version")) for item in existing}
            pending = [item for item in items if (item.get("symbol"), item.get("timestamp"), item.get("action"), item.get("strategy_version")) not in keys]
            if not pending:
                continue
            daily_path.parent.mkdir(parents=True, exist_ok=True)
            with daily_path.open("a", encoding="utf-8", newline="") as handle:
                for item in pending:
                    handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def _read_path(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            return [item for item in records if _is_current_candidate(item)]
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise MarketDataError(f"提示历史读取失败: {error}") from error

    def query(self, trading_date: str | None = None, symbol: str | None = None, action: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        result = []
        for item in reversed(self._read()):
            if trading_date and item.get("trading_date") != trading_date:
                continue
            if symbol and item.get("symbol") != symbol:
                continue
            if action and item.get("action") != action:
                continue
            result.append(item)
            if len(result) >= limit:
                break
        return result

    def _read(self) -> list[dict[str, Any]]:
        return self._read_path(self.path)


@dataclass(frozen=True)
class QuotePoint:
    timestamp: datetime
    price: float
    average_price: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float = 0.0
    amount: float = 0.0
    previous_close: float | None = None


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: float
    average_price: float
    previous_close: float
    timestamp: datetime
    points: tuple[QuotePoint, ...]
    observed_at: datetime
    source: str


@dataclass(frozen=True)
class WatchItem:
    symbol: str
    name: str
    grid_width_pct: float
    enabled: bool = True
    base_shares: int | None = None
    t_capacity_shares: int | None = None
    base_notional_cny: float | None = None
    t_capacity_ratio: float | None = None


@dataclass(frozen=True)
class MonitorSignal:
    symbol: str
    name: str
    status: str
    action: str
    label: str
    price: float | None
    average_price: float | None
    previous_close: float | None
    upper_grid_price: float | None
    lower_grid_price: float | None
    grid_width_pct: float
    change_pct: float | None
    white_yellow_deviation_grids: float | None
    previous_close_distance_grids: float | None
    fast_rise_grids: float | None
    timestamp: datetime | None
    safety: str = "MONITOR_ONLY"
    strategy_version: str = "T_V1"
    deviation_pct: float | None = None
    minute_sigma: float | None = None
    deviation_z: float | None = None
    trend_state: str = "UNCERTAIN"
    trend_strength: float | None = None
    volume_ratio: float | None = None
    regime_state: str = "UNCERTAIN"
    regime_label: str = "状态未知，暂停做T"
    regime_score: float | None = None
    path_efficiency: float | None = None
    one_side_ratio: float | None = None
    vwap_crossings: int | None = None
    vwap_slope: float | None = None
    above_vwap_count: int = 0
    below_vwap_count: int = 0
    range_confirmation_count: int = 0
    trend_confirmation_count: int = 0
    regime_sample_count: int = 0
    regime_reasons: tuple[str, ...] = ()
    trade_markers: tuple[dict[str, Any], ...] = ()
    signal_level: str = "NONE"
    blocked_reasons: tuple[str, ...] = ()
    health_status: str = "UNKNOWN"
    health_reason: str = "行情状态未知"
    expected_gross_edge_pct: float | None = None
    round_trip_cost_pct: float | None = None
    expected_net_edge_pct: float | None = None


@dataclass(frozen=True)
class MonitorSnapshot:
    generated_at: datetime
    signals: tuple[MonitorSignal, ...]
    quotes: Mapping[str, Quote]
    errors: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "quotes", MappingProxyType(dict(self.quotes)))


class JsonQuoteAdapter:
    def __init__(self, allow_fixture_defaults: bool = False):
        self.allow_fixture_defaults = allow_fixture_defaults

    def load(self, path: Path) -> Mapping[str, Quote]:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MarketDataError(f"行情文件读取失败: {error}") from error
        return self.parse(raw)

    def parse(self, raw: Any) -> Mapping[str, Quote]:
        records = self._records(raw)
        quotes: dict[str, Quote] = {}
        for record in records:
            quote = self._quote(record)
            if quote.symbol in quotes:
                raise MarketDataError(f"行情代码重复: {quote.symbol}")
            quotes[quote.symbol] = quote
        return MappingProxyType(quotes)

    def _records(self, raw: Any) -> Sequence[Mapping[str, Any]]:
        if isinstance(raw, list):
            records = raw
        elif isinstance(raw, dict) and isinstance(raw.get("quotes"), list):
            records = raw["quotes"]
        elif isinstance(raw, dict):
            records = []
            for symbol, value in raw.items():
                if not isinstance(value, dict):
                    raise MarketDataError("行情映射值必须是对象")
                records.append({"symbol": symbol, **value})
        else:
            raise MarketDataError("行情 JSON 必须是数组、quotes 数组或代码映射")
        if not all(isinstance(record, dict) for record in records):
            raise MarketDataError("每条行情必须是对象")
        return records

    def _quote(self, raw: Mapping[str, Any]) -> Quote:
        symbol = self._text(raw, "symbol")
        name = str(raw.get("name") or symbol).strip()
        price = self._positive(raw.get("price"), f"{symbol}.price")
        average_price = self._positive(
            raw.get("average_price", raw.get("avg_price")),
            f"{symbol}.average_price",
        )
        previous_close = self._positive(
            raw.get("previous_close", raw.get("pre_close")),
            f"{symbol}.previous_close",
        )
        timestamp = self._time(raw.get("timestamp"), f"{symbol}.timestamp")
        observed_value = raw.get("observed_at", raw.get("collected_at"))
        if observed_value is None:
            if not self.allow_fixture_defaults:
                raise MarketDataError(f"{symbol}.observed_at或collected_at不能为空")
            observed_at = timestamp + timedelta(minutes=1)
        else:
            observed_at = self._time(observed_value, f"{symbol}.observed_at")
        source_value = raw.get("source")
        if not isinstance(source_value, str) or not source_value.strip():
            if not self.allow_fixture_defaults:
                raise MarketDataError(f"{symbol}.source必须是非空字符串")
            source = "TEST_FIXTURE"
        else:
            source = source_value.strip()
        points_raw = raw.get("points", raw.get("timeline", []))
        if not isinstance(points_raw, list):
            raise MarketDataError(f"{symbol}.points 必须是数组")
        points = tuple(self._point(symbol, value) for value in points_raw)
        if any(left.timestamp >= right.timestamp for left, right in zip(points, points[1:])):
            raise MarketDataError(f"{symbol}.points 时间必须严格递增")
        if not points:
            points = (QuotePoint(timestamp, price, average_price),)
        if (
            points[-1].timestamp != timestamp
            or not math.isclose(points[-1].price, price)
            or not math.isclose(points[-1].average_price, average_price)
        ):
            raise MarketDataError(f"{symbol}.points 最新值必须匹配行情时间、实时价和均价")
        return Quote(
            symbol, name, price, average_price, previous_close, timestamp, points,
            observed_at, source,
        )

    def _point(self, symbol: str, raw: Any) -> QuotePoint:
        if isinstance(raw, list) and len(raw) in (3, 8):
            timestamp, price, average_price = raw[:3]
            open_price = raw[3] if len(raw) == 8 else None
            high = raw[4] if len(raw) == 8 else None
            low = raw[5] if len(raw) == 8 else None
            volume = raw[6] if len(raw) == 8 else 0.0
            amount = raw[7] if len(raw) == 8 else 0.0
            raw = {"open": open_price, "high": high, "low": low, "volume": volume, "amount": amount}
        elif isinstance(raw, dict):
            timestamp = raw.get("timestamp", raw.get("time"))
            price = raw.get("price", raw.get("close"))
            average_price = raw.get("average_price", raw.get("avg_price"))
        else:
            raise MarketDataError(f"{symbol}.points 条目格式错误")
        return QuotePoint(
            self._time(timestamp, f"{symbol}.points.timestamp"),
            self._positive(price, f"{symbol}.points.price"),
            self._positive(average_price, f"{symbol}.points.average_price"),
            self._optional_price(raw.get("open")) if isinstance(raw, dict) else None,
            self._optional_price(raw.get("high")) if isinstance(raw, dict) else None,
            self._optional_price(raw.get("low")) if isinstance(raw, dict) else None,
            self._nonnegative(raw.get("volume", 0.0)) if isinstance(raw, dict) else 0.0,
            self._nonnegative(raw.get("amount", 0.0)) if isinstance(raw, dict) else 0.0,
        )

    def _text(self, raw: Mapping[str, Any], field: str) -> str:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise MarketDataError(f"{field} 必须是非空字符串")
        return value.strip()

    def _positive(self, value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MarketDataError(f"{field} 必须是数字")
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise MarketDataError(f"{field} 必须是有限正数")
        return number

    def _optional_price(self, value: Any) -> float | None:
        if value is None:
            return None
        return self._positive(value, "OHLC")

    def _nonnegative(self, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MarketDataError("成交量和成交额必须是数字")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise MarketDataError("成交量和成交额必须是有限非负数")
        return number

    def _time(self, value: Any, field: str) -> datetime:
        if not isinstance(value, str):
            raise MarketDataError(f"{field} 必须是 ISO 时间")
        try:
            result = datetime.fromisoformat(value)
        except ValueError as error:
            raise MarketDataError(f"{field} 必须是 ISO 时间") from error
        if result.tzinfo is None or result.utcoffset() is None:
            raise MarketDataError(f"{field} 必须带时区")
        return result


def load_watchlist(path: Path) -> tuple[WatchItem, ...]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MarketDataError(f"监控列表读取失败: {error}") from error
    records = raw.get("watchlist") if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        raise MarketDataError("监控列表必须是数组或 watchlist 数组")
    result: list[WatchItem] = []
    symbols: set[str] = set()
    for raw_item in records:
        if not isinstance(raw_item, dict):
            raise MarketDataError("监控项必须是对象")
        symbol = str(raw_item.get("symbol", "")).strip()
        name = str(raw_item.get("name") or symbol).strip()
        if not symbol or symbol in symbols:
            raise MarketDataError(f"监控代码为空或重复: {symbol}")
        grid_width = raw_item.get("grid_width_pct", DEFAULT_GRID_WIDTH_PCT)
        if isinstance(grid_width, bool) or not isinstance(grid_width, (int, float)):
            raise MarketDataError(f"{symbol}.grid_width_pct 必须是数字")
        grid_width_pct = float(grid_width)
        if not math.isfinite(grid_width_pct) or not 0 < grid_width_pct < 1:
            raise MarketDataError(f"{symbol}.grid_width_pct 超出范围")
        enabled = raw_item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise MarketDataError(f"{symbol}.enabled 必须是布尔值")
        share_overrides: dict[str, int | None] = {}
        for field in ("base_shares", "t_capacity_shares"):
            value = raw_item.get(field)
            if value is not None and (type(value) is not int or value < 0):
                raise MarketDataError(f"{symbol}.{field} 必须是非负整数")
            share_overrides[field] = value
        base_notional = raw_item.get("base_notional_cny")
        if base_notional is not None and (
            isinstance(base_notional, bool)
            or not isinstance(base_notional, (int, float))
            or not math.isfinite(base_notional)
            or base_notional <= 0
        ):
            raise MarketDataError(f"{symbol}.base_notional_cny 必须是有限正数")
        capacity_ratio = raw_item.get("t_capacity_ratio")
        if capacity_ratio is not None and (
            isinstance(capacity_ratio, bool)
            or not isinstance(capacity_ratio, (int, float))
            or not math.isfinite(capacity_ratio)
            or not 0 < capacity_ratio <= 1
        ):
            raise MarketDataError(f"{symbol}.t_capacity_ratio 必须在(0,1]范围内")
        result.append(WatchItem(
            symbol,
            name,
            grid_width_pct,
            enabled,
            share_overrides["base_shares"],
            share_overrides["t_capacity_shares"],
            float(base_notional) if base_notional is not None else None,
            float(capacity_ratio) if capacity_ratio is not None else None,
        ))
        symbols.add(symbol)
    return tuple(result)


class TMonitorEngine:
    def __init__(self, health_classifier: Any | None = None):
        if health_classifier is None:
            from .market_data import MarketHealthClassifier

            health_classifier = MarketHealthClassifier()
        self.health_classifier = health_classifier

    def evaluate(
        self,
        watchlist: Sequence[WatchItem],
        quotes: Mapping[str, Quote],
        generated_at: datetime | None = None,
        health: Any | None = None,
    ) -> MonitorSnapshot:
        from .market_data import finalized_points

        current = generated_at or datetime.now().astimezone()
        signals: list[MonitorSignal] = []
        errors: list[str] = []
        for item in watchlist:
            if not item.enabled:
                continue
            quote = quotes.get(item.symbol)
            if quote is None:
                signals.append(MonitorSignal(
                    item.symbol, item.name, "MISSING_QUOTE", "UNAVAILABLE",
                    "缺少行情", None, None, None, None, None,
                    item.grid_width_pct, None, None, None, None, None,
                    health_status="OUTAGE",
                    health_reason="缺少行情",
                    blocked_reasons=("MISSING_QUOTE",),
                ))
                continue

            completed = finalized_points(quote.points, quote.observed_at)
            if isinstance(health, Mapping):
                item_health = health.get(item.symbol)
            else:
                item_health = health
            if item_health is None:
                item_health = self.health_classifier.classify(
                    current, completed[-1].timestamp if completed else None, None,
                )
            regime = RegimeDetector().evaluate(completed)
            decision_quote = self._decision_quote(quote, completed)
            strategy_quote = decision_quote or Quote(
                quote.symbol,
                quote.name,
                quote.price,
                quote.average_price,
                quote.previous_close,
                quote.timestamp,
                (),
                quote.observed_at,
                quote.source,
            )
            decision = TStrategy().evaluate(CandidateContext(
                strategy_quote, regime.state, item_health, item.grid_width_pct,
            ))

            latest = completed[-1] if completed else None
            market_values_valid = (
                latest is not None
                and self._finite_positive(latest.price)
                and self._finite_positive(latest.average_price)
                and self._finite_positive(quote.previous_close)
            )
            raw_grid_size = (
                latest.average_price * item.grid_width_pct
                if market_values_valid and self._finite_positive(item.grid_width_pct)
                else None
            )
            grid_size = (
                raw_grid_size if self._finite_positive(raw_grid_size) else None
            )
            change = latest.price / quote.previous_close - 1 if market_values_valid else None
            upper_grid_price = latest.average_price + grid_size if grid_size is not None else None
            lower_grid_price = latest.average_price - grid_size if grid_size is not None else None
            white_yellow_deviation_grids = (
                round(abs(latest.price - latest.average_price) / grid_size, 6)
                if grid_size is not None else None
            )
            previous_close_distance_grids = (
                round(abs(latest.price - quote.previous_close) / grid_size, 6)
                if grid_size is not None else None
            )
            rise_grids = (
                fast_rise_grids(decision_quote, grid_size)
                if decision_quote is not None and grid_size is not None else 0.0
            )
            decision_values_valid = (
                decision_quote is not None
                and all(
                    self._finite_positive(value)
                    for point in decision_quote.points
                    for value in (point.price, point.average_price)
                )
            )
            signals.append(MonitorSignal(
                item.symbol, item.name or quote.name, "OK", decision.action,
                decision.label,
                latest.price if market_values_valid else None,
                latest.average_price if market_values_valid else None,
                quote.previous_close if market_values_valid else None,
                upper_grid_price, lower_grid_price, item.grid_width_pct, change,
                white_yellow_deviation_grids, previous_close_distance_grids,
                rise_grids, latest.timestamp if latest is not None else None,
                health_status=item_health.status,
                health_reason=item_health.reason,
                expected_gross_edge_pct=decision.expected_gross_edge_pct,
                round_trip_cost_pct=decision.round_trip_cost_pct,
                expected_net_edge_pct=decision.expected_net_edge_pct,
                strategy_version=CURRENT_STRATEGY_VERSION,
                deviation_pct=(
                    round(latest.price / latest.average_price - 1, 8)
                    if market_values_valid else None
                ),
                minute_sigma=(
                    self._minute_sigma(decision_quote)
                    if decision_values_valid else None
                ),
                trend_state=(
                    self._trend_state(decision_quote)
                    if decision_values_valid else "UNCERTAIN"
                ),
                trend_strength=(
                    self._trend_strength(decision_quote)
                    if decision_values_valid else None
                ),
                volume_ratio=(
                    self._volume_ratio(decision_quote)
                    if decision_values_valid else None
                ),
                regime_state=regime.state,
                regime_label=regime.label,
                path_efficiency=regime.path_efficiency,
                one_side_ratio=regime.one_side_ratio,
                vwap_crossings=regime.vwap_crossings,
                vwap_slope=regime.vwap_slope,
                above_vwap_count=regime.above_vwap_count,
                below_vwap_count=regime.below_vwap_count,
                range_confirmation_count=regime.range_confirmation_count,
                trend_confirmation_count=regime.trend_confirmation_count,
                regime_sample_count=regime.sample_count,
                regime_reasons=regime.reasons,
                trade_markers=(
                    self._trade_markers(decision_quote, regime.state)
                    if decision_values_valid else ()
                ),
                signal_level="NONE",
                blocked_reasons=decision.blocked_reasons,
            ))
        return MonitorSnapshot(current, tuple(signals), quotes, tuple(errors))

    @staticmethod
    def _decision_quote(quote: Quote, points: Sequence[QuotePoint]) -> Quote | None:
        if not points:
            return None
        latest = points[-1]
        return Quote(
            quote.symbol,
            quote.name,
            latest.price,
            latest.average_price,
            quote.previous_close,
            latest.timestamp,
            tuple(points),
            quote.observed_at,
            quote.source,
        )

    @staticmethod
    def _finite_positive(value: object) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and value > 0
        )

    def _minute_sigma(self, quote: Quote) -> float | None:
        returns = []
        for previous, current in zip(quote.points, quote.points[1:]):
            if previous.price > 0:
                returns.append(current.price / previous.price - 1)
        if len(returns) < 2:
            return None
        mean = sum(returns) / len(returns)
        return round(math.sqrt(sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)), 8)

    def _trend_strength(self, quote: Quote) -> float | None:
        if len(quote.points) < 2 or quote.average_price <= 0:
            return None
        return round((quote.points[-1].average_price - quote.points[0].average_price) / quote.average_price, 8)

    def _trend_state(self, quote: Quote) -> str:
        strength = self._trend_strength(quote)
        if strength is None:
            return "UNCERTAIN"
        if strength >= 0.001:
            return "UPTREND"
        if strength <= -0.001:
            return "DOWNTREND"
        return "RANGE"

    def _trade_markers(self, quote: Quote, regime_state: str) -> tuple[dict[str, Any], ...]:
        if regime_state != "RANGE" or len(quote.points) < 3:
            return ()
        points = quote.points[-20:]
        markers: list[dict[str, Any]] = []
        neutral = 0.0002
        for previous, current in zip(points, points[1:]):
            previous_deviation = previous.price / previous.average_price - 1
            current_deviation = current.price / current.average_price - 1
            if previous_deviation <= -0.01 and current_deviation > previous_deviation and current_deviation <= -neutral:
                markers.append({"type": "B", "timestamp": current.timestamp.isoformat(), "price": current.price})
            elif previous_deviation >= 0.01 and current_deviation < previous_deviation and current_deviation >= neutral:
                markers.append({"type": "S", "timestamp": current.timestamp.isoformat(), "price": current.price})
        return tuple(markers)

    def _volume_ratio(self, quote: Quote) -> float | None:
        volumes = [point.volume for point in quote.points[:-1] if point.volume > 0]
        latest = quote.points[-1].volume if quote.points else 0.0
        if not volumes or latest <= 0:
            return None
        return round(latest / (sum(volumes) / len(volumes)), 6)

def snapshot_to_dict(snapshot: MonitorSnapshot) -> dict[str, Any]:
    return {
        "generated_at": snapshot.generated_at.isoformat(),
        "mode": "MONITOR_ONLY",
        "auto_trade": False,
        "errors": list(snapshot.errors),
        "items": [
            {
                "symbol": signal.symbol,
                "name": signal.name,
                "status": signal.status,
                "action": signal.action,
                "label": signal.label,
                "price": signal.price,
                "average_price": signal.average_price,
                "previous_close": signal.previous_close,
                "upper_grid_price": signal.upper_grid_price,
                "lower_grid_price": signal.lower_grid_price,
                "grid_width_pct": signal.grid_width_pct,
                "change_pct": signal.change_pct,
                "white_yellow_deviation_grids": signal.white_yellow_deviation_grids,
                "previous_close_distance_grids": signal.previous_close_distance_grids,
                "fast_rise_grids": signal.fast_rise_grids,
                "timestamp": signal.timestamp.isoformat() if signal.timestamp else None,
                "health_status": signal.health_status,
                "health_reason": signal.health_reason,
                "expected_gross_edge_pct": signal.expected_gross_edge_pct,
                "round_trip_cost_pct": signal.round_trip_cost_pct,
                "expected_net_edge_pct": signal.expected_net_edge_pct,
                "safety": signal.safety,
                "strategy_version": signal.strategy_version,
                "deviation_pct": signal.deviation_pct,
                "minute_sigma": signal.minute_sigma,
                "deviation_z": signal.deviation_z,
                "trend_state": signal.trend_state,
                "trend_strength": signal.trend_strength,
                "volume_ratio": signal.volume_ratio,
                "regime_state": signal.regime_state,
                "regime_label": signal.regime_label,
                "regime_score": signal.regime_score,
                "path_efficiency": signal.path_efficiency,
                "one_side_ratio": signal.one_side_ratio,
                "vwap_crossings": signal.vwap_crossings,
                "vwap_slope": signal.vwap_slope,
                "above_vwap_count": signal.above_vwap_count,
                "below_vwap_count": signal.below_vwap_count,
                "range_confirmation_count": signal.range_confirmation_count,
                "trend_confirmation_count": signal.trend_confirmation_count,
                "regime_sample_count": signal.regime_sample_count,
                "regime_reasons": list(signal.regime_reasons),
                "trade_markers": list(signal.trade_markers),
                "signal_level": signal.signal_level,
                "blocked_reasons": list(signal.blocked_reasons),
                "points": [
                    {
                        "timestamp": point.timestamp.isoformat(),
                        "price": point.price,
                        "average_price": point.average_price,
                        "open": point.open,
                        "high": point.high,
                        "low": point.low,
                        "volume": point.volume,
                        "amount": point.amount,
                    }
                    for point in snapshot.quotes[signal.symbol].points
                ] if signal.symbol in snapshot.quotes else [],
            }
            for signal in snapshot.signals
        ],
    }
