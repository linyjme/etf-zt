"""Archive Wind daily history unchanged, without promoting it to production data.

Run with ``python -m etf_rotation.wind_history --end-date YYYY-MM-DD``.
Successful archival can still be BLOCKED_VOLUME_UNIT; that is not an import.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import uuid

from .etf_metadata import EtfMetadataStore
from .market_data import load_closed_dates
from .swing_config import load_watchlist


SHANGHAI = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[2]
BEGIN_DATE = date(2023, 1, 1)
SOURCE = "WIND_FUND_KLINE"
REQUIRED_COLUMNS = frozenset((
    "TIME", "OPEN", "MATCH", "HIGH", "LOW", "TURNOVER", "VOLUME", "CHANGEHANDRATE", "AVPRICE",
))
KNOWN_EXCHANGES = {
    "510300": "SSE", "510500": "SSE", "563360": "SSE", "512100": "SSE",
    "159915": "SZSE", "588000": "SSE",
}
PROVIDER_CODES = frozenset((
    "AUTH_ERROR", "PARAMS_FILE_ERROR", "INVALID_PARAMS_JSON", "PARAM_TYPE_ERROR",
    "PARAM_VALIDATION_ERROR", "ROUTE_ERROR", "USAGE_ERROR", "RATE_LIMIT_ERROR",
    "NETWORK_ERROR", "TOOL_RUNTIME_ERROR", "SETUP_ERROR", "UNKNOWN", "backend_error",
))


class WindHistoryError(ValueError):
    """A safe error code; never contains a provider message, stderr, or credential."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _payload(envelope: object) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise WindHistoryError("INVALID_RESPONSE")
    if envelope.get("ok") is False or envelope.get("isError") is True:
        code = envelope.get("code")
        raise WindHistoryError(code if isinstance(code, str) and code in PROVIDER_CODES else "PROVIDER_ERROR")
    content = envelope.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        raise WindHistoryError("INVALID_RESPONSE")
    if content[0].get("type") != "text" or not isinstance(content[0].get("text"), str):
        raise WindHistoryError("INVALID_RESPONSE")
    try:
        payload = json.loads(content[0]["text"])
    except (ValueError, TypeError):
        raise WindHistoryError("INVALID_RESPONSE") from None
    if not isinstance(payload, dict):
        raise WindHistoryError("INVALID_RESPONSE")
    if payload.get("error") is not None or payload.get("ok") is False:
        raise WindHistoryError("backend_error")
    return payload


class WindHistoryClient:
    """Invoke the installed Skill's CLI without reading its configuration."""

    def __init__(self, skill_dir: Path | str | None = None, *, timeout: float = 60):
        self.skill_dir = Path(skill_dir or Path.home() / ".agents" / "skills" / "wind-mcp-skill").resolve()
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise WindHistoryError("INVALID_TIMEOUT")
        self.timeout = timeout

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        scripts = self.skill_dir / "scripts"
        if not (scripts / "cli.mjs").is_file():
            raise WindHistoryError("SKILL_NOT_FOUND")
        request = scripts / f"request-{uuid.uuid4().hex}.json"
        try:
            with request.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(params, handle, ensure_ascii=False, allow_nan=False)
            result = subprocess.run(
                ["node", "scripts/cli.mjs", "call", "fund_data", "get_fund_kline", f"@scripts/{request.name}"],
                cwd=self.skill_dir, capture_output=True, encoding="utf-8", timeout=self.timeout,
                check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                envelope = json.loads(result.stdout)
            except (ValueError, TypeError):
                raise WindHistoryError("INVALID_CLI_RESPONSE") from None
            _payload(envelope)
            if result.returncode != 0:
                raise WindHistoryError("CLI_PROCESS_ERROR")
            return envelope
        except subprocess.TimeoutExpired:
            raise WindHistoryError("CLI_TIMEOUT") from None
        except (OSError, UnicodeError, TypeError):
            raise WindHistoryError("CLI_RUNTIME_ERROR") from None
        finally:
            # Only this invocation's exact, uniquely named parameter file is removed.
            try:
                request.unlink(missing_ok=True)
            except OSError:
                raise WindHistoryError("PARAMETER_CLEANUP_ERROR") from None


def _unit(data: dict[str, Any], columns: list[dict], field: str) -> str | None:
    """Read explicit declarations only; metadata lot sizes are not vendor units."""
    declared = [column.get("unit") for column in columns if column["name"] == field]
    metadata = data.get("unit")
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if isinstance(key, str) and key.replace("单位", "").replace("：", "").replace(":", "").replace(" ", "") == field:
                declared.append(value)
    values = {value.strip() for value in declared if isinstance(value, str) and value.strip()}
    values -= {"未知", "unknown", "UNKNOWN", "N/A", "-"}
    return next(iter(values)) if len(values) == 1 else None


def _validate_response(
    envelope: object, *, end: date, observed_at: datetime, closed_dates: set[date], maximum_rows: int,
) -> tuple[list[str], dict[str, Any]]:
    data = _payload(envelope).get("data")
    if not isinstance(data, dict):
        raise WindHistoryError("INVALID_DATA")
    columns, rows = data.get("columns"), data.get("rows")
    if not isinstance(columns, list) or not columns or not all(
        isinstance(column, dict) and isinstance(column.get("name"), str) for column in columns
    ):
        raise WindHistoryError("INVALID_COLUMNS")
    names = [column["name"] for column in columns]
    if len(set(names)) != len(names) or not REQUIRED_COLUMNS.issubset(names):
        raise WindHistoryError("INVALID_COLUMNS")
    if not isinstance(rows, list) or not rows or len(rows) > maximum_rows:
        raise WindHistoryError("INVALID_ROWS")
    dates: list[str] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(names):
            raise WindHistoryError("INVALID_ROW")
        record = dict(zip(names, row))
        timestamp = record["TIME"]
        try:
            if not isinstance(timestamp, str) or "T" not in timestamp:
                raise ValueError
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
            day = parsed.astimezone(SHANGHAI).date()
        except (ValueError, OverflowError):
            raise WindHistoryError("INVALID_DATE") from None
        if not BEGIN_DATE <= day <= end:
            raise WindHistoryError("DATE_OUT_OF_RANGE")
        if day.weekday() >= 5 or day in closed_dates:
            raise WindHistoryError("NOT_TRADING_DAY")
        if day > observed_at.date() or (day == observed_at.date() and observed_at.time() < time(15, 10)):
            raise WindHistoryError("INCOMPLETE_DAY")
        if dates and day.isoformat() <= dates[-1]:
            raise WindHistoryError("DUPLICATE_OR_UNSORTED_DATE")
        dates.append(day.isoformat())
        numbers: dict[str, Decimal] = {}
        for field in REQUIRED_COLUMNS - {"TIME"}:
            value = record[field]
            try:
                if type(value) not in (str, int, float):
                    raise ValueError
                number = Decimal(str(value))
                if not number.is_finite():
                    raise ValueError
                if field in {"OPEN", "MATCH", "HIGH", "LOW", "AVPRICE"} and number <= 0:
                    raise ValueError
                if field in {"TURNOVER", "VOLUME"} and number < 0:
                    raise ValueError
            except (ValueError, InvalidOperation):
                raise WindHistoryError("INVALID_NUMBER") from None
            numbers[field] = number
        if not numbers["LOW"] <= min(numbers["OPEN"], numbers["MATCH"]) <= max(numbers["OPEN"], numbers["MATCH"]) <= numbers["HIGH"]:
            raise WindHistoryError("INVALID_OHLC")
    return dates, {
        "volume": _unit(data, columns, "VOLUME"), "amount": _unit(data, columns, "TURNOVER"),
        "metadata": data.get("unit"), "columns": columns,
    }


def _write_json(path: Path, payload: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def stage_history(
    *, end_date: str, count: int = 756, output: Path | str = Path("var/swing/wind"),
    skill_dir: Path | str | None = None, client: WindHistoryClient | None = None,
    watchlist_path: Path = ROOT / "data/swing/watchlist.json",
    metadata_path: Path = ROOT / "data/monitor/etf_metadata.json",
    calendar_path: Path = ROOT / "data/monitor/market_calendar.json", now: datetime | None = None,
) -> Path:
    """Save a serial batch and atomically publish its manifest only after validation.

    Partial batch directories may remain after errors, but have no manifest.
    Even explicitly declared units never authorize conversion or production import.
    """
    observed = now or datetime.now(SHANGHAI)
    if not isinstance(observed, datetime) or observed.tzinfo is None or observed.utcoffset() is None:
        raise WindHistoryError("INVALID_OBSERVATION_TIME")
    observed = observed.astimezone(SHANGHAI)
    try:
        end = date.fromisoformat(end_date)
        if end.isoformat() != end_date or not BEGIN_DATE <= end <= observed.date():
            raise ValueError
    except (TypeError, ValueError):
        raise WindHistoryError("INVALID_END_DATE") from None
    if end.year > 2026:
        raise WindHistoryError("CALENDAR_COVERAGE_EXCEEDED")
    if type(count) is not int or count <= 0:
        raise WindHistoryError("INVALID_COUNT")
    if end == observed.date() and observed.time() < time(15, 10):
        raise WindHistoryError("INCOMPLETE_DAY")
    try:
        closed_dates = load_closed_dates(calendar_path)
        metadata = EtfMetadataStore(metadata_path).load()
        watchlist = load_watchlist(watchlist_path, metadata_path)
    except (OSError, ValueError, UnicodeError):
        raise WindHistoryError("INVALID_LOCAL_CONFIG") from None
    if end.weekday() >= 5 or end in closed_dates:
        raise WindHistoryError("NOT_TRADING_DAY")
    windcodes: list[tuple[str, str]] = []
    for item in watchlist:
        if not item.enabled:
            continue
        if item.symbol not in KNOWN_EXCHANGES:
            raise WindHistoryError("UNKNOWN_SYMBOL")
        exchange = metadata[item.symbol].trading.exchange
        if exchange != KNOWN_EXCHANGES[item.symbol]:
            raise WindHistoryError("EXCHANGE_MISMATCH")
        windcodes.append((item.symbol, item.symbol + (".SH" if exchange == "SSE" else ".SZ")))
    if not windcodes:
        raise WindHistoryError("EMPTY_WATCHLIST")
    source_client = client or WindHistoryClient(skill_dir)
    batch = Path(output).resolve() / f"{end.isoformat()}-{uuid.uuid4().hex}"
    temporary_manifest = batch / "manifest.json.tmp"
    items: list[dict[str, Any]] = []
    try:
        batch.mkdir(parents=True, exist_ok=False)
        for symbol, windcode in windcodes:
            pair: dict[str, tuple[list[str], dict[str, Any]]] = {}
            files: dict[str, str] = {}
            for mode, aftype in (("raw", "2"), ("adjusted", "0")):
                params = {
                    "windcode": windcode, "begin_date": BEGIN_DATE.isoformat(), "end_date": end.isoformat(),
                    "period": "1d", "count": -(count + 1), "aftype": aftype, "issusp": "0", "afdate": end.isoformat(),
                }
                # A provider error or invalid probe stops this batch before any next request.
                envelope = source_client.fetch(params)
                _payload(envelope)
                recorded_at = observed if now is not None else datetime.now(SHANGHAI)
                pair[mode] = _validate_response(
                    envelope, end=end, observed_at=recorded_at, closed_dates=closed_dates, maximum_rows=count + 1,
                )
                files[mode] = f"{windcode}.{mode}.json"
                _write_json(batch / files[mode], {"params": params, "observed_at": recorded_at.isoformat(), "response": envelope})
            dates, raw_units = pair["raw"]
            adjusted_dates, adjusted_units = pair["adjusted"]
            if dates != adjusted_dates:
                raise WindHistoryError("DATE_MISMATCH")
            if dates[-1] != end.isoformat():
                raise WindHistoryError("HISTORY_INCOMPLETE")
            expected_dates: list[str] = []
            day = date.fromisoformat(dates[0])
            while day <= end:
                if day.weekday() < 5 and day not in closed_dates:
                    expected_dates.append(day.isoformat())
                day += timedelta(days=1)
            if dates != expected_dates:
                raise WindHistoryError("HISTORY_GAP")
            blocked = ["BLOCKED_PRODUCTION_ADAPTER", "BLOCKED_ADJUSTMENT_PRECISION_REVIEW"]
            if raw_units["volume"] is None or adjusted_units["volume"] is None:
                blocked.insert(0, "BLOCKED_VOLUME_UNIT")
            if raw_units["amount"] is None or adjusted_units["amount"] is None:
                blocked.append("BLOCKED_AMOUNT_UNIT")
            if any(raw_units[field] != adjusted_units[field] for field in ("volume", "amount")):
                blocked.append("BLOCKED_UNIT_MISMATCH")
            items.append({
                "symbol": symbol, "windcode": windcode, "rows": len(dates), "usable_rows": len(dates) - 1,
                "first_date": dates[0], "last_date": dates[-1], "source": SOURCE, "files": files,
                "units": {"raw": raw_units, "adjusted": adjusted_units}, "blocked_reasons": blocked,
            })
        blocked = sorted({reason for item in items for reason in item["blocked_reasons"]})
        manifest = {
            "schema_version": 1, "source": SOURCE, "staging_only": True, "production_import_allowed": False,
            "status": "BLOCKED_VOLUME_UNIT" if "BLOCKED_VOLUME_UNIT" in blocked else "STAGED_ONLY",
            "begin_date": BEGIN_DATE.isoformat(), "end_date": end.isoformat(), "adjustment_anchor": end.isoformat(),
            "observed_at": observed.isoformat(), "requested_usable_rows": count, "seed_rows_per_symbol": 1,
            "blocked_reasons": blocked, "items": items,
        }
        _write_json(temporary_manifest, manifest)
        manifest_path = batch / "manifest.json"
        os.replace(temporary_manifest, manifest_path)
        return manifest_path
    except WindHistoryError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise WindHistoryError("ARCHIVE_FAILED") from None
    finally:
        temporary_manifest.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive unchanged Wind ETF history; staging only, never a production import.")
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--count", type=int, default=756, help="desired usable rows, plus one seed (default: 756)")
    parser.add_argument("--output", type=Path, default=Path("var/swing/wind"))
    parser.add_argument("--skill-dir", type=Path, default=Path.home() / ".agents/skills/wind-mcp-skill")
    args = parser.parse_args(argv)
    try:
        manifest_path = stage_history(**vars(args))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        rows = sum(item["rows"] for item in manifest["items"])
        usable = sum(item["usable_rows"] for item in manifest["items"])
        print(f"{manifest['status']} | staging only; production import disabled | symbols={len(manifest['items'])} rows={rows} usable_rows={usable}")
        print(f"manifest: {manifest_path}")
        return 0
    except WindHistoryError as error:
        print(f"Wind history staging failed: {error.code}", file=sys.stderr)
    except Exception:
        # CLI stdout/stderr and provider errors may contain credentials; never echo them.
        print("Wind history staging failed: LOCAL_RUNTIME_ERROR", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
