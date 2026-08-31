from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import math
from pathlib import Path
from typing import Any, Mapping


VALUATION_FIELDS = (
    "pe_ttm", "pb", "dividend_yield", "pe_percentile_5y", "pe_percentile_10y",
    "pb_percentile_5y", "pb_percentile_10y",
)


@dataclass(frozen=True)
class ValuationSnapshot:
    index_code: str
    index_name: str
    as_of: str | None
    pe_ttm: float | None
    pb: float | None
    dividend_yield: float | None
    pe_percentile_5y: float | None
    pe_percentile_10y: float | None
    pb_percentile_5y: float | None
    pb_percentile_10y: float | None
    level: str
    status: str
    source: str | None

    def to_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in ("index_code", "index_name", "as_of", *VALUATION_FIELDS, "level", "status", "source")}


class ValuationStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, ValuationSnapshot]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
            return {}
        records = payload.get("items")
        if not isinstance(records, list):
            return {}
        result: dict[str, ValuationSnapshot] = {}
        for record in records:
            snapshot = self._parse(record)
            if snapshot is not None:
                result[snapshot.index_code] = snapshot
        return result

    def get(self, index_code: str) -> ValuationSnapshot | None:
        return self.load().get(index_code)

    def _parse(self, record: object) -> ValuationSnapshot | None:
        if not isinstance(record, Mapping):
            return None
        code = record.get("index_code")
        name = record.get("index_name")
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit() or not isinstance(name, str) or not name.strip():
            return None
        values: dict[str, float | None] = {}
        for field in VALUATION_FIELDS:
            value = record.get(field)
            if value is None:
                values[field] = None
            elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                if field.endswith("percentile_5y") or field.endswith("percentile_10y"):
                    if not 0 <= float(value) <= 100:
                        return None
                elif field == "dividend_yield" and float(value) < 0:
                    return None
                elif field in ("pe_ttm", "pb") and float(value) < 0:
                    values[field] = None
                    continue
                values[field] = float(value)
            else:
                return None
        as_of = record.get("as_of")
        if as_of is not None:
            try:
                date.fromisoformat(as_of)
            except (TypeError, ValueError):
                return None
        percentiles = [values[key] for key in ("pe_percentile_10y", "pb_percentile_10y", "pe_percentile_5y", "pb_percentile_5y") if values[key] is not None]
        level = "UNKNOWN" if not percentiles else ("LOW" if sum(percentiles) / len(percentiles) <= 20 else "HIGH" if sum(percentiles) / len(percentiles) >= 80 else "NORMAL")
        status = record.get("status") if record.get("status") in {"OK", "MISSING_VALUATION", "INSUFFICIENT_HISTORY", "STALE", "UNKNOWN"} else ("OK" if percentiles else "UNKNOWN")
        return ValuationSnapshot(code, name.strip(), as_of, **values, level=level, status=status, source=record.get("source") if isinstance(record.get("source"), str) else None)
