from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping
import uuid

from .etf_metadata import EtfMetadataStore, MetadataError
from .market_data import MinuteHistoryStore
from .t_monitor import JsonQuoteAdapter, MarketDataError


def rebuild_history(source: Path, output: Path, metadata_path: Path) -> int:
    """Validate a closing snapshot and atomically replace one runtime history root."""
    source = Path(source)
    output = Path(output)
    metadata_path = Path(metadata_path)
    try:
        quotes = JsonQuoteAdapter().load(source)
        metadata = EtfMetadataStore(metadata_path).load()
    except MetadataError as error:
        raise MarketDataError(str(error)) from error

    missing = sorted(set(quotes) - set(metadata))
    if missing:
        raise MarketDataError("缺少交易元数据: " + ",".join(missing))
    if not quotes:
        raise MarketDataError("收盘快照不包含行情")

    destination_root = output.parent.resolve(strict=False)
    destination_parent = destination_root.parent
    destination_parent.mkdir(parents=True, exist_ok=True)
    staged_root = Path(tempfile.mkdtemp(
        prefix=".monitor-rebuild-", dir=destination_parent,
    ))
    backup_root = destination_parent / f".monitor-backup-{uuid.uuid4().hex}"
    staged_output = staged_root / output.name
    replaced_old = False
    installed_new = False
    try:
        count = MinuteHistoryStore(staged_output).upsert(quotes, metadata)
        _audit_history(staged_output, expected_symbols=set(quotes))
        if destination_root.exists():
            _copy_unrelated_runtime_files(
                destination_root, staged_root, history_name=output.name,
            )
            os.replace(destination_root, backup_root)
            replaced_old = True
        try:
            os.replace(staged_root, destination_root)
            installed_new = True
        except OSError:
            if replaced_old:
                os.replace(backup_root, destination_root)
                replaced_old = False
            raise
        if replaced_old:
            try:
                shutil.rmtree(backup_root)
            except OSError:
                # Installation already succeeded; retain the recoverable backup
                # rather than reporting a false migration failure.
                pass
            replaced_old = False
        return count
    finally:
        if not installed_new and staged_root.exists():
            shutil.rmtree(staged_root, ignore_errors=True)
        if replaced_old and backup_root.exists() and not destination_root.exists():
            os.replace(backup_root, destination_root)


def _copy_unrelated_runtime_files(
    source_root: Path, staged_root: Path, *, history_name: str,
) -> None:
    for source in source_root.iterdir():
        if source.name in {history_name, "history"}:
            continue
        destination = staged_root / source.name
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)


def _audit_history(path: Path, *, expected_symbols: set[str]) -> None:
    try:
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as error:
        raise MarketDataError(f"重建历史审计失败: {error}") from error
    if not records or not all(isinstance(item, Mapping) for item in records):
        raise MarketDataError("重建历史为空或记录格式无效")
    keys: set[tuple[str, str]] = set()
    symbols: set[str] = set()
    for raw in records:
        item: Mapping[str, Any] = raw
        required = (
            item.get("schema_version") == 3,
            item.get("is_complete") is True,
            isinstance(item.get("trading_date"), str) and bool(item["trading_date"]),
            isinstance(item.get("observed_at"), str) and bool(item["observed_at"]),
            isinstance(item.get("symbol"), str) and bool(item["symbol"]),
            isinstance(item.get("timestamp"), str) and bool(item["timestamp"]),
        )
        if not all(required):
            raise MarketDataError("重建历史包含非schema v3完整分钟")
        key = (str(item["symbol"]), str(item["timestamp"]))
        if key in keys:
            raise MarketDataError("重建历史包含重复分钟")
        keys.add(key)
        symbols.add(str(item["symbol"]))
    if symbols != expected_symbols:
        missing = sorted(expected_symbols - symbols)
        extra = sorted(symbols - expected_symbols)
        raise MarketDataError(
            "重建历史标的不完整: missing=" + ",".join(missing)
            + "; extra=" + ",".join(extra)
        )
