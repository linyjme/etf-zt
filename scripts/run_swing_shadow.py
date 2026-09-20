"""Generate a versioned, non-executable shadow replay report."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import argparse
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from etf_rotation.swing_data import DailyBar
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


def run_shadow_report(
    manifest_path: Path, *, history_path: Path | None = None,
    output_root: Path = Path("outputs/swing-research"),
    end_date: str | None = None,
) -> tuple[Path, dict[str, object]]:
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("research manifest schema is invalid")
    resolved_history = Path(history_path or manifest["history_path"])
    history = _load_history(resolved_history)
    items: list[dict[str, object]] = []
    for item in manifest.get("items", []):
        symbol = item["symbol"]
        bars = history.get(symbol, ())
        results = replay_all_variants({symbol: bars}, costs=ExecutionCosts()) if bars else {}
        items.append({
            "symbol": symbol,
            "research_status": item.get("research_status"),
            "sample_class": item.get("sample_class"),
            "data_version": item.get("data_version"),
            "variants": {
                name: {
                    "validation_status": result.validation_status,
                    "performance_claim_allowed": result.performance_claim_allowed,
                    "trade_count": len(result.trades),
                    "net_pnl": result.net_pnl,
                    "max_drawdown": result.max_drawdown,
                    "execution_assumptions": list(result.execution_assumptions),
                }
                for name, result in results.items()
            },
        })
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

