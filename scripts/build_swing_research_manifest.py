"""Build a read-only research manifest from the canonical swing history.

The manifest is a research artifact.  It never promotes data, changes the
runtime watchlist, or changes the formal monitor strategy.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import argparse
import hashlib
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from zoneinfo import ZoneInfo

from etf_rotation.swing_data import DailyBar, DailyBarValidator, SwingDataError
from etf_rotation.etf_metadata import EtfMetadataStore, MetadataError
from etf_rotation.swing_research import ResearchStatus, assess_history


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _load_history(path: Path) -> tuple[tuple[DailyBar, ...], dict[str, int]]:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (), {}
    if not content:
        return (), {}
    bars: list[DailyBar] = []
    invalid_symbols: dict[str, int] = {}
    for line_number, line in enumerate(content.splitlines(), start=1):
        value: object = None
        try:
            if not line:
                raise ValueError("empty line")
            value = json.loads(line)
            bars.append(DailyBar.from_mapping(value))
        except (ValueError, TypeError, RecursionError, SwingDataError) as error:
            symbol = (
                value.get("symbol")
                if isinstance(value, dict) and isinstance(value.get("symbol"), str)
                else "__UNKNOWN__"
            )
            invalid_symbols[symbol] = invalid_symbols.get(symbol, 0) + 1
    return tuple(bars), invalid_symbols


def _load_watchlist(path: Path) -> tuple[str, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, TypeError, RecursionError) as error:
        raise ValueError("watchlist is not valid JSON") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("watchlist schema_version must be 1")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("watchlist items must be a list")
    symbols: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("watchlist item must be an object")
        symbol = item.get("symbol")
        if item.get("enabled") is True:
            if (
                type(symbol) is not str
                or len(symbol) != 6
                or not symbol.isascii()
                or not symbol.isdigit()
            ):
                raise ValueError("enabled watchlist symbol must be six digits")
            if symbol not in symbols:
                symbols.append(symbol)
    return tuple(symbols)


def _quality_for(bars: tuple[DailyBar, ...]) -> tuple[str, str, str]:
    sources = {bar.source for bar in bars}
    if not sources:
        amount_quality = "UNKNOWN"
    elif any("估算" in source for source in sources):
        amount_quality = "ESTIMATED"
    elif all("东方财富" in source for source in sources):
        amount_quality = "PROVIDER_REPORTED"
    else:
        amount_quality = "UNKNOWN"

    ratios = {round(bar.close / bar.adjusted_close, 12) for bar in bars}
    adjustment_status = "REVIEW" if len(ratios) > 1 else "UNKNOWN"
    # An independent receipt is not present in the legacy canonical file.
    crosscheck_status = "PENDING"
    return crosscheck_status, adjustment_status, amount_quality


def _source_receipts(wind_root: Path | None) -> list[dict[str, str]]:
    if wind_root is None or not wind_root.exists():
        return []
    receipts: list[dict[str, str]] = []
    for path in sorted(wind_root.rglob("manifest.json")):
        try:
            content = path.read_bytes()
        except OSError:
            continue
        receipts.append({
            "path": str(path),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    return receipts


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def build_manifest(
    history_path: Path,
    watchlist_path: Path,
    metadata_path: Path | None = None,
    *,
    output_path: Path | None = None,
    wind_root: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Return and optionally persist a deterministic research manifest."""

    history_path = Path(history_path)
    watchlist_path = Path(watchlist_path)
    if output_path is not None and Path(output_path).resolve() == history_path.resolve():
        raise ValueError("output_path must not overwrite runtime history")
    original_history = (
        history_path.read_bytes() if history_path.exists() else None
    )
    bars_by_symbol: dict[str, list[DailyBar]] = defaultdict(list)
    history_bars, invalid_symbols = _load_history(history_path)
    for bar in history_bars:
        bars_by_symbol[bar.symbol].append(bar)
    symbols = _load_watchlist(watchlist_path)
    metadata: dict[str, Any] = {}
    if metadata_path is not None:
        try:
            metadata = EtfMetadataStore(Path(metadata_path)).load()
        except (OSError, MetadataError) as error:
            raise ValueError("ETF metadata is invalid") from error

    items: list[dict[str, Any]] = []
    for symbol in symbols:
        bars = tuple(bars_by_symbol.get(symbol, ()))
        crosscheck_status, adjustment_status, amount_quality = _quality_for(bars)
        assessment = assess_history(
            bars,
            crosscheck_status=crosscheck_status,
            adjustment_status=adjustment_status,
            amount_quality=amount_quality,
        )
        warnings = list(assessment.warnings)
        if invalid_symbols.get(symbol, 0):
            warnings.append("INVALID_HISTORY_RECORD")
        metadata_item = metadata.get(symbol) if metadata_path is not None else None
        if metadata_path is not None and metadata_item is None:
            warnings.append("MISSING_METADATA")
        metadata_validation_status = "NOT_RUN"
        if metadata_item is not None and bars:
            validator = DailyBarValidator(())
            try:
                for bar in bars:
                    validator.validate(bar, metadata_item)
            except SwingDataError:
                warnings.append("METADATA_VALIDATION_FAILED")
                metadata_validation_status = "FAILED"
            else:
                metadata_validation_status = "PASSED"
        status = assessment.status
        if any(item in warnings for item in (
            "INVALID_HISTORY_RECORD", "MISSING_METADATA", "METADATA_VALIDATION_FAILED",
        )):
            status = ResearchStatus.EXCLUDED
        sample_class = (
            "NO_SAMPLE" if assessment.bar_count == 0
            else "FULL_SAMPLE" if assessment.bar_count >= 630
            else "SHORT_SAMPLE"
        )
        sources = sorted({bar.source for bar in bars})
        items.append({
            "symbol": symbol,
            "bar_count": assessment.bar_count,
            "history_start": (
                assessment.history_start.isoformat()
                if assessment.history_start else None
            ),
            "history_end": (
                assessment.history_end.isoformat()
                if assessment.history_end else None
            ),
            "research_status": status.value,
            "sample_class": sample_class,
            "walk_forward_eligible": assessment.walk_forward_eligible,
            "duplicate_dates": list(assessment.duplicate_dates),
            "warnings": sorted(set(warnings)),
            "data_version": assessment.data_version,
            "crosscheck_status": crosscheck_status,
            "adjustment_status": adjustment_status,
            "amount_quality": amount_quality,
            "source": sources,
            "price_basis": "adjusted_ohlc",
            "metadata_status": (
                "VERIFIED" if metadata_path is not None and metadata_item is not None
                else "NOT_SUPPLIED" if metadata_path is None else "MISSING"
            ),
            "metadata_validation_status": metadata_validation_status,
            "trading_metadata": (
                metadata_item.trading.to_dict()
                if metadata_item is not None else None
            ),
        })

    if generated_at is None:
        generated_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": generated_at,
        "history_path": str(history_path),
        "watchlist_path": str(watchlist_path),
        "runtime_history_unchanged": (
            original_history is None
            or history_path.read_bytes() == original_history
        ),
        "items": items,
        "invalid_history_records": invalid_symbols,
        "source_receipts": _source_receipts(wind_root),
        "research_only": True,
    }
    if output_path is not None:
        _write_json_atomic(Path(output_path), manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--watchlist", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--metadata", type=Path,
        default=Path("data/monitor/etf_metadata.json"),
    )
    parser.add_argument("--wind-root", type=Path)
    parser.add_argument("--generated-at")
    args = parser.parse_args()
    build_manifest(
        args.history,
        args.watchlist,
        metadata_path=args.metadata,
        output_path=args.output,
        wind_root=args.wind_root,
        generated_at=args.generated_at,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
