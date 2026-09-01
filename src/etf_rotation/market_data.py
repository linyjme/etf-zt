from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import threading
from types import MappingProxyType
from typing import Any, Mapping
import uuid
from zoneinfo import ZoneInfo

from .constants import DELAYED_MAX_AGE_SECONDS, REALTIME_MAX_AGE_SECONDS
from .etf_metadata import EtfMetadata, TradingMetadata
from .t_monitor import MarketDataError, Quote, QuotePoint


SHANGHAI = ZoneInfo("Asia/Shanghai")
_MORNING_START = time(9, 30)
_MORNING_END = time(11, 30)
_AFTERNOON_START = time(13, 0)
_AFTERNOON_END = time(15, 0)
_HISTORY_LOCKS_GUARD = threading.Lock()
_HISTORY_LOCKS: dict[str, Any] = {}


def _history_lock(path: Path) -> Any:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _HISTORY_LOCKS_GUARD:
        lock = _HISTORY_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _HISTORY_LOCKS[key] = lock
        return lock


@dataclass(frozen=True)
class MarketHealth:
    status: str
    quote_age_seconds: float | None
    reason: str


@dataclass(frozen=True)
class MarketSessionState:
    phase: str
    health_status: str
    active: bool
    catch_up_allowed: bool


def market_session_state(
    now: datetime,
    closed_dates: set[date] | None = None,
) -> MarketSessionState:
    local = _aware_time(now, "当前时间").astimezone(SHANGHAI)
    local_time = local.time().replace(tzinfo=None)
    if local.weekday() >= 5 or local.date() in frozenset(closed_dates or ()):
        return MarketSessionState("CLOSED", "CLOSED", False, False)
    if local_time < _MORNING_START:
        return MarketSessionState("PRE_OPEN", "CLOSED", False, False)
    if local_time <= _MORNING_END:
        return MarketSessionState("MORNING", "REALTIME", True, False)
    if local_time < _AFTERNOON_START:
        return MarketSessionState("LUNCH_BREAK", "LUNCH_BREAK", False, True)
    if local_time <= _AFTERNOON_END:
        return MarketSessionState("AFTERNOON", "REALTIME", True, False)
    return MarketSessionState("CLOSED", "CLOSED", False, True)


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
        session = market_session_state(local, closed_dates=set(self.closed_dates))
        if session.health_status == "CLOSED":
            return MarketHealth("CLOSED", None, "非连续交易时段")
        if session.health_status == "LUNCH_BREAK":
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
            # The public feed reports volume in whole units and can truncate an
            # odd-lot remainder smaller than one unit while keeping exact amount.
            lowest_price_with_remainder = amount / (
                (volume + 1.0) * self.trading.volume_unit_shares
            )
            if (
                implied_price < low - tick - epsilon
                or (
                    implied_price > high + tick + epsilon
                    and lowest_price_with_remainder > high + tick + epsilon
                )
            ):
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


def minute_record(quote: Quote, point: QuotePoint) -> dict[str, Any]:
    timestamp = _aware_time(point.timestamp, "分钟时间").astimezone(SHANGHAI)
    observed_at = _aware_time(quote.observed_at, "观测时间").astimezone(SHANGHAI)
    return {
        "schema_version": 3,
        "symbol": quote.symbol,
        "name": quote.name,
        "trading_date": timestamp.astimezone(SHANGHAI).date().isoformat(),
        "timestamp": timestamp.isoformat(),
        "observed_at": observed_at.isoformat(),
        "is_complete": True,
        "source": quote.source,
        "previous_close": quote.previous_close,
        "open": point.open,
        "high": point.high,
        "low": point.low,
        "price": point.price,
        "average_price": point.average_price,
        "volume": point.volume,
        "amount": point.amount,
    }


class MinuteHistoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = _history_lock(self.path)

    @property
    def daily_root(self) -> Path:
        return self.path.parent / "history"

    def upsert(
        self,
        quotes: Mapping[str, Quote],
        metadata: Mapping[str, EtfMetadata],
    ) -> int:
        with self._lock:
            self._recover_interrupted_transaction()
            indexed = self._indexed(self._read_path(self.path, strict=True))
            changed = 0
            for symbol, quote in quotes.items():
                if quote.symbol != symbol:
                    raise MarketDataError(f"行情代码键值不一致: {symbol}/{quote.symbol}")
                item_metadata = metadata.get(symbol)
                if item_metadata is None:
                    raise MarketDataError(f"缺少交易元数据: {symbol}")
                validator = MarketDataValidator(item_metadata.trading)
                for point in finalized_points(quote.points, quote.observed_at):
                    validator.validate_point(point, quote.previous_close)
                    record = minute_record(quote, point)
                    key = (symbol, record["timestamp"])
                    old = indexed.get(key)
                    if old is None or self._observation(record) > self._observation(old):
                        indexed[key] = record
                        changed += 1

            ordered = [indexed[key] for key in sorted(indexed)]
            self._validate_records(ordered, metadata)
            self._validate_previous_closes(ordered, metadata)
            self._replace_history(ordered)
            return changed

    def available_dates(self) -> list[str]:
        with self._lock:
            self._recover_interrupted_transaction()
            dates = {str(item["trading_date"]) for item in self._read_path(self.path)}
            return sorted((value for value in dates if value), reverse=True)

    def query(self, trading_date: str, symbol: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._recover_interrupted_transaction()
            records = [
                item for item in self._read_path(self.path)
                if item["trading_date"] == trading_date
            ]
            return [
                item for item in records
                if symbol is None or item["symbol"] == symbol
            ]

    def merge(self, quotes: Mapping[str, Quote]) -> Mapping[str, Quote]:
        with self._lock:
            self._recover_interrupted_transaction()
            history: dict[str, list[dict[str, Any]]] = {}
            for item in self._read_path(self.path):
                history.setdefault(item["symbol"], []).append(item)
            result: dict[str, Quote] = {}
            for symbol, quote in quotes.items():
                points = {point.timestamp: point for point in quote.points}
                for record in history.get(symbol, ()):
                    try:
                        point = QuotePoint(
                            datetime.fromisoformat(record["timestamp"]),
                            float(record["price"]),
                            float(record["average_price"]),
                            float(record["open"]),
                            float(record["high"]),
                            float(record["low"]),
                            float(record["volume"]),
                            float(record["amount"]),
                            float(record["previous_close"]),
                        )
                    except (KeyError, TypeError, ValueError) as error:
                        raise MarketDataError(f"历史行情记录无效: {error}") from error
                    points.setdefault(point.timestamp, point)
                ordered = tuple(points[key] for key in sorted(points))
                result[symbol] = Quote(
                    quote.symbol,
                    quote.name,
                    quote.price,
                    quote.average_price,
                    quote.previous_close,
                    quote.timestamp,
                    ordered,
                    quote.observed_at,
                    quote.source,
                )
            return MappingProxyType(result)

    def append_legacy(self, quotes: Mapping[str, Quote]) -> None:
        """Preserve the pre-upsert producer API until its metadata wiring is migrated."""
        with self._lock:
            self._recover_interrupted_transaction()
            indexed = self._indexed(self._read_path(self.path, strict=True))
            for quote in quotes.values():
                for point in finalized_points(quote.points, quote.observed_at):
                    record = self._normalize_record(
                        minute_record(quote, point), strict=True,
                    )
                    if record is None:
                        raise MarketDataError("兼容历史记录不能是未完成分钟")
                    key = (record["symbol"], record["timestamp"])
                    old = indexed.get(key)
                    if old is None or self._observation(record) > self._observation(old):
                        indexed[key] = record
            ordered = [indexed[key] for key in sorted(indexed)]
            self._validate_previous_closes(ordered, {})
            self._replace_history(ordered)

    def _replace_history(self, records: Sequence[Mapping[str, Any]]) -> None:
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for record in records:
            grouped.setdefault(str(record["trading_date"]), []).append(record)
        targets: list[tuple[Path, Sequence[Mapping[str, Any]] | None]] = [
            (self.path, records),
        ]
        for trading_date in sorted(grouped):
            path = self.daily_root / trading_date / "quotes.jsonl"
            targets.append((path, grouped[trading_date]))
        expected_daily = {path for path, _ in targets[1:]}
        if self.daily_root.exists():
            for path in sorted(self.daily_root.glob("*/quotes.jsonl")):
                if path not in expected_daily:
                    targets.append((path, None))
        self._replace_many(targets)

    def _replace_many(
        self,
        targets: Sequence[tuple[Path, Sequence[Mapping[str, Any]] | None]],
    ) -> None:
        staged: dict[Path, Path] = {}
        entries: list[dict[str, Any]] = []
        journal_written = False
        try:
            normalized_targets = [
                (path.resolve(strict=False), records) for path, records in targets
            ]
            for path, records in normalized_targets:
                if records is not None:
                    staged[path] = self._stage_records(path, records)
            for path, records in normalized_targets:
                existed = path.exists()
                backup: Path | None = None
                if existed:
                    backup = path.parent / f".{path.name}.{uuid.uuid4().hex}.bak"
                    self._copy_and_fsync(path, backup)
                entries.append({
                    "target": str(path),
                    "backup": str(backup) if backup is not None else None,
                    "existed": existed,
                    "staged": str(staged[path]) if path in staged else None,
                })
            self._write_transaction_journal(entries)
            journal_written = True
            for (path, records), entry in zip(normalized_targets, entries):
                if records is not None:
                    os.replace(staged[path], path)
                staged.pop(path, None)
                entry["staged"] = None
                if records is None and path.exists():
                    path.unlink()
            self._journal_path().unlink()
            journal_written = False
        except BaseException as original_error:
            if journal_written or self._journal_path().exists():
                try:
                    self._recover_interrupted_transaction()
                except MarketDataError as recovery_error:
                    raise MarketDataError(
                        f"历史事务失败且恢复失败: {original_error}; {recovery_error}"
                    ) from original_error
            for path in staged.values():
                self._safe_unlink(path)
            for entry in entries:
                backup_value = entry.get("backup")
                if isinstance(backup_value, str):
                    self._safe_unlink(Path(backup_value))
            raise
        else:
            for path in staged.values():
                self._safe_unlink(path)
            for entry in entries:
                backup_value = entry.get("backup")
                if isinstance(backup_value, str):
                    self._safe_unlink(Path(backup_value))

    def _journal_path(self) -> Path:
        canonical = self.path.resolve(strict=False)
        return canonical.parent / f".{canonical.name}.transaction.json"

    def _write_transaction_journal(self, entries: Sequence[Mapping[str, Any]]) -> None:
        path = self._journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                delete=False,
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(
                    {"schema_version": 1, "targets": list(entries)},
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        except BaseException:
            if temporary_path is not None:
                self._safe_unlink(temporary_path)
            raise

    def _recover_interrupted_transaction(self) -> None:
        journal = self._journal_path()
        if not journal.exists():
            return
        entries = self._load_transaction_journal(journal)
        try:
            for entry in reversed(entries):
                target = entry["target"]
                if entry["existed"]:
                    backup = entry["backup"]
                    if backup is None or not backup.exists():
                        raise OSError(f"事务备份缺失: {target}")
                    restore = target.parent / (
                        f".{target.name}.restore.{uuid.uuid4().hex}.tmp"
                    )
                    try:
                        self._copy_and_fsync(backup, restore)
                        os.replace(restore, target)
                    finally:
                        self._safe_unlink(restore)
                elif target.exists():
                    target.unlink()
        except OSError as error:
            raise MarketDataError(f"历史事务恢复失败: {error}") from error
        try:
            journal.unlink()
        except OSError as error:
            raise MarketDataError(f"历史事务journal清理失败: {error}") from error
        for entry in entries:
            backup = entry["backup"]
            staged_path = entry["staged"]
            if backup is not None:
                self._safe_unlink(backup)
            if staged_path is not None:
                self._safe_unlink(staged_path)

    def _load_transaction_journal(self, journal: Path) -> list[dict[str, Any]]:
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MarketDataError(f"历史事务journal读取失败: {error}") from error
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("targets"), list)
        ):
            raise MarketDataError("历史事务journal格式无效")
        entries: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for value in payload["targets"]:
            if not isinstance(value, Mapping) or type(value.get("existed")) is not bool:
                raise MarketDataError("历史事务journal目标无效")
            target = self._journal_artifact_path(value.get("target"), "target")
            if target in seen or not self._allowed_transaction_target(target):
                raise MarketDataError("历史事务journal目标越界或重复")
            seen.add(target)
            backup_value = value.get("backup")
            backup = None if backup_value is None else self._journal_artifact_path(
                backup_value, "backup",
            )
            if value["existed"] and (
                backup is None
                or backup.parent != target.parent
                or not backup.name.startswith(f".{target.name}.")
                or backup.suffix != ".bak"
            ):
                raise MarketDataError("历史事务journal备份无效")
            if not value["existed"] and backup is not None:
                raise MarketDataError("历史事务journal不存在目标不能有备份")
            staged_value = value.get("staged")
            staged_path = None if staged_value is None else self._journal_artifact_path(
                staged_value, "staged",
            )
            if staged_path is not None and (
                staged_path.parent != target.parent or staged_path.suffix != ".tmp"
            ):
                raise MarketDataError("历史事务journal临时文件无效")
            entries.append({
                "target": target,
                "backup": backup,
                "existed": value["existed"],
                "staged": staged_path,
            })
        if not entries:
            raise MarketDataError("历史事务journal没有目标")
        return entries

    def _journal_artifact_path(self, value: object, label: str) -> Path:
        if not isinstance(value, str) or not value:
            raise MarketDataError(f"历史事务journal {label}路径无效")
        return Path(value).resolve(strict=False)

    def _allowed_transaction_target(self, target: Path) -> bool:
        canonical = self.path.resolve(strict=False)
        if target == canonical:
            return True
        daily_root = self.daily_root.resolve(strict=False)
        return (
            target.name == "quotes.jsonl"
            and target.parent.parent == daily_root
        )

    @staticmethod
    def _copy_and_fsync(source: Path, destination: Path) -> None:
        shutil.copy2(source, destination)
        with destination.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())

    def _stage_records(
        self, path: Path, records: Sequence[Mapping[str, Any]],
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                delete=False,
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            ) as handle:
                temporary_path = Path(handle.name)
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            result = temporary_path
            temporary_path = None
            return result
        except BaseException:
            if temporary_path is not None:
                self._safe_unlink(temporary_path)
            raise

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def _read_path(
        self, path: Path, *, strict: bool = False,
    ) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("历史行情每行必须是对象")
                normalized = self._normalize_record(value, strict=strict)
                if normalized is not None:
                    records.append(normalized)
        except MarketDataError:
            raise
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise MarketDataError(f"历史行情读取失败: {error}") from error
        return records

    def _normalize_record(
        self, record: Mapping[str, Any], *, strict: bool,
    ) -> dict[str, Any] | None:
        if strict:
            required = {
                "schema_version", "symbol", "name", "trading_date", "timestamp",
                "observed_at", "is_complete", "source", "previous_close", "open",
                "high", "low", "price", "average_price", "volume", "amount",
            }
            missing = sorted(required - set(record))
            if missing:
                raise MarketDataError("历史行情缺少字段: " + ",".join(missing))
            if type(record.get("schema_version")) is not int or record.get("schema_version") not in {2, 3}:
                raise MarketDataError("历史行情schema_version无效")
        symbol = record.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise MarketDataError("历史行情记录缺少symbol")
        timestamp = self._timestamp(record.get("timestamp"), "分钟时间")
        trading_date = timestamp.astimezone(SHANGHAI).date().isoformat()
        supplied_date = record.get("trading_date")
        if supplied_date is not None and supplied_date != trading_date:
            raise MarketDataError(f"{symbol}历史行情trading_date与timestamp不一致")
        if "observed_at" not in record or "is_complete" not in record:
            if strict:
                raise MarketDataError(f"{symbol}历史行情缺少observed_at或is_complete")
            return None
        is_complete = record.get("is_complete")
        if type(is_complete) is not bool:
            raise MarketDataError(f"{symbol}历史行情is_complete无效")
        if not is_complete:
            if strict:
                raise MarketDataError(f"{symbol}历史行情包含未完成分钟")
            return None
        observed_at = self._timestamp(record.get("observed_at"), "观测时间")
        if observed_at < timestamp + timedelta(minutes=1):
            raise MarketDataError(f"{symbol}历史行情包含未完成分钟")
        price = self._number(record.get("price"), "收盘价", positive=True)
        average_price = self._number(
            record.get("average_price", price), "均价", positive=True,
        )
        open_value = record.get("open")
        high_value = record.get("high")
        low_value = record.get("low")
        if not strict:
            open_value = price if open_value is None else open_value
            high_value = price if high_value is None else high_value
            low_value = price if low_value is None else low_value
        open_price = self._number(open_value, "开盘价", positive=True)
        high = self._number(high_value, "最高价", positive=True)
        low = self._number(low_value, "最低价", positive=True)
        previous_close = self._number(record.get("previous_close"), "昨收", positive=True)
        volume = self._number(record.get("volume", 0.0), "成交量", positive=False)
        amount = self._number(record.get("amount", 0.0), "成交额", positive=False)
        source = record.get("source", "LEGACY_HISTORY")
        if not isinstance(source, str) or not source.strip():
            raise MarketDataError(f"{symbol}历史行情source无效")
        name = record.get("name", symbol)
        if not isinstance(name, str) or not name.strip():
            raise MarketDataError(f"{symbol}历史行情name无效")
        return {
            "schema_version": 3,
            "symbol": symbol,
            "name": name,
            "trading_date": trading_date,
            "timestamp": timestamp.isoformat(),
            "observed_at": observed_at.isoformat(),
            "is_complete": True,
            "source": source,
            "previous_close": previous_close,
            "open": open_price,
            "high": high,
            "low": low,
            "price": price,
            "average_price": average_price,
            "volume": volume,
            "amount": amount,
        }

    def _validate_records(
        self,
        records: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, EtfMetadata],
    ) -> None:
        for record in records:
            symbol = str(record["symbol"])
            item_metadata = metadata.get(symbol)
            if item_metadata is None:
                raise MarketDataError(f"缺少交易元数据: {symbol}")
            point = QuotePoint(
                self._timestamp(record.get("timestamp"), "分钟时间"),
                float(record["price"]),
                float(record["average_price"]),
                float(record["open"]),
                float(record["high"]),
                float(record["low"]),
                float(record["volume"]),
                float(record["amount"]),
            )
            MarketDataValidator(item_metadata.trading).validate_point(
                point, float(record["previous_close"]),
            )

    def _validate_previous_closes(
        self,
        records: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, EtfMetadata],
    ) -> None:
        grouped: dict[tuple[str, str], list[float]] = {}
        for record in records:
            key = (str(record["symbol"]), str(record["trading_date"]))
            grouped.setdefault(key, []).append(float(record["previous_close"]))
        for (symbol, trading_date), values in grouped.items():
            item_metadata = metadata.get(symbol)
            tolerance = item_metadata.trading.price_tick if item_metadata is not None else 0.0
            epsilon = max(1.0, max(values)) * 1e-12
            if max(values) - min(values) > tolerance + epsilon:
                raise MarketDataError(f"{symbol} {trading_date} 历史昨收不一致")

    def _indexed(
        self, records: Sequence[dict[str, Any]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for record in records:
            key = (record["symbol"], record["timestamp"])
            old = result.get(key)
            if old is None or self._observation(record) > self._observation(old):
                result[key] = record
        return result

    def _observation(self, record: Mapping[str, Any]) -> datetime:
        return self._timestamp(record.get("observed_at"), "观测时间")

    @staticmethod
    def _timestamp(value: object, label: str) -> datetime:
        if not isinstance(value, str):
            raise MarketDataError(f"历史行情{label}必须是ISO时间")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise MarketDataError(f"历史行情{label}无效: {value}") from error
        return _aware_time(parsed, label).astimezone(SHANGHAI)

    @staticmethod
    def _number(value: object, label: str, *, positive: bool) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MarketDataError(f"历史行情{label}无效")
        number = float(value)
        if not math.isfinite(number) or (number <= 0 if positive else number < 0):
            raise MarketDataError(f"历史行情{label}无效")
        return number


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
