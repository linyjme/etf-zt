from __future__ import annotations

from dataclasses import dataclass, replace
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
    roe_period_inferred: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            field: getattr(self, field)
            for field in (
                "index_code", "index_name", "as_of", *VALUATION_FIELDS,
                "level", "status", "source", "roe_period", "roe_annualized",
                "roe_consistent", "percentile_horizon_used", "generated_at",
                "roe_period_inferred",
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
    stage_conflict: bool = False
    percentile_stage: str | None = None
    pr_stage: str | None = None

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
            "stage_conflict": self.stage_conflict,
            "percentile_stage": self.percentile_stage,
            "pr_stage": self.pr_stage,
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


_STAGE_ORDER = ("DEEP_VALUE", "VALUE", "FAIR", "RICH", "EXPENSIVE")
_PR_CATEGORIES = frozenset({"BROAD", "GOLD"})


def _stage_from_percentile(percentile: float) -> str:
    if percentile <= 10:
        return "DEEP_VALUE"
    if percentile <= 30:
        return "VALUE"
    if percentile < 70:
        return "FAIR"
    if percentile < 90:
        return "RICH"
    return "EXPENSIVE"


def _stage_from_pr(pr: float) -> str:
    if pr <= 0.8:
        return "DEEP_VALUE"
    if pr <= 1.2:
        return "VALUE"
    if pr <= 2.0:
        return "FAIR"
    if pr <= 3.0:
        return "RICH"
    return "EXPENSIVE"


def _stage_with_sources(
    stage: str,
    *,
    percentile_stage: str | None,
    pr_stage: str | None,
    conflict: bool,
) -> ValuationStage:
    return replace(
        _stage_defaults(stage),
        stage_conflict=conflict,
        percentile_stage=percentile_stage,
        pr_stage=pr_stage,
    )


def _percentile_average(snapshot: ValuationSnapshot) -> float | None:
    if snapshot.percentile_horizon_used == "10y":
        values = [snapshot.pe_percentile_10y, snapshot.pb_percentile_10y]
    elif snapshot.percentile_horizon_used == "5y":
        values = [snapshot.pe_percentile_5y, snapshot.pb_percentile_5y]
    else:
        values = []
    values = [value for value in values if value is not None]
    if not values:
        return None
    return sum(values) / len(values)


def classify_valuation_stage(
    snapshot: ValuationSnapshot | None,
    *,
    category: str = "BROAD",
) -> ValuationStage:
    if snapshot is None or snapshot.status not in {"OK"}:
        return _stage_defaults("UNAVAILABLE")
    percentile = _percentile_average(snapshot)
    percentile_stage = (
        _stage_from_percentile(percentile) if percentile is not None else None
    )
    pr_stage = None
    if category.upper() in _PR_CATEGORIES and snapshot.pr_pe_pb is not None:
        pr_stage = _stage_from_pr(snapshot.pr_pe_pb)
    if percentile_stage is not None and pr_stage is not None:
        gap = abs(_STAGE_ORDER.index(percentile_stage) - _STAGE_ORDER.index(pr_stage))
        if gap >= 2:
            return _stage_with_sources(
                "FAIR",
                percentile_stage=percentile_stage,
                pr_stage=pr_stage,
                conflict=True,
            )
        if gap == 1:
            conservative = _STAGE_ORDER[max(
                _STAGE_ORDER.index(percentile_stage),
                _STAGE_ORDER.index(pr_stage),
            )]
            return _stage_with_sources(
                conservative,
                percentile_stage=percentile_stage,
                pr_stage=pr_stage,
                conflict=False,
            )
        return _stage_with_sources(
            percentile_stage,
            percentile_stage=percentile_stage,
            pr_stage=pr_stage,
            conflict=False,
        )
    if percentile_stage is not None:
        return _stage_with_sources(
            percentile_stage,
            percentile_stage=percentile_stage,
            pr_stage=None,
            conflict=False,
        )
    if pr_stage is not None:
        return _stage_with_sources(
            pr_stage,
            percentile_stage=None,
            pr_stage=pr_stage,
            conflict=False,
        )
    return _stage_defaults("UNAVAILABLE")


def _infer_roe_period(
    pe: float | None,
    roe: float | None,
    canonical_pr: float | None,
    stored_pr_pe_roe: object,
) -> str | None:
    """Infer H1 or Q1 from the raw PE/ROE ratio versus canonical PE²/PB.

    A ratio near 2 means the stored ROE is a half-year figure; near 4 means a
    first-quarter figure.  ROE consistency stays an evidence flag and does not
    decide whether the canonical PR can be used.
    """
    raw_pr: float | None = None
    if (
        isinstance(stored_pr_pe_roe, (int, float))
        and not isinstance(stored_pr_pe_roe, bool)
        and math.isfinite(float(stored_pr_pe_roe))
        and float(stored_pr_pe_roe) > 0.0
    ):
        raw_pr = float(stored_pr_pe_roe)
    elif pe is not None and roe is not None and roe > 0.0:
        raw_pr = pe / roe
    if raw_pr is None or canonical_pr is None or canonical_pr <= 0.0:
        return None
    ratio = raw_pr / canonical_pr
    if not math.isfinite(ratio) or ratio <= 0.0:
        return None
    best: str | None = None
    best_gap: float | None = None
    for period, target in (("H1", 2.0), ("Q1", 4.0)):
        gap = abs(ratio - target) / target
        if gap <= 0.20 and (best_gap is None or gap < best_gap):
            best = period
            best_gap = gap
    return best


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

    def load(self, today: date | None = None) -> dict[str, ValuationSnapshot]:
        as_of = self.today if today is None else today
        if type(as_of) is not date:
            raise ValueError("today must be a date")
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
            snapshot = self._parse(record, today=as_of)
            if snapshot is not None:
                result[snapshot.index_code] = snapshot
        return result

    def get(self, index_code: str, today: date | None = None) -> ValuationSnapshot | None:
        return self.load(today=today).get(index_code)

    def _parse(self, record: object, *, today: date | None = None) -> ValuationSnapshot | None:
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
        roe = values["roe_ttm"]
        pe = values["pe_ttm"]
        pb = values["pb"]
        values["pr_pe_pb"] = (pe * pe / (pb * 100)) if pe is not None and pb is not None and pb > 0 else None
        roe_period_inferred = False
        if roe_period is None and roe is not None:
            inferred = _infer_roe_period(pe, roe, values["pr_pe_pb"], record.get("pr_pe_roe"))
            if inferred is not None:
                roe_period = inferred
                roe_period_inferred = True
        factor = ROE_PERIOD_FACTOR.get(roe_period) if isinstance(roe_period, str) else None
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
        as_of_today = today if today is not None else self.today
        supplied_status = record.get("status")
        if supplied_status in {"MISSING_VALUATION", "INSUFFICIENT_HISTORY"}:
            status = supplied_status
        elif as_of_date is None or as_of_today.toordinal() - as_of_date.toordinal() >= self.stale_days:
            status = "STALE" if as_of_date is not None else "UNKNOWN"
        elif not percentiles and values["pr_pe_pb"] is None:
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
            roe_period_inferred=roe_period_inferred,
        )
