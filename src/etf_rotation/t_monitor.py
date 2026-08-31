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


class QuoteHistoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    @property
    def daily_root(self) -> Path:
        return self.path.parent / "history"

    def append(self, quotes: Mapping[str, Quote]) -> None:
        with self._lock:
            existing = self._read()
            self._write_daily(existing)
            records = {(item["symbol"], item["timestamp"]) for item in existing}
            pending = []
            for quote in quotes.values():
                for point in quote.points:
                    key = (quote.symbol, point.timestamp.isoformat())
                    if key not in records:
                        pending.append({
                            "schema_version": 2,
                            "symbol": quote.symbol,
                            "name": quote.name,
                            "previous_close": quote.previous_close,
                            "trading_date": point.timestamp.date().isoformat(),
                            "timestamp": point.timestamp.isoformat(),
                            "price": point.price,
                            "average_price": point.average_price,
                            "open": point.open,
                            "high": point.high,
                            "low": point.low,
                            "volume": point.volume,
                            "amount": point.amount,
                        })
                        records.add(key)
            if not pending:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                for record in pending:
                    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._write_daily(pending)

    def available_dates(self) -> list[str]:
        dates = {self._trading_date(item) for item in self._read()}
        if self.daily_root.exists():
            dates.update(path.parent.name for path in self.daily_root.glob("*/quotes.jsonl"))
        return sorted((date for date in dates if date), reverse=True)

    def query(self, trading_date: str, symbol: str | None = None) -> list[dict[str, Any]]:
        path = self.daily_root / trading_date / "quotes.jsonl"
        records = self._read_path(path) if path.exists() else [
            item for item in self._read() if self._trading_date(item) == trading_date
        ]
        return [item for item in records if symbol is None or item.get("symbol") == symbol]

    def _write_daily(self, records: Sequence[Mapping[str, Any]]) -> None:
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for record in records:
            trading_date = self._trading_date(record)
            if trading_date:
                grouped.setdefault(trading_date, []).append(record)
        for trading_date, daily_records in grouped.items():
            path = self.daily_root / trading_date / "quotes.jsonl"
            existing = self._read_path(path)
            keys = {(item.get("symbol"), item.get("timestamp")) for item in existing}
            pending = [item for item in daily_records if (item.get("symbol"), item.get("timestamp")) not in keys]
            if not pending:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="") as handle:
                for item in pending:
                    handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def _trading_date(self, record: Mapping[str, Any]) -> str:
        return str(record.get("trading_date") or record.get("timestamp", ""))[:10]

    def merge(self, quotes: Mapping[str, Quote]) -> Mapping[str, Quote]:
        history = self._records_by_symbol()
        result = {}
        for symbol, quote in quotes.items():
            points = {point.timestamp: point for point in quote.points}
            for record in history.get(symbol, ()):
                try:
                    point = QuotePoint(
                        datetime.fromisoformat(record["timestamp"]),
                        float(record["price"]), float(record["average_price"]),
                        self._optional_price(record.get("open")),
                        self._optional_price(record.get("high")),
                        self._optional_price(record.get("low")),
                        float(record.get("volume", 0.0)),
                        float(record.get("amount", 0.0)),
                    )
                except (KeyError, TypeError, ValueError) as error:
                    raise MarketDataError(f"历史行情记录无效: {error}") from error
                points.setdefault(point.timestamp, point)
            ordered = tuple(points[key] for key in sorted(points))
            result[symbol] = Quote(
                quote.symbol, quote.name, quote.price, quote.average_price,
                quote.previous_close, quote.timestamp, ordered,
                quote.observed_at, quote.source,
            )
        return MappingProxyType(result)

    def _optional_price(self, value: Any) -> float | None:
        if value is None:
            return None
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None

    def _records(self) -> set[tuple[str, str]]:
        return {(item["symbol"], item["timestamp"]) for item in self._read()}

    def _records_by_symbol(self) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for item in self._read():
            result.setdefault(item["symbol"], []).append(item)
        return result

    def _read(self) -> list[dict[str, Any]]:
        return self._read_path(self.path)

    def _read_path(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("历史行情每行必须是对象")
                    records.append(value)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise MarketDataError(f"历史行情读取失败: {error}") from error
        return records


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
                if not isinstance(item, dict) or item.get("action") not in {"BUY_REMINDER", "SELL_REMINDER", "OBSERVE"}:
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

    def _write_daily(self, records: Sequence[Mapping[str, Any]]) -> None:
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for item in records:
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
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
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
        if not self.path.exists():
            return []
        try:
            return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise MarketDataError(f"提示历史读取失败: {error}") from error


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
    trade_markers: tuple[dict[str, Any], ...] = ()
    signal_level: str = "NONE"
    blocked_reasons: tuple[str, ...] = ()


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
        result.append(WatchItem(symbol, name, grid_width_pct, enabled))
        symbols.add(symbol)
    return tuple(result)


class TMonitorEngine:
    def evaluate(
        self,
        watchlist: Sequence[WatchItem],
        quotes: Mapping[str, Quote],
        generated_at: datetime | None = None,
    ) -> MonitorSnapshot:
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
                ))
                continue
            change = quote.price / quote.previous_close - 1
            grid_size = quote.average_price * item.grid_width_pct
            upper_grid_price = quote.average_price + grid_size
            lower_grid_price = quote.average_price - grid_size
            white_yellow_deviation_grids = round(abs(quote.price - quote.average_price) / grid_size, 6)
            previous_close_distance_grids = round(abs(quote.price - quote.previous_close) / grid_size, 6)
            fast_rise_grids = self._fast_rise_grids(quote, grid_size)
            regime = self._regime(quote)
            regime_state, regime_label, regime_score, path_efficiency, one_side_ratio, vwap_crossings = regime
            if fast_rise_grids + 1e-9 >= 5:
                action, label = "OBSERVE", f"快速上冲 {fast_rise_grids:.2f} 格，优先观望"
            elif white_yellow_deviation_grids + 1e-9 >= 3 and previous_close_distance_grids + 1e-9 >= 5:
                if quote.price > quote.average_price:
                    action, label = "SELL_REMINDER", f"白黄偏离 {white_yellow_deviation_grids:.2f} 格，离昨收 {previous_close_distance_grids:.2f} 格，均值回归减仓提醒"
                elif quote.price < quote.average_price:
                    action, label = "BUY_REMINDER", f"白黄偏离 {white_yellow_deviation_grids:.2f} 格，离昨收 {previous_close_distance_grids:.2f} 格，均值回归回补提醒"
                else:
                    action, label = "WAIT", f"白黄偏离 {white_yellow_deviation_grids:.2f} 格，等待"
            else:
                action, label = "WAIT", f"白黄偏离 {white_yellow_deviation_grids:.2f} 格，等待"
            if regime_state == "UPTREND" and action == "SELL_REMINDER":
                action, label = "OBSERVE", f"上涨趋势日，屏蔽逆势高抛；{regime_label}"
            elif regime_state == "DOWNTREND" and action == "BUY_REMINDER":
                action, label = "OBSERVE", f"下跌趋势日，屏蔽逆势低吸；{regime_label}"
            signals.append(MonitorSignal(
                item.symbol, item.name or quote.name, "OK", action, label,
                quote.price, quote.average_price, quote.previous_close,
                upper_grid_price, lower_grid_price, item.grid_width_pct, change,
                white_yellow_deviation_grids, previous_close_distance_grids,
                fast_rise_grids, quote.timestamp,
                strategy_version="T_V2",
                deviation_pct=round(quote.price / quote.average_price - 1, 8),
                minute_sigma=self._minute_sigma(quote),
                trend_state=self._trend_state(quote),
                trend_strength=self._trend_strength(quote),
                volume_ratio=self._volume_ratio(quote),
                regime_state=regime_state,
                regime_label=regime_label,
                regime_score=regime_score,
                path_efficiency=path_efficiency,
                one_side_ratio=one_side_ratio,
                vwap_crossings=vwap_crossings,
                trade_markers=self._trade_markers(quote, regime_state),
                signal_level="GOLDEN" if action in {"BUY_REMINDER", "SELL_REMINDER"} else "NONE",
            ))
        current = generated_at or max(
            (quote.timestamp for quote in quotes.values()),
            default=datetime.now().astimezone(),
        )
        return MonitorSnapshot(current, tuple(signals), quotes, tuple(errors))

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

    def _regime(self, quote: Quote) -> tuple[str, str, float | None, float | None, float | None, int | None]:
        points = quote.points[-20:]
        if len(points) < 20:
            return "UNCERTAIN", "样本不足，暂停做T", None, None, None, None
        prices = [point.price for point in points]
        averages = [point.average_price for point in points]
        returns = [abs(current - previous) for previous, current in zip(prices, prices[1:])]
        path = sum(returns)
        efficiency = abs(prices[-1] - prices[0]) / path if path > 0 else 0.0
        baseline = sum(averages) / len(averages)
        slope = (averages[-1] - averages[0]) / baseline if baseline > 0 else 0.0
        upper = sum(price > average * 1.0002 for price, average in zip(prices, averages)) / len(points)
        lower = sum(price < average * 0.9998 for price, average in zip(prices, averages)) / len(points)
        one_side = max(upper, lower)
        crossings = sum(
            (prices[index - 1] - averages[index - 1]) * (prices[index] - averages[index]) < 0
            and abs(prices[index] - averages[index]) / averages[index] > 0.0002
            for index in range(1, len(points))
        )
        midpoint = len(points) // 2
        first_prices = prices[:midpoint]
        second_prices = prices[midpoint:]
        high_progress = max(second_prices) > max(first_prices) * 1.0003
        low_progress = min(second_prices) > min(first_prices) * 1.0003
        up_progress = high_progress and low_progress
        high_regression = max(second_prices) < max(first_prices) * 0.9997
        low_regression = min(second_prices) < min(first_prices) * 0.9997
        down_progress = high_regression and low_regression
        overlap_values = []
        for previous, current in zip(points, points[1:]):
            previous_low = previous.low if previous.low is not None else previous.price
            previous_high = previous.high if previous.high is not None else previous.price
            current_low = current.low if current.low is not None else current.price
            current_high = current.high if current.high is not None else current.price
            intersection = max(0.0, min(previous_high, current_high) - max(previous_low, current_low))
            union = max(previous_high, current_high) - min(previous_low, current_low)
            overlap_values.append(intersection / union if union > 0 else 1.0)
        candle_overlap = sum(value >= 0.5 for value in overlap_values) / len(overlap_values)
        range_high = max(prices[:10])
        range_low = min(prices[:10])
        failed_breakout = any(
            price > range_high * 1.0005 or price < range_low * 0.9995
            for price in prices[10:-1]
        ) and range_low * 0.9995 <= prices[-1] <= range_high * 1.0005
        volumes = [point.volume for point in points]
        first_volume = sum(volumes[:midpoint]) / midpoint
        second_volume = sum(volumes[midpoint:]) / midpoint
        volume_stall = (
            first_volume > 0
            and second_volume >= first_volume * 1.2
            and abs(prices[-1] - prices[midpoint]) / baseline < 0.001
        )
        direction = 1 if prices[-1] >= prices[0] else -1
        trend_score = sum((
            one_side >= 0.8,
            slope * direction >= 0.001,
            efficiency >= 0.55,
            up_progress if direction > 0 else down_progress,
        ))
        range_score = sum((
            abs(slope) < 0.001,
            crossings >= 2,
            candle_overlap >= 0.5 and not (up_progress or down_progress),
            failed_breakout,
            volume_stall,
            efficiency <= 0.30,
        ))
        if trend_score >= 3 and one_side >= 0.8:
            state = "UPTREND" if direction > 0 else "DOWNTREND"
            label = "上涨趋势日，禁止逆势高抛" if state == "UPTREND" else "下跌趋势日，禁止逆势低吸"
            score = trend_score / 4
        elif range_score >= 3:
            state, label, score = "RANGE", "震荡日，可等待反转确认后做T", range_score / 6
        else:
            state, label, score = "UNCERTAIN", "状态未确认，暂停做T", max(trend_score / 4, range_score / 6)
        return state, label, round(score, 4), round(efficiency, 4), round(one_side, 4), crossings

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

    def _fast_rise_grids(self, quote: Quote, grid_size: float) -> float:
        if len(quote.points) < 2:
            return 0.0
        previous, latest = quote.points[-2:]
        seconds = (latest.timestamp - previous.timestamp).total_seconds()
        if not 0 < seconds <= 300:
            return 0.0
        return round(max(0.0, (latest.price - previous.price) / grid_size), 6)


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
