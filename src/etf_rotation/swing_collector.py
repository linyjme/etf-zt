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

from .constants import DEFAULT_SWING_HISTORY_COUNT
from .eastmoney_client import (
    Transport,
    _default_transport,
    market_for_symbol as _shared_market_for_symbol,
)
from .swing_config import SwingWatchItem
from .swing_crosscheck import IndependentBar
from .swing_data import DailyBar, SwingDataError


SHANGHAI = ZoneInfo("Asia/Shanghai")
KLINE_ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
KLINE_FALLBACK_ENDPOINT = "https://push2delay.eastmoney.com/api/qt/stock/kline/get"
TENCENT_KLINE_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
ENVIRONMENT_INDEX_PREFIX = {
    "000300": "sh",
    "000852": "sh",
}
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
    amount: float | None


@dataclass(frozen=True)
class _Response:
    pre_close: float | None
    bars: tuple[_ParsedKline, ...]


def tencent_market_symbol(symbol: str, market_prefix: str | None = None) -> str:
    """Return the Tencent code, forcing Shanghai for the environment indices."""
    if market_prefix is None:
        market = _market_for_symbol(symbol)
        market_prefix = "sh" if market == 1 else "sz"
    if market_prefix not in {"sh", "sz"}:
        raise SwingDataError("指数市场前缀无效")
    return f"{market_prefix}{symbol}"


def _market_for_symbol(symbol: str) -> int:
    try:
        return _shared_market_for_symbol(symbol, error_type=SwingDataError)
    except SwingDataError as error:
        raise SwingDataError("证券代码或市场无效") from error


def _source_label(endpoint: str) -> str:
    host = urlsplit(endpoint).netloc
    if endpoint == TENCENT_KLINE_ENDPOINT:
        return (
            f"腾讯 fqkline 原始+前复权 ({host or endpoint}); "
            "amount=OHLC均价×成交量(手)×100估算"
        )
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
        count: int = DEFAULT_SWING_HISTORY_COUNT,
    ) -> tuple[DailyBar, ...]:
        enabled = self._enabled_watchlist(watchlist)
        if type(last_completed_date) is not date:
            raise SwingDataError("last_completed_date必须是date")
        if type(count) is not int or not 0 < count <= _MAX_COUNT:
            raise SwingDataError(f"count必须是1到{_MAX_COUNT}的整数")
        observed_at = self._observed_at()
        collected: list[DailyBar] = []
        for item in enabled:
            collected.extend(
                self._collect_symbol(item, last_completed_date, count, observed_at),
            )
        return tuple(sorted(collected, key=lambda bar: (bar.symbol, bar.trading_date)))

    def collect_indices(
        self,
        last_completed_date: date,
        count: int = DEFAULT_SWING_HISTORY_COUNT,
    ) -> tuple[DailyBar, ...]:
        """Collect CSI 300 and CSI 1000 from Tencent without mixing them into ETFs.

        Eastmoney's market id treats a leading zero as Shenzhen.  These index
        codes are Shanghai instruments (``sh000300`` / ``sh000852``), so the
        environment history uses the Tencent prefix explicitly and is stored
        apart from tradable ETF bars.  Their amount remains an estimate and
        cannot satisfy the ETF provider-reported amount gate.
        """
        if type(last_completed_date) is not date:
            raise SwingDataError("last_completed_date必须是date")
        if type(count) is not int or not 0 < count <= _MAX_COUNT:
            raise SwingDataError(f"count必须是1到{_MAX_COUNT}的整数")
        items = tuple(
            SwingWatchItem(symbol, True) for symbol in ENVIRONMENT_INDEX_PREFIX
        )
        observed_at = self._observed_at()
        responses: dict[str, tuple[_Response, _Response]] = {}
        for item in items:
            prefix = ENVIRONMENT_INDEX_PREFIX[item.symbol]
            raw = self._fetch_tencent(
                item.symbol,
                adjustment=0,
                last_completed_date=last_completed_date,
                count=count,
                market_prefix=prefix,
            )
            adjusted = self._fetch_tencent(
                item.symbol,
                adjustment=1,
                last_completed_date=last_completed_date,
                count=count,
                market_prefix=prefix,
            )
            adjusted = self._complete_tencent_adjusted_suffix(
                item.symbol, raw, adjusted,
            )
            responses[item.symbol] = (raw, adjusted)
        return self._build_bars(
            items, responses, last_completed_date, observed_at, TENCENT_KLINE_ENDPOINT,
        )

    def collect_independent(
        self,
        symbol: str,
        last_completed_date: date,
        count: int = DEFAULT_SWING_HISTORY_COUNT,
    ) -> tuple[IndependentBar, ...]:
        """Fetch the Tencent raw and qfq series as crosscheck evidence.

        The result is never stored as canonical history.  It feeds
        ``swing_crosscheck.crosscheck_history`` so the research manifest can
        record an independent-source receipt for Eastmoney bars.  A session
        whose qfq value Tencent has not published yet keeps
        ``adjusted_close`` as ``None`` instead of deriving it.
        """
        _market_for_symbol(symbol)
        if type(last_completed_date) is not date:
            raise SwingDataError("last_completed_date必须是date")
        if type(count) is not int or not 0 < count <= _MAX_COUNT:
            raise SwingDataError(f"count必须是1到{_MAX_COUNT}的整数")
        raw = self._fetch_tencent(
            symbol, adjustment=0, last_completed_date=last_completed_date,
            count=count, keep_first=True,
        )
        adjusted = self._fetch_tencent(
            symbol, adjustment=1, last_completed_date=last_completed_date,
            count=count, keep_first=True,
        )
        adjusted_by_date = {bar.trading_date: bar for bar in adjusted.bars}
        result: list[IndependentBar] = []
        for bar in raw.bars:
            if bar.trading_date > last_completed_date:
                continue
            adjusted_bar = adjusted_by_date.get(bar.trading_date)
            result.append(IndependentBar(
                trading_date=bar.trading_date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                adjusted_close=None if adjusted_bar is None else adjusted_bar.close,
            ))
        if not result:
            raise SwingDataError(f"{symbol} 腾讯kline没有可用于交叉核验的日线")
        return tuple(result)

    def _collect_symbol(
        self,
        item: SwingWatchItem,
        last_completed_date: date,
        count: int,
        observed_at: datetime,
    ) -> tuple[DailyBar, ...]:
        watch = (item,)
        try:
            responses = self._collect_batch(
                watch, last_completed_date, count, KLINE_ENDPOINT,
            )
            return self._build_bars(
                watch, responses, last_completed_date, observed_at, KLINE_ENDPOINT,
            )
        except DailyRequestFailure:
            try:
                responses = self._collect_batch(
                    watch, last_completed_date, count, KLINE_FALLBACK_ENDPOINT,
                )
                return self._build_bars(
                    watch,
                    responses,
                    last_completed_date,
                    observed_at,
                    KLINE_FALLBACK_ENDPOINT,
                )
            except (DailyRequestFailure, SwingDataError) as fallback_error:
                try:
                    responses = self._collect_tencent_batch(
                        watch, last_completed_date, count,
                    )
                    return self._build_bars(
                        watch,
                        responses,
                        last_completed_date,
                        observed_at,
                        TENCENT_KLINE_ENDPOINT,
                    )
                except (DailyRequestFailure, SwingDataError) as tencent_error:
                    if isinstance(fallback_error, DailyRequestFailure):
                        message = "kline 主备端点请求均失败且腾讯最终回退失败"
                    else:
                        message = "kline 主端点请求失败且备用端点业务校验失败，腾讯最终回退失败"
                    raise SwingDataError(
                        f"{message} ({KLINE_ENDPOINT}; "
                        f"{KLINE_FALLBACK_ENDPOINT}; {TENCENT_KLINE_ENDPOINT})",
                    ) from tencent_error

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
            count=count,
        )

    def _collect_tencent_batch(
        self,
        watchlist: tuple[SwingWatchItem, ...],
        last_completed_date: date,
        count: int,
    ) -> dict[str, tuple[_Response, _Response]]:
        responses: dict[str, tuple[_Response, _Response]] = {}
        for item in watchlist:
            raw = self._fetch_tencent(
                item.symbol,
                adjustment=0,
                last_completed_date=last_completed_date,
                count=count,
            )
            adjusted = self._fetch_tencent(
                item.symbol,
                adjustment=1,
                last_completed_date=last_completed_date,
                count=count,
            )
            adjusted = self._complete_tencent_adjusted_suffix(
                item.symbol, raw, adjusted,
            )
            responses[item.symbol] = (raw, adjusted)
        return responses

    def _fetch_tencent(
        self,
        symbol: str,
        *,
        adjustment: int,
        last_completed_date: date,
        count: int,
        market_prefix: str | None = None,
        keep_first: bool = False,
    ) -> _Response:
        market_symbol = tencent_market_symbol(symbol, market_prefix)
        adjustment_name = "qfq" if adjustment == 1 else ""
        query = urlencode({
            "param": (
                f"{market_symbol},day,,{last_completed_date.isoformat()},"
                f"{count + 1},{adjustment_name}"
            ),
        })
        request = Request(f"{TENCENT_KLINE_ENDPOINT}?{query}", headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.qq.com/",
        })
        try:
            encoded = self.transport(request, self.timeout)
            if type(encoded) is not bytes:
                raise TypeError("transport result is not bytes")
            payload = json.loads(encoded.decode("utf-8"))
        except Exception as error:
            failure = DailyRequestFailure(
                symbol, TENCENT_KLINE_ENDPOINT, adjustment,
            )
            raise failure from error
        return self._parse_tencent_payload(
            payload,
            symbol=symbol,
            market_symbol=market_symbol,
            adjusted=adjustment == 1,
            count=count,
            keep_first=keep_first,
        )

    @staticmethod
    def _complete_tencent_adjusted_suffix(
        symbol: str,
        raw: _Response,
        adjusted: _Response,
    ) -> _Response:
        """Align Tencent qfq output to raw dates when its latest bar lags.

        Tencent occasionally publishes the unadjusted close for the current
        day before its qfq series catches up. The current-day qfq value is
        still derivable from the same day's raw bar and the latest available
        qfq/raw close factor; retaining that suffix prevents a one-day source
        race from blocking the complete batch.
        """
        raw_by_date = {bar.trading_date: bar for bar in raw.bars}
        adjusted_by_date = {bar.trading_date: bar for bar in adjusted.bars}
        missing_dates = tuple(
            day for day in raw_by_date if day not in adjusted_by_date
        )
        if missing_dates and any(
            day <= adjusted.bars[-1].trading_date for day in missing_dates
        ):
            raise SwingDataError(f"{symbol} 腾讯kline复权日期无法对齐")
        common_adjusted_dates = [
            day for day in adjusted_by_date if day in raw_by_date
        ]
        if not common_adjusted_dates:
            raise SwingDataError(f"{symbol} 腾讯kline没有可对齐的复权日期")
        anchor_day = common_adjusted_dates[-1]
        anchor_raw = raw_by_date[anchor_day]
        anchor_adjusted = adjusted_by_date[anchor_day]
        scale = anchor_adjusted.close / anchor_raw.close
        if not math.isfinite(scale) or scale <= 0:
            raise SwingDataError(f"{symbol} 腾讯kline复权比例无效")
        completed = [bar for bar in adjusted.bars if bar.trading_date in raw_by_date]
        for day in sorted(missing_dates):
            source = raw_by_date[day]
            completed.append(_ParsedKline(
                trading_date=day,
                open=source.open * scale,
                close=source.close * scale,
                high=source.high * scale,
                low=source.low * scale,
                volume=source.volume,
                amount=source.amount,
            ))
        return _Response(adjusted.pre_close, tuple(completed))

    def _parse_tencent_payload(
        self,
        payload: Any,
        *,
        symbol: str,
        market_symbol: str,
        adjusted: bool,
        count: int,
        keep_first: bool = False,
    ) -> _Response:
        """Parse one Tencent kline payload.

        The first line normally serves as the previous close and is dropped.
        ``keep_first`` retains it (with no previous close) for the independent
        crosscheck series, where a newly listed ETF's first session would
        otherwise have no counterpart.
        """
        if (
            type(payload) is not dict
            or type(payload.get("code")) is not int
            or payload.get("code") != 0
        ):
            raise SwingDataError(f"{symbol} 腾讯kline返回失败")
        data = payload.get("data")
        if type(data) is not dict:
            raise SwingDataError(f"{symbol} 腾讯kline缺少data")
        security = data.get(market_symbol)
        if type(security) is not dict:
            raise SwingDataError(f"{symbol} 腾讯kline代码不匹配")
        key = "qfqday" if adjusted and "qfqday" in security else "day"
        lines = security.get(key)
        if type(lines) is not list or len(lines) < 2:
            expected_key = "qfqday或day" if adjusted else "day"
            raise SwingDataError(
                f"{symbol} 腾讯kline缺少{expected_key}或首日昨收"
            )
        if len(lines) > count + 1 or len(lines) > _MAX_COUNT + 1:
            raise SwingDataError(f"{symbol} 腾讯kline条数超过count限制")
        bars = tuple(self._parse_tencent_line(symbol, line) for line in lines)
        dates = [bar.trading_date for bar in bars]
        if len(set(dates)) != len(dates):
            raise SwingDataError(f"{symbol} 腾讯kline日期重复")
        if any(left >= right for left, right in zip(dates, dates[1:])):
            raise SwingDataError(f"{symbol} 腾讯kline日期必须严格递增")
        if keep_first:
            return _Response(None, bars)
        return _Response(bars[0].close, bars[1:])

    def _parse_tencent_line(self, symbol: str, value: Any) -> _ParsedKline:
        if type(value) is not list or len(value) != 6:
            raise SwingDataError(f"{symbol} 腾讯kline日线字段无效")
        return _ParsedKline(
            trading_date=self._date(value[0], symbol),
            open=self._positive(value[1], f"{symbol}.open"),
            close=self._positive(value[2], f"{symbol}.close"),
            high=self._positive(value[3], f"{symbol}.high"),
            low=self._positive(value[4], f"{symbol}.low"),
            volume=self._nonnegative(value[5], f"{symbol}.volume"),
            amount=None,
        )

    def _parse_payload(
        self,
        payload: Any,
        *,
        symbol: str,
        expected_market: int,
        require_pre_close: bool,
        count: int,
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
        if len(klines) > count or len(klines) > _MAX_COUNT:
            raise SwingDataError(f"{symbol} kline条数超过count限制")
        bars = tuple(self._parse_line(symbol, line) for line in klines)
        dates = [bar.trading_date for bar in bars]
        if len(set(dates)) != len(dates):
            raise SwingDataError(f"{symbol} kline日期重复")
        if any(left >= right for left, right in zip(dates, dates[1:])):
            raise SwingDataError(f"{symbol} kline日期必须严格递增")
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
            item_source = source
            if endpoint == TENCENT_KLINE_ENDPOINT:
                def _raw_exceeds_gap(bar: _ParsedKline) -> bool:
                    previous = previous_close_by_date[bar.trading_date]
                    return any(
                        previous <= 0 or abs(price / previous - 1.0) > 0.25
                        for price in (bar.open, bar.high, bar.low, bar.close)
                    )
                if any(_raw_exceeds_gap(bar) for bar in raw_retained):
                    adjusted_pre_close = adjusted_response.pre_close
                    if adjusted_pre_close is None:
                        raise SwingDataError(f"{item.symbol}缺少复权首日昨收")
                    previous_close_by_date = {}
                    previous_close = adjusted_pre_close
                    for adjusted_bar in adjusted_response.bars:
                        previous_close_by_date[adjusted_bar.trading_date] = previous_close
                        previous_close = adjusted_bar.close
                    raw_by_date = adjusted_by_date
                    host = urlsplit(endpoint).netloc
                    item_source = (
                        f"腾讯 fqkline 一致前复权序列 ({host or endpoint}); "
                        "amount=OHLC均价×成交量(手)×100估算"
                    )
            for trading_day in retained_dates:
                raw = raw_by_date[trading_day]
                adjusted = adjusted_by_date[trading_day]
                adjusted_prices = self._adjusted_prices(
                    raw, adjusted, item.symbol, endpoint,
                )
                result.append(DailyBar.from_mapping({
                    "schema_version": 1,
                    "symbol": item.symbol,
                    "trading_date": trading_day.isoformat(),
                    "observed_at": observed_at.isoformat(),
                    "source": item_source,
                    "open": raw.open,
                    "high": raw.high,
                    "low": raw.low,
                    "close": raw.close,
                    "previous_close": previous_close_by_date[trading_day],
                    "volume": raw.volume,
                    "amount": (
                        raw.amount
                        if raw.amount is not None
                        else self._estimated_amount(raw, item.symbol)
                    ),
                    "adjusted_open": adjusted_prices[0],
                    "adjusted_high": adjusted_prices[1],
                    "adjusted_low": adjusted_prices[2],
                    "adjusted_close": adjusted_prices[3],
                    "is_final": True,
                }))
        return tuple(sorted(result, key=lambda bar: (bar.symbol, bar.trading_date)))

    @staticmethod
    def _adjusted_prices(
        raw: _ParsedKline,
        adjusted: _ParsedKline,
        symbol: str,
        endpoint: str,
    ) -> tuple[float, float, float, float]:
        raw_prices = (raw.open, raw.high, raw.low, raw.close)
        adjusted_prices = (
            adjusted.open, adjusted.high, adjusted.low, adjusted.close,
        )
        try:
            scale = adjusted.close / raw.close
        except (ZeroDivisionError, OverflowError):
            scale = math.nan

        # Vendors round each adjusted OHLC field independently.  Comparing the
        # four raw/adjusted ratios exactly therefore rejects valid Eastmoney
        # data (the disagreement is usually only a few price ticks).  Use the
        # close-derived factor as the canonical factor, validate that every
        # adjusted field is close to the same multiplicative series, and emit
        # a normalized series so DailyBar receives one exact positive scale.
        #
        # Eastmoney front-adjusts ETF cash distributions by subtracting the
        # cumulative payout (差价法), so on a wide-range day the open/high/low
        # deviate from the close-derived multiplicative series by more than
        # the tolerance even though the data is internally consistent.  An
        # adjusted series whose raw-minus-adjusted offset is the same for all
        # four fields is therefore accepted as well; the emitted bar still
        # uses the provider's adjusted close and one exact scale.
        price_tolerance = 0.011
        finite_scale = math.isfinite(scale) and scale > 0
        multiplicative = finite_scale and all(
            math.isclose(
                candidate,
                original * scale,
                rel_tol=0.0,
                abs_tol=price_tolerance,
            )
            for original, candidate in zip(raw_prices, adjusted_prices)
        )
        offsets = tuple(
            original - candidate
            for original, candidate in zip(raw_prices, adjusted_prices)
        )
        additive = finite_scale and all(
            math.isclose(offset, offsets[3], rel_tol=0.0, abs_tol=price_tolerance)
            for offset in offsets
        )
        if not (multiplicative or additive):
            provider = "腾讯kline" if endpoint == TENCENT_KLINE_ENDPOINT else "东方财富kline"
            raise SwingDataError(f"{symbol} {provider}复权OHLC不一致")
        return tuple(price * scale for price in raw_prices)

    @staticmethod
    def _estimated_amount(bar: _ParsedKline, symbol: str) -> float:
        try:
            average_price = math.fsum((bar.open, bar.high, bar.low, bar.close)) / 4.0
            amount = average_price * bar.volume * 100.0
        except (OverflowError, ValueError) as error:
            raise SwingDataError(f"{symbol} 腾讯kline成交额估算失败") from error
        if not math.isfinite(amount) or amount < 0:
            raise SwingDataError(f"{symbol} 腾讯kline成交额估算必须是有限非负数")
        return amount

    @staticmethod
    def _validate_retained_dates(symbol: str, dates: list[date]) -> None:
        if len(set(dates)) != len(dates):
            raise SwingDataError(f"{symbol} kline日期重复")
        if any(left >= right for left, right in zip(dates, dates[1:])):
            raise SwingDataError(f"{symbol} kline日期必须严格递增")
