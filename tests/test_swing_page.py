from __future__ import annotations

import json
import re
import subprocess
import unittest
from datetime import date, timedelta

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
    def test_valuation_markup_shows_index_metrics_and_missing_values(self) -> None:
        result = run_swing_helpers("""
const ready=valuationMarkup({index_code:'000300',index_name:'沪深300',status:'OK',level:'LOW',as_of:'2026-09-18',pe_ttm:13.2,pb:1.4,dividend_yield:3.1,pe_percentile_5y:22,pb_percentile_5y:20,roe_ttm:10,source:'Wind'});
const missing=valuationMarkup({index_code:'000690',index_name:'科创成长',status:'MISSING_VALUATION',level:'UNKNOWN',as_of:null,pe_ttm:null,pb:null,dividend_yield:null,source:null});
console.log(JSON.stringify({ready,missing}));
""")
        self.assertIn("指数估值（观察层）", result["ready"])
        self.assertIn("沪深300", result["ready"])
        self.assertIn("13.20", result["ready"])
        self.assertIn("3.10%", result["ready"])
        self.assertIn("低估", result["ready"])
        self.assertIn("科创成长", result["missing"])
        self.assertIn("MISSING_VALUATION", result["missing"])
        self.assertIn("—", result["missing"])

    def test_page_declares_valuation_observation_layer(self) -> None:
        for label in ("指数估值（观察层）", "估值分位", "估值对本次信号的影响"):
            self.assertIn(label, SWING_PAGE)

    def test_indicator_markup_explains_values_and_warmup_without_zero_fill(self) -> None:
        result = run_swing_helpers("""
const ready=indicatorMarkup({schema_version:1,status:'READY',as_of_trading_date:'2026-09-19',bar_count:130,macd:{dif:0.0123,dea:0.0045,histogram:0.0156},kdj:{k:61.2,d:55.1,j:73.4},rsi:{rsi14:58.7},moving_averages:{ma5:4.1,ma10:4.0,ma20:3.9,ma60:3.7}});
const warm=indicatorMarkup({schema_version:1,status:'WARMUP',reason:'INSUFFICIENT_COMPLETED_BARS',as_of_trading_date:null,bar_count:12,macd:{dif:null,dea:null,histogram:null},kdj:{k:null,d:null,j:null},rsi:{rsi14:null},moving_averages:{ma5:null,ma10:null,ma20:null,ma60:null}});
console.log(JSON.stringify({ready,warm}));
""")
        self.assertIn("MACD", result["ready"])
        self.assertIn("DIF", result["ready"])
        self.assertIn("KDJ", result["ready"])
        self.assertIn("RSI14", result["ready"])
        self.assertIn("0.012", result["ready"])
        self.assertIn("WARMUP", result["warm"])
        self.assertIn("12", result["warm"])
        self.assertIn("—", result["warm"])

    def test_indicator_markup_shows_hybrid_context_without_zero_fill(self) -> None:
        result = run_swing_helpers("""
const html=indicatorMarkup({schema_version:1,status:'READY',as_of_trading_date:'2026-09-19',bar_count:130,
macd:{dif:0.0123,dea:0.0045,histogram:0.0156},kdj:{k:61.2,d:55.1,j:73.4},rsi:{rsi14:58.7},
moving_averages:{ma5:4.1,ma10:4.0,ma20:3.9,ma60:3.7},bias20:{value:1.2},
bollinger:{middle:3.85,upper:4.1,lower:3.6,stddev:0.125},volume:{ma20:1000,ratio20:1.25,contraction:false},
weekly:{status:'READY',bar_count:26,close:4.0,ma10:3.9,ma20:3.7,as_of_trading_date:'2026-09-19'}});
console.log(JSON.stringify({html}));
""")
        for label in ("BIAS20", "布林", "20日量比", "MA10 3.900 / MA20 3.700", "1.20%", "1.25"):
            self.assertIn(label, result["html"])

    def test_page_declares_indicator_observation_layer(self) -> None:
        for label in ("技术指标（观察层）", "MACD", "KDJ", "RSI14", "指标状态"):
            self.assertIn(label, SWING_PAGE)

    def test_shadow_markup_separates_research_candidates_from_execution(self) -> None:
        result = run_swing_helpers("""
const html=shadowMarkup({status:'AVAILABLE',executable:false,blocked_reasons:['DATA_QUALITY_UNKNOWN'],variants:{V2_A:{state:'TECHNICAL_CANDIDATE',executable:false,blocked_reasons:['DATA_QUALITY_UNKNOWN']},V2_B:{state:'OBSERVE',executable:false,blocked_reasons:['TREND_NOT_CONFIRMED']},V2_C:{state:'RANGE_BLOCKED',executable:false,blocked_reasons:['RANGE_MODE']}}});
console.log(JSON.stringify({html}));
""")
        for label in ("影子策略（研究层）", "技术候选", "仅研究", "不可执行", "数据质量未知", "震荡阻断"):
            self.assertIn(label, result["html"])
        self.assertIn("不会生成可执行买入", result["html"])

    def test_shadow_markup_labels_hybrid_variant_and_reasons(self) -> None:
        result = run_swing_helpers("""
const html=shadowMarkup({status:'AVAILABLE',executable:false,blocked_reasons:['MOMENTUM_SCORE_BELOW_THRESHOLD'],variants:{HYBRID:{state:'OBSERVE',executable:false,blocked_reasons:['MOMENTUM_SCORE_BELOW_THRESHOLD'],evidence:{trend_score:3,momentum_score:1}}}});
console.log(JSON.stringify({html}));
""")
        for label in ("混合影子", "动量评分不足", "趋势评分"):
            self.assertIn(label, result["html"])

    def test_page_renders_shadow_layer_after_valuation(self) -> None:
        self.assertIn("shadowMarkup(item.shadow)", SWING_PAGE)
        self.assertIn("V2-A/B/C 只用于对照正式策略", SWING_PAGE)
        self.assertIn("shadow-summary", SWING_PAGE)

    def test_history_preparation_copy_separates_missing_history_from_activation(self) -> None:
        result = run_swing_helpers("""
const payload={available_symbols:[
 {symbol:'515180',can_enable:false,daily_count:0,minimum_daily_bars:70,enable_block_reason:'先独立补齐历史'},
 {symbol:'510300',can_enable:true,daily_count:240,minimum_daily_bars:70,latest_daily_date:'2026-09-03'}]};
console.log(JSON.stringify([historyPreparationCopy(payload,'515180'),historyPreparationCopy(payload,'510300')]));
""")
        self.assertIn("0/70", result[0]["label"])
        self.assertIn("独立补齐", result[0]["reason"])
        self.assertIn("240", result[1]["label"])
        self.assertIn("可启用", result[1]["label"])
        self.assertIn("2026-09-03", result[1]["reason"])

    def test_unready_detail_visibly_explains_preparation_not_waiting(self) -> None:
        render = re.search(r"function renderDetail\(\)\{([\s\S]*?)\n\}", SWING_PAGE).group(0)
        result = run_swing_helpers("""
const state={selectedSymbol:'515180',snapshot:{available_symbols:[
 {symbol:'515180',can_enable:false,daily_count:0,minimum_daily_bars:70,
 enable_block_reason:'先独立补齐历史；等待或刷新不会自动补齐'}]}};
const detailNode={innerHTML:''};
function itemFor(){return null}
""" + render + "\nrenderDetail();console.log(JSON.stringify(detailNode.innerHTML));")
        self.assertIn("0/70", result)
        self.assertIn("等待或刷新不会自动补齐", result)
        self.assertNotIn("启用并等待日线生产者", result)

    def test_page_has_strategy_first_layout_and_no_demo_numbers(self) -> None:
        self.assertIn("指数ETF波段监控", SWING_PAGE)
        self.assertIn('<nav aria-label="监控模式">', SWING_PAGE)
        self.assertIn('href="/"', SWING_PAGE)
        self.assertIn('href="/swing" aria-current="page"', SWING_PAGE)
        self.assertIn('href="/pr">PR估值</a>', SWING_PAGE)
        self.assertNotIn('id="portfolio-risk"', SWING_PAGE)
        self.assertIn('id="swing-watchlist"', SWING_PAGE)
        self.assertIn('id="swing-detail"', SWING_PAGE)
        self.assertIn('id="swing-errors" role="alert"', SWING_PAGE)
        self.assertIn('id="swing-live-status" aria-live="polite"', SWING_PAGE)
        for label in (
            "当前标的持仓", "报告成本", "波段标的", "MA20", "MA60",
            "阻断原因", "成交账本", "波段回测", "初始化波段账户",
        ):
            self.assertIn(label, SWING_PAGE)
        self.assertNotIn("100,000", SWING_PAGE)
        self.assertNotIn("4.700", SWING_PAGE)
        self.assertIn("仅监控，不自动交易", SWING_PAGE)

    def test_page_exposes_v11_shadow_action_workbench_without_auto_execution(self) -> None:
        for fragment in (
            "SWING_V11_SHADOW", "今日行动", "数据时点", "阻断原因",
            "可执行候选", "不支持自动下单", "人工确认",
        ):
            self.assertIn(fragment, SWING_PAGE)
        self.assertIn('id="v11-action-summary"', SWING_PAGE)
        self.assertIn("renderV11Summary", SWING_PAGE)

    def test_chart_accessibility_responsiveness_and_empty_states_are_explicit(self) -> None:
        for fragment in (
            "最近120个完成交易日", "复权收盘（最新交易日基准）", "MA20", "MA60", "计划买入区",
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
        self.assertIn("只可启停已核验的ETF", SWING_PAGE)
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
const payload={revision:1,items:[{symbol:'510300',execution_status:'READY_TO_EXECUTE'}],health:{service:'OK',configuration:'OK',calendar:'OK',daily:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME',minute_crosscheck:'NOT_RUN'},errors:{}};
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

    def test_chart_close_and_mas_share_latest_basis_across_dividend(self) -> None:
        # Synthetic history reproduces the 2026-03-12 close/MA20 relation:
        # adjusted 8.277 < 8.2953, while historical raw 8.426 > 8.2953.
        target_date = date(2026, 3, 12)
        prior_adjusted = (8.2953 * 20 - 8.277) / 19
        for latest_scale in (1.0, 2.5):
            with self.subTest(latest_scale=latest_scale):
                bars = []
                for index in range(61):
                    adjusted = prior_adjusted if index < 59 else 8.277 if index == 59 else 8.4
                    raw = adjusted + 0.149 if index < 60 else adjusted
                    bars.append({
                        "symbol": "510500",
                        "trading_date": (target_date + timedelta(days=index - 59)).isoformat(),
                        "close": raw * latest_scale,
                        "adjusted_close": adjusted,
                    })
                result = run_swing_helpers(
                    "console.log(JSON.stringify(chartSeries("
                    + json.dumps(list(reversed(bars))) + ")));",
                )
                self.assertEqual(result["scale"], latest_scale)
                points = result["points"]
                target = points[59]
                self.assertAlmostEqual(target["close"], 8.277 * latest_scale)
                self.assertAlmostEqual(target["rawClose"], 8.426 * latest_scale)
                self.assertAlmostEqual(target["ma20"], 8.2953 * latest_scale)
                for index, (point, bar) in enumerate(zip(points, bars)):
                    self.assertAlmostEqual(point["close"], bar["adjusted_close"] * latest_scale)
                    self.assertEqual(point["rawClose"], bar["close"])
                    for window in (20, 60):
                        if index + 1 < window:
                            self.assertIsNone(point[f"ma{window}"])
                            continue
                        adjusted_ma = sum(
                            item["adjusted_close"] for item in bars[index - window + 1:index + 1]
                        ) / window
                        self.assertAlmostEqual(point[f"ma{window}"], adjusted_ma * latest_scale)
                        if abs(bar["adjusted_close"] - adjusted_ma) > 1e-10:
                            self.assertEqual(
                                point["close"] > point[f"ma{window}"],
                                bar["adjusted_close"] > adjusted_ma,
                            )
                for window in (20, 60):
                    self.assertLess(target["close"], target[f"ma{window}"])
                    self.assertGreater(points[-1]["close"], points[-1][f"ma{window}"])
                self.assertAlmostEqual(points[-1]["close"], bars[-1]["close"])

    def test_chart_without_corporate_action_keeps_historical_prices(self) -> None:
        bars = [{
            "trading_date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            "close": (index + 100) * 2,
            "adjusted_close": index + 100,
        } for index in range(75)]
        result = run_swing_helpers(
            "console.log(JSON.stringify(chartSeries(" + json.dumps(bars) + ")));",
        )
        self.assertEqual(result["scale"], 2)
        self.assertEqual([point["close"] for point in result["points"]], [bar["close"] for bar in bars])
        self.assertEqual([point.get("rawClose") for point in result["points"]], [bar["close"] for bar in bars])

    def test_chart_tooltip_displays_raw_close_separately_from_plotted_close(self) -> None:
        tooltip_function = "function bindChartTooltip()" + SWING_PAGE.split(
            "function bindChartTooltip()", 1,
        )[1].split("function renderHealth()", 1)[0]
        result = run_swing_helpers(
            tooltip_function + """
const handlers={},tooltip={style:{}},cursor={style:{},setAttribute(){}},
  svg={addEventListener(name,handler){handlers[name]=handler},getBoundingClientRect(){return {left:0,top:0,width:860}}},
  hit={dataset:{left:'62',width:'780'}},nodes={'#swing-chart':svg,'#chart-hit':hit,'#chart-tooltip':tooltip,'#chart-cursor':cursor};
const document={querySelector:selector=>nodes[selector]},window={swingChartPoints:[{date:'2026-03-12',close:8.277,rawClose:8.426,ma20:8.2953,ma60:8.31}]};
const fmt=(value,digits)=>value.toFixed(digits);
bindChartTooltip();handlers.focus();
console.log(JSON.stringify(tooltip.textContent));
""",
        )
        self.assertIn("复权收盘 8.277", result)
        self.assertIn("当日原始收盘 8.426", result)
        self.assertIn("MA20 8.295", result)
        self.assertNotIn("原始价格轴", SWING_PAGE)
        self.assertIn("买入区和止损线保留真实委托价格", SWING_PAGE)

    def test_chart_order_price_overlays_are_not_rescaled(self) -> None:
        chart_function = "function chartHtml(symbol)" + SWING_PAGE.split(
            "function chartHtml(symbol)", 1,
        )[1].split("function planHtml(item)", 1)[0]
        result = run_swing_helpers(
            chart_function + """
const state=createPageState(),window={},fmt=(value,digits)=>value.toFixed(digits);
const bars=Array.from({length:60},(_,index)=>({trading_date:`2026-${String(1+Math.floor(index/28)).padStart(2,'0')}-${String(1+index%28).padStart(2,'0')}`,close:20,adjusted_close:10}));
state.dailyBySymbol.set('510500',new Map(bars.map(bar=>[bar.trading_date,bar])));
const itemFor=()=>({formal_decision:{planned_entry_low:19,planned_entry_high:21,evidence:{hard_stop_raw_mapped:18,trailing_stop_raw:19.5}}});
console.log(JSON.stringify(chartHtml('510500')));
""",
        )
        entry = re.search(r'<rect class="entry-zone"[^>]* y="([^"]+)"[^>]* height="([^"]+)"', result)
        self.assertIsNotNone(entry)
        # Plot range stays 18..21: the current price is 20, not 10 or 40.
        self.assertAlmostEqual(float(entry.group(1)), 18)
        self.assertAlmostEqual(float(entry.group(2)), 284 * 2 / 3)
        for css_class, price in (("hard-stop", 18), ("trailing-stop", 19.5)):
            line = re.search(rf'<line class="{css_class}"[^>]* y1="([^"]+)"', result)
            self.assertIsNotNone(line)
            self.assertAlmostEqual(float(line.group(1)), 18 + (21 - price) * 284 / 3)
        self.assertIn('<i class="close"></i>复权收盘（最新交易日基准）', result)
        self.assertNotIn("原始价格轴", result)

    def test_quality_panel_is_explicit_about_estimates_samples_and_account_risk(self) -> None:
        result = run_swing_helpers("""
const html=qualityMarkup({bar_count:756,start_date:'2023-07-24',end_date:'2026-09-02',
  sources:['<img src=x onerror=alert(1)>'],amount_quality:'ESTIMATED',
  adjustment_status:'RATIO_CHANGED_REQUIRES_REVIEW',crosscheck_status:'NOT_RECORDED',
  last_observed_at:'2026-09-02T18:21:17+08:00',minimum_daily_bars:70,
  minimum_backtest_bars:71,walk_forward_required_bars:630,walk_forward_fold_count:2,
  warnings:['AMOUNT_ESTIMATED']},
  {common_bar_count:460,walk_forward_required_bars:630,walk_forward_fold_count:0,aligned:true},
  {effective_risk_per_trade:0.001,risk_setting_source:'ACCOUNT'});
console.log(JSON.stringify({html,empty:qualityMarkup(null,null,{})}));
""")
        html = result["html"]
        for fragment in ("成交额", "估算", "未记录独立核验", "756", "460", "630", "单标的收益回测至少 71 日", "0.10%", "账户设置", "复权比例", "不代表策略有效"):
            self.assertIn(fragment, html)
        self.assertIn("&lt;img", html)
        self.assertNotIn("<img", html)
        self.assertIn("暂无质量记录", result["empty"])
        self.assertIn("qualityMarkup(item.data_quality,state.snapshot?.history_coverage,evidence)", SWING_PAGE)

    def test_quality_panel_exposes_source_and_date_warnings_in_chinese(self) -> None:
        result = run_swing_helpers("""
console.log(JSON.stringify(qualityMarkup({amount_quality:'PROVIDER_REPORTED',
  warnings:['MIXED_SOURCES','DUPLICATE_TRADING_DATE','<script>unknown</script>']},
  {warnings:['MISSING_SYMBOL_HISTORY','NON_ALIGNED_COMMON_HISTORY',
    'INSUFFICIENT_COMMON_WALK_FORWARD_SAMPLE']})));
""")
        for fragment in ("混合来源", "重复交易日", "缺少标的历史", "共同日期未对齐", "共同样本不足"):
            self.assertIn(fragment, result)
        self.assertNotIn("MIXED_SOURCES", result)
        self.assertIn("&lt;script&gt;unknown&lt;/script&gt;", result)
        self.assertNotIn("<script>", result)

    def test_status_and_execution_contract_are_understandable(self) -> None:
        result = run_swing_helpers("""
console.log(JSON.stringify({closed:statusText('CLOSED'),stale:statusText('STALE'),paused:statusText('PAUSED_MARKET_NOT_REALTIME'),denied:statusText('denied'),uninitialized:statusText('UNINITIALIZED'),
  blocked:statusText('trend_gate'),unsupported:backtestReasonCopy('CORPORATE_ACTION_UNSUPPORTED')}));
""")
        self.assertEqual(result["closed"], "已收盘")
        self.assertEqual(result["stale"], "行情已过期")
        self.assertEqual(result["paused"], "当前行情非实时，盘中提醒暂停")
        self.assertEqual(result["denied"], "已拒绝通知")
        self.assertEqual(result["uninitialized"], "账户未初始化")
        self.assertIn("趋势", result["blocked"])
        self.assertIn("复权", result["unsupported"])
        self.assertIn("次日开盘执行", SWING_PAGE)
        self.assertIn("不模拟盘中回到买入区的成交", SWING_PAGE)

    def test_unavailable_backtest_renders_the_reason_in_chinese(self) -> None:
        result = run_swing_helpers("""
console.log(JSON.stringify(backtestMarkup({schema_version:1,read_only:true,scope:'symbol',
  symbol:'510300',status:'DATA_UNAVAILABLE',reason:'CORPORATE_ACTION_UNSUPPORTED'},'沪深300')));
""")
        self.assertIn("复权", result)
        self.assertIn("暂停收益回测", result)

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
const payload={revision:2,items:[{symbol:'510300',execution_status:'READY_TO_EXECUTE',intraday_overlay:'APPROACHING_ENTRY_ZONE'}],active_alerts:[{alert_id:'a'.repeat(24),scope:'INTRADAY',state:'PREDEFINED_STOP_TOUCHED',currently_active:true}],alerts:[{alert_id:'b'.repeat(24),scope:'INTRADAY',state:'APPROACHING_ENTRY_ZONE',currently_active:true}],health:{service:'OK',configuration:'OK',calendar:'OK',daily:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME',minute_crosscheck:'NOT_RUN'},errors:{}};
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
const snapshot=revision=>({revision,items:[{symbol:'510300',execution_status:'OBSERVE_ONLY'}],health:{service:'OK',configuration:'OK',calendar:'OK',daily:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME',minute_crosscheck:'NOT_RUN'},errors:{}});
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
const base={revision:1,items:[{symbol:'510300',execution_status:'OBSERVE_ONLY'}],health:{service:'OK',configuration:'OK',calendar:'OK',daily:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME',minute_crosscheck:'NOT_RUN'},errors:{}};
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

    def test_real_api_health_without_strategy_keeps_observation_and_candidate_distinct(self) -> None:
        result = run_swing_helpers("""
const health={service:'OK',configuration:'OK',calendar:'OK',daily:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME',minute_crosscheck:'NOT_RUN'};
function inspect(patch={},errors={},execution='READY_TO_EXECUTE'){
  const state=createPageState(),payload={revision:1,items:[{symbol:'510300',formal_state:execution==='OBSERVE_ONLY'?'TREND_BLOCKED':'TRIAL_ENTRY_CANDIDATE',execution_status:execution}],health:{...health,...patch},errors};
  applySnapshotPayload(state,payload,true);
  return {safe:!state.safetyPaused,ready:state.selectedExecutable,copy:executionStatusCopy(state)};
}
const faults={};
for(const key of ['service','configuration','daily','portfolio','alerts','intraday']){
  faults[key]=[inspect({[key]:undefined}),inspect({[key]:'UNKNOWN'}),inspect({[key]:'BLOCKED'})];
}
console.log(JSON.stringify({health,observe:inspect({}, {},'OBSERVE_ONLY'),ready:inspect(),faults,calendarError:inspect({calendar:'BLOCKED'},{calendar:'calendar failed'})}));
""")
        self.assertNotIn("strategy", result["health"])
        self.assertEqual(result["observe"], {
            "safe": True, "ready": False,
            "copy": "数据正常，当前仅观察/无执行候选",
        })
        self.assertEqual(result["ready"], {
            "safe": True, "ready": True,
            "copy": "执行候选已就绪，仍需手工确认",
        })
        for field, faults in result["faults"].items():
            for fault in faults:
                with self.subTest(field=field, fault=fault):
                    self.assertFalse(fault["safe"])
                    self.assertFalse(fault["ready"])
        self.assertFalse(result["calendarError"]["safe"])
        self.assertFalse(result["calendarError"]["ready"])

    def test_uninitialized_account_has_one_reason_and_local_ledger_guidance(self) -> None:
        result = run_swing_helpers("""
const state=createPageState();
applySnapshotPayload(state,{revision:1,as_of_trading_date:'2026-09-02',items:[{symbol:'510300',formal_state:'TREND_BLOCKED',formal_decision:{blocked_reasons:['trend_gate']},execution_status:'OBSERVE_ONLY'}],health:{service:'OK',configuration:'OK',calendar:'OK',daily:'OK',portfolio:'UNINITIALIZED',alerts:'OK',intraday:'REALTIME',minute_crosscheck:'NOT_RUN'},errors:{portfolio:'portfolio account is not initialized'}},true);
const before=JSON.stringify(state.snapshot);
console.log(JSON.stringify({reasons:executionPauseReasons(state),status:executionStatusCopy(state),warning:executionWarningCopy(state),unchanged:before===JSON.stringify(state.snapshot)}));
""")
        self.assertEqual(result["reasons"], ["账户未初始化，仓位提醒暂停"])
        self.assertIn("账户未初始化，仓位提醒暂停", result["status"])
        for fragment in (
            "账户未初始化", "仓位提醒暂停", "成交账本 → 初始化波段账户",
            "本地手工记账", "不会自动初始化", "真实", "2026-09-02",
            "已撤销盘中临时提醒", "最后正式计划",
        ):
            self.assertIn(fragment, result["warning"])
        self.assertNotIn("实时/数据/组合门槛未全部恢复", result["status"])
        self.assertNotIn("portfolio account", result["warning"])
        self.assertTrue(result["unchanged"])

    def test_pause_reasons_cover_each_real_health_failure_without_raw_status(self) -> None:
        result = run_swing_helpers("""
const health={service:'OK',configuration:'OK',calendar:'OK',daily:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME',minute_crosscheck:'NOT_RUN'};
const cases=[['service','BLOCKED'],['configuration','BLOCKED'],['daily','BLOCKED'],['portfolio','BLOCKED'],['portfolio','UNKNOWN'],['alerts','BLOCKED'],['intraday','DELAYED'],['intraday','OUTAGE'],['intraday','UNAVAILABLE'],['intraday','LUNCH_BREAK'],['intraday','CLOSED'],['intraday','STALE']];
for(const key of ['service','configuration','daily','portfolio','alerts','intraday'])for(const value of [undefined,'<img src=x onerror=alert(1)>'])cases.push([key,value]);
console.log(JSON.stringify(cases.map(([key,value])=>{
  const state=createPageState();applySnapshotPayload(state,{revision:1,items:[{symbol:'510300',execution_status:'READY_TO_EXECUTE'}],health:{...health,[key]:value},errors:{}},true);
  return {key,value,reasons:executionPauseReasons(state),paused:state.safetyPaused,ready:state.selectedExecutable};
})));
""")
        fields = {
            "service": "服务", "configuration": "策略配置", "daily": "日线",
            "portfolio": "账本", "alerts": "提醒", "intraday": "行情",
        }
        market_copy = {
            "DELAYED": "延迟", "OUTAGE": "断流", "UNAVAILABLE": "暂无",
            "LUNCH_BREAK": "午休", "CLOSED": "收盘", "STALE": "过期",
        }
        for item in result:
            with self.subTest(key=item["key"], value=item.get("value")):
                self.assertTrue(item["paused"])
                self.assertFalse(item["ready"])
                self.assertEqual(len(item["reasons"]), 1)
                reason = item["reasons"][0]
                if item.get("value") in market_copy:
                    self.assertIn(market_copy[item["value"]], reason)
                else:
                    self.assertIn(fields[item["key"]], reason)
                self.assertNotIn("<img", reason)
                self.assertNotIn("UNKNOWN", reason)
                self.assertNotIn("BLOCKED", reason)

    def test_pause_reasons_include_deduplicated_backend_and_frontend_failures_safely(self) -> None:
        result = run_swing_helpers("""
const state=createPageState(),raw='<img src=x onerror=alert(1)> private-error';
state.failureSources=new Set(['snapshot','sse','daily',raw,'another_unknown_source']);
state.failureMessages=new Map([['snapshot',raw],['sse',raw],['daily',raw]]);
applySnapshotPayload(state,{revision:1,items:[{symbol:'510300',execution_status:'READY_TO_EXECUTE'}],health:{service:'OK',configuration:'OK',calendar:'BLOCKED',daily:'BLOCKED',portfolio:'UNINITIALIZED',alerts:'OK',intraday:'DELAYED',minute_crosscheck:'NOT_RUN'},errors:{portfolio:raw,daily:raw,calendar:raw,[raw]:raw,other:raw}},true);
const before=JSON.stringify({snapshot:state.snapshot,sources:[...state.failureSources],messages:[...state.failureMessages]});
console.log(JSON.stringify({reasons:executionPauseReasons(state),status:executionStatusCopy(state),warning:executionWarningCopy(state),unchanged:before===JSON.stringify({snapshot:state.snapshot,sources:[...state.failureSources],messages:[...state.failureMessages]})}));
""")
        reasons = result["reasons"]
        self.assertEqual(len(reasons), len(set(reasons)))
        self.assertEqual(sum("账户未初始化" in reason for reason in reasons), 1)
        for fragment in ("日线", "延迟", "交易日历", "快照", "事件", "页面", "后台"):
            self.assertTrue(any(fragment in reason for reason in reasons), fragment)
        self.assertTrue(any("同步失败" in reason for reason in reasons))
        for reason in reasons:
            self.assertIn(reason, result["status"])
            self.assertIn(reason, result["warning"])
        self.assertNotIn("private-error", json.dumps(result))
        self.assertNotIn("<img", json.dumps(result))
        self.assertTrue(result["unchanged"])

    def test_backend_errors_alone_still_pause_with_safe_component_reasons(self) -> None:
        result = run_swing_helpers("""
const health={service:'OK',configuration:'OK',calendar:'OK',daily:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME',minute_crosscheck:'NOT_RUN'};
console.log(JSON.stringify(['service','configuration','daily','portfolio','alerts','intraday','calendar','<script>unknown</script>'].map(key=>{
  const state=createPageState();applySnapshotPayload(state,{revision:1,items:[{symbol:'510300',execution_status:'READY_TO_EXECUTE'}],health,errors:{[key]:'raw-secret-error'}},true);
  return {key,reasons:executionPauseReasons(state),paused:state.safetyPaused,ready:state.selectedExecutable};
})));
""")
        for item in result:
            with self.subTest(key=item["key"]):
                self.assertTrue(item["paused"])
                self.assertFalse(item["ready"])
                self.assertEqual(len(item["reasons"]), 1)
                self.assertNotIn("raw-secret", item["reasons"][0])
                self.assertNotIn("<script>", item["reasons"][0])
                if item["key"] == "portfolio":
                    self.assertIn("账本", item["reasons"][0])
                    self.assertNotIn("未初始化", item["reasons"][0])

    def test_waiting_for_snapshot_is_syncing_and_health_observation_is_not_failure(self) -> None:
        result = run_swing_helpers("""
const state=createPageState(),waiting={reasons:executionPauseReasons(state),copy:executionWarningCopy(state)};
const payload={revision:1,items:[{symbol:'510300',formal_state:'TREND_BLOCKED',execution_status:'OBSERVE_ONLY'}],health:{service:'OK',configuration:'OK',calendar:'OK',daily:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME',minute_crosscheck:'NOT_RUN'},errors:{}};
applySnapshotPayload(state,payload,true);const observe={reasons:executionPauseReasons(state),copy:executionWarningCopy(state),paused:state.safetyPaused};
payload.revision=2;payload.items[0].execution_status='READY_TO_EXECUTE';applySnapshotPayload(state,payload,false);
console.log(JSON.stringify({waiting,observe,ready:{reasons:executionPauseReasons(state),copy:executionWarningCopy(state),status:executionStatusCopy(state)}}));
""")
        self.assertEqual(result["waiting"]["reasons"], ["波段状态同步中"])
        self.assertIn("同步中", result["waiting"]["copy"])
        self.assertNotIn("undefined", result["waiting"]["copy"])
        self.assertNotIn("null", result["waiting"]["copy"])
        self.assertEqual(result["observe"], {
            "reasons": [], "copy": "数据正常，当前仅观察/无执行候选。", "paused": False,
        })
        self.assertEqual(result["ready"], {
            "reasons": [], "copy": "", "status": "执行候选已就绪，仍需手工确认",
        })

    def test_pause_reason_recovery_keeps_formal_plan_and_requires_daily_recovery(self) -> None:
        result = run_swing_helpers("""
const state=createPageState(),formal={planned_entry_low:4,planned_shares:100,evidence:{}};
const payload=revision=>({revision,as_of_trading_date:'2026-09-02',items:[{symbol:'510300',formal_state:'TRIAL_ENTRY_CANDIDATE',formal_decision:formal,execution_status:'READY_TO_EXECUTE',intraday_overlay:'APPROACHING_ENTRY_ZONE'}],active_alerts:[{alert_id:'temporary',scope:'INTRADAY',state:'APPROACHING_ENTRY_ZONE'},{alert_id:'formal',scope:'FORMAL',state:'TRIAL_ENTRY_CANDIDATE'}],health:{service:'OK',configuration:'OK',calendar:'OK',daily:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME',minute_crosscheck:'NOT_RUN'},errors:{}});
applySnapshotPayload(state,payload(1),true);
state.failureSources.add('daily');state.failureSources.add('sse');revokeIntraday(state,'raw error');refreshExecutionPaused(state);
const failed={copy:executionWarningCopy(state),overlay:state.snapshot.items[0].intraday_overlay,alerts:state.snapshot.active_alerts.map(a=>a.alert_id),plan:state.lastFormalPlan.formal_decision};
eventStreamOpened(state);acceptSseSnapshot(state,payload(2),false);
const partial={paused:state.safetyPaused,reasons:executionPauseReasons(state),overlay:state.snapshot.items[0].intraday_overlay};
state.failureSources.delete('daily');acceptSseSnapshot(state,payload(3),false);
console.log(JSON.stringify({failed,partial,recovered:{paused:state.safetyPaused,ready:state.selectedExecutable,reasons:executionPauseReasons(state),overlay:state.snapshot.items[0].intraday_overlay,plan:state.lastFormalPlan.formal_decision}}));
""")
        self.assertIn("2026-09-02", result["failed"]["copy"])
        self.assertIn("最后正式计划", result["failed"]["copy"])
        self.assertIsNone(result["failed"]["overlay"])
        self.assertEqual(result["failed"]["alerts"], ["formal"])
        self.assertTrue(result["partial"]["paused"])
        self.assertIsNone(result["partial"]["overlay"])
        self.assertEqual(result["partial"]["reasons"], ["日线同步失败"])
        self.assertFalse(result["recovered"]["paused"])
        self.assertTrue(result["recovered"]["ready"])
        self.assertEqual(result["recovered"]["reasons"], [])
        self.assertEqual(result["recovered"]["overlay"], "APPROACHING_ENTRY_ZONE")
        self.assertEqual(result["failed"]["plan"], result["recovered"]["plan"])

    def test_rendered_warning_plan_and_status_share_safe_reason_copy(self) -> None:
        plan_function = "function planHtml(item)" + SWING_PAGE.split(
            "function planHtml(item)", 1,
        )[1].split("function renderDetail()", 1)[0]
        warning_function = "function renderWarning()" + SWING_PAGE.split(
            "function renderWarning()", 1,
        )[1].split("function renderAll()", 1)[0]
        result = run_swing_helpers(plan_function + warning_function + """
const state=createPageState(),warningNode={setAttribute(key,value){this[key]=value}},text=statusText,fmt=()=> '—',pct=()=> '—';
applySnapshotPayload(state,{revision:1,as_of_trading_date:'<img src=x onerror=alert(1)>',items:[{symbol:'510300',formal_decision:{},execution_status:'OBSERVE_ONLY'}],health:{service:'OK',configuration:'OK',calendar:'OK',daily:'OK',portfolio:'UNINITIALIZED',alerts:'OK',intraday:'REALTIME',minute_crosscheck:'NOT_RUN'},errors:{portfolio:'<script>secret</script>'}},true);
renderWarning();console.log(JSON.stringify({warning:warningNode.innerHTML,expected:escapeHtml(executionWarningCopy(state)),html:planHtml(state.snapshot.items[0]),status:executionStatusCopy(state),tone:warningNode.className,role:warningNode.role,hidden:warningNode.hidden}));
""")
        self.assertIn(result["expected"], result["warning"])
        self.assertIn("<details>", result["warning"])
        self.assertIn("notice-warning", result["tone"])
        self.assertEqual(result["role"], "status")
        self.assertFalse(result["hidden"])
        self.assertIn(result["status"], result["html"])
        self.assertIn("&lt;img", result["html"])
        self.assertNotIn("<img", result["html"])
        self.assertNotIn("<script>", result["html"])
        self.assertNotIn("secret", result["warning"])
        self.assertIn("warningNode.innerHTML=executionWarningMarkup(state)", SWING_PAGE)
        self.assertIn("${escapeHtml(notice.summary||executionStatusCopy(state))}</span>", SWING_PAGE)

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
const snapshot=revision=>({revision,items:[{symbol:'510300',execution_status:'READY_TO_EXECUTE'}],health:{service:'OK',configuration:'OK',calendar:'OK',daily:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME',minute_crosscheck:'NOT_RUN'},errors:{}});
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
const payload={revision:1,items:[{symbol:'510300',execution_status:'OBSERVE_ONLY'}],health:{service:'OK',configuration:'OK',calendar:'OK',daily:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME',minute_crosscheck:'NOT_RUN'},errors:{}};
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
