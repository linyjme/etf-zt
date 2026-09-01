from __future__ import annotations

import json
import subprocess
import unittest

from etf_rotation.swing_page import SWING_PAGE


def run_swing_helpers(body: str) -> object:
    start = "/* SWING_PAGE_HELPERS_START */"
    end = "/* SWING_PAGE_HELPERS_END */"
    if start not in SWING_PAGE or end not in SWING_PAGE:
        raise AssertionError("SWING_PAGE does not expose pure JavaScript helpers")
    helpers = SWING_PAGE.split(start, 1)[1].split(end, 1)[0]
    completed = subprocess.run(
        ["node", "-"],
        input=helpers + "\n" + body,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


class SwingPageContractTests(unittest.TestCase):
    def test_page_has_semantic_risk_first_layout_and_no_demo_numbers(self) -> None:
        self.assertIn("指数ETF波段监控", SWING_PAGE)
        self.assertIn('<nav aria-label="监控模式">', SWING_PAGE)
        self.assertIn('href="/"', SWING_PAGE)
        self.assertIn('href="/swing" aria-current="page"', SWING_PAGE)
        self.assertIn('id="portfolio-risk"', SWING_PAGE)
        self.assertIn('id="swing-watchlist"', SWING_PAGE)
        self.assertIn('id="swing-detail"', SWING_PAGE)
        self.assertIn('id="swing-errors" role="alert"', SWING_PAGE)
        self.assertIn('id="swing-live-status" aria-live="polite"', SWING_PAGE)
        self.assertLess(SWING_PAGE.index('id="portfolio-risk"'), SWING_PAGE.index('id="swing-watchlist"'))
        for label in (
            "组合权益", "计划总风险", "波段标的", "MA20", "MA60",
            "阻断原因", "成交账本", "波段回测", "初始化波段账户",
        ):
            self.assertIn(label, SWING_PAGE)
        self.assertNotIn("100,000", SWING_PAGE)
        self.assertNotIn("4.700", SWING_PAGE)
        self.assertIn("仅监控，不自动交易", SWING_PAGE)

    def test_chart_accessibility_responsiveness_and_empty_states_are_explicit(self) -> None:
        for fragment in (
            "最近120个完成交易日", "原始收盘", "MA20", "MA60", "计划买入区",
            "硬止损", "移动止损", 'tabindex="0"', 'role="img"',
            "无日线数据", "日线数据不足", "行情状态", "信号数据日期",
            "@media(max-width:850px)", "@media(max-width:600px)",
            "grid-template-columns:repeat(2,minmax(0,1fr))", "touchmove",
        ):
            self.assertIn(fragment, SWING_PAGE)
        self.assertIn("adjustment_scale", SWING_PAGE)
        self.assertIn("adjusted_close", SWING_PAGE)
        self.assertIn("latest.close/latest.adjusted_close", SWING_PAGE)

    def test_only_metadata_symbols_can_be_toggled_and_forms_are_accessible(self) -> None:
        self.assertIn("/api/swing/watchlist", SWING_PAGE)
        self.assertNotIn('name="symbol" inputmode="numeric"', SWING_PAGE)
        self.assertNotIn("添加未知标的", SWING_PAGE)
        self.assertIn("只可启停已核验的六只ETF", SWING_PAGE)
        for form_id in (
            "portfolio-initialize-form", "trade-form", "reverse-form",
        ):
            self.assertIn(f'id="{form_id}"', SWING_PAGE)
        self.assertIn("失败后会保留已输入内容", SWING_PAGE)
        self.assertIn("'Content-Type':'application/json'", SWING_PAGE)
        self.assertIn("'Idempotency-Key':idempotencyKey", SWING_PAGE)
        self.assertIn("prepareIntent", SWING_PAGE)
        self.assertNotIn("broker", SWING_PAGE.lower())
        self.assertIn("encodeURIComponent", SWING_PAGE)

    def test_watch_rows_and_alerts_expose_status_evidence_and_manual_record_action(self) -> None:
        self.assertIn("select.append(dot,strong,small)", SWING_PAGE)
        self.assertIn("查看判断依据", SWING_PAGE)
        self.assertIn('data-alert-trade="', SWING_PAGE)
        self.assertIn("openTradeForAlert", SWING_PAGE)

    def test_page_uses_incremental_daily_sse_and_permission_gated_notifications(self) -> None:
        for fragment in (
            "/api/swing/snapshot", "/api/swing/daily-quotes", "/api/swing/events",
            "new EventSource", "Notification.requestPermission", "通知需点击授权",
            "APPROACHING_ENTRY_ZONE", "PREDEFINED_STOP_TOUCHED",
            "notifiedAlertIds", "executionPaused", "lastFormalPlan",
        ):
            self.assertIn(fragment, SWING_PAGE)
        permission_index = SWING_PAGE.index("Notification.requestPermission")
        click_index = SWING_PAGE.rfind("addEventListener('click'", 0, permission_index)
        self.assertGreater(click_index, -1)

    def test_daily_authoritative_reset_accepts_revision_rollback_and_rejects_old_delta(self) -> None:
        result = run_swing_helpers(
            """
const state=createPageState();
applyDailyPayload(state,{symbol:'510300',revision:8,reset:true,upserts:[{trading_date:'2026-08-29',close:4}]});
applyDailyPayload(state,{symbol:'510300',revision:3,reset:true,upserts:[{trading_date:'2026-09-01',close:5}]});
const accepted=applyDailyPayload(state,{symbol:'510300',revision:2,reset:false,upserts:[{trading_date:'2026-09-02',close:6}]});
console.log(JSON.stringify({accepted,revision:state.dailyRevisions.get('510300'),bars:[...state.dailyBySymbol.get('510300').values()]}));
""",
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["revision"], 3)
        self.assertEqual([bar["trading_date"] for bar in result["bars"]], ["2026-09-01"])

    def test_reset_clears_before_upsert_validates_symbol_and_preserves_selection(self) -> None:
        result = run_swing_helpers(
            """
const state=createPageState(); state.selectedSymbol='510300';
applyDailyPayload(state,{symbol:'510300',revision:1,reset:true,upserts:[{trading_date:'2026-08-29',close:4}]});
let bad=''; try{applyDailyPayload(state,{symbol:'<img>',revision:2,reset:true,upserts:[]})}catch(error){bad=error.message}
applyDailyPayload(state,{symbol:'510300',revision:2,reset:true,upserts:[]});
console.log(JSON.stringify({bad,selected:state.selectedSymbol,size:state.dailyBySymbol.get('510300').size}));
""",
        )
        self.assertTrue(result["bad"])
        self.assertEqual(result["selected"], "510300")
        self.assertEqual(result["size"], 0)

    def test_feed_failure_revokes_only_intraday_overlays_and_pauses_formal_plan(self) -> None:
        result = run_swing_helpers(
            """
const state=createPageState();
state.snapshot={as_of_trading_date:'2026-08-31',items:[{symbol:'510300',formal_state:'TRIAL_ENTRY_CANDIDATE',intraday_overlay:'APPROACHING_ENTRY_ZONE'}],active_alerts:[{alert_id:'a',scope:'INTRADAY',state:'PREDEFINED_STOP_TOUCHED'},{alert_id:'b',scope:'FORMAL',state:'TRIAL_ENTRY_CANDIDATE'}]};
revokeIntraday(state,'断流');
console.log(JSON.stringify({paused:state.executionPaused,overlay:state.snapshot.items[0].intraday_overlay,alerts:state.snapshot.active_alerts.map(a=>a.alert_id),date:state.lastFormalPlan.as_of_trading_date}));
""",
        )
        self.assertTrue(result["paused"])
        self.assertIsNone(result["overlay"])
        self.assertEqual(result["alerts"], ["b"])
        self.assertEqual(result["date"], "2026-08-31")

    def test_notification_gate_deduplicates_only_new_active_alerts(self) -> None:
        result = run_swing_helpers(
            """
const seen=new Set();
const active={alert_id:'a',active:true,retracted:false};
const first=shouldNotifyAlert(active,true,'granted',seen); if(first)seen.add('a');
console.log(JSON.stringify({noClick:shouldNotifyAlert({alert_id:'b',active:true},false,'granted',seen),first,again:shouldNotifyAlert(active,true,'granted',seen),retracted:shouldNotifyAlert({alert_id:'c',active:true,retracted:true},true,'granted',seen)}));
""",
        )
        self.assertEqual(result, {"noClick": False, "first": True, "again": False, "retracted": False})

    def test_html_escaping_and_dynamic_identifier_validation(self) -> None:
        result = run_swing_helpers(
            "console.log(JSON.stringify({escaped:escapeHtml('<img src=x onerror=1>'),alert:validAlertId('a'.repeat(24)),uuid:validEventId('00000000-0000-4000-8000-000000000000')}));",
        )
        self.assertEqual(result["escaped"], "&lt;img src=x onerror=1&gt;")
        self.assertTrue(result["alert"])
        self.assertTrue(result["uuid"])

    def test_numeric_parser_never_turns_missing_or_boolean_values_into_zero(self) -> None:
        result = run_swing_helpers(
            "console.log(JSON.stringify([null,undefined,'',true,false,'  ',Infinity,'1.25',2].map(value=>finite(value))));",
        )
        self.assertEqual(result, [None, None, None, None, None, None, None, 1.25, 2])

    def test_execution_requires_complete_server_health_and_valid_sse_snapshot(self) -> None:
        result = run_swing_helpers(
            """
const state=createPageState();
state.failureSources.add('snapshot'); state.failureSources.add('sse');
state.snapshot={health:{intraday:'REALTIME'}}; refreshExecutionPaused(state);
const afterOpen=eventStreamOpened(state);
const payload={revision:1,items:[{symbol:'510300',execution_status:'READY_TO_EXECUTE'}],health:{service:'OK',configuration:'OK',daily:'OK',strategy:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME'},errors:{}};
const accepted=acceptSseSnapshot(state,payload,false);
payload.health.portfolio='UNINITIALIZED'; applySnapshotPayload(state,payload,true); refreshExecutionPaused(state);
console.log(JSON.stringify({afterOpen,accepted,afterValid:!state.failureSources.size,blocked:state.executionPaused}));
""",
        )
        self.assertEqual(result, {"afterOpen": True, "accepted": True, "afterValid": True, "blocked": True})

    def test_chart_computes_ma_on_179_bars_before_slicing_last_120(self) -> None:
        result = run_swing_helpers(
            """
const bars=Array.from({length:179},(_,index)=>({symbol:'510300',trading_date:`2026-${String(1+Math.floor(index/28)).padStart(2,'0')}-${String(1+index%28).padStart(2,'0')}`,close:index+1,adjusted_close:index+1}));
const series=chartSeries(bars);
console.log(JSON.stringify({length:series.points.length,firstMa60:series.points[0].ma60,firstClose:series.points[0].close}));
""",
        )
        self.assertEqual(result["length"], 120)
        self.assertIsNotNone(result["firstMa60"])

    def test_idempotency_key_is_reused_until_success_or_payload_change(self) -> None:
        result = run_swing_helpers(
            """
let serial=0; const registry=new Map(),factory=()=>`key-${++serial}`;
const first=prepareIntent(registry,'trade','/api/swing/trades',{shares:100},factory);
const retry=prepareIntent(registry,'trade','/api/swing/trades',{shares:100},factory);
const changed=prepareIntent(registry,'trade','/api/swing/trades',{shares:200},factory);
settleIntentSuccess(registry,'trade',changed.key);
const afterSuccess=prepareIntent(registry,'trade','/api/swing/trades',{shares:200},factory);
console.log(JSON.stringify({first:first.key,retry:retry.key,changed:changed.key,afterSuccess:afterSuccess.key}));
""",
        )
        self.assertEqual(result, {"first": "key-1", "retry": "key-1", "changed": "key-2", "afterSuccess": "key-3"})

    def test_initial_positions_use_api_field_names_and_validate_lots(self) -> None:
        result = run_swing_helpers(
            """
const good=buildInitialPositions([{symbol:'510300',enabled:true,shares:'200',average_cost:'4.1',planned_risk_per_share:'0.2'}]);
let bad='';try{buildInitialPositions([{symbol:'510300',enabled:true,shares:'150',average_cost:'4.1',planned_risk_per_share:'0'}])}catch(error){bad=error.message}
console.log(JSON.stringify({good,bad}));
""",
        )
        self.assertEqual(result["good"]["510300"], {"shares": 200, "average_cost": 4.1, "planned_risk_per_share": 0.2})
        self.assertTrue(result["bad"])
        self.assertIn('id="initial-position-rows"', SWING_PAGE)
        self.assertIn("initial_positions", SWING_PAGE)
        self.assertIn("limit=179", SWING_PAGE)

    def test_pointer_index_uses_plot_bounds_not_whole_svg(self) -> None:
        result = run_swing_helpers(
            """
console.log(JSON.stringify({
  left:pointerIndex(162,162,780,120),
  middle:pointerIndex(552,162,780,120),
  right:pointerIndex(942,162,780,120),
  before:pointerIndex(100,162,780,120),
  after:pointerIndex(1000,162,780,120)
}));
""",
        )
        self.assertEqual(result, {"left": 0, "middle": 60, "right": 119, "before": 0, "after": 119})
        self.assertIn("hit.getBoundingClientRect()", SWING_PAGE)


if __name__ == "__main__":
    unittest.main()
