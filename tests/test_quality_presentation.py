from __future__ import annotations

from datetime import datetime
import json
import re
import subprocess
import unittest

from etf_rotation.swing_page import SWING_PAGE
from etf_rotation.t_page import PAGE
from tests import test_swing_service as service_fixtures
from tests.test_swing_page import run_swing_helpers
from tests.test_t_monitor import run_page_helpers


class TQualityPresentationTests(unittest.TestCase):
    def test_data_error_is_unsafe_even_without_a_qualified_quote(self) -> None:
        result = run_page_helpers("""
const rows=['OK','MISSING_QUOTE'].flatMap(status=>[null,'2026-09-03T11:20:00+08:00'].map(timestamp=>
  marketPresentation({symbol:'510300',status,health_status:'DATA_ERROR',timestamp,
    timestamp_basis:'MINUTE_START',action:'BUY_CANDIDATE'},Date.parse('2026-09-03T11:22:00+08:00'))));
console.log(JSON.stringify(rows));
""")
        for row in result:
            self.assertEqual(row["healthKey"], "DATA_ERROR")
            self.assertEqual(row["statusText"], "数据校验异常")
            self.assertEqual(row["dotClass"], "data-error")
            self.assertTrue(row["unsafe"])
            self.assertFalse(row["candidate"])
            self.assertNotIn("断流", row["warning"])

    def test_quality_guard_only_blocks_the_affected_symbol(self) -> None:
        result = run_page_helpers("""
const now=Date.parse('2026-09-03T11:22:00+08:00'),base={status:'OK',timestamp:'2026-09-03T11:21:00+08:00',timestamp_basis:'MINUTE_START',action:'BUY_CANDIDATE'};
const bad={...base,symbol:'510300',health_status:'DATA_ERROR'},good={...base,symbol:'510500',health_status:'REALTIME'};
acceptMarketSummary([bad,good],8,now);
console.log(JSON.stringify([currentMarketPresentation(bad,now),currentMarketPresentation(good,now)]));
""")
        self.assertEqual(result[0]["statusText"], "数据校验异常")
        self.assertFalse(result[0]["candidate"])
        self.assertTrue(result[1]["candidate"])
        self.assertFalse(result[1]["unsafe"])

    def test_network_failure_keeps_the_existing_feed_warning(self) -> None:
        result = run_page_helpers("""
console.log(JSON.stringify(marketPresentation({status:'OK',health_status:'DATA_ERROR'},Date.now(),{ready:false,message:'行情连接中断'})));
""")
        self.assertEqual(result["statusText"], "行情连接中断")
        self.assertEqual(result["dotClass"], "outage")
        self.assertFalse(result["candidate"])

    def test_quality_notice_is_separate_escaped_and_names_isolated_minutes(self) -> None:
        result = run_page_helpers("""
const issue={symbol:'510300',timestamp:'2026-09-03T11:21:00+08:00',reason:'<img src=x onerror="boom">'};
const node={hidden:true,innerHTML:''},errors={textContent:'真实网络错误'};
renderQualityNotice({validation_issues:[issue],items:[{symbol:'510300',validation_issues:[issue]}]},node);
console.log(JSON.stringify({node,errors}));
""")
        html = result["node"]["innerHTML"]
        self.assertFalse(result["node"]["hidden"])
        self.assertIn("510300", html)
        self.assertIn("2026-09-03T11:21:00+08:00", html)
        self.assertIn("已隔离", html)
        self.assertIn("仅相关标的提醒暂停", html)
        self.assertIn("其他有效行情继续", html)
        self.assertIn("&lt;img", html)
        self.assertNotIn("<img", html)
        self.assertEqual(html.count("2026-09-03T11:21:00+08:00"), 1)
        self.assertEqual(result["errors"]["textContent"], "真实网络错误")
        self.assertIn('id="quality-notice"', PAGE)
        self.assertIn("renderQualityNotice(data,", PAGE)
        self.assertIn("errors.textContent=(data.errors||[]).join", PAGE)

    def test_quality_notice_supports_per_item_issues_and_clears_when_resolved(self) -> None:
        result = run_page_helpers("""
const node={hidden:true,innerHTML:''};
renderQualityNotice({items:[{symbol:'510500',validation_issues:[{timestamp:'2026-09-03T11:21:00+08:00',reason:'invalid'}]}]},node);
const shown={...node};renderQualityNotice({validation_issues:[],items:[]},node);
console.log(JSON.stringify({shown,cleared:node}));
""")
        self.assertIn("510500", result["shown"]["innerHTML"])
        self.assertTrue(result["cleared"]["hidden"])
        self.assertEqual(result["cleared"]["innerHTML"], "")

    def test_quality_notice_reports_missing_intraday_symbols(self) -> None:
        result = run_page_helpers("""
const node={hidden:true,innerHTML:''};
renderQualityNotice({intraday_coverage:{coverage_pct:75,missing_symbols:['512170','515120']}},node);
console.log(JSON.stringify(node));
""")
        self.assertFalse(result["hidden"])
        self.assertIn("盘中数据覆盖不完整", result["innerHTML"])
        self.assertIn("75.0%", result["innerHTML"])
        self.assertIn("512170", result["innerHTML"])

    def test_unchanged_quality_notice_preserves_user_expanded_details(self) -> None:
        result = run_page_helpers("""
const data={validation_issues:[{symbol:'510300',timestamp:'2026-09-03T11:21:00+08:00',reason:'量价校验失败'}]};
let html='',writes=0;
const node={hidden:true,get innerHTML(){return html},set innerHTML(value){html=value;writes++}};
renderQualityNotice(data,node);
// Opening a native details element changes its serialized innerHTML.
html=html.replace('<details>','<details open="">');
renderQualityNotice(JSON.parse(JSON.stringify(data)),node);
const unchanged={writes,open:html.includes('<details open'),hidden:node.hidden};
renderQualityNotice({validation_issues:[],items:[]},node);
console.log(JSON.stringify({unchanged,cleared:{writes,html,hidden:node.hidden}}));
""")
        self.assertEqual(result["unchanged"], {"writes": 1, "open": True, "hidden": False})
        self.assertEqual(result["cleared"], {"writes": 2, "html": "", "hidden": True})

    def test_quality_block_reason_is_explained_in_chinese(self) -> None:
        result = run_page_helpers("""
const text=reasonText(['MARKET_DATA_INVALID']);
const html=regimeMarkup({blocked_reasons:['MARKET_DATA_INVALID']});
console.log(JSON.stringify({text,html}));
""")
        self.assertEqual(result["text"], "存在未核验的异常分钟，暂停候选提醒")
        self.assertIn(result["text"], result["html"])
        self.assertNotIn("MARKET_DATA_INVALID", result["html"])

    def chart_result(self, timestamps: list[str]) -> dict:
        chart_source = "function marketMinute" + PAGE.split("function marketMinute", 1)[1].split("function attachChartTooltip", 1)[0]
        return run_page_helpers(chart_source + "\n" + r"""
const points=TIMESTAMPS.map((timestamp,index)=>({timestamp,price:10+index/100,average_price:10,previous_close:10}));
const quotePoints=new Map([['510300',new Map(points.map(p=>[p.timestamp,p]))]]),num=value=>String(value);
const html=chart({symbol:'510300',previous_close:10,grid_width_pct:0.002});
const paths=[...html.matchAll(/<path\b[^>]*class="(three-grid|five-grid|average-line|price-line)"[^>]*d="([^"]+)"/g)].map(match=>({kind:match[1],commands:match[2].replace(/[^ML]/g,'')}));
console.log(JSON.stringify({paths,minutes:points.map(point=>marketMinute(point.timestamp))}));
""".replace("TIMESTAMPS", json.dumps(timestamps)))

    def test_all_six_chart_paths_break_at_missing_same_session_minute(self) -> None:
        result = self.chart_result([
            f"2026-09-03T{minute}:00+08:00"
            for minute in ("09:30", "09:31", "09:33", "09:34")
        ])
        self.assertEqual(len(result["paths"]), 6)
        self.assertEqual([path["commands"] for path in result["paths"]], ["MLML"] * 6)

    def test_chart_keeps_normal_lunch_layout_and_breaks_afternoon_gap(self) -> None:
        result = self.chart_result([
            f"2026-09-03T{minute}:00+08:00"
            for minute in ("11:29", "11:30", "13:00", "13:01", "13:03")
        ])
        self.assertEqual(result["minutes"], [119, 120, 120, 121, 123])
        self.assertEqual([path["commands"] for path in result["paths"]], ["MLLLM"] * 6)

    def test_chart_breaks_across_lunch_when_either_boundary_minute_is_missing(self) -> None:
        for before, after in (("11:29", "13:00"), ("11:30", "13:01"), ("11:28", "13:02")):
            with self.subTest(before=before, after=after):
                result = self.chart_result([
                    f"2026-09-03T{before}:00+08:00", f"2026-09-03T{after}:00+08:00",
                ])
                self.assertEqual([path["commands"] for path in result["paths"]], ["MM"] * 6)

    def test_both_page_scripts_have_valid_javascript_syntax(self) -> None:
        for page in (PAGE, SWING_PAGE):
            with self.subTest(page=page[:80]):
                script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
                result = subprocess.run(["node", "--check"], input=script, text=True,
                                        encoding="utf-8", capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)


class SwingQualityPresentationTests(unittest.TestCase):
    def fixture_service(self, *, timestamp: str | None = None):
        fixture = service_fixtures.SwingServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture.make_service(intraday_provider=lambda: {"items": [{
            "symbol": "510300", "price": 106.0, "timestamp": timestamp,
            "health_status": "DATA_ERROR", "timestamp_basis": "MINUTE_START",
        }]})

    def test_validator_preserves_data_error_without_quote_and_outside_session(self) -> None:
        service = self.fixture_service()
        for hour in (10, 12, 16):
            for timestamp in (None, "2026-09-01T09:59:00+08:00"):
                with self.subTest(hour=hour, timestamp=timestamp):
                    result = service._validated_realtime_quote({
                        "health_status": "DATA_ERROR", "timestamp": timestamp,
                        "timestamp_basis": "MINUTE_START",
                    }, datetime(2026, 9, 1, hour, tzinfo=service_fixtures.SHANGHAI))
                    self.assertEqual(result, (False, timestamp, "DATA_ERROR"))

    def test_aggregate_exposes_quality_status_and_never_creates_overlay(self) -> None:
        service = self.fixture_service(timestamp="2026-09-01T13:59:00+08:00")
        snapshot = service.refresh_intraday()
        self.assertEqual(snapshot["health"]["intraday"], "DATA_ERROR")
        item = snapshot["items"][0]
        self.assertEqual(item["intraday_health_status"], "DATA_ERROR")
        self.assertIsNone(item["intraday_overlay"])
        self.assertEqual(item["execution_status"], "PAUSED_MARKET_NOT_REALTIME")

    def test_swing_quality_failure_is_red_clear_and_still_blocks_execution(self) -> None:
        result = run_swing_helpers("""
const state={snapshot:{health:{service:'OK',configuration:'OK',daily:'OK',portfolio:'OK',alerts:'OK',intraday:'DATA_ERROR'},errors:{},items:[{symbol:'510300',execution_status:'READY_TO_EXECUTE'}]},failureSources:new Set(),selectedSymbol:'510300'};
refreshExecutionPaused(state);
console.log(JSON.stringify({notice:executionNotice(state),reasons:executionPauseReasons(state),label:statusText('DATA_ERROR'),paused:state.safetyPaused,executable:state.selectedExecutable}));
""")
        self.assertEqual(result["notice"]["tone"], "danger")
        self.assertIn("数据校验异常", result["notice"]["summary"])
        self.assertIn("数据校验异常", " ".join(result["reasons"]))
        self.assertNotIn("断流", json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["label"], "数据校验异常")
        self.assertTrue(result["paused"])
        self.assertFalse(result["executable"])


if __name__ == "__main__":
    unittest.main()
