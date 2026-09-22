"""Generate a versioned, non-executable shadow replay report."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import argparse
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from etf_rotation.swing_data import DailyBar
from etf_rotation.swing_v11 import (
    V11Context,
    V11State,
    calculate_v11_indicators,
    classify_v11_environment,
    evaluate_v11,
    load_v11_config,
)
from etf_rotation.swing_shadow_backtest import ExecutionCosts, replay_all_variants


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _v11_outcome_bucket(
    state: str, reasons: list[str] | tuple[str, ...], status: str,
) -> str:
    """Classify one read-only V11 result for shadow-report aggregation."""
    normalized = {str(reason) for reason in reasons}
    if status != "AVAILABLE" or not normalized and state == "DATA_UNAVAILABLE":
        return "DATA_BLOCKED"
    data_markers = {
        "DATA_QUALITY_UNVERIFIED", "DATA_QUALITY_UNKNOWN", "DATA_UNAVAILABLE",
        "NO_COMPLETED_BARS", "INSUFFICIENT_COMPLETED_BARS",
        "INDICATOR_CONTEXT_UNAVAILABLE", "METADATA_INCOMPLETE",
        "ENVIRONMENT_UNKNOWN", "QUASI_CLOSE_NOT_VERIFIED", "AS_OF_KIND_UNSUPPORTED",
    }
    if normalized & data_markers:
        return "DATA_BLOCKED"
    if state in {"TECHNICAL_CANDIDATE", "ACTION_CANDIDATE", "POSITION_ACTION"} and not normalized:
        return "CANDIDATE"
    if normalized:
        return "RULE_BLOCKED"
    return "NO_SIGNAL"


def _load_history(
    path: Path,
) -> tuple[dict[str, tuple[DailyBar, ...]], set[str], list[dict[str, object]]]:
    groups: dict[str, list[DailyBar]] = defaultdict(list)
    invalid_symbols: set[str] = set()
    invalid_lines: list[dict[str, object]] = []
    if not path.exists():
        return {}, invalid_symbols, invalid_lines
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        symbol: str | None = None
        try:
            payload = json.loads(line)
            if isinstance(payload, dict) and isinstance(payload.get("symbol"), str):
                symbol = payload["symbol"]
            bar = DailyBar.from_mapping(payload)
            groups[bar.symbol].append(bar)
        except Exception as error:
            if symbol is not None and len(symbol) == 6 and symbol.isdigit():
                invalid_symbols.add(symbol)
            invalid_lines.append({
                "line": line_number,
                "symbol": symbol,
                "reason": type(error).__name__,
            })
    for symbol in invalid_symbols:
        groups.pop(symbol, None)
    return {symbol: tuple(bars) for symbol, bars in groups.items()}, invalid_symbols, invalid_lines


def _v11_shadow_result(
    symbol: str, bars: tuple[DailyBar, ...], item: dict[str, object], config,
) -> dict[str, object]:
    """Build evidence only; V11 is never converted into a replay trade."""
    if not bars:
        return {
            "strategy_version": "SWING_V11_SHADOW",
            "validation_status": "DATA_UNAVAILABLE",
            "performance_claim_allowed": False,
            "candidate_count": 0,
            "blocked_reason_counts": {"NO_COMPLETED_BARS": 1},
            "average_planned_notional_cny": None,
            "stop_width_rejection_count": 0,
            "missing_data_count": 1,
            "decision": None,
            "outcome": "DATA_BLOCKED",
            "outcome_counts": {"DATA_BLOCKED": 1},
        }
    data_quality = "VERIFIED" if (
        item.get("crosscheck_status") == "PASSED"
        and item.get("amount_quality") == "PROVIDER_REPORTED"
        and item.get("adjustment_status") == "VERIFIED"
    ) else "UNVERIFIED"
    try:
        indicators = calculate_v11_indicators(bars)
        indicator = {
            "bar_count": indicators["bar_count"],
            "price": bars[-1].adjusted_close,
            "ma10": indicators["moving_averages"].get("ma10"),
            "ma20": indicators["moving_averages"].get("ma20"),
            "ma60": indicators["moving_averages"].get("ma60"),
            "ma250": indicators["moving_averages"].get("ma250"),
            "ma20_slope_pct_10d": indicators["moving_averages"].get("ma20_slope_pct_10d"),
            "weekly_close": indicators["weekly"].get("close"),
            "weekly_ma10": indicators["weekly"].get("ma10"),
            "weekly_ma20": indicators["weekly"].get("ma20"),
            "bias20_pct": indicators["bias20"].get("value"),
            "volume_ratio20": indicators["volume"].get("ratio20"),
            "atr14": indicators.get("atr14"),
            **(indicators.get("setups") or {}),
            "macd_dif": indicators["macd"].get("dif"),
            "macd_dea": indicators["macd"].get("dea"),
            "macd_dif_nonnegative": bool(
                indicators["macd"].get("dif") is not None
                and indicators["macd"].get("dif") >= 0
            ),
        }
        decision = evaluate_v11(
            bars,
            config=config,
            context=V11Context(
                data_quality=data_quality,
                environment_state=classify_v11_environment(indicator),
                category=str(item.get("category") or "BROAD"),
                indicator=indicator,
                as_of_trading_date=bars[-1].trading_date.isoformat(),
            ),
        )
    except (ValueError, TypeError, KeyError):
        return {
            "strategy_version": "SWING_V11_SHADOW",
            "validation_status": "DATA_UNAVAILABLE",
            "performance_claim_allowed": False,
            "candidate_count": 0,
            "blocked_reason_counts": {"INDICATOR_CONTEXT_UNAVAILABLE": 1},
            "average_planned_notional_cny": None,
            "stop_width_rejection_count": 0,
            "missing_data_count": 1,
            "decision": None,
            "outcome": "DATA_BLOCKED",
            "outcome_counts": {"DATA_BLOCKED": 1},
        }
    reasons = list(decision.blocked_reasons)
    is_candidate = decision.state in {
        V11State.TECHNICAL_CANDIDATE, V11State.ACTION_CANDIDATE,
        V11State.POSITION_ACTION,
    }
    outcome = _v11_outcome_bucket(
        decision.state.value, list(decision.blocked_reasons), "AVAILABLE",
    )
    return {
        "strategy_version": "SWING_V11_SHADOW",
        "validation_status": "SHADOW_ONLY",
        "performance_claim_allowed": False,
        "candidate_count": int(is_candidate),
        "blocked_reason_counts": dict(Counter(reasons)),
        "average_planned_notional_cny": decision.planned_notional_cny,
        "stop_width_rejection_count": int("STOP_WIDTH_OVER_CAP" in reasons),
        "missing_data_count": int(
            decision.state is V11State.DATA_UNAVAILABLE
            or "DATA_QUALITY_UNVERIFIED" in reasons
        ),
        "decision": decision.to_dict(),
        "outcome": outcome,
        "outcome_counts": {outcome: 1},
    }


def _quality_blockers(manifest: dict[str, object], items: list[dict[str, object]]) -> list[str]:
    blockers: set[str] = set()
    for item in items:
        if not (
            item.get("crosscheck_status") == "PASSED"
            and item.get("amount_quality") == "PROVIDER_REPORTED"
            and item.get("adjustment_status") == "VERIFIED"
            and item.get("research_status") == "VALIDATED"
        ):
            blockers.add("DATA_QUALITY")
        if item.get("walk_forward_eligible") is not True:
            blockers.add("WALK_FORWARD")
    if int(manifest.get("oos_windows", 0) or 0) < 2:
        blockers.add("OOS_WINDOWS")
    return sorted(blockers)


def run_shadow_report(
    manifest_path: Path, *, history_path: Path | None = None,
    output_root: Path = Path("outputs/swing-research"),
    end_date: str | None = None,
    strategy_versions: tuple[str, ...] | None = None,
) -> tuple[Path, dict[str, object]]:
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("research manifest schema is invalid")
    resolved_history = Path(history_path or manifest["history_path"])
    history, invalid_symbols, invalid_lines = _load_history(resolved_history)
    if end_date is not None:
        cutoff = datetime.fromisoformat(end_date).date()
        history = {symbol: tuple(bar for bar in bars if bar.trading_date <= cutoff)
                   for symbol, bars in history.items()}
    config_path = Path(__file__).parents[1] / "data" / "swing" / "v11_strategy.json"
    v11_config = load_v11_config(config_path)
    requested = tuple(strategy_versions or ("V1", "V2_A", "V2_B", "V2_C", "SWING_V11_SHADOW"))
    items: list[dict[str, object]] = []
    v11_outcome_counts: Counter[str] = Counter()
    # Expand SWING_* request aliases to concrete replay variant names for compatibility.
    effective_requested = set(requested)
    if "SWING_V1" in requested:
        effective_requested.add("V1")
    if "SWING_V2_SHADOW" in requested:
        effective_requested.update(("V2_A", "V2_B", "V2_C", "HYBRID"))
    for item in manifest.get("items", []):
        symbol = item["symbol"]
        bars = history.get(symbol, ())
        invalid_history = symbol in invalid_symbols or not bars
        quality_blocked = (
            item.get("research_status") != "VERIFIED"
            or item.get("crosscheck_status") != "PASSED"
            or item.get("adjustment_status") != "VERIFIED"
        )
        results = replay_all_variants({symbol: bars}, costs=ExecutionCosts()) if bars and not invalid_history else {}
        variants: dict[str, dict[str, object]] = {}
        for name, result in results.items():
            key = "SWING_V1" if name == "V1" and "SWING_V1" in requested else name
            if name in effective_requested:
                if quality_blocked:
                    variants[key] = {
                        "validation_status": "BLOCKED_DATA_QUALITY",
                        "performance_claim_allowed": False,
                        "trade_count": 0,
                        "folds": [],
                        "metrics": {},
                        "net_pnl": 0.0,
                        "max_drawdown": 0.0,
                        "execution_assumptions": list(ExecutionCosts().assumptions()),
                    }
                else:
                    variants[key] = {
                        "validation_status": result.validation_status,
                        "performance_claim_allowed": result.performance_claim_allowed,
                        "trade_count": len(result.trades),
                        "folds": list(result.folds),
                        "metrics": dict(result.metrics),
                        "net_pnl": result.net_pnl,
                        "max_drawdown": result.max_drawdown,
                        "execution_assumptions": list(result.execution_assumptions),
                    }
        if invalid_history:
            for name in ("V1", "V2_A", "V2_B", "V2_C", "HYBRID"):
                variants[name] = {
                    "validation_status": "BLOCKED_INVALID_HISTORY",
                    "performance_claim_allowed": False,
                    "trade_count": 0,
                    "folds": [],
                    "metrics": {},
                    "net_pnl": 0.0,
                    "max_drawdown": 0.0,
                    "execution_assumptions": list(ExecutionCosts().assumptions()),
                }
        if "SWING_V11_SHADOW" in requested:
            v11_result = _v11_shadow_result(
                symbol, bars, item, v11_config,
            )
            variants["SWING_V11_SHADOW"] = v11_result
            v11_outcome_counts.update(v11_result.get("outcome_counts", {}))
        items.append({
            "symbol": symbol,
            "research_status": item.get("research_status"),
            "sample_class": item.get("sample_class"),
            "data_version": item.get("data_version"),
            "invalid_history": invalid_history,
            "invalid_history_reason": (
                "INVALID_DAILY_HISTORY" if symbol in invalid_symbols else
                "NO_DAILY_HISTORY" if not bars else None
            ),
            "variants": variants,
        })
    blocking_reasons = _quality_blockers(manifest, list(manifest.get("items", [])))
    if "SWING_V11_SHADOW" in requested:
        blocking_reasons.append("V11_SHADOW_ONLY")
    blocking_reasons = sorted(set(blocking_reasons))
    generated_at = datetime.now(SHANGHAI).isoformat(timespec="microseconds")
    input_digest = hashlib.sha256(json.dumps({
        symbol: [bar.to_dict() for bar in bars] for symbol, bars in sorted(history.items())
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    run_key = json.dumps({
        "manifest": manifest.get("generated_at"),
        "items": [(item["symbol"], item.get("data_version")) for item in items],
        "end_date": end_date,
        "input_digest": input_digest,
        "invalid_history_lines": invalid_lines,
        "generated_at": generated_at,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    run_id = hashlib.sha256(run_key).hexdigest()[:16]
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": generated_at,
        "end_date": end_date,
        "manifest_path": str(manifest_path),
        "history_path": str(resolved_history),
        "research_only": True,
        "formal_strategy_unchanged": True,
        "strategy_versions": list(requested),
        "performance_claim_allowed": not blocking_reasons,
        "blocking_reasons": blocking_reasons,
        "shadow_outcome_counts": dict(v11_outcome_counts),
        "input_digest": input_digest,
        "invalid_history_lines": invalid_lines,
        "validation": {
            "status": "BLOCKED_DATA_QUALITY",
            "performance_claim_allowed": False,
            "walk_forward": {"train_sessions": 504, "test_sessions": 126, "step_sessions": 126,
                "minimum_common_sessions": 630, "required_folds": 2},
            "costs": list(ExecutionCosts().assumptions()),
        },
        "items": items,
    }
    output_path = Path(output_root) / run_id / "shadow-report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--history", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path("outputs/swing-research"))
    parser.add_argument("--end-date")
    args = parser.parse_args()
    output, report = run_shadow_report(
        args.manifest, history_path=args.history,
        output_root=args.output_root, end_date=args.end_date,
    )
    print(json.dumps({"output": str(output), "run_id": report["run_id"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
