from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

from etf_rotation.swing_alerts import AlertInput, SwingAlertStore
from etf_rotation.swing_page import SWING_PAGE
from etf_rotation.swing_portfolio import TradeInput
from etf_rotation.swing_service import SwingServiceError
from tests import test_swing_service as service_fixtures
from tests.test_swing_page import run_swing_helpers


def snapshot_fixture() -> dict[str, object]:
    return {
        "schema_version": 1,
        "snapshot_id": "synthetic-import",
        "recorded_at": "2026-09-01T14:00:00+08:00",
        "reporting_date": "2026-09-01",
        "source": "synthetic test snapshot",
        "positions_as_of": None,
        "account_as_of": None,
        "notes": ["Synthetic values; no historical trade or stop inferred."],
        "account": {
            "reported_total_assets": 1000.0,
            "reported_securities_value": 800.0,
            "available_cash": 150.0,
            "other_assets": 50.0,
            "original_capital": 1200.0,
            "additional_loss_budget": 100.0,
        },
        "positions": [
            {
                "symbol": "510300", "name": "Synthetic ETF", "asset_type": "ETF",
                "management_mode": "OBSERVE", "shares": 100,
                "sellable_shares": 0, "average_cost": 1.234,
                "reported_market_value": 100.0, "reported_holding_pnl": -23.4,
                "entry_date": None, "stop_loss": None,
            },
            {
                "symbol": "600036", "name": "Synthetic stock", "asset_type": "STOCK",
                "management_mode": "LONG_TERM_ONLY", "shares": 100,
                "sellable_shares": None, "average_cost": 2.345,
                "reported_market_value": None, "reported_holding_pnl": None,
                "entry_date": None, "stop_loss": None,
            },
        ],
    }


def snapshot_view() -> dict[str, object]:
    return {
        "status": "SNAPSHOT_ONLY", "snapshot": snapshot_fixture(), "error": None,
        "read_only": True, "strategy_ready": False,
    }


class SwingHoldingsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = service_fixtures.SwingServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = self.fixture.paths.portfolio_snapshot.with_name("holdings_snapshot.json")

    def write_snapshot(self) -> None:
        self.path.write_text(json.dumps(snapshot_fixture()), encoding="utf-8")

    def test_bootstrap_publishes_snapshot_without_initializing_ledger(self) -> None:
        self.fixture.paths = replace(
            self.fixture.paths,
            trades=self.fixture.paths.trades.with_name("unused-trades.jsonl"),
            portfolio_snapshot=self.fixture.paths.portfolio_snapshot.with_name("unused-portfolio.json"),
        )
        self.write_snapshot()
        service = self.fixture.make_service()
        service.refresh_intraday()
        state = service.snapshot()
        self.assertIn("holdings_snapshot", state)
        self.assertEqual(state["holdings_snapshot"]["status"], "SNAPSHOT_ONLY")
        self.assertEqual(state["health"]["portfolio"], "SNAPSHOT_ONLY")
        self.assertIsNone(state["portfolio"])
        self.assertFalse(self.fixture.paths.trades.exists())
        self.assertFalse(self.fixture.paths.portfolio_snapshot.exists())
        self.assertEqual(service.portfolio()["revision"], state["revision"])
        self.assertEqual(service.portfolio()["holdings_snapshot"], state["holdings_snapshot"])
        state["holdings_snapshot"]["snapshot"]["positions"].clear()
        self.assertEqual(len(service.snapshot()["holdings_snapshot"]["snapshot"]["positions"]), 2)

    def test_snapshot_suppresses_existing_ledger_actions_without_losing_holding_facts(self) -> None:
        self.write_snapshot()
        before = self.fixture.paths.trades.read_bytes()
        service = self.fixture.make_service()
        service.refresh_intraday()
        state = service.snapshot()
        self.assertIsNone(state["portfolio"])
        self.assertEqual(before, self.fixture.paths.trades.read_bytes())
        self.assertTrue(service._has_position("510300"))
        self.assertTrue(service._has_position("600036"))
        self.assertFalse(state["active_alerts"])
        for item in state["items"]:
            self.assertNotEqual(item["execution_status"], "READY_TO_EXECUTE")
            self.assertEqual(item["execution_status"], "PAUSED_HOLDINGS_SNAPSHOT")
            self.assertIsNone(item["intraday_overlay"])
            self.assertEqual(item["formal_decision"]["planned_shares"], 0)
            self.assertIsNone(item["formal_decision"]["planned_stop"])
            self.assertIsNone(item["formal_decision"]["valid_for_trading_date"])
            self.assertTrue(item["formal_decision"]["evidence"]["snapshot_has_position"])
            self.assertIn("holdings_snapshot_only", item["formal_decision"]["blocked_reasons"])
            self.assertIn("ma20_raw", item["formal_decision"]["evidence"])
            self.assertNotIn(item["formal_state"], {
                "ADD_CANDIDATE", "REDUCE_CANDIDATE", "EXIT_CANDIDATE",
            })
        self.assertEqual(state["items"][0]["formal_state"], "TRIAL_ENTRY_OBSERVE")
        self.assertIsNotNone(state["items"][0]["formal_decision"]["planned_entry_low"])
        self.assertIsNotNone(state["items"][0]["formal_decision"]["planned_entry_high"])

    def test_live_snapshot_and_invalid_snapshot_block_every_ledger_write(self) -> None:
        service = self.fixture.make_service()
        trade = TradeInput("510300", "BUY", 100, 1.0, 0.0,
                           datetime(2026, 9, 1, 14, tzinfo=service_fixtures.SHANGHAI))
        for content in (json.dumps(snapshot_fixture()), "{broken"):
            self.path.write_text(content, encoding="utf-8")
            before = self.fixture.paths.trades.read_bytes()
            operations = (
                lambda: service.initialize_portfolio("not allowed", 1000.0, "blocked-init"),
                lambda: service.record_trade(trade, "blocked-buy"),
                lambda: service.reverse_trade("12345678-1234-4234-8234-123456789abc", "blocked-reverse"),
            )
            for operation in operations:
                with self.subTest(content=content[:10], operation=operation):
                    with self.assertRaisesRegex(SwingServiceError, "快照|snapshot"):
                        operation()
            self.assertEqual(before, self.fixture.paths.trades.read_bytes())

    def test_producer_detects_added_snapshot_and_gets_remain_published_reads(self) -> None:
        service = self.fixture.make_service()
        service.refresh_intraday()
        before = service.snapshot()
        self.write_snapshot()
        self.assertEqual(service.snapshot(), before)
        service.refresh_intraday()
        after = service.snapshot()
        self.assertIn("holdings_snapshot", after)
        self.assertEqual(after["holdings_snapshot"]["status"], "SNAPSHOT_ONLY")
        self.assertGreater(after["revision"], before["revision"])
        self.assertEqual(after["revision"], service.portfolio()["revision"])
        self.assertFalse(after["active_alerts"])

    def test_absent_snapshot_keeps_existing_account_behavior(self) -> None:
        service = self.fixture.make_service()
        service.refresh_intraday()
        state = service.snapshot()
        self.assertEqual(state.get("holdings_snapshot", {}).get("status"), "ABSENT")
        self.assertEqual(state["health"]["portfolio"], "OK")
        self.assertEqual(state["items"][0]["execution_status"], "READY_TO_EXECUTE")

    def test_snapshot_does_not_invent_cash_caps_or_sized_orders(self) -> None:
        self.write_snapshot()
        decision = self.fixture.make_service().snapshot()["items"][0]["formal_decision"]
        for reason in ("cash_cap", "single_symbol_cap", "trade_risk_cap", "minimum_lot"):
            self.assertNotIn(reason, decision["blocked_reasons"])
        self.assertEqual(decision["planned_shares"], 0)
        self.assertIsNone(decision["evidence"]["position_risk_amount"])
        self.assertIsNone(decision["valid_for_trading_date"])

    def test_daily_producer_sees_report_even_when_no_daily_refresh_is_due(self) -> None:
        service = self.fixture.make_service()
        self.write_snapshot()
        service.refresh_once(datetime(2026, 9, 1, 14, tzinfo=service_fixtures.SHANGHAI))
        self.assertEqual(service.snapshot()["holdings_snapshot"]["status"], "SNAPSHOT_ONLY")
        self.assertIsNone(service.portfolio()["projection"])

    def test_formal_alert_history_is_retained_but_never_actionable(self) -> None:
        service = self.fixture.make_service()
        decision = service._formal["510300"]
        SwingAlertStore(self.fixture.paths.alerts).publish_formal(AlertInput(
            trading_date=decision.as_of_trading_date, symbol="510300",
            state=decision.state.value, strategy_version=decision.strategy_version,
            level="YELLOW", label="synthetic old candidate", evidence={},
        ))
        self.write_snapshot()
        service.refresh_intraday()
        self.assertFalse(service.snapshot()["active_alerts"])
        history = service.alerts(include_retracted=True)["items"]
        self.assertTrue(history)
        for alert in history:
            self.assertFalse(alert["active_notification"])
            self.assertFalse(alert["currently_active"])

    def test_pending_identity_can_validate_holdings_but_not_enable_strategy(self) -> None:
        report = snapshot_fixture()
        report["positions"][0]["symbol"] = "513999"
        pending = {"symbol": "513999", "name": "Synthetic pending ETF"}
        self.path.write_text(json.dumps(report), encoding="utf-8")
        with patch("etf_rotation.swing_service.load_pending_etfs", return_value={"513999": pending}):
            service = self.fixture.make_service()
            state = service.snapshot()
        self.assertEqual(state["holdings_snapshot"]["status"], "SNAPSHOT_ONLY")
        self.assertEqual(state["pending_instruments"], [pending])
        self.assertNotIn("513999", service._metadata)
        self.assertNotIn("513999", {item["symbol"] for item in state["items"]})
        with self.assertRaisesRegex(SwingServiceError, "metadata|configured|unknown|verified|verified metadata"):
            service.update_watchlist("513999", True)

    def test_missing_or_short_history_cannot_be_newly_enabled(self) -> None:
        self.fixture.paths.metadata.write_text(json.dumps(
            service_fixtures.metadata_fixture(("510300", "159915")),
        ), encoding="utf-8")
        service = self.fixture.make_service()
        before = self.fixture.paths.watchlist.read_bytes()
        for count in (0, 10):
            service._history = self.fixture.initial_bars + tuple(
                replace(bar, symbol="159915") for bar in self.fixture.initial_bars[:count]
            )
            with self.subTest(count=count):
                with self.assertRaisesRegex(SwingServiceError, "日线"):
                    service.update_watchlist("159915", True)
                self.assertEqual(self.fixture.paths.watchlist.read_bytes(), before)
        availability = {item["symbol"]: item for item in service.snapshot()["available_symbols"]}
        self.assertFalse(availability["159915"]["can_enable"])
        self.assertIn("日线", availability["159915"]["enable_block_reason"])

    def test_already_enabled_empty_history_remains_an_idempotent_toggle(self) -> None:
        service = self.fixture.make_service()
        service._history = ()
        before = self.fixture.paths.watchlist.read_bytes()
        service.update_watchlist("510300", True)
        self.assertEqual(self.fixture.paths.watchlist.read_bytes(), before)

    def test_failed_clock_still_loads_report_and_withdraws_old_candidates(self) -> None:
        for method in ("refresh_once", "refresh_intraday"):
            with self.subTest(method=method):
                self.path.unlink(missing_ok=True)
                failed = False

                def clock() -> datetime:
                    if failed:
                        raise RuntimeError("synthetic clock failure")
                    return datetime(2026, 9, 1, 14, tzinfo=service_fixtures.SHANGHAI)

                service = self.fixture.make_service(clock=clock)
                self.assertEqual(service.snapshot()["items"][0]["formal_state"], "TRIAL_ENTRY_CANDIDATE")
                self.write_snapshot()
                failed = True
                getattr(service, method)()
                state = service.snapshot()
                self.assertEqual(state["holdings_snapshot"]["status"], "SNAPSHOT_ONLY")
                self.assertEqual(state["health"]["service"], "CLOCK_FAILED")
                self.assertIsNone(state["portfolio"])
                self.assertFalse(state["active_alerts"])
                decision = state["items"][0]["formal_decision"]
                self.assertEqual(decision["planned_shares"], 0)
                self.assertIsNone(decision["planned_stop"])
                self.assertIsNone(decision["valid_for_trading_date"])
                self.assertNotEqual(state["items"][0]["execution_status"], "READY_TO_EXECUTE")
                with self.assertRaisesRegex(SwingServiceError, "快照"):
                    service.initialize_portfolio("blocked", 1000, "clock-blocked-init")


class SwingHoldingsPageTests(unittest.TestCase):
    def test_snapshot_disables_ready_status_and_explains_unknown_risk(self) -> None:
        body = "const view=" + json.dumps(snapshot_view()) + ";" + """
const state=createPageState();
applySnapshotPayload(state,{revision:1,holdings_snapshot:view,items:[{symbol:'510300',execution_status:'READY_TO_EXECUTE'}],health:{service:'OK',configuration:'OK',daily:'OK',portfolio:'OK',alerts:'OK',intraday:'REALTIME'},errors:{}},true);
console.log(JSON.stringify({paused:state.safetyPaused,ready:state.selectedExecutable,copy:executionWarningCopy(state)}));
"""
        result = run_swing_helpers(body)
        self.assertTrue(result["paused"])
        self.assertFalse(result["ready"])
        self.assertIn("快照", result["copy"])
        self.assertIn("迁移", result["copy"])
        self.assertNotIn("填写真实账户", result["copy"])
        self.assertNotIn("最后正式计划", result["copy"])

    def test_snapshot_plan_shows_technical_observation_without_shares(self) -> None:
        plan = "function planHtml(item)" + SWING_PAGE.split(
            "function planHtml(item)", 1,
        )[1].split("function renderDetail()", 1)[0]
        body = "const view=" + json.dumps(snapshot_view()) + ";" + """
const fmt=(value,digits=2)=>finite(value)==null?'—':Number(value).toLocaleString('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits});
const text=value=>statusText(value);
""" + plan + """
const state=createPageState();
applySnapshotPayload(state,{revision:1,holdings_snapshot:view,items:[{symbol:'510300',formal_state:'TRIAL_ENTRY_OBSERVE',execution_status:'PAUSED_HOLDINGS_SNAPSHOT',formal_decision:{planned_entry_low:1.2,planned_entry_high:1.3,planned_shares:0,evidence:{ma20_raw:1.1,pullback_low_touched:true,reclaim_close_above_ma20:true,confirmation_above_previous_high:false,anti_chase_ok:true,trend_ma20_above_ma60:true,technical_trial_ready:true}}}],health:{service:'OK',configuration:'OK',daily:'OK',portfolio:'SNAPSHOT_ONLY',alerts:'OK',intraday:'CLOSED'},errors:{}},true);
console.log(JSON.stringify({html:planHtml(state.snapshot.items[0])}));
"""
        html = run_swing_helpers(body)["html"]
        self.assertIn("技术观察", html)
        self.assertIn("不可执行", html)
        self.assertIn("1.200", html)
        self.assertIn("技术买入区", html)
        self.assertNotIn("<dt>建议份额</dt>", html)
        self.assertIn("收回确认", html)

    def test_selected_holding_escapes_name_without_repeating_other_positions(self) -> None:
        view = snapshot_view()
        first = view["snapshot"]["positions"][0]
        first["name"] = "<img src=x onerror=alert(1)>"
        for index in range(7):
            view["snapshot"]["positions"].append({**first, "symbol": f"51031{index}"})
        result = run_swing_helpers("const view=" + json.dumps(view) + ";" + """
console.log(JSON.stringify({exists:typeof selectedHoldingMarkup==='function',html:typeof selectedHoldingMarkup==='function'?selectedHoldingMarkup({holdings_snapshot:view},'510300'):''}));
""")
        self.assertTrue(result["exists"])
        self.assertEqual(result["html"].count('data-holding-field='), 3)
        self.assertNotIn("600036", result["html"])
        self.assertIn("1.234", result["html"])
        self.assertIn("未知", result["html"])
        self.assertIn("&lt;img", result["html"])
        self.assertNotIn("<img", result["html"])

    def test_strategy_summary_does_not_repeat_global_account_or_budget(self) -> None:
        result = run_swing_helpers("const view=" + json.dumps(snapshot_view()) + ";" + """
console.log(JSON.stringify({html:selectedHoldingMarkup({holdings_snapshot:view},'510300')}));
""")
        for fragment in ("总资产", "1,000.00", "150.00", "80.00%", "900.00", "参考底线", "今日候选"):
            self.assertNotIn(fragment, result["html"])

    def test_invalid_snapshot_blocks_forms_and_does_not_render_error_html(self) -> None:
        view = {"status": "INVALID", "snapshot": None, "error": "<script>secret</script>",
                "read_only": True, "strategy_ready": False}
        result = run_swing_helpers("const view=" + json.dumps(view) + ";" + """
console.log(JSON.stringify({exists:typeof holdingsSnapshotPresent==='function',blocked:typeof holdingsSnapshotPresent==='function'&&holdingsSnapshotPresent({holdings_snapshot:view}),html:selectedHoldingMarkup({holdings_snapshot:view},'510300')}));
""")
        self.assertTrue(result["exists"])
        self.assertTrue(result["blocked"])
        self.assertNotIn("<script>", result["html"])
        self.assertIn("持仓快照", result["html"])
        self.assertNotIn("六只ETF", SWING_PAGE)
        self.assertIn("renderPortfolioWriteLock", SWING_PAGE)
        self.assertIn("holdingsSnapshotPresent(state.snapshot)", SWING_PAGE)

    def test_selected_summary_uses_position_time_and_keeps_global_notes_elsewhere(self) -> None:
        view = snapshot_view()
        view["snapshot"]["notes"] = ["<script>synthetic note</script>"]
        result = run_swing_helpers("const view=" + json.dumps(view) + ";" + """
console.log(JSON.stringify({html:selectedHoldingMarkup({holdings_snapshot:view},'510300')}));
""")
        for fragment in ("报告可卖", "持仓截图时点：未知", "非实时估值", "买入日期和止损未知"):
            self.assertIn(fragment, result["html"])
        for fragment in ("<script>", "&lt;script&gt;", "账户截图时点", "报告证券市值", "其他资产"):
            self.assertNotIn(fragment, result["html"])
        self.assertIn(".selected-holding", SWING_PAGE)

    def test_snapshot_form_lock_restores_only_controls_it_disabled(self) -> None:
        render = SWING_PAGE.split("function renderPortfolioWriteLock(){", 1)[1].split("function renderWatchlist(){", 1)[0]
        body = "const state={snapshot:{holdings_snapshot:" + json.dumps(snapshot_view()) + "}};" + """
const forms=Array.from({length:3},()=>({hidden:false,controls:[{disabled:false,dataset:{}},{disabled:true,dataset:{}}],querySelectorAll(selector){return selector==='[data-snapshot-disabled]'?this.controls.filter(control=>control.dataset.snapshotDisabled):this.controls}}));
const document={getElementById(id){return forms[['portfolio-initialize-form','trade-form','reverse-form'].indexOf(id)]}};
""" + "function renderPortfolioWriteLock(){" + render + """
renderPortfolioWriteLock();renderPortfolioWriteLock();const locked=forms.every(form=>form.hidden&&form.controls.every(control=>control.disabled));
state.snapshot={holdings_snapshot:{status:'ABSENT'}};renderPortfolioWriteLock();
console.log(JSON.stringify({locked,restored:forms.every(form=>!form.hidden&&!form.controls[0].disabled&&form.controls[1].disabled)}));
"""
        self.assertEqual(run_swing_helpers(body), {"locked": True, "restored": True})

    def test_submit_guard_never_posts_snapshot_ledger_mutations(self) -> None:
        submit = SWING_PAGE.split("async function submitIntent(", 1)[1].split("async function toggleWatch(", 1)[0]
        body = "const state={snapshot:{holdings_snapshot:" + json.dumps(snapshot_view()) + "}};" + """
let calls=0;const intentRegistry=new Map();async function postLocalRecord(){calls++;return {}};
""" + "async function submitIntent(" + submit + """
(async()=>{let rejected=0;for(const path of ['/api/swing/portfolio/initialize','/api/swing/trades','/api/swing/trades/synthetic/reverse'])try{await submitIntent('test',path,{})}catch(error){if(error.message.includes('快照'))rejected++}console.log(JSON.stringify({calls,rejected}))})();
"""
        self.assertEqual(run_swing_helpers(body), {"calls": 0, "rejected": 3})

    def test_snapshot_statuses_use_explicit_chinese_labels(self) -> None:
        result = run_swing_helpers("console.log(JSON.stringify(['SNAPSHOT_ONLY','PAUSED_HOLDINGS_SNAPSHOT','holdings_snapshot_only','SNAPSHOT_UNKNOWN'].map(statusText)));")
        for label in result:
            self.assertIn("快照", label)
            self.assertNotIn("0.75", label)

    def test_disabled_watch_toggle_uses_published_eligibility(self) -> None:
        result = run_swing_helpers("""
const payload={available_symbols:[{symbol:'510300',can_enable:true},{symbol:'159915',can_enable:false}]};
console.log(JSON.stringify({exists:typeof watchActivationAllowed==='function',allowed:typeof watchActivationAllowed==='function'&&watchActivationAllowed(payload,'510300'),blocked:typeof watchActivationAllowed==='function'&&!watchActivationAllowed(payload,'159915')}));
""")
        self.assertEqual(result, {"exists": True, "allowed": True, "blocked": True})
        self.assertIn("!toggle.checked&&!watchActivationAllowed(state.snapshot,symbol)", SWING_PAGE)


if __name__ == "__main__":
    unittest.main()
