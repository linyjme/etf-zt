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

    def test_later_snapshot_cannot_restore_intraday_overlay_during_failure(self) -> None:
        result = run_swing_helpers(
            """
const state=createPageState(); state.selectedSymbol='510300'; state.failureSources.add('daily');
const payload={revision:2,items:[{symbol:'510300',execution_status:'READY_TO_EXECUTE',intraday_overlay:'APPROACHING_ENTRY_ZONE'}],active_alerts:[{alert_id:'a'.repeat(24),scope:'INTRADAY',state:'PREDEFINED_STOP_TOUCHED',currently_active:true}],alerts:[{alert_id:'b'.repeat(24),scope:'INTRADAY',state:'APPROACHING_ENTRY_ZONE',currently_active:true}],health:{service:'OK',configuration:'OK',daily:'OK',strategy:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME'},errors:{}};
applySnapshotPayload(state,payload,false); refreshExecutionPaused(state);
const candidates=(state.snapshot.active_alerts||[]).filter(alert=>shouldNotifyAlert(alert,true,'granted',new Set(),state.safetyPaused));
console.log(JSON.stringify({paused:state.safetyPaused,overlay:state.snapshot.items[0].intraday_overlay,active:state.snapshot.active_alerts.length,notifications:candidates.length}));
""",
        )
        self.assertEqual(result, {"paused": True, "overlay": None, "active": 0, "notifications": 0})

    def test_read_model_bundle_installs_only_when_all_revisions_match_snapshot(self) -> None:
        result = run_swing_helpers(
            """
const state=createPageState(); state.selectedSymbol='510300';
const snapshot=revision=>({revision,items:[{symbol:'510300',execution_status:'OBSERVE_ONLY'}],health:{service:'OK',configuration:'OK',daily:'OK',strategy:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME'},errors:{}});
const aux=revision=>({watch:{revision,items:[]},portfolio:{revision,projection:null},alerts:{revision,items:[]}});
applyReadModelBundle(state,snapshot(5),aux(5),true);
let mismatch=''; try{applyReadModelBundle(state,snapshot(6),{watch:aux(6).watch,portfolio:aux(5).portfolio,alerts:aux(6).alerts},false)}catch(error){mismatch=error.message}
const preserved={revision:state.snapshotRevision,watch:state.auxiliary.watch.revision,portfolio:state.auxiliary.portfolio.revision,alerts:state.auxiliary.alerts.revision};
applyReadModelBundle(state,snapshot(6),aux(6),false);
console.log(JSON.stringify({mismatch,preserved,installed:state.snapshotRevision,auxRevision:state.auxiliary.alerts.revision}));
""",
        )
        self.assertTrue(result["mismatch"])
        self.assertEqual(result["preserved"], {"revision": 5, "watch": 5, "portfolio": 5, "alerts": 5})
        self.assertEqual(result["installed"], 6)
        self.assertEqual(result["auxRevision"], 6)

    def test_safety_pause_is_separate_from_selected_execution_candidate(self) -> None:
        result = run_swing_helpers(
            """
const state=createPageState(); state.selectedSymbol='510300';
const base={revision:1,items:[{symbol:'510300',execution_status:'OBSERVE_ONLY'}],health:{service:'OK',configuration:'OK',daily:'OK',strategy:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME'},errors:{}};
applySnapshotPayload(state,base,true); refreshExecutionPaused(state);
const observe={safetyPaused:state.safetyPaused,selectedExecutable:state.selectedExecutable,copy:executionStatusCopy(state)};
base.revision=2;base.items[0].execution_status='READY_TO_EXECUTE';applySnapshotPayload(state,base,false);refreshExecutionPaused(state);
console.log(JSON.stringify({observe,ready:{safetyPaused:state.safetyPaused,selectedExecutable:state.selectedExecutable,copy:executionStatusCopy(state)}}));
""",
        )
        self.assertEqual(result["observe"], {
            "safetyPaused": False,
            "selectedExecutable": False,
            "copy": "数据正常，当前仅观察/无执行候选",
        })
        self.assertEqual(result["ready"], {
            "safetyPaused": False,
            "selectedExecutable": True,
            "copy": "执行候选已就绪，仍需手工确认",
        })

    def test_alert_history_and_notification_ids_are_bounded(self) -> None:
        result = run_swing_helpers(
            """
const alerts=Array.from({length:240},(_,index)=>({alert_id:String(index),currently_active:index===0,retracted:false,trading_date:`2026-08-${String(1+index%28).padStart(2,'0')}`}));
const visible=boundedAlerts(alerts,80);const notified=new Set(Array.from({length:400},(_,index)=>String(index))),active=new Set(['0']);
pruneRememberedAlertIds(notified,active,128);
console.log(JSON.stringify({visible:visible.length,keptActive:visible.some(item=>item.alert_id==='0'),remembered:notified.size,rememberedActive:notified.has('0')}));
""",
        )
        self.assertEqual(result, {"visible": 81, "keptActive": True, "remembered": 128, "rememberedActive": True})

    def test_requests_tabs_and_watchlist_status_have_safety_contracts(self) -> None:
        for fragment in (
            "new AbortController()", "GET_TIMEOUT_MS", "POST_TIMEOUT_MS",
            'role="tabpanel"', 'aria-controls="alerts-panel"',
            'aria-labelledby="alerts-tab"', "ArrowLeft", "ArrowRight",
            "event.key==='Home'", "event.key==='End'", "tabIndex",
            "监控列表已保存", "form-result success", "#swing-live-status.success",
        ):
            self.assertIn(fragment, SWING_PAGE)
        self.assertNotIn("setError('监控列表已保存')", SWING_PAGE)

    def test_backtest_panel_calls_real_read_only_endpoint_and_is_failure_isolated(self) -> None:
        for fragment in (
            'id="backtest-scope"', 'id="backtest-run"',
            'id="backtest-result"', '/api/swing/backtest?',
            "loadBacktest", "renderBacktestResult", "回测读取失败",
        ):
            self.assertIn(fragment, SWING_PAGE)
        self.assertNotIn("回测结果将在独立回测模块完成后显示", SWING_PAGE)
        load_function = SWING_PAGE.split("async function loadBacktest", 1)[1].split(
            "async function", 1,
        )[0]
        self.assertNotIn("markFailure", load_function)
        self.assertNotIn("loadSnapshot", load_function)

    def test_backtest_helpers_validate_scope_and_escape_server_content(self) -> None:
        result = run_swing_helpers(
            """
let bad='';try{backtestQuery('symbol','<img>')}catch(error){bad=error.message}
const query=backtestQuery('symbol','510300');
const empty=backtestMarkup({schema_version:1,scope:'symbol',symbol:'510300',status:'INSUFFICIENT_SAMPLE',reason:'<bad>',read_only:true},'沪深300ETF');
const metrics={cumulative_return:.12,annualized_return:.08,maximum_drawdown:.06,calmar:1.33,sharpe:1.1,win_rate:.55,average_profit:800,average_loss:-400,payoff_ratio:2,average_holding_days:18,utilization:.4,longest_losing_streak:2,fees:123.45,slippage:45.67,spread_cost:12.34,rejection_counts:{CASH:3,RISK:2}};
const summary={status:'OK',cumulative_return:.02,maximum_drawdown:.01,completed_round_trips:1,outperformance:.005};
const fold={fold_index:1,train_start_date:'2024-01-02',train_end_date:'2025-12-31',test_start_date:'2026-01-02',test_end_date:'2026-06-30',train_bar_count:504,test_bar_count:126,train:summary,test:summary};
const variants=[];for(const short of [18,20,22])for(const long of [55,60,65])for(const initial of [1.75,2,2.25])for(const trailing of [2.75,3,3.25])variants.push({parameters:{short_ma_days:short,long_ma_days:long,initial_stop_atr:initial,trailing_stop_atr:trailing},folds:[fold],stability:{fold_count:1,test_ok_count:1,mean_test_return:.02,positive_test_fold_count:1}});
const completePayload={schema_version:1,scope:'portfolio',status:'OK',reason:null,read_only:true,common_start_date:'2024-01-02',common_end_date:'2026-08-31',initial_cash:100000,cash:12000,ending_equity:112000,completed_round_trips:7,uncompleted_leg_count:1,outperformance:.03,round_trips:Array.from({length:7},()=>({net_pnl:1000})),metrics,baseline:{ending_equity:109000,cumulative_return:.09},walk_forward:{status:'OK',reason:null,train_days:504,test_days:126,step_days:126,selected_variant:null,variants}};
const complete=backtestMarkup(completePayload,'组合');
const shortWalk=cloneJson(completePayload);shortWalk.walk_forward={status:'INSUFFICIENT_SAMPLE',reason:'HISTORY_TOO_SHORT',train_days:504,test_days:126,step_days:126,selected_variant:null,variants:[]};const acceptedShortWalk=validateBacktestPayload(shortWalk,'portfolio',null)===shortWalk;
let incomplete='';try{validateBacktestPayload({schema_version:1,scope:'portfolio',status:'OK',read_only:true},'portfolio',null)}catch(error){incomplete=error.message}
const state=createPageState();state.snapshotRevision=1;state.snapshot={as_of_trading_date:'2026-08-31',strategy:'SWING_V1',daily_history_digest:'a'.repeat(64),items:[]};const key1=backtestCacheKey('portfolio',null,state);state.snapshotRevision=2;const key2=backtestCacheKey('portfolio',null,state);state.snapshot.strategy='SWING_V2';const key3=backtestCacheKey('portfolio',null,state);state.dailyRevisions.set('510300',3);const key4=backtestCacheKey('portfolio',null,state);state.dailyBySymbol.set('510300',new Map([['2026-08-31',{trading_date:'2026-08-31',open:4,high:4.1,low:3.9,close:4.05,adjusted_close:4.05,volume:100,amount:40500}]]));const key5=backtestCacheKey('portfolio',null,state);state.snapshot.daily_history_digest='b'.repeat(64);const key6=backtestCacheKey('portfolio',null,state);
console.log(JSON.stringify({bad,query,empty,complete,acceptedShortWalk,incomplete,key1,key2,key3,key4,key5,key6,current:backtestResponseCurrent(2,2,key3,key3),staleToken:backtestResponseCurrent(1,2,key3,key3),staleKey:backtestResponseCurrent(2,2,key1,key3)}));
""",
        )
        self.assertTrue(result["bad"])
        self.assertEqual(
            result["query"],
            "/api/swing/backtest?scope=symbol&symbol=510300",
        )
        self.assertIn("&lt;bad&gt;", result["empty"])
        self.assertNotIn("<bad>", result["empty"])
        self.assertIn("样本不足", result["empty"])
        self.assertTrue(result["acceptedShortWalk"])
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["key1"], result["key2"])
        self.assertNotEqual(result["key2"], result["key3"])
        self.assertEqual(result["key3"], result["key4"])
        self.assertNotEqual(result["key4"], result["key5"])
        self.assertNotEqual(result["key5"], result["key6"])
        self.assertTrue(result["current"])
        self.assertFalse(result["staleToken"])
        self.assertFalse(result["staleKey"])
        for text in (
            "12.00%", "6.00%", "55.00%", "112,000.00", "Calmar",
            "盈亏比", "平均持仓日", "最长连续亏损", "CASH 3",
            "全部 81 组参数（不择优）", "#81", "第 1 折",
        ):
            self.assertIn(text, result["complete"])

    def test_auxiliary_intraday_alerts_are_hidden_and_inert_until_safe_recovery(self) -> None:
        result = run_swing_helpers(
            """
const state=createPageState(); state.selectedSymbol='510300';
const snapshot=revision=>({revision,items:[{symbol:'510300',execution_status:'READY_TO_EXECUTE'}],health:{service:'OK',configuration:'OK',daily:'OK',strategy:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME'},errors:{}});
const intraday={alert_id:'a'.repeat(24),symbol:'510300',scope:'INTRADAY',state:'APPROACHING_ENTRY_ZONE',currently_active:true};
const bundle=revision=>({watch:{revision,items:[]},portfolio:{revision,projection:{}},alerts:{revision,items:[intraday]}});
applyReadModelBundle(state,snapshot(1),bundle(1),true);refreshExecutionPaused(state);
const healthy={visible:alertsForDisplay(state.auxiliary.alerts.items,state).length,trade:canActOnAlert(state,intraday.scope,intraday.state)};
state.failureSources.add('daily');refreshExecutionPaused(state);
const failed={visible:alertsForDisplay(state.auxiliary.alerts.items,state).length,trade:canActOnAlert(state,intraday.scope,intraday.state)};
applyReadModelBundle(state,snapshot(2),bundle(2),false);refreshExecutionPaused(state);
const failedNewBundle={visible:alertsForDisplay(state.auxiliary.alerts.items,state).length,stored:state.auxiliary.alerts.items.length};
state.failureSources.delete('daily');applyReadModelBundle(state,snapshot(3),bundle(3),false);refreshExecutionPaused(state);
const recovered={visible:alertsForDisplay(state.auxiliary.alerts.items,state).length,trade:canActOnAlert(state,intraday.scope,intraday.state)};
console.log(JSON.stringify({healthy,failed,failedNewBundle,recovered}));
""",
        )
        self.assertEqual(result, {
            "healthy": {"visible": 1, "trade": True},
            "failed": {"visible": 0, "trade": False},
            "failedNewBundle": {"visible": 0, "stored": 1},
            "recovered": {"visible": 1, "trade": True},
        })
        self.assertIn("data-alert-scope", SWING_PAGE)
        self.assertIn("data-alert-state", SWING_PAGE)
        self.assertIn("boundedAlerts(alertsForDisplay(source,state)", SWING_PAGE)
        self.assertIn("if(!canActOnAlert(state", SWING_PAGE)

    def test_installed_alert_view_retains_all_active_and_only_recent_history(self) -> None:
        result = run_swing_helpers(
            """
const state=createPageState(); state.selectedSymbol='510300';
const items=Array.from({length:10000},(_,index)=>({alert_id:String(index),currently_active:[2,5000,9999].includes(index),published_at:`2026-09-01T${String(index%24).padStart(2,'0')}:00:00+08:00`}));
items.push({...items[9999],currently_active:false});
const payload={revision:1,items:[{symbol:'510300',execution_status:'OBSERVE_ONLY'}],health:{service:'OK',configuration:'OK',daily:'OK',strategy:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME'},errors:{}};
const bundle={watch:{revision:1,items:[]},portfolio:{revision:1,projection:null},alerts:{revision:1,items}};
applyReadModelBundle(state,payload,bundle,true);
const retained=state.auxiliary.alerts.items,active=retained.filter(alert=>alert.currently_active).map(alert=>alert.alert_id).sort();
console.log(JSON.stringify({retained:retained.length,active,unique:new Set(retained.map(alert=>alert.alert_id)).size,requestedBound:80}));
""",
        )
        self.assertEqual(result, {
            "retained": 83,
            "active": ["2", "5000", "9999"],
            "unique": 83,
            "requestedBound": 80,
        })
        self.assertIn("/api/swing/alerts?limit=80", SWING_PAGE)


if __name__ == "__main__":
    unittest.main()
