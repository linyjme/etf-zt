"""Strict completed daily-bar schema and cross-record validation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import errno
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time as _time
from types import MappingProxyType
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
_FILE_LOCKS_GUARD = threading.Lock()
_FILE_LOCKS: dict[str, threading.RLock] = {}
_WINDOWS_LOCK_RETRY_SECONDS = 0.01
_WINDOWS_LOCK_CONTENTION_ERRNOS = frozenset((errno.EACCES, errno.EAGAIN))
_WINDOWS_LOCK_CONTENTION_WINERRORS = frozenset((32, 33))


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
            raise SwingDataError("日线记录映射读取失败") from error

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
            type(symbol) is not str
            or len(symbol) != 6
            or not symbol.isascii()
            or not symbol.isdigit()
        ):
            raise SwingDataError("日线symbol必须是6位ASCII数字")
        trading_date = _iso_date(payload["trading_date"])
        observed_at = _iso_datetime(payload["observed_at"])
        source = payload["source"]
        if type(source) is not str or not source.strip():
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
        _validate_direct_json_scalar_types(self)
        try:
            trading_date = self.trading_date.isoformat()
            observed_at = self.observed_at
            if observed_at.tzinfo is not None and observed_at.utcoffset() is not None:
                observed_at = observed_at.astimezone(SHANGHAI)
            observed_at_text = observed_at.isoformat()
        except Exception as error:
            raise SwingDataError("日线记录序列化失败") from error
        if type(trading_date) is not str:
            raise SwingDataError("日线trading_date序列化结果必须是字符串")
        if type(observed_at_text) is not str:
            raise SwingDataError("日线observed_at序列化结果必须是字符串")
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "trading_date": trading_date,
            "observed_at": observed_at_text,
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
            raise SwingDataError("休市日期集合无效") from error
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
                raise SwingDataError("ETF元数据映射读取失败") from error
            if metadata is None:
                raise SwingDataError(f"缺少ETF元数据: {normalized.symbol}")
            self.validate(normalized, metadata)

            previous = previous_by_symbol.get(normalized.symbol)
            if previous is not None:
                self._validate_adjacent(previous, normalized, metadata)
            previous_by_symbol[normalized.symbol] = normalized

    @staticmethod
    def _normalize_record(record: DailyBar) -> DailyBar:
        if type(record) is not DailyBar:
            raise SwingDataError("日线record必须是DailyBar")
        _validate_direct_record_types(record)
        try:
            payload = record.to_dict()
        except SwingDataError:
            raise
        except Exception as error:
            raise SwingDataError("日线record序列化失败") from error
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


def _process_file_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FILE_LOCKS[key] = lock
        return lock


class _SiblingFileLock:
    """Cross-instance and cross-process lock held on a stable sibling file."""

    def __init__(self, path: Path, shared: bool):
        canonical = Path(path).resolve(strict=False)
        self.path = canonical.parent / f".{canonical.name}.lock"
        self.shared = bool(shared)
        self._process_lock = _process_file_lock(self.path)
        self._handle: Any | None = None

    def __enter__(self) -> _SiblingFileLock:
        self._process_lock.acquire()
        try:
            self._handle = self.path.open("a+b")
            self._acquire_platform_lock()
        except BaseException:
            handle = self._handle
            self._handle = None
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            self._process_lock.release()
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        release_error: BaseException | None = None
        handle = self._handle
        self._handle = None
        try:
            if handle is not None:
                try:
                    self._release_platform_lock(handle)
                except BaseException as error:
                    release_error = error
                try:
                    handle.close()
                except BaseException as error:
                    if release_error is None:
                        release_error = error
        finally:
            self._process_lock.release()
        if release_error is not None and exc_type is None:
            raise release_error
        return False

    def _acquire_platform_lock(self) -> None:
        handle = self._handle
        if handle is None:
            raise RuntimeError("lock file is not open")
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            mode = msvcrt.LK_NBRLCK if self.shared else msvcrt.LK_NBLCK
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), mode, 1)
                    return
                except OSError as error:
                    if not self._windows_lock_contended(error):
                        raise
                _time.sleep(_WINDOWS_LOCK_RETRY_SECONDS)
        else:
            import fcntl

            mode = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
            fcntl.flock(handle.fileno(), mode)

    @staticmethod
    def _windows_lock_contended(error: OSError) -> bool:
        return (
            error.errno in _WINDOWS_LOCK_CONTENTION_ERRNOS
            or getattr(error, "winerror", None) in _WINDOWS_LOCK_CONTENTION_WINERRORS
        )

    @staticmethod
    def _release_platform_lock(handle: Any) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class DailyHistoryStore:
    """Persist an authoritative canonical JSONL history of completed daily bars."""

    def __init__(
        self,
        path: Path,
        metadata: Mapping[str, EtfMetadata],
        closed_dates: Iterable[date],
    ):
        self.path = Path(path)
        self.metadata = self._snapshot_metadata(metadata)
        self.validator = DailyBarValidator(closed_dates)

    def load(self) -> tuple[DailyBar, ...]:
        if not self.path.parent.exists():
            return ()
        with _SiblingFileLock(self.path, shared=True):
            return self._load_unlocked()

    def query(self, symbol: str) -> tuple[DailyBar, ...]:
        if (
            type(symbol) is not str
            or len(symbol) != 6
            or not symbol.isascii()
            or not symbol.isdigit()
        ):
            raise SwingDataError("查询symbol必须是6位ASCII数字")
        return tuple(record for record in self.load() if record.symbol == symbol)

    def upsert(self, records: Sequence[DailyBar]) -> tuple[DailyBar, ...]:
        incoming = self._materialize_incoming(records)
        if not incoming:
            return self.load()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _SiblingFileLock(self.path, shared=False):
            current = self._load_unlocked()
            indexed = {
                (record.symbol, record.trading_date): record for record in current
            }
            for record in incoming:
                key = (record.symbol, record.trading_date)
                existing = indexed.get(key)
                if existing is None or record.observed_at >= existing.observed_at:
                    indexed[key] = record
            merged = tuple(indexed[key] for key in sorted(indexed))
            self.validator.validate_sequence(merged, self.metadata)
            if merged != current:
                self._atomic_replace(merged)
            return merged

    @staticmethod
    def _snapshot_metadata(
        metadata: Mapping[str, EtfMetadata],
    ) -> Mapping[str, EtfMetadata]:
        if not isinstance(metadata, Mapping):
            raise SwingDataError("ETF元数据必须是映射")
        try:
            items = tuple(metadata.items())
            snapshot: dict[str, EtfMetadata] = {}
            for key, value in items:
                if type(key) is not str:
                    raise SwingDataError("ETF元数据symbol必须是字符串")
                snapshot[key] = value
        except SwingDataError:
            raise
        except Exception as error:
            raise SwingDataError("ETF元数据映射读取失败") from error
        return MappingProxyType(snapshot)

    def _materialize_incoming(
        self, records: Sequence[DailyBar],
    ) -> tuple[DailyBar, ...]:
        try:
            materialized = tuple(records)
        except Exception as error:
            raise SwingDataError("日线批次读取失败") from error

        reduced: dict[tuple[str, date], DailyBar] = {}
        for record in materialized:
            if type(record) is not DailyBar:
                raise SwingDataError("日线record必须是DailyBar")
            try:
                metadata = self.metadata.get(record.symbol)
            except Exception as error:
                raise SwingDataError("ETF元数据映射读取失败") from error
            if metadata is None:
                raise SwingDataError(f"缺少ETF元数据: {record.symbol}")
            self.validator.validate(record, metadata)
            key = (record.symbol, record.trading_date)
            existing = reduced.get(key)
            if existing is None or record.observed_at >= existing.observed_at:
                reduced[key] = record
        return tuple(reduced[key] for key in sorted(reduced))

    def _load_unlocked(self) -> tuple[DailyBar, ...]:
        try:
            content = self.path.read_bytes()
        except FileNotFoundError:
            return ()
        if content == b"":
            return ()
        if not content.endswith(b"\n"):
            raise SwingDataError("日线历史末行不完整")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SwingDataError("日线历史不是有效UTF-8") from error

        records: list[DailyBar] = []
        for line_number, line in enumerate(text[:-1].split("\n"), start=1):
            if not line:
                raise SwingDataError(f"日线历史第{line_number}行为空")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise SwingDataError(f"日线历史第{line_number}行JSON无效") from error
            if type(value) is not dict:
                raise SwingDataError(f"日线历史第{line_number}行必须是对象")
            try:
                record = DailyBar.from_mapping(value)
            except SwingDataError as error:
                raise SwingDataError(f"日线历史第{line_number}行无效") from error
            if line != self._encode_record(record):
                raise SwingDataError(f"日线历史第{line_number}行不是规范JSON")
            records.append(record)
        result = tuple(records)
        self.validator.validate_sequence(result, self.metadata)
        return result

    def _atomic_replace(self, records: Sequence[DailyBar]) -> None:
        lines = tuple(self._encode_record(record) + "\n" for record in records)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                delete=False,
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            ) as handle:
                temporary_path = Path(handle.name)
                for line in lines:
                    handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        except BaseException:
            if temporary_path is not None:
                self._safe_unlink(temporary_path)
            raise

    @staticmethod
    def _encode_record(record: DailyBar) -> str:
        return json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink()
        except BaseException:
            pass


def _iso_date(value: object) -> date:
    if type(value) is not str:
        raise SwingDataError("日线trading_date必须是ISO日期")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise SwingDataError(f"日线trading_date无效: {value}") from error
    if parsed.isoformat() != value:
        raise SwingDataError(f"日线trading_date必须使用YYYY-MM-DD格式: {value}")
    return parsed


def _iso_datetime(value: object) -> datetime:
    if type(value) is not str:
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
    if type(value) not in (int, float):
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
        raise SwingDataError(f"{label}数值运算失败") from error
    if not math.isfinite(number):
        raise SwingDataError(f"{label}数值运算结果必须有限")
    return number


def _one_tick_tolerance(tick: object, *operands: object) -> float:
    tick_value = _positive_number(tick, "price_tick")
    values = tuple(_finite_number(value, "价格边界") for value in operands)
    try:
        max_ulp = max(math.ulp(value) for value in values)
    except Exception as error:
        raise SwingDataError("价格边界精度计算失败") from error
    resolution = _safe_product(max_ulp, _ULP_MULTIPLIER, "价格边界ULP容差")
    if tick_value < resolution:
        raise SwingDataError("最小价位低于当前价格数量级的可表示精度")
    return _safe_add(tick_value, resolution, "一个最小价位容差")


def _validate_direct_record_types(record: DailyBar) -> None:
    _validate_direct_json_scalar_types(record)
    if type(record.trading_date) is not date:
        raise SwingDataError("日线record.trading_date类型无效")
    if type(record.observed_at) is not datetime:
        raise SwingDataError("日线record.observed_at类型无效")


def _validate_direct_json_scalar_types(record: DailyBar) -> None:
    if type(record.schema_version) is not int:
        raise SwingDataError("日线record.schema_version类型无效")
    if type(record.symbol) is not str:
        raise SwingDataError("日线record.symbol类型无效")
    if type(record.source) is not str:
        raise SwingDataError("日线record.source类型无效")
    for field in (*_PRICE_FIELDS, "volume", "amount"):
        value = getattr(record, field)
        if type(value) not in (int, float):
            raise SwingDataError(f"日线record.{field}类型无效")
        if type(value) is float and not math.isfinite(value):
            raise SwingDataError(f"日线record.{field}必须是有限数字")
    if type(record.is_final) is not bool:
        raise SwingDataError("日线record.is_final类型无效")
