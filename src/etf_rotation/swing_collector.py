"""Validated raw-plus-adjusted Eastmoney daily-bar collection."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
import json
import math
from typing import Any
from urllib.parse import urlencode, urlsplit
from urllib.request import Request
from zoneinfo import ZoneInfo

from .eastmoney_client import (
    Transport,
    _default_transport,
    market_for_symbol as _shared_market_for_symbol,
)
from .swing_config import SwingWatchItem
from .swing_data import DailyBar, SwingDataError


SHANGHAI = ZoneInfo("Asia/Shanghai")
KLINE_ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
KLINE_FALLBACK_ENDPOINT = "https://push2delay.eastmoney.com/api/qt/stock/kline/get"
FIELDS1 = "f1,f2,f3,f4,f5,f6"
FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
_FINAL_TIME = time(15, 10)
_MAX_COUNT = 10_000


class DailyRequestFailure(Exception):
    """Safe request/decode failure used to decide endpoint fallback."""

    def __init__(self, symbol: str, endpoint: str, adjustment: int):
        super().__init__(
            f"{symbol} kline fqt={adjustment} 请求或解码失败 ({endpoint})",
        )
        self.symbol = symbol
        self.endpoint = endpoint
        self.adjustment = adjustment


@dataclass(frozen=True)
class _ParsedKline:
    trading_date: date
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float


@dataclass(frozen=True)
class _Response:
    pre_close: float | None
    bars: tuple[_ParsedKline, ...]


def _market_for_symbol(symbol: str) -> int:
    try:
        return _shared_market_for_symbol(symbol, error_type=SwingDataError)
    except SwingDataError as error:
        raise SwingDataError("证券代码或市场无效") from error


def _source_label(endpoint: str) -> str:
    host = urlsplit(endpoint).netloc
    return f"东方财富 kline ({host or endpoint})"


class EastmoneyDailyCollector:
    def __init__(
        self,
        timeout: float = 8.0,
        transport: Transport | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise SwingDataError("timeout必须是有限正数")
        if transport is not None and not callable(transport):
            raise SwingDataError("transport必须可调用")
        if now is not None and not callable(now):
            raise SwingDataError("now必须可调用")
        self.timeout = float(timeout)
        self.transport = _default_transport if transport is None else transport
        self.now = (lambda: datetime.now(SHANGHAI)) if now is None else now

    def collect(
        self,
        watchlist: Sequence[SwingWatchItem],
        last_completed_date: date,
        count: int = 260,
    ) -> tuple[DailyBar, ...]:
        enabled = self._enabled_watchlist(watchlist)
        if type(last_completed_date) is not date:
            raise SwingDataError("last_completed_date必须是date")
        if type(count) is not int or not 0 < count <= _MAX_COUNT:
            raise SwingDataError(f"count必须是1到{_MAX_COUNT}的整数")
        observed_at = self._observed_at()

        endpoint = KLINE_ENDPOINT
        try:
            responses = self._collect_batch(
                enabled, last_completed_date, count, endpoint,
            )
        except DailyRequestFailure as primary_error:
            endpoint = KLINE_FALLBACK_ENDPOINT
            try:
                responses = self._collect_batch(
                    enabled, last_completed_date, count, endpoint,
                )
                return self._build_bars(
                    enabled,
                    responses,
                    last_completed_date,
                    observed_at,
                    endpoint,
                )
            except DailyRequestFailure as fallback_error:
                raise SwingDataError(
                    "kline 主备端点请求均失败 "
                    f"({KLINE_ENDPOINT}; {KLINE_FALLBACK_ENDPOINT})",
                ) from fallback_error
            except SwingDataError as fallback_error:
                raise SwingDataError(
                    "kline 主端点请求失败且备用端点业务校验失败 "
                    f"({KLINE_ENDPOINT}; {KLINE_FALLBACK_ENDPOINT})",
                ) from fallback_error
        return self._build_bars(
            enabled,
            responses,
            last_completed_date,
            observed_at,
            endpoint,
        )

    def _enabled_watchlist(
        self, watchlist: Sequence[SwingWatchItem],
    ) -> tuple[SwingWatchItem, ...]:
        if not isinstance(watchlist, Sequence) or isinstance(
            watchlist, (str, bytes, bytearray),
        ):
            raise SwingDataError("watchlist必须是序列")
        try:
            items = tuple(watchlist)
        except Exception as error:
            raise SwingDataError("watchlist读取失败") from error
        enabled: list[SwingWatchItem] = []
        seen: set[str] = set()
        for item in items:
            if type(item) is not SwingWatchItem:
                raise SwingDataError("watchlist项目类型无效")
            if type(item.enabled) is not bool:
                raise SwingDataError("watchlist项目enabled必须是布尔值")
            _market_for_symbol(item.symbol)
            if item.symbol in seen:
                raise SwingDataError("watchlist存在重复证券代码")
            seen.add(item.symbol)
            if not item.enabled:
                continue
            enabled.append(item)
        if not enabled:
            raise SwingDataError("watchlist没有启用的证券")
        return tuple(enabled)

    def _observed_at(self) -> datetime:
        try:
            value = self.now()
        except Exception as error:
            raise SwingDataError("采集时间读取失败") from error
        if type(value) is not datetime:
            raise SwingDataError("采集时间必须是datetime")
        try:
            if value.tzinfo is None or value.utcoffset() is None:
                raise SwingDataError("采集时间必须带时区")
            return value.astimezone(SHANGHAI)
        except SwingDataError:
            raise
        except Exception as error:
            raise SwingDataError("采集时间时区无效") from error

    def _collect_batch(
        self,
        watchlist: tuple[SwingWatchItem, ...],
        last_completed_date: date,
        count: int,
        endpoint: str,
    ) -> dict[str, tuple[_Response, _Response]]:
        responses: dict[str, tuple[_Response, _Response]] = {}
        for item in watchlist:
            raw = self._fetch(
                item.symbol,
                adjustment=0,
                last_completed_date=last_completed_date,
                count=count,
                endpoint=endpoint,
            )
            adjusted = self._fetch(
                item.symbol,
                adjustment=1,
                last_completed_date=last_completed_date,
                count=count,
                endpoint=endpoint,
            )
            responses[item.symbol] = (raw, adjusted)
        return responses

    def _fetch(
        self,
        symbol: str,
        *,
        adjustment: int,
        last_completed_date: date,
        count: int,
        endpoint: str,
    ) -> _Response:
        expected_market = _market_for_symbol(symbol)
        query = urlencode({
            "secid": f"{expected_market}.{symbol}",
            "fields1": FIELDS1,
            "fields2": FIELDS2,
            "klt": 101,
            "fqt": adjustment,
            "lmt": count,
            "end": last_completed_date.strftime("%Y%m%d"),
        })
        request = Request(f"{endpoint}?{query}", headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        })
        try:
            encoded = self.transport(request, self.timeout)
            if type(encoded) is not bytes:
                raise TypeError("transport result is not bytes")
            payload = json.loads(encoded.decode("utf-8"))
        except Exception as error:
            failure = DailyRequestFailure(symbol, endpoint, adjustment)
            raise failure from error
        return self._parse_payload(
            payload,
            symbol=symbol,
            expected_market=expected_market,
            require_pre_close=adjustment == 0,
        )

    def _parse_payload(
        self,
        payload: Any,
        *,
        symbol: str,
        expected_market: int,
        require_pre_close: bool,
    ) -> _Response:
        if type(payload) is not dict or type(payload.get("rc")) is not int or payload.get("rc") != 0:
            raise SwingDataError(f"{symbol} kline返回失败")
        data = payload.get("data")
        if type(data) is not dict:
            raise SwingDataError(f"{symbol} kline缺少data")
        if type(data.get("code")) is not str or data.get("code") != symbol:
            raise SwingDataError(f"{symbol} kline代码不匹配")
        if type(data.get("market")) is not int or data.get("market") != expected_market:
            raise SwingDataError(f"{symbol} kline市场不匹配")
        if "name" in data:
            name = data["name"]
            if type(name) is not str or not name.strip():
                raise SwingDataError(f"{symbol} kline名称无效")
        pre_close = None
        if require_pre_close:
            pre_close = self._positive(data.get("preKPrice"), f"{symbol}.preKPrice")
        klines = data.get("klines")
        if type(klines) is not list or not klines:
            raise SwingDataError(f"{symbol} kline缺少日线")
        bars = tuple(self._parse_line(symbol, line) for line in klines)
        return _Response(pre_close, bars)

    def _parse_line(self, symbol: str, value: Any) -> _ParsedKline:
        if type(value) is not str:
            raise SwingDataError(f"{symbol} kline日线格式错误")
        fields = value.split(",")
        if len(fields) < 7:
            raise SwingDataError(f"{symbol} kline日线字段不完整")
        trading_date = self._date(fields[0], symbol)
        return _ParsedKline(
            trading_date=trading_date,
            open=self._positive(fields[1], f"{symbol}.open"),
            close=self._positive(fields[2], f"{symbol}.close"),
            high=self._positive(fields[3], f"{symbol}.high"),
            low=self._positive(fields[4], f"{symbol}.low"),
            volume=self._nonnegative(fields[5], f"{symbol}.volume"),
            amount=self._nonnegative(fields[6], f"{symbol}.amount"),
        )

    @staticmethod
    def _date(value: str, symbol: str) -> date:
        if type(value) is not str or len(value) != 10 or not value.isascii():
            raise SwingDataError(f"{symbol} kline日期无效")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise SwingDataError(f"{symbol} kline日期无效") from error
        if parsed.isoformat() != value:
            raise SwingDataError(f"{symbol} kline日期无效")
        return parsed

    @staticmethod
    def _positive(value: Any, field: str) -> float:
        number = EastmoneyDailyCollector._finite(value, field)
        if number <= 0:
            raise SwingDataError(f"{field}必须是有限正数")
        return number

    @staticmethod
    def _nonnegative(value: Any, field: str) -> float:
        number = EastmoneyDailyCollector._finite(value, field)
        if number < 0:
            raise SwingDataError(f"{field}必须是有限非负数")
        return number

    @staticmethod
    def _finite(value: Any, field: str) -> float:
        if isinstance(value, bool):
            raise SwingDataError(f"{field}必须是有限数字")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise SwingDataError(f"{field}必须是有限数字") from error
        if not math.isfinite(number):
            raise SwingDataError(f"{field}必须是有限数字")
        return number

    def _build_bars(
        self,
        watchlist: tuple[SwingWatchItem, ...],
        responses: dict[str, tuple[_Response, _Response]],
        last_completed_date: date,
        observed_at: datetime,
        endpoint: str,
    ) -> tuple[DailyBar, ...]:
        result: list[DailyBar] = []
        today = observed_at.date()
        current_day_complete = observed_at.timetz().replace(tzinfo=None) >= _FINAL_TIME
        source = _source_label(endpoint)
        for item in watchlist:
            try:
                raw_response, adjusted_response = responses[item.symbol]
            except (KeyError, TypeError, ValueError) as error:
                raise SwingDataError(f"{item.symbol} kline批次结果缺失") from error
            previous_close_by_date: dict[date, float] = {}
            previous_close = raw_response.pre_close
            if previous_close is None:
                raise SwingDataError(f"{item.symbol}缺少首日昨收")
            for raw in raw_response.bars:
                previous_close_by_date[raw.trading_date] = previous_close
                previous_close = raw.close

            def retained(bar: _ParsedKline) -> bool:
                return (
                    bar.trading_date <= last_completed_date
                    and bar.trading_date <= today
                    and (bar.trading_date != today or current_day_complete)
                )

            raw_retained = tuple(filter(retained, raw_response.bars))
            adjusted_retained = tuple(filter(retained, adjusted_response.bars))
            retained_dates = [bar.trading_date for bar in raw_retained]
            adjusted_retained_dates = [
                bar.trading_date for bar in adjusted_retained
            ]
            self._validate_retained_dates(item.symbol, retained_dates)
            self._validate_retained_dates(item.symbol, adjusted_retained_dates)
            if set(retained_dates) != set(adjusted_retained_dates):
                raise SwingDataError(f"{item.symbol}过滤后原始与复权日期不匹配")
            if not retained_dates:
                raise SwingDataError(f"{item.symbol}没有可保留的已完成日线")
            raw_by_date = {bar.trading_date: bar for bar in raw_retained}
            adjusted_by_date = {
                bar.trading_date: bar for bar in adjusted_retained
            }
            for trading_day in retained_dates:
                raw = raw_by_date[trading_day]
                adjusted = adjusted_by_date[trading_day]
                result.append(DailyBar.from_mapping({
                    "schema_version": 1,
                    "symbol": item.symbol,
                    "trading_date": trading_day.isoformat(),
                    "observed_at": observed_at.isoformat(),
                    "source": source,
                    "open": raw.open,
                    "high": raw.high,
                    "low": raw.low,
                    "close": raw.close,
                    "previous_close": previous_close_by_date[trading_day],
                    "volume": raw.volume,
                    "amount": raw.amount,
                    "adjusted_open": adjusted.open,
                    "adjusted_high": adjusted.high,
                    "adjusted_low": adjusted.low,
                    "adjusted_close": adjusted.close,
                    "is_final": True,
                }))
        return tuple(sorted(result, key=lambda bar: (bar.symbol, bar.trading_date)))

    @staticmethod
    def _validate_retained_dates(symbol: str, dates: list[date]) -> None:
        if len(set(dates)) != len(dates):
            raise SwingDataError(f"{symbol} kline日期重复")
        if any(left >= right for left, right in zip(dates, dates[1:])):
            raise SwingDataError(f"{symbol} kline日期必须严格递增")
