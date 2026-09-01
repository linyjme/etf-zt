from __future__ import annotations

from collections.abc import Callable, Sequence
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .t_monitor import JsonQuoteAdapter, MarketDataError, WatchItem


SHANGHAI = ZoneInfo("Asia/Shanghai")
SOURCE_NAME = "东方财富 trends2"
TRENDS2_ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
TRENDS2_FALLBACK_ENDPOINT = "https://push2delay.eastmoney.com/api/qt/stock/trends2/get"
_FIELDS1 = "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58"
Transport = Callable[[Request, float], bytes]


class _RequestFailure(Exception):
    def __init__(self, symbol: str, endpoint: str, error: Exception):
        super().__init__(f"{symbol} trends2 请求失败 ({endpoint}): {error}")
        self.symbol = symbol
        self.endpoint = endpoint
        self.error = error


def market_for_symbol(symbol: str) -> int:
    if len(symbol) != 6 or not symbol.isdigit():
        raise MarketDataError(f"证券代码必须是6位数字: {symbol}")
    if symbol[0] in "0123":
        return 0
    if symbol[0] in "5679":
        return 1
    raise MarketDataError(f"无法映射证券市场: {symbol}")


def _powershell_transport(request: Request, timeout: float) -> bytes:
    encoded_url = base64.b64encode(request.full_url.encode("utf-8")).decode("ascii")
    script = (
        "$u=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
        + encoded_url
        + "'));$r=Invoke-WebRequest -UseBasicParsing -Uri $u -TimeoutSec "
        + str(max(1, int(timeout)))
        + ";[Convert]::ToBase64String($r.Content)"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        timeout=timeout + 3,
    )
    return base64.b64decode(result.stdout.strip(), validate=True)


def _default_transport(request: Request, timeout: float) -> bytes:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl is None:
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    command = [
        curl,
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--connect-timeout",
        str(timeout),
        "--max-time",
        str(timeout),
    ]
    for name, value in request.header_items():
        command.extend(("--header", f"{name}: {value}"))
    command.append(request.full_url)
    last_error = "行情请求失败"
    for attempt in range(3):
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=timeout + 2,
            )
        except subprocess.TimeoutExpired as error:
            last_error = str(error)
        else:
            if result.stdout:
                try:
                    json.loads(result.stdout.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
                else:
                    return result.stdout
            last_error = result.stderr.decode("utf-8", errors="replace").strip() or f"curl 退出码 {result.returncode}"
        if attempt < 2:
            time.sleep(0.2 * (attempt + 1))
    try:
        return _powershell_transport(request, timeout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        raise OSError(last_error)


class Trends2QuoteCollector:
    def __init__(
        self,
        timeout: float = 8.0,
        transport: Transport | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        if timeout <= 0:
            raise ValueError("timeout 必须为正数")
        self.timeout = timeout
        self.transport = transport or _default_transport
        self.now = now or (lambda: datetime.now(SHANGHAI))

    def collect(self, watchlist: Sequence[WatchItem]) -> dict[str, Any]:
        enabled = tuple(item for item in watchlist if item.enabled)
        if not enabled:
            raise MarketDataError("监控列表没有启用的证券")
        symbols = [item.symbol for item in enabled]
        if len(set(symbols)) != len(symbols):
            raise MarketDataError("监控列表存在重复证券代码")
        observed_at = self.now()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise MarketDataError("采集时间必须带时区")
        endpoint = TRENDS2_ENDPOINT
        try:
            records, urls = self._collect_batch(enabled, endpoint)
        except _RequestFailure as primary_error:
            endpoint = TRENDS2_FALLBACK_ENDPOINT
            try:
                records, urls = self._collect_batch(enabled, endpoint)
            except _RequestFailure as fallback_error:
                raise MarketDataError(
                    "trends2 主备端点请求均失败: "
                    f"主端点 {primary_error}; 备用端点 {fallback_error}"
                ) from fallback_error
            except MarketDataError as fallback_error:
                raise MarketDataError(
                    f"trends2 备用端点失败 ({endpoint}): {fallback_error}"
                ) from fallback_error
        for record in records:
            safe_points = [
                point for point in record["points"]
                if datetime.fromisoformat(point["timestamp"]) <= observed_at
            ]
            if not safe_points:
                raise MarketDataError(f"{record['symbol']}没有不晚于观测时间的分钟点")
            record["points"] = safe_points
            record["price"] = safe_points[-1]["price"]
            record["average_price"] = safe_points[-1]["average_price"]
            record["timestamp"] = safe_points[-1]["timestamp"]
            record["observed_at"] = observed_at.isoformat()
            record["collected_at"] = observed_at.isoformat()
        quotes = JsonQuoteAdapter().parse({"quotes": records})
        for quote in quotes.values():
            if any(point.timestamp > quote.observed_at for point in quote.points):
                raise MarketDataError(f"{quote.symbol}分钟时间晚于观测时间")
        missing = [symbol for symbol in symbols if symbol not in quotes]
        if missing or len(quotes) != len(enabled):
            raise MarketDataError("行情完整性校验失败: " + ",".join(missing))
        return {
            "schema_version": 2,
            "source": {
                "name": SOURCE_NAME,
                "endpoint": endpoint,
                "urls": urls,
            },
            "observed_at": observed_at.isoformat(),
            "collected_at": observed_at.isoformat(),
            "quotes": records,
        }

    def collect_to_file(self, watchlist: Sequence[WatchItem], path: Path) -> dict[str, Any]:
        payload = self.collect(watchlist)
        self._atomic_write(path, payload)
        return payload

    def _collect_batch(
        self, watchlist: Sequence[WatchItem], endpoint: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        records = []
        urls = []
        for item in watchlist:
            url = self._url(item.symbol, endpoint)
            urls.append(url)
            records.append(self._fetch(item, url, endpoint))
        return records, urls

    def _url(self, symbol: str, endpoint: str = TRENDS2_ENDPOINT) -> str:
        query = urlencode({
            "secid": f"{market_for_symbol(symbol)}.{symbol}",
            "fields1": _FIELDS1,
            "fields2": _FIELDS2,
            "iscr": 0,
            "ndays": 1,
        })
        return f"{endpoint}?{query}"

    def _fetch(self, item: WatchItem, url: str, endpoint: str) -> dict[str, Any]:
        request = Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        })
        try:
            raw = json.loads(self.transport(request, self.timeout).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _RequestFailure(item.symbol, endpoint, error) from error
        if not isinstance(raw, dict) or raw.get("rc") != 0:
            raise MarketDataError(f"{item.symbol} trends2 返回失败")
        data = raw.get("data")
        if not isinstance(data, dict):
            raise MarketDataError(f"{item.symbol} trends2 缺少 data")
        if str(data.get("code", "")) != item.symbol:
            raise MarketDataError(f"{item.symbol} trends2 代码不匹配")
        expected_market = market_for_symbol(item.symbol)
        if data.get("market") != expected_market:
            raise MarketDataError(f"{item.symbol} trends2 市场不匹配")
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            name = item.name
        previous_close = self._positive(data.get("preClose"), f"{item.symbol}.preClose")
        trends = data.get("trends")
        if not isinstance(trends, list) or not trends:
            raise MarketDataError(f"{item.symbol} trends2 缺少分钟点")
        points = [self._point(item.symbol, value) for value in trends]
        record = {
            "symbol": item.symbol,
            "name": name.strip(),
            "price": points[-1]["price"],
            "average_price": points[-1]["average_price"],
            "previous_close": previous_close,
            "timestamp": points[-1]["timestamp"],
            "points": points,
            "source": SOURCE_NAME,
            "schema_version": 2,
        }
        return record

    def _point(self, symbol: str, value: Any) -> list[Any]:
        if not isinstance(value, str):
            raise MarketDataError(f"{symbol} trends2 分钟点格式错误")
        fields = value.split(",")
        if len(fields) < 8:
            raise MarketDataError(f"{symbol} trends2 分钟点字段不完整")
        try:
            timestamp = datetime.strptime(fields[0], "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
        except ValueError as error:
            raise MarketDataError(f"{symbol} trends2 分钟时间无效") from error
        open_price = self._positive(fields[1], f"{symbol}.open")
        price = self._positive(fields[2], f"{symbol}.price")
        high = self._positive(fields[3], f"{symbol}.high")
        low = self._positive(fields[4], f"{symbol}.low")
        volume = self._nonnegative(fields[5], f"{symbol}.volume")
        amount = self._nonnegative(fields[6], f"{symbol}.amount")
        average_price = self._positive(fields[7], f"{symbol}.average_price")
        if high < max(open_price, price, low) or low > min(open_price, price, high):
            raise MarketDataError(f"{symbol} trends2 OHLC关系无效")
        return {
            "timestamp": timestamp.isoformat(),
            "price": price,
            "average_price": average_price,
            "open": open_price,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": amount,
        }

    def _positive(self, value: Any, field: str) -> float:
        if isinstance(value, bool):
            raise MarketDataError(f"{field} 必须是有限正数")
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise MarketDataError(f"{field} 必须是有限正数") from error
        if not 0 < number < float("inf"):
            raise MarketDataError(f"{field} 必须是有限正数")
        return number

    def _nonnegative(self, value: Any, field: str) -> float:
        if isinstance(value, bool):
            raise MarketDataError(f"{field} 必须是有限非负数")
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise MarketDataError(f"{field} 必须是有限非负数") from error
        if not 0 <= number < float("inf"):
            raise MarketDataError(f"{field} 必须是有限非负数")
        return number

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="\n", dir=destination.parent,
                prefix=f".{destination.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
