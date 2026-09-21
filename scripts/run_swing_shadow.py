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


def _load_history(path: Path) -> dict[str, tuple[DailyBar, ...]]:
    groups: dict[str, list[DailyBar]] = defaultdict(list)
    if not path.exists():
        return {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            groups[json.loads(line)["symbol"]].append(
                DailyBar.from_mapping(json.loads(line))
            )
        except Exception as error:
            raise ValueError(f"invalid daily history line {line_number}") from error
    return {symbol: tuple(bars) for symbol, bars in groups.items()}


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
        }
    reasons = list(decision.blocked_reasons)
    is_candidate = decision.state in {
        V11State.TECHNICAL_CANDIDATE, V11State.ACTION_CANDIDATE,
        V11State.POSITION_ACTION,
    }
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
    history = _load_history(resolved_history)
    config_path = Path(__file__).parents[1] / "data" / "swing" / "v11_strategy.json"
    v11_config = load_v11_config(config_path)
    requested = tuple(strategy_versions or ("V1", "V2_A", "V2_B", "V2_C", "SWING_V11_SHADOW"))
    items: list[dict[str, object]] = []
    for item in manifest.get("items", []):
        symbol = item["symbol"]
        bars = history.get(symbol, ())
        results = replay_all_variants({symbol: bars}, costs=ExecutionCosts()) if bars else {}
        variants: dict[str, dict[str, object]] = {}
        for name, result in results.items():
            key = "SWING_V1" if name == "V1" and "SWING_V1" in requested else name
            if name in requested or key in requested:
                variants[key] = {
                    "validation_status": result.validation_status,
                    "performance_claim_allowed": result.performance_claim_allowed,
                    "trade_count": len(result.trades),
                    "net_pnl": result.net_pnl,
                    "max_drawdown": result.max_drawdown,
                    "execution_assumptions": list(result.execution_assumptions),
                }
        if "SWING_V11_SHADOW" in requested:
            variants["SWING_V11_SHADOW"] = _v11_shadow_result(
                symbol, bars, item, v11_config,
            )
        items.append({
            "symbol": symbol,
            "research_status": item.get("research_status"),
            "sample_class": item.get("sample_class"),
            "data_version": item.get("data_version"),
            "variants": variants,
        })
    blocking_reasons = _quality_blockers(manifest, list(manifest.get("items", [])))
    if "SWING_V11_SHADOW" in requested:
        blocking_reasons.append("V11_SHADOW_ONLY")
    blocking_reasons = sorted(set(blocking_reasons))
    generated_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    run_key = json.dumps({
        "manifest": manifest.get("generated_at"),
        "items": [(item["symbol"], item.get("data_version")) for item in items],
        "end_date": end_date,
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

