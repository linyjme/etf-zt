"""Private reported holdings, deliberately separate from confirmed trades.

A report is not a current quote, a reconciled broker account or a strategy
position lifecycle. Unknown dates, prices and stops stay unknown. The store is
immutable: a second, different report requires an explicit migration workflow.
"""

from __future__ import annotations

from collections.abc import Mapping
import copy
from datetime import date, datetime
import json
import math
import os
from pathlib import Path
import tempfile

from .swing_data import _SiblingFileLock


class HoldingsSnapshotError(ValueError):
    """Invalid or conflicting report; errors never include private values."""


_TOP_FIELDS = {
    "schema_version", "snapshot_id", "recorded_at", "reporting_date", "source",
    "positions_as_of", "account_as_of", "notes", "account", "positions",
}
_ACCOUNT_FIELDS = {
    "reported_total_assets", "reported_securities_value", "available_cash",
    "other_assets", "original_capital", "additional_loss_budget",
}
_POSITION_FIELDS = {
    "symbol", "name", "asset_type", "management_mode", "shares",
    "sellable_shares", "average_cost", "reported_market_value",
    "reported_holding_pnl", "entry_date", "stop_loss",
}
_MAX_FILE_BYTES = 1_000_000


def _text(value: object, maximum: int = 300) -> None:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise HoldingsSnapshotError("invalid snapshot text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise HoldingsSnapshotError("invalid snapshot text encoding") from None


def _number(value: object, *, nullable: bool = False, signed: bool = False) -> None:
    if value is None and nullable:
        return
    if (
        type(value) not in (int, float)
        or abs(value) > 1e15
        or not math.isfinite(value)
        or (not signed and value < 0)
    ):
        raise HoldingsSnapshotError("invalid snapshot number")


def _timestamp(value: object, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    try:
        if type(value) is not str:
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except ValueError:
        raise HoldingsSnapshotError("invalid snapshot timestamp") from None


def validate_snapshot(payload: object, metadata: Mapping[str, object]) -> dict:
    """Return a detached, validated report without enriching unknown fields."""
    if type(payload) is not dict or set(payload) != _TOP_FIELDS:
        raise HoldingsSnapshotError("invalid snapshot fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise HoldingsSnapshotError("unsupported snapshot schema")
    _text(payload["snapshot_id"], 120)
    _text(payload["source"], 1000)
    recorded = _timestamp(payload["recorded_at"])
    try:
        raw_date = payload["reporting_date"]
        if type(raw_date) is not str or len(raw_date) != 10:
            raise ValueError
        report_date = date.fromisoformat(raw_date)
        if report_date > recorded.date():
            raise ValueError
    except ValueError:
        raise HoldingsSnapshotError("invalid reporting date") from None
    for key in ("positions_as_of", "account_as_of"):
        timestamp = _timestamp(payload[key], nullable=True)
        if timestamp is not None and timestamp > recorded:
            raise HoldingsSnapshotError("source timestamp is after import")
    notes = payload["notes"]
    if type(notes) is not list or len(notes) > 30:
        raise HoldingsSnapshotError("invalid snapshot notes")
    for note in notes:
        _text(note, 1000)

    account = payload["account"]
    if type(account) is not dict or set(account) != _ACCOUNT_FIELDS:
        raise HoldingsSnapshotError("invalid account fields")
    for key, value in account.items():
        _number(value, nullable=key in {"original_capital", "additional_loss_budget"})
    components = sum(account[key] for key in (
        "reported_securities_value", "available_cash", "other_assets",
    ))
    if not math.isclose(components, account["reported_total_assets"], rel_tol=0, abs_tol=.011):
        raise HoldingsSnapshotError("reported account totals do not reconcile")
    budget = account["additional_loss_budget"]
    if budget is not None and budget > account["reported_total_assets"]:
        raise HoldingsSnapshotError("loss budget exceeds reported assets")

    positions = payload["positions"]
    if type(positions) is not list or not 1 <= len(positions) <= 200:
        raise HoldingsSnapshotError("invalid positions collection")
    symbols: set[str] = set()
    for position in positions:
        if type(position) is not dict or set(position) != _POSITION_FIELDS:
            raise HoldingsSnapshotError("invalid position fields")
        symbol = position["symbol"]
        if (
            type(symbol) is not str or len(symbol) != 6
            or not symbol.isascii() or not symbol.isdigit() or symbol in symbols
        ):
            raise HoldingsSnapshotError("invalid or duplicate security symbol")
        symbols.add(symbol)
        _text(position["name"], 100)
        kind = position["asset_type"]
        if type(kind) is not str or kind not in {"ETF", "STOCK"}:
            raise HoldingsSnapshotError("unsupported asset type")
        if kind == "ETF" and symbol not in metadata:
            raise HoldingsSnapshotError("ETF metadata unavailable")
        required_mode = "LONG_TERM_ONLY" if kind == "STOCK" else "OBSERVE"
        if position["management_mode"] != required_mode:
            raise HoldingsSnapshotError("invalid position management mode")
        shares, sellable = position["shares"], position["sellable_shares"]
        if type(shares) is not int or not 0 < shares <= 1_000_000_000_000:
            raise HoldingsSnapshotError("invalid position shares")
        if sellable is not None and (type(sellable) is not int or not 0 <= sellable <= shares):
            raise HoldingsSnapshotError("invalid reported sellable shares")
        _number(position["average_cost"])
        if position["average_cost"] <= 0:
            raise HoldingsSnapshotError("average cost must be positive")
        _number(position["reported_market_value"], nullable=True)
        _number(position["reported_holding_pnl"], nullable=True, signed=True)
        if position["entry_date"] is not None or position["stop_loss"] is not None:
            raise HoldingsSnapshotError("snapshot import does not configure strategy parameters")
    return copy.deepcopy(payload)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise HoldingsSnapshotError("duplicate JSON field")
        result[key] = value
    return result


def _read(path: Path, metadata: Mapping[str, object]) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(_MAX_FILE_BYTES + 1)
    if len(raw) > _MAX_FILE_BYTES:
        raise HoldingsSnapshotError("snapshot file too large")
    return validate_snapshot(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object), metadata)


def _view(status: str, snapshot: dict | None = None, error: str | None = None) -> dict:
    return {
        "status": status, "snapshot": snapshot, "error": error,
        "read_only": True, "strategy_ready": False,
    }


def read_holdings_snapshot(path: Path, metadata: Mapping[str, object]) -> dict:
    """Read once for producer publication; invalid input never becomes healthy."""
    try:
        payload = _read(Path(path), metadata)
    except FileNotFoundError:
        return _view("ABSENT")
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return _view("INVALID", error="持仓快照不可用，请核对本地导入文件；策略保持停用")
    return _view("SNAPSHOT_ONLY", payload)


def import_holdings_snapshot(path: Path, payload: object, metadata: Mapping[str, object]) -> dict:
    """Atomically create one report, or accept an identical retry; never replace it."""
    validated = validate_snapshot(payload, metadata)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _SiblingFileLock(path, shared=False):
        existing = read_holdings_snapshot(path, metadata)
        if existing["status"] != "ABSENT":
            if existing["status"] == "SNAPSHOT_ONLY" and existing["snapshot"] == validated:
                return existing
            raise HoldingsSnapshotError("existing holdings snapshot cannot be overwritten")
        raw = (json.dumps(validated, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
        if len(raw) > _MAX_FILE_BYTES:
            raise HoldingsSnapshotError("snapshot file too large")
        descriptor, temporary = tempfile.mkstemp(prefix=".holdings-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return _view("SNAPSHOT_ONLY", validated)
