from __future__ import annotations

import json
import re
import unittest

from etf_rotation.swing_page import SWING_PAGE
from tests.test_swing_holdings_integration import snapshot_view
from tests.test_swing_page import run_swing_helpers


def page_payload() -> dict:
    return {
        "revision": 7, "as_of_trading_date": "2026-09-01",
        "holdings_snapshot": snapshot_view(),
        "items": [{"symbol": "510300", "formal_state": "UPTREND_WATCH",
                   "execution_status": "PAUSED_HOLDINGS_SNAPSHOT"}],
        "health": {"service": "OK", "configuration": "OK", "calendar": "OK",
                   "daily": "OK", "portfolio": "SNAPSHOT_ONLY", "alerts": "OK",
                   "intraday": "REALTIME", "minute_crosscheck": "NOT_RUN"},
        "errors": {"portfolio": "synthetic report only"},
    }


def inspect_notice(payload: dict | None, sources: tuple[str, ...] = ()) -> dict:
    return run_swing_helpers("const payload=" + json.dumps(payload) + ";const sources=" + json.dumps(sources) + ";" + """
const state=createPageState();state.failureSources=new Set(sources);if(payload)applySnapshotPayload(state,payload,true);
const before=JSON.stringify(state.snapshot),exists=typeof executionNotice==='function'&&typeof executionWarningMarkup==='function';
console.log(JSON.stringify({exists,notice:exists?executionNotice(state):null,html:exists?executionWarningMarkup(state):'',paused:state.safetyPaused,ready:state.selectedExecutable,unchanged:before===JSON.stringify(state.snapshot)}));
""")


class SwingAccountLayoutTests(unittest.TestCase):
    def test_global_navigation_replaces_full_account_sections(self) -> None:
        self.assertIn('href="/pr">PR估值</a>', SWING_PAGE)
        self.assertIn('href="/">做T监控</a>', SWING_PAGE)
        self.assertIn('href="/swing" aria-current="page">波段监控</a>', SWING_PAGE)
        for fragment in ('id="portfolio-risk"', 'id="holdings-snapshot"',
                         "function renderRisk()", "function holdingsAccountMarkup(",
                         "function holdingsSnapshotMarkup("):
            self.assertNotIn(fragment, SWING_PAGE)
        self.assertIn("renderPortfolioWriteLock()", SWING_PAGE)

    def test_summary_only_renders_the_selected_holding(self) -> None:
        payload = page_payload()
        payload["holdings_snapshot"]["snapshot"]["positions"][0]["name"] = "<img src=x onerror=alert(1)>"
        result = run_swing_helpers("const payload=" + json.dumps(payload) + ";" + """
console.log(JSON.stringify({exists:typeof selectedHoldingMarkup==='function',html:typeof selectedHoldingMarkup==='function'?selectedHoldingMarkup(payload,'510300'):''}));
""")
        self.assertTrue(result["exists"])
        for fragment in ("报告份额", "报告可卖", "报告成本", "1.234", "100", "非实时", "持仓截图时点", "未知", "&lt;img"):
            self.assertIn(fragment, result["html"])
        for fragment in ("Synthetic stock", "600036", "总资产", "额外损失预算", "<table", "<img"):
            self.assertNotIn(fragment, result["html"])
        self.assertRegex(result["html"], r'data-holding-field="sellable_shares">0<')

    def test_unregistered_and_unknown_sellable_quantities_are_not_zero(self) -> None:
        result = run_swing_helpers("const payload=" + json.dumps(page_payload()) + ";" + """
const exists=typeof selectedHoldingMarkup==='function';console.log(JSON.stringify({exists,missing:exists?selectedHoldingMarkup(payload,'159915'):'',unknown:exists?selectedHoldingMarkup(payload,'600036'):''}));
""")
        self.assertTrue(result["exists"])
        self.assertIn("未登记", result["missing"])
        self.assertIn("不代表空仓", result["missing"])
        for field in ("shares", "sellable_shares", "average_cost"):
            self.assertRegex(result["missing"], rf'data-holding-field="{field}">—<')
        self.assertRegex(result["unknown"], r'data-holding-field="sellable_shares">—<')

    def test_chart_precedes_compact_holding_summary(self) -> None:
        detail = SWING_PAGE.split("function renderDetail(){", 1)[1].split("function bindChartTooltip", 1)[0]
        # Check the populated detail, not the separate no-history empty state.
        rendered = detail.split('detailNode.innerHTML=`<div class="detail-head">', 1)[1]
        self.assertIn("selectedHoldingMarkup(state.snapshot,state.selectedSymbol)", rendered)
        self.assertLess(rendered.index("chartHtml(item.symbol)"), rendered.index("selectedHoldingMarkup("))

    def test_valid_snapshot_is_neutral_but_still_blocks_execution(self) -> None:
        result = inspect_notice(page_payload())
        self.assertTrue(result["exists"])
        self.assertEqual(result["notice"]["tone"], "info")
        self.assertIn("持仓", result["notice"]["summary"])
        self.assertTrue(result["paused"])
        self.assertFalse(result["ready"])
        self.assertTrue(result["unchanged"])
        self.assertIn("<details>", result["html"])
        self.assertNotIn("<details open", result["html"])
        self.assertIn("迁移", result["html"])
        self.assertLess(len(result["notice"]["summary"]), 55)

    def test_snapshot_never_masks_concurrent_market_or_service_failure(self) -> None:
        cases = (
            ({"intraday": "OUTAGE"}, {"intraday": "synthetic outage"}, (), "行情"),
            ({"service": "CLOCK_FAILED"}, {"service": "synthetic clock failure"}, (), "服务"),
            ({}, {}, ("sse",), "连接"),
            ({}, {"unexpected": "<script>private-detail</script>"}, (), "后台"),
            ({"daily": "BLOCKED"}, {"daily": "synthetic corrupt history"}, (), "日线"),
        )
        for health, errors, sources, copy in cases:
            with self.subTest(health=health, sources=sources):
                payload = page_payload()
                payload["health"].update(health)
                payload["errors"].update(errors)
                result = inspect_notice(payload, sources)
                self.assertTrue(result["exists"])
                self.assertEqual(result["notice"]["tone"], "danger")
                self.assertIn(copy, result["notice"]["summary"])
                self.assertTrue(result["paused"])
                self.assertFalse(result["ready"])
                self.assertNotIn("private-detail", result["html"])
                self.assertNotIn("<script>", result["html"])

    def test_missing_configuration_and_waiting_for_data_are_yellow(self) -> None:
        for case in ("initial", "account", "configuration", "intraday"):
            with self.subTest(case=case):
                payload = page_payload()
                if case == "initial":
                    payload = None
                elif case == "account":
                    payload["holdings_snapshot"] = {"status": "ABSENT"}
                    payload["health"]["portfolio"] = "UNINITIALIZED"
                    payload["errors"] = {"portfolio": "synthetic account uninitialized"}
                elif case == "configuration":
                    payload["health"].update(service="BLOCKED", configuration="BLOCKED")
                    payload["errors"]["configuration"] = "synthetic configuration unavailable"
                else:
                    payload["health"]["intraday"] = "UNAVAILABLE"
                    payload["errors"]["intraday"] = "synthetic no realtime quote"
                result = inspect_notice(payload)
                self.assertTrue(result["exists"])
                self.assertEqual(result["notice"]["tone"], "warning")
                self.assertTrue(result["paused"])
                self.assertFalse(result["ready"])

    def test_invalid_snapshot_is_danger_and_remains_safe(self) -> None:
        payload = page_payload()
        payload["holdings_snapshot"] = {"status": "INVALID", "snapshot": None,
                                        "error": "<script>private-detail</script>"}
        payload["health"]["portfolio"] = "BLOCKED"
        result = inspect_notice(payload)
        self.assertTrue(result["exists"])
        self.assertEqual(result["notice"]["tone"], "danger")
        self.assertIn("持仓快照", result["notice"]["summary"])
        self.assertTrue(result["paused"])
        self.assertNotIn("private-detail", result["html"])

    def test_market_recovery_does_not_unlock_snapshot_strategy(self) -> None:
        result = run_swing_helpers("const payload=" + json.dumps(page_payload()) + ";" + """
const state=createPageState(),exists=typeof executionNotice==='function';payload.health.intraday='OUTAGE';payload.errors.intraday='synthetic outage';applySnapshotPayload(state,payload,true);const failed=exists?executionNotice(state):null;
payload.revision++;payload.health.intraday='REALTIME';delete payload.errors.intraday;applySnapshotPayload(state,payload,false);console.log(JSON.stringify({exists,failed,recovered:exists?executionNotice(state):null,paused:state.safetyPaused,ready:state.selectedExecutable}));
""")
        self.assertTrue(result["exists"])
        self.assertEqual(result["failed"]["tone"], "danger")
        self.assertEqual(result["recovered"]["tone"], "info")
        self.assertTrue(result["paused"])
        self.assertFalse(result["ready"])

    def test_ready_state_has_no_empty_warning_card(self) -> None:
        payload = page_payload()
        payload["holdings_snapshot"] = {"status": "ABSENT"}
        payload["health"]["portfolio"] = "OK"
        payload["errors"] = {}
        payload["items"][0]["execution_status"] = "READY_TO_EXECUTE"
        result = inspect_notice(payload)
        self.assertTrue(result["exists"])
        self.assertEqual(result["html"], "")
        self.assertFalse(result["paused"])
        self.assertTrue(result["ready"])
        self.assertIn("warningNode.hidden=", SWING_PAGE)


if __name__ == "__main__":
    unittest.main()
