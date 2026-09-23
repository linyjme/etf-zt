import unittest

from etf_rotation.constants import (
    DELAYED_MAX_AGE_SECONDS, RANGE_CONFIRMATIONS, RANGE_WINDOW_MINUTES,
    REALTIME_MAX_AGE_SECONDS, TREND_CONFIRMATIONS,
)
from etf_rotation.t_page import PAGE
from tests.test_t_monitor import run_page_helpers


class MinutePageStatusTests(unittest.TestCase):
    def test_latest_0951_minute_is_realtime_at_095201(self) -> None:
        state = run_page_helpers("""
const item={health_status:'REALTIME',status:'OK',action:'BUY_CANDIDATE',
  timestamp:'2026-09-03T09:51:00+08:00',timestamp_basis:'MINUTE_START'};
console.log(JSON.stringify(marketPresentation(item,Date.parse('2026-09-03T09:52:01+08:00'))));
""")
        self.assertTrue(state["candidate"])
        self.assertFalse(state["stale"])
        self.assertEqual(state["dotClass"], "live")

    def test_completed_minute_boundaries_use_shared_python_constants(self) -> None:
        states = run_page_helpers("""
const item={health_status:'REALTIME',status:'OK',action:'SELL_CANDIDATE',
  timestamp:'2026-09-03T09:51:00+08:00',timestamp_basis:'MINUTE_START'};
const end=Date.parse('2026-09-03T09:52:00+08:00');
console.log(JSON.stringify([0,75000,75001,180000,180001].map(age=>marketPresentation(item,end+age))));
""")
        self.assertEqual([row["dotClass"] for row in states], ["live", "live", "delayed", "delayed", "outage"])
        self.assertEqual([row["candidate"] for row in states], [True, True, False, False, False])
        self.assertIn(f"REALTIME_MAX_AGE_SECONDS={REALTIME_MAX_AGE_SECONDS}", PAGE)
        self.assertIn(f"DELAYED_MAX_AGE_SECONDS={DELAYED_MAX_AGE_SECONDS}", PAGE)

    def test_legacy_timestamp_keeps_conservative_sixty_second_guard(self) -> None:
        states = run_page_helpers("""
const item={health_status:'REALTIME',status:'OK',action:'BUY_CANDIDATE',timestamp:'2026-09-03T09:51:00+08:00'};
const timestamp=Date.parse(item.timestamp);
console.log(JSON.stringify([0,60000,60001].map(age=>marketPresentation(item,timestamp+age))));
""")
        self.assertEqual([row["candidate"] for row in states], [True, True, False])
        self.assertEqual(states[-1]["dotClass"], "stale")

    def test_unknown_basis_malformed_and_future_minute_do_not_authorize_candidate(self) -> None:
        states = run_page_helpers("""
const item={health_status:'REALTIME',status:'OK',action:'BUY_CANDIDATE',
  timestamp:'2026-09-03T09:51:00+08:00',timestamp_basis:'MINUTE_START'};
const now=Date.parse('2026-09-03T09:52:01+08:00');
const rows=[{timestamp:'2026-09-03T09:52:00+08:00'}, {timestamp:'not-a-date'},
  {timestamp:null}, {timestamp_basis:'MINUTE_END'}, {timestamp_basis:null},
  {timestamp_basis:'',timestamp:'2026-09-03T09:52:00+08:00'}];
console.log(JSON.stringify(rows.map(extra=>marketPresentation({...item,...extra},now))));
""")
        for state in states:
            self.assertFalse(state["candidate"])
            self.assertTrue(state["unsafe"])
            self.assertNotEqual(state["dotClass"], "live")

    def test_collection_timestamps_cannot_renew_old_quote(self) -> None:
        state = run_page_helpers("""
const now='2026-09-03T09:57:00+08:00';
const item={health_status:'REALTIME',status:'OK',action:'BUY_CANDIDATE',
  timestamp:'2026-09-03T09:51:00+08:00',timestamp_basis:'MINUTE_START',
  collected_at:now,generated_at:now,observed_at:now};
console.log(JSON.stringify(marketPresentation(item,Date.parse(now))));
""")
        self.assertFalse(state["candidate"])
        self.assertEqual(state["dotClass"], "outage")

    def test_backend_unsafe_health_never_promoted_and_sessions_preserved(self) -> None:
        states = run_page_helpers("""
const item={health_status:'REALTIME',status:'OK',action:'BUY_CANDIDATE',
  timestamp:'2026-09-03T09:51:00+08:00',timestamp_basis:'MINUTE_START'};
const now=Date.parse('2026-09-03T09:52:01+08:00');
const health=['DELAYED','OUTAGE','UNKNOWN','UNRECOGNIZED','LUNCH_BREAK','CLOSED'];
console.log(JSON.stringify(health.map(health_status=>marketPresentation({...item,health_status},now))));
""")
        self.assertEqual([row["dotClass"] for row in states], ["delayed", "outage", "unknown", "unknown", "paused", "closed"])
        self.assertTrue(all(not row["candidate"] for row in states))

    def test_unknown_health_code_uses_safe_fallback_even_for_object_property_names(self) -> None:
        states = run_page_helpers("""
const item={health_status:'constructor',status:'OK',action:'BUY_CANDIDATE',
  timestamp:'2026-09-03T09:51:00+08:00',timestamp_basis:'MINUTE_START'};
const now=Date.parse('2026-09-03T09:52:01+08:00');
console.log(JSON.stringify(['constructor','__proto__'].map(health_status=>marketPresentation({...item,health_status},now))));
""")
        self.assertEqual([row.get("dotClass") for row in states], ["unknown", "unknown"])
        self.assertTrue(all(not row["candidate"] for row in states))

    def test_failed_feed_revokes_new_format_candidate_immediately(self) -> None:
        state = run_page_helpers("""
const item={health_status:'REALTIME',status:'OK',action:'BUY_CANDIDATE',
  timestamp:'2026-09-03T09:51:00+08:00',timestamp_basis:'MINUTE_START'};
console.log(JSON.stringify(marketPresentation(item,Date.parse('2026-09-03T09:52:01+08:00'),{ready:false,message:'行情连接中断'})));
""")
        self.assertFalse(state["candidate"])
        self.assertEqual(state["dotClass"], "outage")

    def test_delayed_payload_expires_without_promoting_backend_status(self) -> None:
        states = run_page_helpers("""
const item={health_status:'DELAYED',status:'OK',action:'BUY_CANDIDATE',
  timestamp:'2026-09-03T09:51:00+08:00',timestamp_basis:'MINUTE_START'};
const end=Date.parse('2026-09-03T09:52:00+08:00');
console.log(JSON.stringify([1000,180001].map(age=>marketPresentation(item,end+age))));
""")
        self.assertEqual([row["dotClass"] for row in states], ["delayed", "outage"])
        self.assertTrue(all(not row["candidate"] for row in states))


class RegimePageExplanationTests(unittest.TestCase):
    def presentation(self, item: str) -> dict[str, object]:
        result = run_page_helpers(f"""
const item={item};
console.log(JSON.stringify(typeof regimePresentation==='function'?regimePresentation(item):null));
""")
        self.assertIsNotNone(result, "missing regime explanation helper")
        return result

    def test_short_sample_count_is_actual_and_heading_describes_pattern(self) -> None:
        view = self.presentation("{regime_state:'UNCERTAIN',regime_sample_count:7,regime_reasons:['INSUFFICIENT_SAMPLES'],range_confirmation_count:0,trend_confirmation_count:0}")
        self.assertEqual(view["heading"], "形态未确认")
        self.assertIn("样本不足", view["summary"])
        self.assertEqual(view["sampleProgress"], f"7 / {RANGE_WINDOW_MINUTES}")
        self.assertEqual(view["rangeProgress"], f"0 / {RANGE_CONFIRMATIONS}")
        self.assertEqual(view["trendProgress"], f"0 / {TREND_CONFIRMATIONS}")

    def test_pending_and_mixed_conditions_are_explained_differently(self) -> None:
        pending = self.presentation("{regime_state:'UNCERTAIN',regime_sample_count:20,regime_reasons:['RANGE_CONFIRMATION_PENDING'],range_confirmation_count:2,trend_confirmation_count:0}")
        mixed = self.presentation("{regime_state:'UNCERTAIN',regime_sample_count:20,regime_reasons:['RANGE_VWAP_CROSSINGS_INSUFFICIENT','TREND_PATH_NOT_EFFICIENT'],range_confirmation_count:0,trend_confirmation_count:0}")
        self.assertIn("等待连续确认", pending["summary"])
        self.assertEqual(pending["rangeProgress"], "2 / 3")
        self.assertIn("震荡与趋势条件均未满足", mixed["summary"])
        self.assertIn("穿越均价线次数不足", mixed["reasonsText"])
        self.assertIn("趋势方向性不足", mixed["reasonsText"])

    def test_wait_has_readable_three_grid_and_cost_reasons(self) -> None:
        view = self.presentation("{action:'WAIT',regime_state:'RANGE',regime_sample_count:20,regime_reasons:['RANGE_CONFIRMED'],blocked_reasons:['DEVIATION_BELOW_3_GRIDS','COST_NOT_COVERED'],range_confirmation_count:3,trend_confirmation_count:0}")
        self.assertEqual(view["heading"], "震荡日")
        self.assertIn("偏离均价不足三格", view["blockedText"])
        self.assertIn("预期毛边际不足以覆盖双边成本", view["blockedText"])

    def test_unknown_reason_does_not_invent_mixed_conditions(self) -> None:
        view = self.presentation("{regime_state:'UNCERTAIN',regime_sample_count:20,regime_reasons:['NEW_REASON_CODE']}")
        self.assertEqual(view["summary"], "形态未确认，暂停做T")
        self.assertIn("未识别原因（NEW_REASON_CODE）", view["reasonsText"])

    def test_explanation_markup_escapes_dynamic_values_and_unknown_reason_codes(self) -> None:
        result = run_page_helpers("""
const item={regime_state:'<img src=x onerror=alert(1)>',regime_sample_count:'<script>',
  regime_label:'<b>label</b>',regime_reasons:['<img src=x onerror=alert(1)>'],
  blocked_reasons:['<svg onload=alert(1)>'],range_confirmation_count:null};
console.log(JSON.stringify(typeof regimeMarkup==='function'?regimeMarkup(item):null));
""")
        self.assertIsNotNone(result, "missing safe regime markup helper")
        self.assertNotIn("<img", result)
        self.assertNotIn("<svg", result)
        self.assertNotIn("<script", result)
        self.assertNotIn("<b>label</b>", result)
        self.assertIn("未识别原因", result)
        self.assertIn("&lt;img", result)
        self.assertIn("&lt;svg", result)

    def test_render_uses_explanations_and_runtime_expiry_without_network_reload(self) -> None:
        self.assertIn("regimeMarkup(item)", PAGE)
        self.assertIn("setInterval(refreshMarketState,1000)", PAGE)
        self.assertIn("function refreshMarketState()", PAGE)
        refresh = PAGE.split("function refreshMarketState()", 1)[1].split("function render(data)", 1)[0]
        self.assertNotIn("fetch(", refresh)
        self.assertNotIn("render(latestData)", refresh)

    def test_runtime_timer_revokes_existing_candidate_and_navigation_highlight(self) -> None:
        self.assertIn("function refreshMarketState()", PAGE, "missing local quote-expiry timer")
        refresh = "function refreshMarketState()" + PAGE.split("function refreshMarketState()", 1)[1].split("function render(data)", 1)[0]
        result = run_page_helpers(refresh + """
const latestData={items:[{symbol:'510300',health_status:'REALTIME',status:'OK',action:'BUY_CANDIDATE',
  timestamp:'2026-09-03T09:51:00+08:00',timestamp_basis:'MINUTE_START'}]};
const selectedSymbol='510300',showMissing={checked:true},feedState={ready:true};
let removed=false,navCandidate=true,connection=null,timeStale=false;
const signal={textContent:'做T候选'},alert={remove(){removed=true}},marketTime={classList:{toggle(name,value){timeStale=value}}},health={textContent:'实时监控中'};
const detail={querySelector(selector){return selector==='.signal'?signal:selector==='.candidate-alert'?alert:selector==='.market-time'?marketTime:selector==='#market-health'?health:null}};
function renderNav(items){navCandidate=marketPresentation(items[0],Date.now(),feedState).candidate}
function updateConnection(state){connection=state.dotClass}
Date.now=()=>Date.parse('2026-09-03T09:53:15.001+08:00');
refreshMarketState();
console.log(JSON.stringify({removed,navCandidate,connection,timeStale,label:signal.textContent,health:health.textContent}));
""")
        self.assertEqual(result, {"removed": True, "navCandidate": False, "connection": "delayed", "timeStale": True, "label": "偏离观察", "health": "行情延迟 · 行情延迟，候选提醒已撤销"})


class MarketRecoveryLatchTests(unittest.TestCase):
    def evaluate(self, body: str) -> dict[str, object]:
        result = run_page_helpers("""
const future={symbol:'510300',health_status:'REALTIME',status:'OK',action:'BUY_CANDIDATE',
  timestamp:'2026-09-03T09:52:00+08:00',timestamp_basis:'MINUTE_START'};
const at=time=>Date.parse('2026-09-03T'+time+'+08:00');
const guarded=typeof currentMarketPresentation==='function'?currentMarketPresentation:marketPresentation;
const accept=(item,now,revision=1)=>typeof acceptMarketSummary==='function'?acceptMarketSummary([item],revision,now):undefined;
const confirm=(symbol,generation,points,revision,now)=>typeof confirmMarketSummary==='function'?confirmMarketSummary(symbol,generation,points,revision,now):false;
const generation=()=>typeof marketSafety==='undefined'?0:marketSafety.get('510300').generation;
const point={timestamp:future.timestamp,is_complete:true};
""" + body)
        return result

    def test_rejected_future_minute_cannot_become_candidate_when_minute_completes(self) -> None:
        result = self.evaluate("""
accept(future,at('09:52:59'));
const before=guarded(future,at('09:52:59'));
const after=guarded(future,at('09:53:00'));
const rerender=guarded(future,at('09:53:01'));
console.log(JSON.stringify({before,after,rerender}));
""")
        for row in result.values():
            self.assertFalse(row["candidate"])
            self.assertNotEqual(row["dotClass"], "live")

    def test_expired_candidate_stays_revoked_after_clock_rollback(self) -> None:
        result = self.evaluate("""
accept(future,at('09:53:00'));
const fresh=guarded(future,at('09:53:00'));
const delayed=guarded(future,at('09:54:15.001'));
const outage=guarded(future,at('09:56:00.001'));
const rollback=guarded(future,at('09:53:01'));
console.log(JSON.stringify({fresh,delayed,outage,rollback}));
""")
        self.assertTrue(result["fresh"]["candidate"])
        self.assertEqual(result["delayed"]["dotClass"], "delayed")
        self.assertEqual(result["outage"]["dotClass"], "outage")
        self.assertEqual(result["rollback"]["dotClass"], "outage")
        self.assertFalse(result["rollback"]["candidate"])

    def test_only_new_valid_summary_and_matching_completed_minute_restore_candidate(self) -> None:
        result = self.evaluate("""
accept(future,at('09:52:59'));
guarded(future,at('09:52:59'));
const rejectedGeneration=generation();
const oldQuotes=confirm(future.symbol,rejectedGeneration,[point],1,at('09:53:00'));
const noSummary=guarded(future,at('09:53:00'));
const fresh={...future};accept(fresh,at('09:53:01'),2);
const currentGeneration=generation();
const onlySummary=guarded(fresh,at('09:53:01'));
const oldRequest=confirm(fresh.symbol,rejectedGeneration,[point],2,at('09:53:01'));
const wrongMinute=confirm(fresh.symbol,currentGeneration,[{...point,timestamp:'2026-09-03T09:51:00+08:00'}],2,at('09:53:01'));
const incomplete=confirm(fresh.symbol,currentGeneration,[{...point,is_complete:false}],2,at('09:53:01'));
const oldRevision=confirm(fresh.symbol,currentGeneration,[point],1,at('09:53:01'));
const stillBlocked=guarded(fresh,at('09:53:01'));
const recovered=confirm(fresh.symbol,currentGeneration,[point],2,at('09:53:02'));
const ready=guarded(fresh,at('09:53:02'));
console.log(JSON.stringify({oldQuotes,noSummary,onlySummary,oldRequest,wrongMinute,incomplete,oldRevision,stillBlocked,recovered,ready}));
""")
        for key in ("oldQuotes", "oldRequest", "wrongMinute", "incomplete", "oldRevision"):
            self.assertFalse(result[key], key)
        for key in ("noSummary", "onlySummary", "stillBlocked"):
            self.assertFalse(result[key]["candidate"], key)
            self.assertNotEqual(result[key]["dotClass"], "live", key)
        self.assertTrue(result["recovered"])
        self.assertTrue(result["ready"]["candidate"])

    def test_summary_which_was_invalid_at_receipt_cannot_recover_by_delayed_quotes(self) -> None:
        result = self.evaluate("""
accept(future,at('09:52:59'));
const rejected=guarded(future,at('09:52:59'));
const confirmed=confirm(future.symbol,generation(),[point],1,at('09:53:01'));
const after=guarded(future,at('09:53:01'));
console.log(JSON.stringify({rejected,confirmed,after}));
""")
        self.assertFalse(result["confirmed"])
        self.assertFalse(result["after"]["candidate"])

    def test_candidate_that_expires_during_recovery_needs_another_summary(self) -> None:
        result = self.evaluate("""
accept(future,at('09:52:59'));guarded(future,at('09:52:59'));
const fresh={...future};accept(fresh,at('09:53:01'),2);
const expired=confirm(fresh.symbol,generation(),[point],2,at('09:54:15.001'));
const rollback=confirm(fresh.symbol,generation(),[point],2,at('09:53:02'));
const after=guarded(fresh,at('09:53:02'));
console.log(JSON.stringify({expired,rollback,after}));
""")
        self.assertFalse(result["expired"])
        self.assertFalse(result["rollback"])
        self.assertFalse(result["after"]["candidate"])

    def test_page_render_timer_and_quote_recovery_use_same_guard(self) -> None:
        self.assertIn("currentMarketPresentation(item,Date.now(),feedState)", PAGE)
        self.assertIn("currentMarketPresentation(item,refreshedAt.getTime(),feedState)", PAGE)
        self.assertIn("acceptMarketSummary(data.items||[],data.revision)", PAGE)
        self.assertIn("confirmMarketSummary(symbol,generation,points,quoteRevisions.get(symbol))", PAGE)

    def test_initial_feed_not_ready_does_not_permanently_lock_valid_items(self) -> None:
        result = self.evaluate("""
accept(future,at('09:53:00'));
const connecting=guarded(future,at('09:53:00'),{ready:false,message:'正在连接'});
const ready=guarded(future,at('09:53:01'),{ready:true});
console.log(JSON.stringify({connecting,ready}));
""")
        self.assertFalse(result["connecting"]["candidate"])
        self.assertTrue(result["ready"]["candidate"])

    def test_scheduled_closure_is_not_treated_as_a_quote_fault(self) -> None:
        result = self.evaluate("""
const paused={...future,health_status:'LUNCH_BREAK'};
accept(paused,at('12:00:00'));
const lunch=guarded(paused,at('12:00:00'));
const fresh={...future,timestamp:'2026-09-03T13:00:00+08:00'};
accept(fresh,at('13:01:01'),2);
const reopened=guarded(fresh,at('13:01:01'));
console.log(JSON.stringify({lunch,reopened}));
""")
        self.assertEqual(result["lunch"]["dotClass"], "paused")
        self.assertFalse(result["lunch"]["candidate"])
        self.assertTrue(result["reopened"]["candidate"])

    def test_feed_failure_requires_new_summary_before_quotes_can_restore_candidate(self) -> None:
        result = self.evaluate("""
accept(future,at('09:53:00'));
guarded(future,at('09:53:00'));
if(typeof rejectMarketFeed==='function')rejectMarketFeed([future],'连接失败',at('09:53:01'));
const failed=guarded(future,at('09:53:01'),{ready:false,message:'连接失败'});
const oldQuotes=confirm(future.symbol,generation(),[point],1,at('09:53:02'));
const noSummary=guarded(future,at('09:53:02'),{ready:true});
const fresh={...future};accept(fresh,at('09:53:03'),2);
const recovered=confirm(fresh.symbol,generation(),[point],2,at('09:53:04'));
const ready=guarded(fresh,at('09:53:04'),{ready:true});
console.log(JSON.stringify({failed,oldQuotes,noSummary,recovered,ready}));
""")
        self.assertFalse(result["failed"]["candidate"])
        self.assertFalse(result["oldQuotes"])
        self.assertFalse(result["noSummary"]["candidate"])
        self.assertTrue(result["recovered"])
        self.assertTrue(result["ready"]["candidate"])

    def test_unchanged_timer_navigation_does_not_replace_focused_buttons(self) -> None:
        nav = "function renderNav(items)" + PAGE.split("function renderNav(items)", 1)[1].split("function refreshMarketState()", 1)[0]
        result = self.evaluate(nav + """
let writes=0;const watchCount={textContent:''},watchList={set innerHTML(value){writes++}},selectedSymbol='510300',feedState={ready:true};
Date.now=()=>at('09:53:00');accept(future,Date.now());
renderNav([future]);renderNav([future]);
console.log(JSON.stringify({writes}));
""")
        self.assertEqual(result["writes"], 1)


if __name__ == "__main__":
    unittest.main()
