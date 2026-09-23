from __future__ import annotations

import importlib
import importlib.util
import json
import re
import subprocess
import unittest


def notification_page() -> str:
    if importlib.util.find_spec("etf_rotation.notification_page") is None:
        raise AssertionError("The independent NOTIFICATION_PAGE module is missing")
    page = getattr(importlib.import_module("etf_rotation.notification_page"), "NOTIFICATION_PAGE", None)
    if not isinstance(page, str):
        raise AssertionError("NOTIFICATION_PAGE must be an HTML string")
    return page


def snapshot() -> dict:
    return {
        "config": {"enabled": False, "smtp_host": "smtp.163.com", "smtp_port": 465,
                   "sender": "s***@163.com", "recipient": "r***@example.invalid", "username": "s***",
                   "anomaly_enabled": True, "health_enabled": True,
                   "excluded_symbols": [], "rules": {}},
        "secret_configured": False, "mode": "OBSERVATION", "csrf_token": "synthetic-csrf",
        "checks": {"connection": False, "email": False},
        "items": [{"symbol": "510300", "name": "合成测试ETF", "eligible": True,
                   "health_status": "REALTIME", "status": "READY", "reason": "",
                   "change_pct": 0, "timestamp": "2026-09-04T10:05:00+08:00"}],
        "events": [], "last_checked_at": "2026-09-04T10:05:00+08:00", "session": "MORNING", "error": None,
    }


def run_helpers(body: str) -> object:
    page = notification_page()
    start, end = "/* NOTIFICATION_PAGE_HELPERS_START */", "/* NOTIFICATION_PAGE_HELPERS_END */"
    if start not in page or end not in page:
        raise AssertionError("The notification page must expose its JavaScript helper block")
    helpers = page.split(start, 1)[1].split(end, 1)[0]
    script = helpers + "\n(async()=>{const fixture=" + json.dumps(snapshot()) + ";\n" + body
    script += "\n})().catch(error=>{console.error(error);process.exitCode=1});"
    result = subprocess.run(["node", "-"], input=script, text=True, encoding="utf-8",
                            capture_output=True, check=False, timeout=10)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def run_page(body: str) -> object:
    page = notification_page()
    script = re.findall(r"<script>(.*?)</script>", page, flags=re.S)[0]
    ids = re.findall(r'<[^>]+\bid="([^"]+)"[^>]*>', page)
    harness = "const fixture=" + json.dumps(snapshot()) + ";const ids=" + json.dumps(ids) + ";\n" + r"""
const nodes=Object.fromEntries(ids.map(id=>[id,{id,value:'',placeholder:'',checked:false,disabled:true,hidden:false,textContent:'',innerHTML:'',dataset:{},listeners:{},addEventListener(event,callback){this.listeners[event]=callback}}]));
const timers=new Map();let timerId=0;
const setTimeout=callback=>{timers.set(++timerId,callback);return timerId};const clearTimeout=id=>timers.delete(id);
let current=structuredClone(fixture),failGet=false,failPost=false,confirmAnswer=true,confirmCalls=0;
let failureBody={error:'invalid_request',message:'操作未完成：请检查字段、确认状态、测试间隔及邮箱配置；只读模式不能发信'};
const requests=[];const window={confirm(){confirmCalls++;return confirmAnswer},addEventListener(){}};
const document={hidden:true,getElementById:id=>nodes[id],addEventListener(){},querySelectorAll:selector=>selector==='[data-rule-symbol]'?[]:Object.values(nodes).filter(node=>['sender','recipient','username','secret','anomaly-enabled','health-enabled','enable','pause','save-config','connection-test','test-email','replay','replay-symbol','replay-date'].includes(node.id))};
const crypto={randomUUID:()=>`synthetic-${requests.length}`};
const fetch=async(url,options)=>{requests.push({url,method:options.method,headers:options.headers,body:options.body?JSON.parse(options.body):null});if(options.method==='GET'){if(failGet)throw Error('synthetic offline');return {ok:true,json:async()=>structuredClone(current)}}if(failPost)return {ok:false,json:async()=>failureBody};return {ok:true,json:async()=>({queued:true})}};
async function drain(){for(let count=0;count<40;count++)await Promise.resolve()}
"""
    program = harness + script + "\n(async()=>{\n" + body
    program += "\n})().catch(error=>{console.error(error);process.exitCode=1});"
    result = subprocess.run(["node", "-"], input=program, text=True, encoding="utf-8",
                            capture_output=True, check=False, timeout=10)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class NotificationPageTests(unittest.TestCase):
    def test_existing_pages_have_notification_navigation_with_mobile_wrapping(self) -> None:
        modules = (("etf_rotation.t_page", "PAGE"), ("etf_rotation.swing_page", "SWING_PAGE"),
                   ("etf_rotation.pr_page", "PR_PAGE"))
        for module_name, export in modules:
            module = importlib.import_module(module_name)
            page = getattr(module, export, None)
            if page is None and module_name == "etf_rotation.t_page":
                page = module.T_PAGE
            with self.subTest(page=module_name):
                navigation = re.search(r'<nav aria-label="监控模式">(.*?)</nav>', page, re.S).group(1)
                self.assertEqual(navigation.count('href="/notifications"'), 1)
                self.assertEqual(navigation.count('aria-current="page"'), 1)
                self.assertIn('nav[aria-label="监控模式"]{display:flex;flex-wrap:wrap;', page)

    def test_page_has_dark_theme_four_sections_and_shared_navigation(self) -> None:
        page = notification_page()
        for fragment in ("color-scheme:dark", "--bg:#07111f", "--panel:#101d30", "--line:#293d58",
                         'href="/swing">波段监控', 'href="/">做T监控', 'href="/pr">PR估值',
                         'href="/notifications" aria-current="page">通知中心', '<main class="shell">',
                         'id="notification-status"', 'id="mail-settings"', 'id="subscription-settings"',
                         'id="notification-records"', "主动下单提醒未启用", "电脑保持唤醒", "关闭浏览器",
                         "不要将授权码粘贴到聊天", "smtp.163.com", "SSL · 465"):
            self.assertIn(fragment, page)
        self.assertLess(page.index('<nav aria-label="监控模式">'), page.index("<h1>"))

    def test_mobile_layout_and_accessible_error_and_pending_states_exist(self) -> None:
        page = notification_page()
        for fragment in ("@media(max-width:600px)", "min-width:0", "overflow-x:auto", "flex-wrap:wrap",
                         'role="alert"', 'aria-live="polite"', "待确认", "尚未通过", "已排队"):
            self.assertIn(fragment, page)

    def test_trial_default_and_disabled_strategy_scope_are_explicit(self) -> None:
        page = notification_page()
        for fragment in ("未校准的试运行默认值", "不是投资建议", "策略接入和账户核验", "30–240"):
            self.assertIn(fragment, page)

    def test_event_details_show_allowlisted_evidence_and_data_time_only(self) -> None:
        result = run_helpers("""
const event={id:'synthetic-event',kind:'ANOMALY',symbol:'510300',direction:'UP',created_at:'2026-09-04T10:05:59+08:00',expires_at:'2026-09-04T10:07:59+08:00',status:'OBSERVED',attempts:0,reason:'',payload:{name:'合成ETF',timestamp:'2026-09-04T10:05:00+08:00',price:4.12,change_pct:1.23,threshold_pct:1,cooldown_minutes:30,reason:'合成异动依据',epoch:'private-epoch',secret:'never-render',account_total:987654321}};
console.log(JSON.stringify(notificationEventsMarkup([event])));
""")
        for fragment in ("<details", "查看详情", "触发依据", "行情时点", "2026-09-04T10:05:00+08:00", "1.23", "4.120", "合成异动依据"):
            self.assertIn(fragment, result)
        for fragment in ("private-epoch", "never-render", "987654321"):
            self.assertNotIn(fragment, result)

    def test_open_event_details_survive_background_refresh(self) -> None:
        result = run_helpers("""
let html='',detail=null,writes=0;const container={get innerHTML(){return html},set innerHTML(value){writes++;html=value;detail={open:false,dataset:{eventDetail:'synthetic-event'}}},querySelectorAll(selector){return detail&&(selector==='details[data-event-detail]'||detail.open)?[detail]:[]}};
const present=createNotificationEventPresenter(container),event={id:'synthetic-event',kind:'ANOMALY',status:'PENDING',attempts:0};
present([event]);detail.open=true;present([event]);const unchanged=writes;present([{...event,status:'SERVER_ACCEPTED',attempts:1}]);console.log(JSON.stringify({unchanged,writes,open:detail.open,accepted:html.includes('不代表送达')}));
""")
        self.assertEqual(result, {"unchanged": 1, "writes": 2, "open": True, "accepted": True})

    def test_credentials_are_only_optional_unpopulated_password_inputs(self) -> None:
        page = notification_page()
        secret = re.search(r'<input\b[^>]*id="secret"[^>]*>', page).group()
        self.assertIn('type="password"', secret)
        self.assertIn('autocomplete="off"', secret)
        self.assertNotIn('value=', secret)
        for fragment in ("localStorage", "sessionStorage", "sendBeacon", "smtp-password", "http://", "https://"):
            self.assertNotIn(fragment, page)
        self.assertIn("secretInput.value=''", page)
        self.assertNotRegex(page, r"(?:secretInput|byId\('secret'\)).value\s*=\s*(?:state|snapshot|config)")
        self.assertIn("window.confirm", page)

    def test_all_javascript_is_syntactically_valid(self) -> None:
        scripts = re.findall(r"<script>(.*?)</script>", notification_page(), flags=re.S)
        self.assertEqual(len(scripts), 1)
        result = subprocess.run(["node", "--check"], input=scripts[0], text=True, encoding="utf-8",
                                capture_output=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_or_malformed_state_fails_closed(self) -> None:
        result = run_helpers("""
const cases=[null,{},...['config','mode','csrf_token','secret_configured','checks','items','events'].map(key=>{const row=structuredClone(fixture);delete row[key];return row}),{...fixture,checks:{}},{...fixture,mode:'UNKNOWN'}];
console.log(JSON.stringify(cases.map(row=>{try{parseNotificationSnapshot(row);return 'accepted'}catch(error){return error.message}})));
""")
        self.assertTrue(all("暂不可用" in value for value in result))

    def test_labels_are_chinese_and_acceptance_is_not_delivery(self) -> None:
        result = run_helpers("""
console.log(JSON.stringify(['OBSERVATION','ENABLED','PAUSED','ERROR','READ_ONLY','SERVER_ACCEPTED','CONNECTION_OK','UNKNOWN','QUEUED','SENDING','CANCELLED','REALTIME'].map(notificationLabel)));
""")
        self.assertEqual(result[:5], ["仅观察", "通知已启用", "通知已暂停", "通知异常", "只读模式"])
        self.assertIn("不代表送达", result[5])
        self.assertIn("未发送邮件", result[6])
        self.assertIn("结果未知", result[7])
        self.assertNotIn("SERVER_ACCEPTED", result)

    def test_event_and_rule_markup_escapes_all_dynamic_text(self) -> None:
        result = run_helpers("""
const attack='\"><img src=x onerror=alert(1)>';
fixture.items[0]={...fixture.items[0],name:attack,reason:attack,timestamp:attack};
const event={id:attack,kind:attack,symbol:attack,direction:attack,created_at:attack,expires_at:attack,payload:{note:attack},status:'SERVER_ACCEPTED',attempts:1,reason:attack};
console.log(JSON.stringify({items:notificationItemsMarkup(fixture.items),rules:notificationRulesMarkup(fixture),events:notificationEventsMarkup([event])}));
""")
        for html in result.values():
            self.assertNotIn("<img", html)
            self.assertIn("&lt;img", html)
        self.assertIn("不代表送达", result["events"])

    def test_zero_change_is_distinct_from_missing_and_rules_have_bounds(self) -> None:
        result = run_helpers("""
console.log(JSON.stringify({zero:notificationNumber(0),missing:notificationNumber(null),rules:notificationRulesMarkup(fixture)}));
""")
        self.assertEqual(result["zero"], "0.00")
        self.assertEqual(result["missing"], "—")
        for fragment in ('min="0.1"', 'max="10"', 'min="30"', 'max="240"', 'value="1"', 'value="30"', 'type="checkbox"'):
            self.assertIn(fragment, result["rules"])

    def test_existing_market_health_states_have_specific_chinese_labels(self) -> None:
        result = run_helpers("console.log(JSON.stringify(['LUNCH_BREAK','OUTAGE','DELAYED','MISSING','OBSERVED',''].map(notificationLabel)));")
        self.assertEqual(result, ["午间休市", "行情断流", "行情延迟", "行情未接通", "仅记录，未发送", "—"])

    def test_data_quality_error_has_specific_chinese_label(self) -> None:
        result = run_helpers("console.log(JSON.stringify(notificationLabel('DATA_ERROR')));")
        self.assertEqual(result, "行情质量异常")

    def test_transport_failures_have_chinese_actionable_reasons(self) -> None:
        result = run_helpers("console.log(JSON.stringify(['AUTHENTICATION_FAILED','RECIPIENT_REJECTED','CERTIFICATE_VERIFICATION_FAILED','NETWORK_ERROR','DELIVERY_OUTCOME_UNKNOWN','INVALID_CONFIGURATION_OR_SECRET'].map(notificationReason)));")
        self.assertEqual(result, ["邮箱认证失败，请检查已保存的用户名与授权码", "收件地址被拒绝", "安全证书验证失败，未降低连接安全性", "网络连接失败", "邮件提交后的结果未知，不会盲目重发", "邮箱配置或授权码无效"])

    def test_cancelled_before_submission_has_specific_chinese_reason(self) -> None:
        result = run_helpers("console.log(JSON.stringify(notificationReason('CANCELLED_BEFORE_SUBMISSION')));")
        self.assertEqual(result, "提交正文前条件失效或已暂停，已取消")

    def test_partial_config_never_posts_placeholders_or_empty_credentials(self) -> None:
        result = run_helpers("""
const form={sender:'',recipient:' next@example.invalid ',username:'',secret:'',anomaly_enabled:true,health_enabled:false};
const rows=[{symbol:'510300',excluded:true,threshold_pct:'1.2',cooldown_minutes:'45'}];
console.log(JSON.stringify(notificationConfigUpdate(form,rows,fixture.config)));
""")
        self.assertEqual(result, {"recipient": "next@example.invalid", "anomaly_enabled": True,
                                  "health_enabled": False, "excluded_symbols": ["510300"],
                                  "rules": {"510300": {"threshold_pct": 1.2, "cooldown_minutes": 45}}})

    def test_rule_validation_rejects_bad_bounds_and_preserves_unlisted_rules(self) -> None:
        result = run_helpers("""
const form={anomaly_enabled:true,health_enabled:true};
const bad=[['0','30'],['10.1','30'],['1','0'],['1','29'],['1','241'],['1','30.5'],['','30'],['NaN','30']];
const rejected=bad.map(([threshold_pct,cooldown_minutes])=>{try{notificationConfigUpdate(form,[{symbol:'510300',threshold_pct,cooldown_minutes}],fixture.config);return false}catch{return true}});
fixture.config.rules={'510500':{threshold_pct:2,cooldown_minutes:60}};fixture.config.excluded_symbols=['510500'];
console.log(JSON.stringify({rejected,saved:notificationConfigUpdate(form,[],fixture.config)}));
""")
        self.assertTrue(all(result["rejected"]))
        self.assertEqual(result["saved"]["rules"], {"510500": {"threshold_pct": 2, "cooldown_minutes": 60}})
        self.assertEqual(result["saved"]["excluded_symbols"], ["510500"])

    def test_poll_hydration_does_not_overwrite_in_progress_forms(self) -> None:
        result = run_helpers("""
let configCalls=0,ruleCalls=0;
const hydrate=createNotificationHydrator({hydrateConfig:()=>configCalls++,hydrateRules:()=>ruleCalls++});
hydrate({...fixture,items:[]});hydrate(fixture);hydrate({...fixture,config:{...fixture.config,anomaly_enabled:false}});
const before={configCalls,ruleCalls};hydrate(fixture,{force:true});console.log(JSON.stringify({before,after:{configCalls,ruleCalls}}));
""")
        self.assertEqual(result, {"before": {"configCalls": 1, "ruleCalls": 1}, "after": {"configCalls": 2, "ruleCalls": 2}})

    def test_write_gates_reject_read_only_error_and_unverified_enable(self) -> None:
        result = run_helpers("""
console.log(JSON.stringify({observation:notificationCanWrite(fixture),readOnly:notificationCanWrite({...fixture,mode:'READ_ONLY'}),error:notificationCanWrite({...fixture,mode:'ERROR'}),missing:notificationCanWrite(null),enable:notificationCanEnable(fixture),verified:notificationCanEnable({...fixture,secret_configured:true,checks:{connection:true,email:true}})}));
""")
        self.assertEqual(result, {"observation": True, "readOnly": False, "error": False, "missing": False, "enable": False, "verified": True})


class NotificationClientTests(unittest.TestCase):
    def test_safe_replay_errors_have_specific_chinese_messages(self) -> None:
        result = run_helpers("""
const messages=['存在未解除的分钟质量隔离问题，不能回放','该日没有通过校验的分钟历史','不能回放未来日期','回放日期无效','回放日期不是交易日或为已配置休市日','历史事务尚未完成，不能只读回放','只能回放已持有ETF'];
console.log(JSON.stringify({specific:messages.map(message=>notificationRequestError({error:'replay_unavailable',message})),fallback:notificationRequestError({error:'replay_unavailable',message:'unapproved synthetic detail'})}));
""")
        self.assertEqual(result["specific"], ["存在未解除的分钟质量隔离问题，不能回放", "该日没有通过校验的分钟历史", "不能回放未来日期", "回放日期无效", "回放日期不是交易日或为已配置休市日", "历史事务尚未完成，不能只读回放", "只能回放已持有ETF"])
        self.assertEqual(result["fallback"], "回放不可用：历史缺失、质量隔离未解除或日期/分钟校验未通过。")

    def test_read_uses_no_store_and_preserves_paginated_offset(self) -> None:
        result = run_helpers("""
const requests=[];
const client=createNotificationClient({fetcher:async(url,options)=>{requests.push({url,method:options.method,cache:options.cache});return {ok:true,json:async()=>fixture}},onState:()=>{},onError:()=>{}});
await client.refresh(50);await client.refresh();console.log(JSON.stringify({requests,offset:client.offset()}));
""")
        self.assertEqual(result["offset"], 50)
        self.assertEqual(result["requests"], [{"url": "/api/notifications?limit=50&offset=50", "method": "GET", "cache": "no-store"}] * 2)

    def test_every_post_has_json_csrf_and_new_idempotency_key(self) -> None:
        result = run_helpers("""
const requests=[];let key=0;
const client=createNotificationClient({fetcher:async(url,options)=>{requests.push({url,...options});return {ok:true,json:async()=>options.method==='GET'?fixture:{queued:true}}},onState:()=>{},onError:()=>{},newKey:()=>`synthetic-${++key}`});
await client.refresh();await client.post('connection-test',{confirmed:true});await client.post('test-email',{confirmed:true});
console.log(JSON.stringify(requests.filter(row=>row.method==='POST').map(row=>({url:row.url,headers:row.headers,body:JSON.parse(row.body)}))));
""")
        for index, row in enumerate(result):
            self.assertEqual(row["headers"], {"Content-Type": "application/json", "X-CSRF-Token": "synthetic-csrf", "Idempotency-Key": f"synthetic-{index + 1}"})
            self.assertEqual(row["body"], {"confirmed": True})
        self.assertEqual(result[1]["url"], "/api/notifications/test-email")

    def test_status_failure_disables_writes_until_fresh_success(self) -> None:
        result = run_helpers("""
let fail=false,posts=0;const states=[],errors=[];
const client=createNotificationClient({fetcher:async(url,options)=>{if(fail)throw Error('synthetic network error');if(options.method==='POST')posts++;return {ok:true,json:async()=>fixture}},onState:row=>states.push(row),onError:message=>errors.push(message),newKey:()=> 'synthetic'});
await client.refresh();fail=true;await client.refresh();let blocked=false;try{await client.post('config',{})}catch{blocked=true}
const unavailable=client.state()===null;fail=false;await client.refresh();console.log(JSON.stringify({unavailable,blocked,posts,recovered:client.state().mode,errors,final:states.at(-1).mode}));
""")
        self.assertTrue(result["unavailable"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["posts"], 0)
        self.assertEqual(result["recovered"], "OBSERVATION")
        self.assertTrue(result["errors"])

    def test_malformed_snapshot_clears_state_and_post_error_is_visible(self) -> None:
        result = run_helpers("""
let malformed=false;const errors=[];
const client=createNotificationClient({fetcher:async(url,options)=>options.method==='POST'?{ok:false,json:async()=>({error:'invalid_request',message:'操作未完成：请检查字段、确认状态、测试间隔及邮箱配置；只读模式不能发信'})}:{ok:true,json:async()=>malformed?{}:fixture},onState:()=>{},onError:message=>errors.push(message),newKey:()=> 'synthetic'});
await client.refresh();let message;try{await client.post('config',{})}catch(error){message=error.message}
malformed=true;await client.refresh();console.log(JSON.stringify({message,errors,missing:client.state()===null}));
""")
        self.assertIn("操作未完成", result["message"])
        self.assertTrue(result["missing"])
        self.assertTrue(any("操作未完成" in error for error in result["errors"]))

    def test_actual_adapter_error_shape_prefers_fixed_chinese_message(self) -> None:
        result = run_helpers("""
const message='操作未完成：请检查字段、确认状态、测试间隔及邮箱配置；只读模式不能发信';const errors=[];
const client=createNotificationClient({fetcher:async(url,options)=>options.method==='POST'?{ok:false,json:async()=>({error:'invalid_request',message})}:{ok:true,json:async()=>fixture},onError:message=>errors.push(message),newKey:()=> 'synthetic'});
await client.refresh();let shown;try{await client.post('config',{})}catch(error){shown=error.message}console.log(JSON.stringify({message,shown,errors}));
""")
        self.assertEqual(result["shown"], result["message"])
        self.assertEqual(result["errors"], [result["message"]])
        self.assertNotIn("invalid_request", result["shown"])

    def test_unrecognized_error_details_do_not_reflect_arbitrary_server_data(self) -> None:
        result = run_helpers("""
const cases=[{error:'synthetic-secret',message:'<img src=x>synthetic-secret'},{error:'notification_storage',message:'synthetic-secret'},{error:'forbidden'},{error:'not_found'}];const results=[];
for(const body of cases){const client=createNotificationClient({fetcher:async(url,options)=>options.method==='POST'?{ok:false,json:async()=>body}:{ok:true,json:async()=>fixture},newKey:()=> 'synthetic'});await client.refresh();try{await client.post('config',{})}catch(error){results.push(error.message)}}console.log(JSON.stringify(results));
""")
        self.assertEqual(result, ["操作失败，请刷新状态后重试。", "通知存储不可用，未执行该操作", "通知设置仅允许本机同源确认操作", "通知接口不存在，请刷新页面后重试。"])

    def test_queued_test_does_not_claim_passed_checks(self) -> None:
        result = run_helpers("""
const client=createNotificationClient({fetcher:async(url,options)=>({ok:true,json:async()=>options.method==='GET'?fixture:{queued:true,event_id:'synthetic-event'}}),onState:()=>{},onError:()=>{},newKey:()=> 'synthetic'});
await client.refresh();const reply=await client.post('test-email',{confirmed:true});console.log(JSON.stringify({reply,checks:client.state().checks}));
""")
        self.assertTrue(result["reply"]["queued"])
        self.assertEqual(result["checks"], {"connection": False, "email": False})

    def test_all_endpoint_actions_poll_interval_and_replay_are_wired(self) -> None:
        page = notification_page()
        for suffix in ("config", "connection-test", "test-email", "enabled", "replay"):
            self.assertIn(f"post('{suffix}'", page)
        for fragment in ("10000", "crypto.randomUUID()", "raw_crossings", "merged_events", "invalid_samples", "sample_count", 'type="date"', "confirmed:true"):
            self.assertIn(fragment, page)

    def test_concurrent_writes_waiting_for_status_do_not_both_submit(self) -> None:
        result = run_helpers("""
let release,posts=0;
const client=createNotificationClient({fetcher:async(url,options)=>{if(options.method==='GET')return new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>fixture})});posts++;return {ok:true,json:async()=>({queued:true})}},onState:()=>{},onError:()=>{},newKey:()=> 'synthetic'});
const refresh=client.refresh();const one=client.post('connection-test',{confirmed:true});const two=client.post('connection-test',{confirmed:true}).catch(()=> 'blocked');release();await refresh;const replies=await Promise.all([one,two]);console.log(JSON.stringify({posts,blocked:replies[1]==='blocked'}));
""")
        self.assertEqual(result, {"posts": 1, "blocked": True})


class NotificationPageDomTests(unittest.TestCase):
    def test_replay_failure_reason_persists_in_result_and_feedback_after_poll(self) -> None:
        result = run_page("""
await client.refresh();nodes['replay-symbol'].value='510300';nodes['replay-date'].value='2026-09-03';failPost=true;failureBody={error:'replay_unavailable',message:'该日没有通过校验的分钟历史'};
nodes['replay-form'].listeners.submit({preventDefault(){}});await drain();const before=nodes['replay-output'].textContent;await client.refresh();
console.log(JSON.stringify({before,after:nodes['replay-output'].textContent,feedback:nodes['action-feedback'].textContent,transientErrorCleared:nodes['page-error'].hidden}));
""")
        self.assertIn("该日没有通过校验的分钟历史", result["before"])
        self.assertEqual(result["after"], result["before"])
        self.assertIn("该日没有通过校验的分钟历史", result["feedback"])
        self.assertTrue(result["transientErrorCleared"])

    def test_status_shows_available_count_and_clears_it_on_failure(self) -> None:
        result = run_page("""
current.items.push({...current.items[0],symbol:'510500',eligible:false});await client.refresh();const count=nodes['eligible-count'].textContent;failGet=true;await client.refresh();console.log(JSON.stringify({count,failed:nodes['eligible-count'].textContent}));
""")
        self.assertEqual(result, {"count": "可用标的：1 / 2", "failed": "可用标的：待确认"})

    def test_poll_retains_unsaved_text_toggles_and_rule_dom(self) -> None:
        result = run_page("""
await client.refresh();nodes.sender.value='draft@example.invalid';nodes.secret.value='synthetic-only';nodes['anomaly-enabled'].checked=false;const previous=nodes['rule-rows'].innerHTML;
current.items[0].name='新显示名称';current.config.sender='n***@163.com';await client.refresh();
console.log(JSON.stringify({sender:nodes.sender.value,secret:nodes.secret.value,anomaly:nodes['anomaly-enabled'].checked,rulesRetained:previous===nodes['rule-rows'].innerHTML,placeholder:nodes.sender.placeholder}));
""")
        self.assertEqual(result, {"sender": "draft@example.invalid", "secret": "synthetic-only", "anomaly": False,
                                  "rulesRetained": True, "placeholder": "s***@163.com"})

    def test_cancelling_explicit_confirmation_sends_nothing(self) -> None:
        result = run_page("""
current.secret_configured=true;current.checks={connection:true,email:true};await client.refresh();confirmAnswer=false;
nodes['test-email'].listeners.click();nodes.enable.listeners.click();await drain();
console.log(JSON.stringify({confirmCalls,posts:requests.filter(row=>row.method==='POST').length}));
""")
        self.assertEqual(result, {"confirmCalls": 2, "posts": 0})

    def test_save_clears_secret_even_on_failure_and_displays_error(self) -> None:
        result = run_page("""
await client.refresh();nodes.secret.value='synthetic-only';failPost=true;nodes['config-form'].listeners.submit({preventDefault(){}});await drain();
console.log(JSON.stringify({secret:nodes.secret.value,error:nodes['page-error'].textContent,visible:!nodes['page-error'].hidden,payload:requests.find(row=>row.method==='POST').body}));
""")
        self.assertEqual(result["secret"], "")
        self.assertTrue(result["visible"])
        self.assertIn("操作未完成", result["error"])
        self.assertEqual(result["payload"]["secret"], "synthetic-only")
        self.assertNotIn("enabled", result["payload"])
        self.assertNotIn("sender", result["payload"])

    def test_status_failure_removes_previous_status_and_disables_all_actions(self) -> None:
        result = run_page("""
await client.refresh();failGet=true;await client.refresh();
console.log(JSON.stringify({disabled:document.querySelectorAll('[data-write]').every(node=>node.disabled),mode:nodes.mode.textContent,error:nodes['page-error'].textContent,events:nodes['event-rows'].innerHTML}));
""")
        self.assertTrue(result["disabled"])
        self.assertIn("待确认", result["mode"])
        self.assertIn("暂不可用", result["error"])
        self.assertIn("暂不可用", result["events"])


if __name__ == "__main__":
    unittest.main()
