from __future__ import annotations

import json
import subprocess
import unittest
from datetime import date
from pathlib import Path

from etf_rotation.swing_v11 import V11Context, evaluate_v11, load_v11_config
from etf_rotation.swing_page import SWING_PAGE
from scripts.run_swing_shadow import _v11_outcome_bucket, _v11_shadow_result
from tests.swing_helpers import swing_strategy_bars


ROOT = Path(__file__).resolve().parents[1]


class SwingV11P3Tests(unittest.TestCase):
    def test_quasi_close_requires_real_1445_evidence(self) -> None:
        bars = swing_strategy_bars(300)
        config = load_v11_config(ROOT / "data" / "swing" / "v11_strategy.json")
        decision = evaluate_v11(
            bars,
            config=config,
            context=V11Context(
                environment_state="ATTACK",
                category="BROAD",
                data_quality="VERIFIED",
                as_of_kind="QUASI_CLOSE_1445",
                as_of_trading_date=bars[-1].trading_date.isoformat(),
                quasi_close={
                    "price": 100.0,
                    "observed_at": f"{bars[-1].trading_date.isoformat()}T14:20:00+08:00",
                    "health": "REALTIME",
                },
            ),
        )
        self.assertIn("QUASI_CLOSE_NOT_VERIFIED", decision.blocked_reasons)
        self.assertFalse(decision.executable)

    def test_quasi_close_normalizes_utc_and_rejects_naive_timestamp(self) -> None:
        bars = swing_strategy_bars(300)
        config = load_v11_config(ROOT / "data" / "swing" / "v11_strategy.json")
        common = dict(
            environment_state="ATTACK",
            category="BROAD",
            data_quality="VERIFIED",
            as_of_kind="QUASI_CLOSE_1445",
            as_of_trading_date=bars[-1].trading_date.isoformat(),
        )
        utc_decision = evaluate_v11(
            bars,
            config=config,
            context=V11Context(
                **common,
                quasi_close={
                    "price": 100.0,
                    "observed_at": f"{bars[-1].trading_date.isoformat()}T06:50:00+00:00",
                    "health": "REALTIME",
                },
            ),
        )
        self.assertNotIn("QUASI_CLOSE_NOT_VERIFIED", utc_decision.blocked_reasons)
        naive_decision = evaluate_v11(
            bars,
            config=config,
            context=V11Context(
                **common,
                quasi_close={
                    "price": 100.0,
                    "observed_at": f"{bars[-1].trading_date.isoformat()}T14:50:00",
                    "health": "REALTIME",
                },
            ),
        )
        self.assertIn("QUASI_CLOSE_NOT_VERIFIED", naive_decision.blocked_reasons)

    def test_missing_v11_evidence_is_fail_closed(self) -> None:
        bars = swing_strategy_bars(300)
        config = load_v11_config(ROOT / "data" / "swing" / "v11_strategy.json")
        decision = evaluate_v11(
            bars,
            config=config,
            context=V11Context(
                environment_state="ATTACK",
                category="BROAD",
                data_quality="VERIFIED",
                indicator={
                    "bar_count": 300,
                    "price": 100.0,
                    "ma20": 98.0,
                    "ma60": 95.0,
                    "weekly_close": 101.0,
                    "weekly_ma20": 96.0,
                },
            ),
        )
        self.assertEqual(decision.state.value, "OBSERVE")
        for marker in (
            "T3_EVIDENCE_UNAVAILABLE", "T4_EVIDENCE_UNAVAILABLE",
            "A1_A2_EVIDENCE_UNAVAILABLE", "A3_EVIDENCE_UNAVAILABLE",
            "A4_EVIDENCE_UNAVAILABLE", "A6_EVIDENCE_UNAVAILABLE",
        ):
            self.assertIn(marker, decision.blocked_reasons)
        self.assertFalse(decision.evidence["t4_return_60d"])

    def test_shadow_replay_keeps_full_v11_evidence(self) -> None:
        bars = swing_strategy_bars(300)
        item = {
            "symbol": "510300",
            "category": "BROAD",
            "crosscheck_status": "PASSED",
            "amount_quality": "PROVIDER_REPORTED",
            "adjustment_status": "VERIFIED",
        }
        result = _v11_shadow_result("510300", bars, item, load_v11_config(
            ROOT / "data" / "swing" / "v11_strategy.json",
        ))
        evidence = (result["decision"] or {}).get("evidence", {})
        self.assertIn("return_60d_pct", evidence)
        self.assertIn("pullback_recovery_within_3d", evidence)
        self.assertIn("macd_histogram_improving_2d", evidence)

    def test_shadow_outcome_buckets_separate_data_rule_and_signal(self) -> None:
        self.assertEqual(_v11_outcome_bucket("TECHNICAL_CANDIDATE", [], "AVAILABLE"), "CANDIDATE")
        self.assertEqual(_v11_outcome_bucket("OBSERVE", ["TREND_NOT_CONFIRMED"], "AVAILABLE"), "RULE_BLOCKED")
        self.assertEqual(_v11_outcome_bucket("OBSERVE", ["DATA_QUALITY_UNVERIFIED"], "AVAILABLE"), "DATA_BLOCKED")
        self.assertEqual(_v11_outcome_bucket("OBSERVE", [], "AVAILABLE"), "NO_SIGNAL")

    def test_page_exposes_gate_and_shadow_outcome_evidence(self) -> None:
        for label in ("数据门控", "环境状态", "相对强度", "连续确认", "候选分类", "DATA_BLOCKED", "RULE_BLOCKED"):
            self.assertIn(label, SWING_PAGE)

    def test_page_does_not_label_blocked_candidate_as_candidate(self) -> None:
        start = "/* SWING_PAGE_HELPERS_START */"
        end = "/* SWING_PAGE_HELPERS_END */"
        helpers = SWING_PAGE.split(start, 1)[1].split(end, 1)[0]
        body = """
const html=shadowMarkup({status:'AVAILABLE',blocked_reasons:['DATA_QUALITY_UNKNOWN'],variants:{HYBRID:{state:'TECHNICAL_CANDIDATE',blocked_reasons:['DATA_QUALITY_UNKNOWN'],evidence:{}}}});
console.log(JSON.stringify(html));
"""
        completed = subprocess.run(["node", "-"], input=helpers + body, text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        html = json.loads(completed.stdout)
        self.assertIn("数据质量未知", html)
        self.assertNotIn("混合影子 · 技术候选", html)


if __name__ == "__main__":
    unittest.main()
