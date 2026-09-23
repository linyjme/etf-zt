from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .etf_metadata import is_valid_index_code


VALUATION_FIELDS = (
    "pe_ttm", "pb", "dividend_yield", "pe_percentile_5y", "pe_percentile_10y",
    "pb_percentile_5y", "pb_percentile_10y", "roe_ttm", "pr_pe_roe", "pr_pe_pb",
)
ROE_PERIOD_FACTOR = {"TTM": 1.0, "FY": 1.0, "H1": 2.0, "Q1": 4.0, "Q3": 4.0 / 3.0}
VALID_ROE_PERIODS = frozenset(ROE_PERIOD_FACTOR)
VALUATION_STATUS = frozenset({"OK", "MISSING_VALUATION", "INSUFFICIENT_HISTORY", "STALE", "UNKNOWN"})


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
    roe_ttm: float | None
    pr_pe_roe: float | None
    pr_pe_pb: float | None
    level: str
    status: str
    source: str | None
    roe_period: str | None = None
    roe_annualized: float | None = None
    roe_consistent: bool = False
    percentile_horizon_used: str | None = None
    generated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            field: getattr(self, field)
            for field in (
                "index_code", "index_name", "as_of", *VALUATION_FIELDS,
                "level", "status", "source", "roe_period", "roe_annualized",
                "roe_consistent", "percentile_horizon_used", "generated_at",
            )
        }


@dataclass(frozen=True)
class ValuationStage:
    stage: str
    size_multiplier: float
    allow_topup: bool
    allow_b_breakout: bool
    s1_bias_limit: float | None
    e3_session: int
    reduce_at_r: float | None
    allow_a_pullback: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "size_multiplier": self.size_multiplier,
            "allow_topup": self.allow_topup,
            "allow_b_breakout": self.allow_b_breakout,
            "s1_bias_limit": self.s1_bias_limit,
            "e3_session": self.e3_session,
            "reduce_at_r": self.reduce_at_r,
            "allow_a_pullback": self.allow_a_pullback,
        }


def _stage_defaults(stage: str) -> ValuationStage:
    if stage == "DEEP_VALUE":
        return ValuationStage(stage, 1.0, True, True, None, 12, None)
    if stage == "VALUE":
        return ValuationStage(stage, 1.0, True, True, None, 10, None)
    if stage == "FAIR":
        return ValuationStage(stage, 1.0, True, True, None, 10, None)
    if stage == "RICH":
        return ValuationStage(stage, 0.5, False, True, 8.0, 7, 1.5)
    if stage == "EXPENSIVE":
        return ValuationStage(stage, 0.5, False, True, 8.0, 5, 1.0, False)
    return ValuationStage("UNAVAILABLE", 1.0, True, True, None, 10, None)


def classify_valuation_stage(
    snapshot: ValuationSnapshot | None,
    *,
    category: str = "BROAD",
) -> ValuationStage:
    if snapshot is None or snapshot.status not in {"OK"}:
        return _stage_defaults("UNAVAILABLE")
    percentile: float | None = None
    if snapshot.percentile_horizon_used == "10y":
        values = [snapshot.pe_percentile_10y, snapshot.pb_percentile_10y]
    elif snapshot.percentile_horizon_used == "5y":
        values = [snapshot.pe_percentile_5y, snapshot.pb_percentile_5y]
    else:
        values = []
    values = [value for value in values if value is not None]
    if values:
        percentile = sum(values) / len(values)
    if percentile is not None:
        if percentile <= 10:
            return _stage_defaults("DEEP_VALUE")
        if percentile <= 30:
            return _stage_defaults("VALUE")
        if percentile < 70:
            return _stage_defaults("FAIR")
        if percentile < 90:
            return _stage_defaults("RICH")
        return _stage_defaults("EXPENSIVE")
    if category.upper() not in {"BROAD", "GOLD"} or not snapshot.roe_consistent:
        return _stage_defaults("UNAVAILABLE")
    pr = snapshot.pr_pe_pb
    if pr is None:
        return _stage_defaults("UNAVAILABLE")
    if pr <= 0.8:
        return _stage_defaults("DEEP_VALUE")
    if pr <= 1.2:
        return _stage_defaults("VALUE")
    if pr <= 2.0:
        return _stage_defaults("FAIR")
    if pr <= 3.0:
        return _stage_defaults("RICH")
    return _stage_defaults("EXPENSIVE")


class ValuationStore:
    def __init__(
        self,
        path: Path,
        *,
        today: date | None = None,
        stale_days: int = 10,
    ):
        self.path = Path(path)
        self.today = today or date.today()
        self.stale_days = stale_days

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
        if not is_valid_index_code(code) or not isinstance(name, str) or not name.strip():
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
                as_of_date = date.fromisoformat(as_of)
            except (TypeError, ValueError):
                return None
        else:
            as_of_date = None
        roe_period = record.get("roe_period")
        if roe_period is not None and roe_period not in VALID_ROE_PERIODS:
            roe_period = None
        factor = ROE_PERIOD_FACTOR.get(roe_period) if isinstance(roe_period, str) else None
        roe = values["roe_ttm"]
        pe = values["pe_ttm"]
        pb = values["pb"]
        values["pr_pe_pb"] = (pe * pe / (pb * 100)) if pe is not None and pb is not None and pb > 0 else None
        roe_annualized = roe * factor if roe is not None and factor is not None else None
        pr_pe_roe = (pe / roe_annualized) if pe is not None and roe_annualized and roe_annualized > 0 else None
        roe_consistent = False
        if pr_pe_roe is not None and values["pr_pe_pb"] is not None:
            denominator = max(abs(values["pr_pe_pb"]), 1e-12)
            roe_consistent = abs(pr_pe_roe - values["pr_pe_pb"]) / denominator <= 0.20
        values["pr_pe_roe"] = pr_pe_roe if roe_consistent else None
        percentile_horizon_used: str | None = None
        for horizon in ("10y", "5y"):
            if any(values[key] is not None for key in (f"pe_percentile_{horizon}", f"pb_percentile_{horizon}")):
                percentile_horizon_used = horizon
                break
        percentiles = [
            values[key]
            for key in (
                f"pe_percentile_{percentile_horizon_used}",
                f"pb_percentile_{percentile_horizon_used}",
            )
            if percentile_horizon_used and values[key] is not None
        ]
        level = (
            "UNKNOWN"
            if not percentiles
            else "LOW"
            if sum(percentiles) / len(percentiles) <= 20
            else "HIGH"
            if sum(percentiles) / len(percentiles) >= 80
            else "NORMAL"
        )
        supplied_status = record.get("status")
        if supplied_status in {"MISSING_VALUATION", "INSUFFICIENT_HISTORY"}:
            status = supplied_status
        elif as_of_date is None or self.today.toordinal() - as_of_date.toordinal() >= self.stale_days:
            status = "STALE" if as_of_date is not None else "UNKNOWN"
        elif roe is not None and roe_period is None:
            status = "UNKNOWN"
        elif not percentiles:
            status = "UNKNOWN"
        else:
            status = "OK"
        return ValuationSnapshot(
            code, name.strip(), as_of, **values, level=level, status=status,
            source=record.get("source") if isinstance(record.get("source"), str) else None,
            roe_period=roe_period, roe_annualized=roe_annualized,
            roe_consistent=roe_consistent,
            percentile_horizon_used=percentile_horizon_used,
            generated_at=record.get("generated_at") if isinstance(record.get("generated_at"), str) else None,
        )
