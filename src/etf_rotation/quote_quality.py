"""Preserve rejected minute evidence without changing source values or validators."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import Any

from .etf_metadata import EtfMetadata, TradingMetadata
from .market_data import MarketDataValidator, SHANGHAI, finalized_points
from .t_monitor import JsonQuoteAdapter, MarketDataError, Quote, QuotePoint


_ISSUE_FIELDS = frozenset({
    "schema_version", "symbol", "timestamp", "trading_date", "observed_at",
    "source", "reason", "previous_close", "volume_unit_shares", "point",
})
_POINT_FIELDS = frozenset({
    "timestamp", "price", "average_price", "open", "high", "low", "volume", "amount",
})
_LOCK_GUARD = threading.Lock()
_LOCKS: dict[Path, Any] = {}


def _invalid_issue() -> None:
    raise MarketDataError("分钟隔离记录无效")


def _text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _finite(value: object, *, positive: bool = False) -> bool:
    return (
        type(value) in (int, float)
        and 0 <= value <= sys.float_info.max
        and math.isfinite(value)
        and (not positive or value > 0)
    )


def _issue_time(value: object) -> datetime:
    if not _text(value):
        _invalid_issue()
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            _invalid_issue()
        return parsed.astimezone(SHANGHAI)
    except (ValueError, OverflowError):
        raise MarketDataError("分钟隔离记录无效") from None


def _validated_issues(value: object) -> list[dict[str, Any]]:
    if type(value) is not list:
        _invalid_issue()
    for issue in value:
        if type(issue) is not dict or set(issue) not in (
            _ISSUE_FIELDS, _ISSUE_FIELDS | {"last_observed_at"},
        ):
            _invalid_issue()
        symbol = issue["symbol"]
        if (
            type(issue["schema_version"]) is not int or issue["schema_version"] != 1
            or type(symbol) is not str or len(symbol) != 6
            or not symbol.isascii() or not symbol.isdigit()
            or not _text(issue["source"]) or not _text(issue["reason"])
            or not _finite(issue["previous_close"], positive=True)
            or type(issue["volume_unit_shares"]) is not int
            or issue["volume_unit_shares"] <= 0
        ):
            _invalid_issue()
        stamp = _issue_time(issue["timestamp"])
        observed = _issue_time(issue["observed_at"])
        if issue["trading_date"] != stamp.date().isoformat() or observed - stamp < timedelta(minutes=1):
            _invalid_issue()
        if "last_observed_at" in issue and _issue_time(issue["last_observed_at"]) < observed:
            _invalid_issue()
        point = issue["point"]
        if type(point) is not dict or set(point) != _POINT_FIELDS or point["timestamp"] != issue["timestamp"]:
            _invalid_issue()
        for name in ("price", "average_price"):
            if not _finite(point[name], positive=True):
                _invalid_issue()
        for name in ("open", "high", "low"):
            if point[name] is not None and not _finite(point[name], positive=True):
                _invalid_issue()
        for name in ("volume", "amount"):
            if not _finite(point[name]):
                _invalid_issue()
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise MarketDataError("分钟隔离记录无效") from None
    return deepcopy(value)


def read_validation_issues(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read the persisted quality lock without requiring trading metadata."""
    if not isinstance(payload, dict):
        raise MarketDataError("行情文件必须是对象")
    return _validated_issues(payload.get("validation_issues", []))


def _point_record(point: QuotePoint) -> dict[str, Any]:
    return {
        "timestamp": point.timestamp.astimezone(SHANGHAI).isoformat(),
        "price": point.price, "average_price": point.average_price,
        "open": point.open, "high": point.high, "low": point.low,
        "volume": point.volume, "amount": point.amount,
    }


def _quote_record(quote: Quote, points: list[QuotePoint]) -> dict[str, Any]:
    latest = points[-1]
    return {
        "schema_version": 2, "symbol": quote.symbol, "name": quote.name,
        "price": latest.price, "average_price": latest.average_price,
        "previous_close": quote.previous_close,
        "timestamp": latest.timestamp.astimezone(SHANGHAI).isoformat(),
        "observed_at": quote.observed_at.astimezone(SHANGHAI).isoformat(),
        "source": quote.source, "points": [_point_record(point) for point in points],
    }


def _evidence_key(issue: Mapping[str, Any]) -> str:
    return json.dumps(
        {key: value for key, value in issue.items() if key not in {"observed_at", "last_observed_at"}},
        ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    )


def _advance_observation(stored: dict[str, Any], incoming: Mapping[str, Any]) -> bool:
    """Keep first evidence intact while recording the newest recurrence."""
    latest = incoming.get("last_observed_at", incoming["observed_at"])
    previous = stored.get("last_observed_at", stored["observed_at"])
    if _issue_time(latest) <= _issue_time(previous):
        return False
    stored["last_observed_at"] = latest
    return True


def _deduplicated(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for issue in issues:
        key = _evidence_key(issue)
        if key not in records:
            records[key] = deepcopy(issue)
        else:
            _advance_observation(records[key], issue)
    return list(records.values())


def prepare_quote_batch(
    payload: dict[str, Any], metadata: Mapping[str, EtfMetadata],
) -> tuple[dict[str, Any], Mapping[str, Quote], list[dict[str, Any]]]:
    """Remove only rejected finalized points; retained issues are not a health verdict."""
    issues = read_validation_issues(payload)
    clean = deepcopy(payload)
    adapter = JsonQuoteAdapter()
    quotes = adapter.parse(clean)
    if not isinstance(metadata, Mapping):
        raise MarketDataError("缺少交易元数据")
    records = []
    for symbol, quote in quotes.items():
        item = metadata.get(symbol)
        if not isinstance(item, EtfMetadata) or not isinstance(item.trading, TradingMetadata):
            raise MarketDataError(f"缺少交易元数据: {symbol}")
        if item.symbol != symbol:
            raise MarketDataError(f"交易元数据代码不一致: {symbol}")
        validator = MarketDataValidator(item.trading)
        completed = set(finalized_points(quote.points, quote.observed_at))
        retained = []
        for point in quote.points:
            try:
                if point in completed:
                    validator.validate_point(point, quote.previous_close)
            except MarketDataError as error:
                raw_point = _point_record(point)
                issues.append({
                    "schema_version": 1, "symbol": symbol,
                    "timestamp": raw_point["timestamp"],
                    "trading_date": point.timestamp.astimezone(SHANGHAI).date().isoformat(),
                    "observed_at": quote.observed_at.astimezone(SHANGHAI).isoformat(),
                    "source": quote.source, "reason": str(error),
                    "previous_close": quote.previous_close,
                    "volume_unit_shares": item.trading.volume_unit_shares,
                    "point": raw_point,
                })
            else:
                retained.append(point)
        if retained:
            records.append(_quote_record(quote, retained))
    if not issues:
        return clean, quotes, []
    issues = _deduplicated(_validated_issues(issues))
    # A symbol-keyed legacy object becomes an explicit quotes envelope; do not
    # leave rejected raw points behind in its old symbol entries.
    if "quotes" not in clean:
        clean = {}
    clean["quotes"] = records
    clean["validation_issues"] = deepcopy(issues)
    return clean, adapter.parse(clean), issues


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _invalid_issue()
        result[key] = value
    return result


class MinuteQuarantineStore:
    """Durable local JSONL evidence, deduplicated independently of observation time."""

    def __init__(self, path: Path):
        self.path = Path(path)
        with _LOCK_GUARD:
            self._lock = _LOCKS.setdefault(self.path.resolve(), threading.RLock())

    def _read(self) -> list[dict[str, Any]]:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError):
            raise MarketDataError("分钟隔离日志读取失败") from None
        try:
            rows = [json.loads(line, object_pairs_hook=_unique_json_object) for line in content.splitlines()]
            return _validated_issues(rows)
        except (ValueError, TypeError, OverflowError):
            raise MarketDataError("分钟隔离日志损坏") from None

    def read(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._read()

    def append(self, issues: list[dict[str, Any]]) -> int:
        validated = _validated_issues(issues)
        with self._lock:
            existing = self._read()
            known = {_evidence_key(issue): issue for issue in existing}
            added = []
            updated = False
            for issue in validated:
                key = _evidence_key(issue)
                if key not in known:
                    known[key] = issue
                    added.append(issue)
                elif _advance_observation(known[key], issue):
                    updated = True
            if not added and not updated:
                return 0
            temporary: Path | None = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", newline="\n", dir=self.path.parent,
                    prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    for issue in existing + added:
                        handle.write(json.dumps(issue, ensure_ascii=False, allow_nan=False, sort_keys=True))
                        handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                temporary = None
            except (OSError, ValueError, TypeError, UnicodeError):
                raise MarketDataError("分钟隔离日志写入失败") from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return len(added)


__all__ = ["prepare_quote_batch", "read_validation_issues", "MinuteQuarantineStore"]
