"""Explicit ETF history staging, separate from strategy activation.

Run ``stage`` to collect and inspect a private batch. Stop the local monitor
before ``apply``: its in-memory history must not race the backup/merge or remain
stale afterwards. This module never reads or writes the strategy watchlist.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Protocol
import uuid

from .etf_metadata import EtfMetadata, EtfMetadataStore
from .market_data import load_closed_dates
from .swing_collector import EastmoneyDailyCollector
from .swing_config import SwingWatchItem, load_strategy
from .swing_data import DailyBar, DailyBarValidator, DailyHistoryStore, SHANGHAI


class HistoryBootstrapError(ValueError):
    """Sanitized bootstrap failure, suitable for operator-facing output."""


@dataclass(frozen=True)
class BootstrapPaths:
    root: Path = field(default_factory=Path.cwd)

    @property
    def metadata(self) -> Path:
        return Path(self.root) / "data/monitor/etf_metadata.json"

    @property
    def calendar(self) -> Path:
        return Path(self.root) / "data/monitor/market_calendar.json"

    @property
    def strategy(self) -> Path:
        return Path(self.root) / "data/swing/strategy.json"

    @property
    def history(self) -> Path:
        return Path(self.root) / "var/swing/daily_quotes.jsonl"

    @property
    def staging(self) -> Path:
        return Path(self.root) / "var/swing/onboarding"


class DailyCollector(Protocol):
    def collect(
        self, watchlist: Sequence[SwingWatchItem], last_completed_date: date,
        count: int,
    ) -> Sequence[DailyBar]: ...


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False,
                       sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _request(
    symbols: Sequence[str], end_date: date | str, count: int,
    paths: BootstrapPaths, now: Callable[[], datetime] | None,
) -> tuple[tuple[str, ...], date, dict[str, EtfMetadata], set[date], int]:
    if not isinstance(symbols, Sequence) or isinstance(symbols, (str, bytes)):
        raise HistoryBootstrapError("symbols必须是显式证券代码序列")
    selected = tuple(symbols)
    if not selected or any(type(s) is not str or len(s) != 6 or not s.isascii()
                           or not s.isdigit() for s in selected):
        raise HistoryBootstrapError("symbols必须包含6位ASCII证券代码")
    if len(set(selected)) != len(selected):
        raise HistoryBootstrapError("symbols不能重复")
    try:
        metadata = EtfMetadataStore(paths.metadata).load()
        closed_dates = load_closed_dates(paths.calendar)
        strategy = load_strategy(paths.strategy)
    except Exception:
        raise HistoryBootstrapError("当前ETF元数据、交易日历或策略配置加载失败") from None
    if any(symbol not in metadata for symbol in selected):
        raise HistoryBootstrapError("仅正式ETF元数据中的代码可准备；未知或观察登记代码不可使用")
    minimum = max(70, strategy.minimum_daily_bars)
    if type(count) is not int or not minimum <= count <= 10_000:
        raise HistoryBootstrapError(f"count必须是{minimum}到10000之间的整数")
    try:
        target = date.fromisoformat(end_date) if type(end_date) is str else end_date
        if type(target) is not date or (type(end_date) is str and target.isoformat() != end_date):
            raise ValueError
    except (TypeError, ValueError):
        raise HistoryBootstrapError("end_date必须使用YYYY-MM-DD日期") from None
    current = datetime.now(SHANGHAI) if now is None else now()
    if type(current) is not datetime or current.tzinfo is None or current.utcoffset() is None:
        raise HistoryBootstrapError("当前时间必须带时区")
    current = current.astimezone(SHANGHAI)
    if target.weekday() >= 5 or target in closed_dates:
        raise HistoryBootstrapError("目标日期必须是交易日")
    if target > current.date() or (
        target == current.date() and current.time().replace(tzinfo=None) < time(15, 10)
    ):
        raise HistoryBootstrapError("目标日期尚未完成；当日必须等到上海时间15:10后")
    return tuple(sorted(selected)), target, metadata, closed_dates, minimum


def _validate_batch(
    records: Sequence[DailyBar], selected: tuple[str, ...], target: date,
    count: int, minimum: int, metadata: dict[str, EtfMetadata], closed_dates: set[date],
) -> tuple[tuple[DailyBar, ...], dict[str, Any]]:
    try:
        batch_limit = count * len(selected)
        bars = tuple(islice(records, batch_limit + 1))
        if len(bars) > batch_limit:
            raise HistoryBootstrapError("批次条数超过请求上限")
        if any(type(bar) is not DailyBar for bar in bars):
            raise ValueError
        if {bar.symbol for bar in bars} != set(selected):
            raise HistoryBootstrapError("批次包含未选择代码或缺少所选代码")
        validator = DailyBarValidator(closed_dates)
        for bar in bars:
            validator.validate(bar, metadata[bar.symbol])
        bars = tuple(sorted(bars, key=lambda bar: (bar.symbol, bar.trading_date)))
        validator.validate_sequence(bars, metadata)
    except HistoryBootstrapError:
        raise
    except Exception:
        raise HistoryBootstrapError("日线未通过日期、完整性、OHLC、昨收或量价校验") from None
    summary = {}
    for symbol in selected:
        per_symbol = tuple(bar for bar in bars if bar.symbol == symbol)
        if len(per_symbol) < minimum:
            raise HistoryBootstrapError(f"{symbol}日线不足：需要至少{minimum}条，实际{len(per_symbol)}条")
        if len(per_symbol) > count:
            raise HistoryBootstrapError(f"{symbol}日线条数超过请求上限{count}")
        if per_symbol[-1].trading_date != target:
            raise HistoryBootstrapError(f"{symbol}最后交易日不等于目标日期")
        sources = sorted({bar.source for bar in per_symbol})
        estimated = any("估算" in source or "estimat" in source.lower() for source in sources)
        summary[symbol] = {
            "count": len(per_symbol), "latest": target.isoformat(), "source": sources,
            "warnings": ["成交额包含估算值，不能据此核验未知成交量单位"] if estimated else [],
        }
    return bars, summary


def stage_history(
    symbols: Sequence[str], end_date: date | str, count: int = 240,
    paths: BootstrapPaths | None = None, collector: DailyCollector | None = None,
    *, now: Callable[[], datetime] | None = None,
) -> Path:
    """Collect verified explicit symbols into a unique private stage directory.

    Returns an absolute directory containing canonical ``daily_quotes.jsonl``,
    ``manifest.json`` and ``manifest.sha256``. The temporary enabled flags are
    transport input only; no formal history or strategy switch is written.
    ``count`` is the requested maximum per symbol, not the acceptance minimum:
    at least max(70, current strategy.minimum_daily_bars) bars are required.
    """
    paths = BootstrapPaths() if paths is None else paths
    selected, target, metadata, closures, minimum = _request(symbols, end_date, count, paths, now)
    collector = EastmoneyDailyCollector() if collector is None else collector
    try:
        incoming = collector.collect(tuple(SwingWatchItem(s, True) for s in selected), target, count)
    except Exception:
        raise HistoryBootstrapError("历史行情采集失败；未写入正式历史") from None
    bars, summary = _validate_batch(incoming, selected, target, count, minimum, metadata, closures)
    try:
        paths.staging.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="history-", dir=paths.staging)).resolve()
        staged_history = stage / "daily_quotes.jsonl"
        DailyHistoryStore(staged_history, metadata, closures).upsert(bars)
        manifest = {
            "schema_version": 1, "symbols": list(selected), "end_date": target.isoformat(),
            "requested_count": count, "row_count": len(bars), "summary": summary,
            "sha256": hashlib.sha256(staged_history.read_bytes()).hexdigest(),
        }
        encoded = _json_bytes(manifest)
        _write_new(stage / "manifest.json", encoded)
        _write_new(stage / "manifest.sha256", (hashlib.sha256(encoded).hexdigest() + "\n").encode("ascii"))
        return stage
    except Exception:
        raise HistoryBootstrapError("历史暂存写入失败；未写入正式历史") from None


def apply_history(
    stage_dir: Path | str, paths: BootstrapPaths | None = None,
    *, now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Revalidate, back up exact old bytes and atomically merge a staged batch.

    The operator must stop the monitor and other history writers first, then
    restart it after apply. Checksums detect corruption, not a malicious actor
    able to rewrite the batch and all of its checksums together.
    """
    paths = BootstrapPaths() if paths is None else paths
    stage = Path(stage_dir).resolve()
    try:
        encoded = (stage / "manifest.json").read_bytes()
        expected_manifest_hash = (hashlib.sha256(encoded).hexdigest() + "\n").encode("ascii")
        if (stage / "manifest.sha256").read_bytes() != expected_manifest_hash:
            raise ValueError
        manifest = json.loads(encoded)
        if (type(manifest) is not dict or set(manifest) != {
            "schema_version", "symbols", "end_date", "requested_count", "row_count", "summary", "sha256",
        } or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
                or _json_bytes(manifest) != encoded):
            raise ValueError
        content = (stage / "daily_quotes.jsonl").read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest["sha256"]:
            raise ValueError
    except Exception:
        raise HistoryBootstrapError("暂存文件或manifest哈希校验失败；拒绝应用") from None
    selected, target, metadata, closures, minimum = _request(
        manifest["symbols"], manifest["end_date"], manifest["requested_count"], paths, now,
    )
    try:
        staged = DailyHistoryStore(stage / "daily_quotes.jsonl", metadata, closures).load()
        # The store enforces canonical serialization and full-sequence validity.
        bars, summary = _validate_batch(staged, selected, target, manifest["requested_count"], minimum, metadata, closures)
        if type(manifest["row_count"]) is not int or len(bars) != manifest["row_count"] or summary != manifest["summary"]:
            raise HistoryBootstrapError("暂存条数、日期或来源摘要不一致")
        if (stage / "daily_quotes.jsonl").read_bytes() != content:
            raise HistoryBootstrapError("暂存数据在校验期间改变；拒绝应用")
        store = DailyHistoryStore(paths.history, metadata, closures)
        current = store.load()
        history_existed = paths.history.exists()
        original = paths.history.read_bytes() if history_existed else b""
        if original != b"".join(_json_bytes(bar.to_dict()) for bar in current):
            raise HistoryBootstrapError("正式历史在校验期间改变；请停止服务再应用")
        backup_root = paths.history.parent / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = (backup_root / f"daily_quotes-{uuid.uuid4().hex}.jsonl").resolve()
        _write_new(backup, original)
        merged = store.upsert(bars)
        return {"stage_dir": str(stage), "backup_path": str(backup),
                "history_existed": history_existed, "total_count": len(merged), "summary": summary}
    except HistoryBootstrapError:
        raise
    except Exception:
        raise HistoryBootstrapError("当前历史或暂存数据复验、备份或合并失败；请检查数据并保持服务停止") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="准备独立ETF日线，不启用策略；apply前必须停止本地服务")
    commands = parser.add_subparsers(dest="command", required=True)
    staging = commands.add_parser("stage", help="仅收集、校验并暂存历史")
    staging.add_argument("--symbols", nargs="+", required=True)
    staging.add_argument("--end-date", required=True)
    staging.add_argument("--count", type=int, default=240)
    applying = commands.add_parser("apply", help="先停止本地服务，再备份并合并暂存历史")
    applying.add_argument("--stage-dir", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "stage":
            stage = stage_history(arguments.symbols, arguments.end_date, arguments.count)
            result = {"stage_dir": str(stage), **json.loads((stage / "manifest.json").read_bytes())}
        else:
            print("应用前必须停止本地监控服务及其他历史写入进程；应用后重启。本命令不启用watchlist。", file=sys.stderr)
            result = apply_history(arguments.stage_dir)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except HistoryBootstrapError as error:
        print(str(error), file=sys.stderr)
    except Exception:
        print("历史准备命令失败；未输出外部异常详情", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
