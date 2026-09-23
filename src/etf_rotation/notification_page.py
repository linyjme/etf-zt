"""Local-only notification settings, observation status and read-only replay UI."""

NOTIFICATION_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>通知中心 · 本地监控</title>
<style>
:root{color-scheme:dark;--bg:#07111f;--panel:#101d30;--line:#293d58;--text:#e9f0fa;--muted:#91a3bb;--blue:#65a8ff;--amber:#f7c975;--red:#ffadb5}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#152945,var(--bg) 46%);color:var(--text);font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif}a{color:#acd1ff}.shell{width:100%;max-width:1480px;margin:auto;padding:22px 18px 36px;min-width:0}h1{font-size:27px;line-height:1.3;margin:0}h2{font-size:18px;margin:0 0 12px}h3{font-size:15px;margin:18px 0 10px}p{margin:8px 0}.muted,.subtitle,.hint{color:var(--muted);font-size:13px}.subtitle{margin-bottom:20px}
nav[aria-label="监控模式"]{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}nav[aria-label="监控模式"] a{border:1px solid var(--line);border-radius:999px;padding:6px 12px;text-decoration:none;white-space:nowrap}nav[aria-label="监控模式"] a[aria-current=page]{border-color:var(--blue);background:#18345a;color:white}
.panel{background:linear-gradient(145deg,#12223a,#0c1728);border:1px solid var(--line);border-radius:14px;padding:22px;margin-top:18px;min-width:0;box-shadow:0 12px 38px #0004}.section-head,.actions,.checks,.replay-form{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.section-head{justify-content:space-between}.section-head h2{margin:0}.mode{display:inline-block;border:1px solid #45658c;border-radius:999px;padding:4px 12px;color:#b9d9ff}.mode[data-mode=ENABLED]{border-color:#4b8976;color:#9fdec8}.mode[data-mode=ERROR]{border-color:#91454e;color:var(--red)}.callout{border-left:3px solid var(--amber);padding:8px 12px;margin:14px 0;color:#f7dca9;background:#352d1c44}.checks{gap:8px 22px;margin:14px 0;color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 20px}.field{display:flex;flex-direction:column;gap:5px;min-width:0}.field input{width:100%}input,select,button{font:inherit}input,select{border:1px solid #385271;border-radius:7px;padding:9px 10px;min-width:0;background:#091726;color:var(--text)}input::placeholder{color:#91a3bb}input[type=checkbox]{accent-color:var(--blue);width:17px;height:17px;padding:0;vertical-align:middle}button{border:1px solid #3c6ca7;background:#132842;color:#b9d9ff;border-radius:8px;padding:8px 15px;cursor:pointer}button:disabled,input:disabled,select:disabled{opacity:.5;cursor:not-allowed}button.primary{background:#1d4779;border-color:#608bba;color:white}button.danger{border-color:#88554e;color:#ffc3b7}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible,.table-scroll:focus-visible{outline:3px solid #91c7ff;outline-offset:3px}.actions{margin-top:16px}.error{padding:12px 15px;margin-top:16px;border:1px solid #91454e;border-radius:8px;background:#371c27;color:var(--red);overflow-wrap:anywhere}.feedback{color:#b9d9ff;overflow-wrap:anywhere}.toggle{display:inline-flex;align-items:center;gap:8px}.subscriptions{display:flex;gap:14px 28px;flex-wrap:wrap;margin:14px 0}.disabled-option{color:var(--muted)}
.table-scroll{width:100%;min-width:0;overflow-x:auto;margin:16px 0 10px;border:1px solid var(--line);border-radius:8px}table{width:100%;min-width:850px;border-collapse:collapse;font-variant-numeric:tabular-nums}caption{text-align:left;padding:10px 12px;color:var(--muted);font-size:13px}th,td{text-align:left;padding:12px;border-top:1px solid var(--line);vertical-align:top}th{background:#0a1626;color:#b9cde4;font-size:12px;white-space:nowrap}td{overflow-wrap:anywhere}td.numeric{white-space:nowrap}td small{display:block;color:var(--muted)}.rule-table{min-width:620px}.rule-table input[type=number]{width:100px}.events-table{min-width:1000px}.events-table td{max-width:260px}.empty{padding:16px;color:var(--muted)}.replay-form .field{flex:1 1 180px}.replay-form button{align-self:flex-end}.replay-results{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:16px}.replay-results div{border:1px solid var(--line);border-radius:9px;padding:14px}.replay-results strong{display:block;font-size:22px}.replay-results span{color:var(--muted);font-size:13px}details{margin-top:16px}summary{cursor:pointer;color:#acd1ff}code{color:#b9d9ff}footer{margin-top:24px;color:var(--muted);font-size:13px}
@media(max-width:900px){.replay-results{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){.shell{padding:14px 10px 28px}.panel{padding:16px}.grid{grid-template-columns:minmax(0,1fr)}h1{font-size:24px}.actions button{flex:1 1 140px}.section-head{align-items:flex-start}.replay-form button{width:100%}.replay-results{gap:8px}.replay-results div{padding:10px}}
</style>
</head>
<body>
<main class="shell">
<nav aria-label="监控模式"><a href="/swing">波段监控</a><a href="/">做T监控</a><a href="/pr">PR估值</a><a href="/notifications" aria-current="page">通知中心</a></nav>
<header><h1>通知中心</h1><p class="subtitle">持仓 ETF 异动与监控故障 · 独立通知，不改变策略或账户</p></header>
<div id="page-error" class="error" role="alert" hidden></div>
<section class="panel" id="notification-status" aria-labelledby="status-title">
<div class="section-head"><h2 id="status-title">通知状态</h2><span class="mode" id="mode" aria-live="polite">待确认 · 正在读取</span></div>
<p class="callout" id="mode-detail">尚未确认当前状态，所有操作暂不可用。</p>
<div class="checks"><span id="eligible-count">可用标的：待确认</span><span id="secret-status">授权码：待确认</span><span id="connection-status">连接测试：尚未通过</span><span id="email-status">测试邮件：尚未通过</span></div>
<p class="muted" id="last-checked">最近检查：—</p>
<div class="actions"><button id="enable" data-write type="button" class="primary" disabled>启用通知</button><button id="pause" data-write type="button" class="danger" disabled>暂停通知</button><button id="refresh" type="button">刷新状态</button></div>
<p class="feedback" id="action-feedback" aria-live="polite"></p>
<div class="table-scroll" tabindex="0" role="region" aria-label="持仓 ETF 通知观察状态，可横向滚动"><table><caption>只展示已持仓 ETF；暂不可用项目仍保留，不能触发异动邮件。</caption><thead><tr><th>名称 / 代码</th><th>通知资格</th><th>数据状态</th><th>最近五分钟变化</th><th>观察结果 / 原因</th><th>行情时点</th></tr></thead><tbody id="observation-rows"><tr><td colspan="6" class="empty">正在读取持仓 ETF…</td></tr></tbody></table></div>
</section>
<form id="config-form" autocomplete="off">
<section class="panel" id="mail-settings" aria-labelledby="mail-title">
<h2 id="mail-title">邮箱设置</h2><p class="muted">当前支持网易 163 邮箱：<code>smtp.163.com</code> · SSL · 465。授权码仅在本机输入，不要将授权码粘贴到聊天。</p>
<p class="hint">下方灰字仅为已保存信息的脱敏提示。留空表示不修改；填写新值后点击“保存邮箱与规则”。保存不会自动启用通知。</p>
<div class="grid">
<label class="field" for="sender">新发件邮箱<input id="sender" data-write type="email" autocomplete="off" placeholder="未设置；留空不修改" disabled></label>
<label class="field" for="recipient">新收件邮箱<input id="recipient" data-write type="email" autocomplete="off" placeholder="未设置；留空不修改" disabled></label>
<label class="field" for="username">新登录用户名<input id="username" data-write type="text" autocomplete="off" placeholder="未设置；留空不修改" disabled></label>
<label class="field" for="secret">新邮箱授权码<input id="secret" data-write type="password" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="仅输入新的授权码；留空保留" disabled></label>
</div>
<div class="actions"><button id="connection-test" data-write type="button" disabled>测试连接（不发邮件）</button><button id="test-email" data-write type="button" disabled>发送测试邮件…</button></div>
<p class="hint">测试使用已保存的设置。连接通过后，再发送测试邮件；“邮件服务器已接受”不代表邮件已送达，请同时检查收件箱与垃圾邮件。</p>
</section>
<section class="panel" id="subscription-settings" aria-labelledby="subscription-title">
<h2 id="subscription-title">订阅与规则</h2>
<div class="subscriptions"><label class="toggle"><input id="anomaly-enabled" data-write type="checkbox" disabled>ETF 五分钟异动</label><label class="toggle"><input id="health-enabled" data-write type="checkbox" disabled>监控故障与恢复</label><label class="toggle disabled-option"><input type="checkbox" disabled>主动下单提醒未启用（须完成策略接入和账户核验，不在本次范围）</label></div>
<p class="hint">阈值 1% 是未校准的试运行默认值，不是投资建议或有效策略参数；默认冷却 30 分钟。每个 ETF 独立设置；阈值 0.1%–10%，冷却 30–240 分钟。勾选“排除”后该 ETF 不参与通知。</p>
<div class="table-scroll" tabindex="0" role="region" aria-label="持仓 ETF 通知规则，可横向滚动"><table class="rule-table"><thead><tr><th>持仓 ETF / 代码</th><th>排除</th><th>阈值（%）</th><th>冷却（分钟）</th></tr></thead><tbody id="rule-rows"><tr><td colspan="4" class="empty">尚无持仓 ETF 规则。</td></tr></tbody></table></div>
<div class="actions"><button id="save-config" data-write type="submit" class="primary" disabled>保存邮箱与规则</button></div><p class="hint">页面每 10 秒更新状态，不覆盖正在编辑的内容。保存后以服务端确认的配置为准。</p>
</section>
</form>
<section class="panel" id="notification-records" aria-labelledby="records-title">
<h2 id="records-title">记录与历史回放</h2><p class="hint">已排队 ≠ 已提交；服务器已接受 ≠ 已送达。发送结果未知时不会盲目重发。</p>
<div class="table-scroll" tabindex="0" role="region" aria-label="通知记录，可横向滚动"><table class="events-table"><thead><tr><th>创建时间</th><th>类型 / 标的</th><th>方向</th><th>处理状态</th><th>尝试次数</th><th>说明 / 有效期</th></tr></thead><tbody id="event-rows"><tr><td colspan="6" class="empty">尚无通知记录。</td></tr></tbody></table></div>
<div class="actions"><button id="previous-page" type="button" disabled>上一页</button><span class="muted" id="record-page">第 1 页 · 每页 50 条</span><button id="next-page" type="button" disabled>下一页</button></div>
<details><summary>历史回放 · 只计算，不发送邮件</summary><p class="hint">选择持仓 ETF 与历史交易日，使用已保存的阈值与冷却规则检查已归档分钟数据。回放不会创建实时通知或修改策略。</p>
<form id="replay-form" class="replay-form"><label class="field" for="replay-symbol">持仓 ETF<select id="replay-symbol" data-write disabled><option value="">暂无可选 ETF</option></select></label><label class="field" for="replay-date">历史交易日<input id="replay-date" data-write type="date" required disabled></label><button id="replay" data-write type="submit" disabled>运行只读回放</button></form><div id="replay-output" aria-live="polite"></div></details>
</section>
<footer>通知由本机后台服务运行，关闭浏览器不影响检测。电脑保持唤醒、联网并保持本地服务运行；休眠或关机期间无法通知。邮件可能延迟、丢失或进入垃圾箱，本功能仅供观察，不是交易指令或风险控制保证。</footer>
<noscript><p>请启用 JavaScript，以读取本地通知状态。</p></noscript>
</main>
<script>
/* NOTIFICATION_PAGE_HELPERS_START */
'use strict';
const NOTIFICATION_STATE_ERROR='通知状态暂不可用，已禁用操作。请刷新重试。';
function escapeNotificationText(value){return String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))}
function notificationNumber(value,digits=2){return typeof value==='number'&&Number.isFinite(value)?value.toLocaleString('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits}):'—'}
function notificationLabel(value){
  const marketLabels={LUNCH_BREAK:'午间休市',OUTAGE:'行情断流',DELAYED:'行情延迟',DATA_ERROR:'行情质量异常',MISSING:'行情未接通',OBSERVED:'仅记录，未发送','':'—'};
  if(Object.hasOwn(marketLabels,value))return marketLabels[value];
  const labels={OBSERVATION:'仅观察',ENABLED:'通知已启用',PAUSED:'通知已暂停',ERROR:'通知异常',READ_ONLY:'只读模式',SERVER_ACCEPTED:'服务器已接受（不代表送达）',SENT:'服务器已接受（不代表送达）',CONNECTION_OK:'连接通过（未发送邮件）',UNKNOWN:'发送结果未知',QUEUED:'已排队',PENDING:'等待处理',SENDING:'正在提交',CANCELLED:'已取消',EXPIRED:'已过期',FAILED:'处理失败',SUPPRESSED:'已抑制',RETRY:'等待重试',READY:'可观察',UNAVAILABLE:'暂不可用',INSUFFICIENT:'样本不足',INACTIVE:'非活跃时段',REALTIME:'实时数据',STALE:'行情过期',DISCONNECTED:'未连接',UNVERIFIED:'待核验',PENDING_METADATA:'资料待核验',SNAPSHOT_ONLY:'仅持仓快照',OK:'正常',HEALTHY:'正常',FAULT:'监控故障',RECOVERED:'监控恢复',RECOVERY:'监控恢复',ANOMALY:'ETF 异动',HEALTH:'监控故障',TEST:'测试邮件',TEST_EMAIL:'测试邮件',CONNECTION_TEST:'连接测试',UP:'上涨',DOWN:'下跌',NONE:'—',MORNING:'上午交易时段',AFTERNOON:'下午交易时段',LUNCH:'午间休市',CLOSED:'已休市',PRE_OPEN:'开盘前',AFTER_CLOSE:'收盘后',HOLIDAY:'非交易日',NON_TRADING:'非交易时段',OPEN:'交易时段',TRADING:'交易时段'};
  return labels[value]??'待确认';
}
function notificationReason(value){
  if(value===null||value===undefined||value==='')return '—';
  if(value==='CANCELLED_BEFORE_SUBMISSION')return '提交正文前条件失效或已暂停，已取消';
  const reasons={AUTHENTICATION_FAILED:'邮箱认证失败，请检查已保存的用户名与授权码',RECIPIENT_REJECTED:'收件地址被拒绝',SENDER_REJECTED:'发件地址被拒绝',CERTIFICATE_VERIFICATION_FAILED:'安全证书验证失败，未降低连接安全性',NETWORK_ERROR:'网络连接失败',DELIVERY_OUTCOME_UNKNOWN:'邮件提交后的结果未知，不会盲目重发',INVALID_CONFIGURATION_OR_SECRET:'邮箱配置或授权码无效',INVALID_CONFIGURATION_SECRET_OR_MESSAGE:'邮箱配置、授权码或邮件内容无效',SMTP_GREETING_REJECTED:'邮件服务器拒绝建立会话',MESSAGE_REJECTED:'邮件内容被服务器拒绝',TLS_FAILED:'安全连接建立失败',SMTP_FAILED:'邮件服务处理失败'};
  if(Object.hasOwn(reasons,value))return reasons[value];
  return /^[A-Z][A-Z0-9_]*$/.test(String(value))?notificationLabel(value):String(value);
}
function parseNotificationSnapshot(payload){
  const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
  const fail=()=>{throw Error(NOTIFICATION_STATE_ERROR)};
  if(!object(payload)||!object(payload.config)||!['OBSERVATION','ENABLED','PAUSED','ERROR','READ_ONLY'].includes(payload.mode)||typeof payload.csrf_token!=='string'||!payload.csrf_token||typeof payload.secret_configured!=='boolean'||!object(payload.checks)||typeof payload.checks.connection!=='boolean'||typeof payload.checks.email!=='boolean'||!Array.isArray(payload.items)||!Array.isArray(payload.events))fail();
  const config=payload.config;
  if(typeof config.enabled!=='boolean'||typeof config.anomaly_enabled!=='boolean'||typeof config.health_enabled!=='boolean'||!Array.isArray(config.excluded_symbols)||!object(config.rules))fail();
  for(const field of ['sender','recipient','username'])if(typeof config[field]!=='string')fail();
  const seen=new Set();
  for(const item of payload.items){if(!object(item)||typeof item.symbol!=='string'||!/^\d{6}$/.test(item.symbol)||seen.has(item.symbol)||typeof item.name!=='string'||typeof item.eligible!=='boolean')fail();seen.add(item.symbol)}
  if(payload.events.some(event=>!object(event)))fail();
  return payload;
}
function notificationCanWrite(state){return !!state&&!!state.csrf_token&&!['ERROR','READ_ONLY'].includes(state.mode)}
function notificationCanEnable(state){return notificationCanWrite(state)&&state.secret_configured===true&&state.checks.connection===true&&state.checks.email===true}
function notificationItemsMarkup(items){
  if(!items.length)return '<tr><td colspan="6" class="empty">尚无持仓 ETF。导入有效持仓后再查看；本页不会用策略仓位代替账户持仓。</td></tr>';
  const esc=escapeNotificationText;
  return items.map(item=>`<tr><td>${esc(item.name)}<small>${esc(item.symbol)}</small></td><td>${item.eligible?'参与观察':'不具备通知资格'}</td><td>${notificationLabel(item.health_status)}</td><td class="numeric">${notificationNumber(item.change_pct)}${typeof item.change_pct==='number'&&Number.isFinite(item.change_pct)?'%':''}</td><td>${notificationLabel(item.status)}<small>${esc(notificationReason(item.reason))}</small></td><td>${esc(item.timestamp??'—')}</td></tr>`).join('');
}
function notificationRulesMarkup(state){
  if(!state.items.length)return '<tr><td colspan="4" class="empty">尚无持仓 ETF 规则。</td></tr>';
  const esc=escapeNotificationText;
  return state.items.map(item=>{const rule=state.config.rules[item.symbol]??{};return `<tr data-rule-symbol="${esc(item.symbol)}"><td>${esc(item.name)}<small>${esc(item.symbol)}</small></td><td><input data-write class="rule-excluded" type="checkbox" aria-label="排除 ${esc(item.name)}" ${state.config.excluded_symbols.includes(item.symbol)?'checked ':''}disabled></td><td><input data-write class="rule-threshold" type="number" min="0.1" max="10" step="0.1" value="${esc(rule.threshold_pct??1)}" aria-label="${esc(item.name)} 异动阈值百分比" required disabled></td><td><input data-write class="rule-cooldown" type="number" min="30" max="240" step="1" value="${esc(rule.cooldown_minutes??30)}" aria-label="${esc(item.name)} 冷却分钟" required disabled></td></tr>`}).join('');
}
function notificationEventsMarkup(events){
  if(!events.length)return '<tr><td colspan="6" class="empty">当前页暂无通知记录。</td></tr>';
  const esc=escapeNotificationText;
  return events.map(event=>`<tr><td>${esc(event.created_at??'—')}</td><td>${notificationLabel(event.kind)}<small>${esc(event.symbol||'系统通知')}</small></td><td>${notificationLabel(event.direction)}</td><td>${notificationLabel(event.status)}</td><td>${notificationNumber(event.attempts,0)}</td><td>${esc(notificationReason(event.reason))}<small>有效期至 ${esc(event.expires_at??'—')}</small>${notificationEventDetailMarkup(event)}</td></tr>`).join('');
}
function notificationEventDetailMarkup(event){
  const esc=escapeNotificationText,payload=event.payload&&typeof event.payload==='object'&&!Array.isArray(event.payload)?event.payload:{};
  const percent=value=>typeof value==='number'&&Number.isFinite(value)?notificationNumber(value)+'%':'—';
  const facts=[['事件编号',event.id??'—'],['名称 / 代码',`${payload.name??'系统通知'} / ${event.symbol||'—'}`],['行情时点',payload.timestamp??'—'],['检测时点',event.created_at??'—'],['有效期至',event.expires_at??'—'],['完成分钟价格',notificationNumber(payload.price,3)],['五分钟涨跌幅',percent(payload.change_pct)],['触发阈值',percent(payload.threshold_pct)],['触发依据',notificationReason(payload.reason)],['投递说明',notificationReason(event.reason)]];
  return `<details data-event-detail="${esc(event.id??'')}"><summary>查看详情</summary>${facts.map(([label,value])=>`<p class="hint">${label}：${esc(value)}</p>`).join('')}<p class="hint">仅观察，不是交易指令；阅读时条件可能已失效，请核验最新行情。</p></details>`;
}
function createNotificationEventPresenter(container){
  return events=>{
    const markup=notificationEventsMarkup(events);if(container.innerHTML===markup)return;
    const opened=new Set(Array.from(container.querySelectorAll?.('details[open]')??[]).map(detail=>detail.dataset.eventDetail));
    container.innerHTML=markup;
    for(const detail of container.querySelectorAll?.('details[data-event-detail]')??[])detail.open=opened.has(detail.dataset.eventDetail);
  };
}
function notificationConfigUpdate(form,rows,current){
  const update={anomaly_enabled:form.anomaly_enabled===true,health_enabled:form.health_enabled===true,excluded_symbols:[],rules:{...current.rules}};
  for(const field of ['sender','recipient','username','secret']){const value=typeof form[field]==='string'?form[field].trim():'';if(value)update[field]=value}
  const listed=new Set(rows.map(row=>row.symbol));
  update.excluded_symbols=current.excluded_symbols.filter(symbol=>!listed.has(symbol));
  for(const row of rows){
    const threshold=Number(row.threshold_pct),cooldown=Number(row.cooldown_minutes);
    if(!/^\d{6}$/.test(row.symbol)||!Number.isFinite(threshold)||threshold<0.1||threshold>10||!Number.isInteger(cooldown)||cooldown<30||cooldown>240)throw Error('规则无效：阈值应为 0.1%–10%，冷却应为 30–240 的整数分钟。');
    if(row.excluded)update.excluded_symbols.push(row.symbol);
    update.rules[row.symbol]={threshold_pct:threshold,cooldown_minutes:cooldown};
  }
  update.excluded_symbols=[...new Set(update.excluded_symbols)];return update;
}
function createNotificationHydrator({hydrateConfig,hydrateRules}){
  let configReady=false,rulesReady=false;
  return (state,{force=false}={})=>{
    if(!configReady||force){hydrateConfig(state);configReady=true}
    if((!rulesReady&&state.items.length>0)||force){hydrateRules(state);rulesReady=state.items.length>0}
  };
}
function notificationRequestError(body){
  const messages={forbidden:'通知设置仅允许本机同源确认操作',not_found:'通知接口不存在，请刷新页面后重试。',notification_unavailable:'通知模块未就绪；原行情服务不受影响',invalid_query:'通知记录或分页参数不可用',invalid_request:'操作未完成：请检查字段、确认状态、测试间隔及邮箱配置；只读模式不能发信',notification_storage:'通知存储不可用，未执行该操作'};
  messages.replay_unavailable='回放不可用：历史缺失、质量隔离未解除或日期/分钟校验未通过。';
  const approved=new Set([...Object.values(messages),'仅允许本机同源访问','存在未解除的分钟质量隔离问题，不能回放','该日没有通过校验的分钟历史','不能回放未来日期','回放日期无效','回放日期不是交易日或为已配置休市日','历史事务尚未完成，不能只读回放','只能回放已持有ETF']);
  if(typeof body?.message==='string'&&approved.has(body.message))return body.message;
  return typeof body?.error==='string'&&Object.hasOwn(messages,body.error)?messages[body.error]:'操作失败，请刷新状态后重试。';
}
function createNotificationClient({fetcher=fetch,onState=()=>{},onError=()=>{},newKey=()=>crypto.randomUUID()}){
  let state=null,offset=0,active=null,writing=false;
  async function request(url,options){
    const controller=new AbortController();let timer;
    const timeout=new Promise((resolve,reject)=>{timer=setTimeout(()=>{controller.abort();reject(Error('请求超时，结果尚未确认。请刷新状态；不要立即重复发送。'))},15000)});
    try{return await Promise.race([(async()=>{const reply=await fetcher(url,{...options,credentials:'same-origin',signal:controller.signal});let body;try{body=await reply.json()}catch{throw Error('服务响应无效，请刷新状态后重试。')}if(!reply.ok)throw Error(notificationRequestError(body));if(!body||typeof body!=='object'||Array.isArray(body))throw Error('服务响应无效，请刷新状态后重试。');if(body.error)throw Error(notificationRequestError(body));return body})(),timeout])}finally{clearTimeout(timer)}
  }
  async function refresh(nextOffset=offset){
    if(writing)return false;
    if(active){if(nextOffset===offset)return active;await active;return refresh(nextOffset)}
    if(!Number.isInteger(nextOffset)||nextOffset<0)return false;
    offset=nextOffset;
    active=(async()=>{try{state=parseNotificationSnapshot(await request(`/api/notifications?limit=50&offset=${offset}`,{method:'GET',cache:'no-store'}));onState(state);return true}catch{state=null;onState(null);onError(NOTIFICATION_STATE_ERROR);return false}})();
    try{return await active}finally{active=null}
  }
  async function post(action,payload){
    if(writing){const error=Error('已有操作正在处理，请稍候。');onError(error.message);throw error}
    writing=true;
    try{
      if(active)await active;
      if(!notificationCanWrite(state))throw Error(NOTIFICATION_STATE_ERROR);
      if(!['config','connection-test','test-email','enabled','replay'].includes(action))throw Error('不支持的操作。');
      if(action==='enabled'&&payload.enabled===true&&!notificationCanEnable(state))throw Error('请先配置授权码，并通过连接和测试邮件检查。');
      return await request(`/api/notifications/${action}`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.csrf_token,'Idempotency-Key':newKey()},body:JSON.stringify(payload)});
    }catch(error){onError(error.message||'操作失败，请重试。');throw error}finally{writing=false}
  }
  return {refresh,post,state:()=>state,offset:()=>offset};
}
/* NOTIFICATION_PAGE_HELPERS_END */
const byId=id=>document.getElementById(id),secretInput=byId('secret');
const presentEvents=createNotificationEventPresenter(byId('event-rows'));
let actionBusy=false,forceHydration=false,pollTimer=null,recordBusy=false;
const showError=message=>{byId('page-error').textContent=message;byId('page-error').hidden=!message};
const hydrate=createNotificationHydrator({
  hydrateConfig:state=>{for(const field of ['sender','recipient','username'])byId(field).placeholder=state.config[field]||'未设置；留空不修改';byId('anomaly-enabled').checked=state.config.anomaly_enabled;byId('health-enabled').checked=state.config.health_enabled},
  hydrateRules:state=>{byId('rule-rows').innerHTML=notificationRulesMarkup(state);const previous=byId('replay-symbol').value;byId('replay-symbol').innerHTML=state.items.length?state.items.map(item=>`<option value="${escapeNotificationText(item.symbol)}">${escapeNotificationText(item.name)} · ${escapeNotificationText(item.symbol)}</option>`).join(''):'<option value="">暂无可选 ETF</option>';if(state.items.some(item=>item.symbol===previous))byId('replay-symbol').value=previous}
});
function updateControls(){
  const state=client.state(),writable=notificationCanWrite(state)&&!actionBusy;
  document.querySelectorAll('[data-write]').forEach(element=>{element.disabled=!writable});
  byId('enable').disabled=!writable||!notificationCanEnable(state)||state?.mode==='ENABLED';
  byId('pause').disabled=!writable||state?.mode!=='ENABLED';
  byId('connection-test').disabled=!writable||!state?.secret_configured;
  byId('test-email').disabled=!writable||!state?.secret_configured||!state?.checks.connection;
  byId('replay').disabled=!writable||!byId('replay-symbol').value;
  byId('refresh').disabled=actionBusy||recordBusy;
  byId('previous-page').disabled=actionBusy||recordBusy||!state||client.offset()===0;
  byId('next-page').disabled=actionBusy||recordBusy||!state||state.events.length<50;
}
function present(state){
  if(!state){byId('eligible-count').textContent='可用标的：待确认';byId('mode').textContent='待确认 · 状态读取失败';byId('mode').dataset.mode='ERROR';byId('mode-detail').textContent=NOTIFICATION_STATE_ERROR;byId('secret-status').textContent='授权码：待确认';byId('connection-status').textContent='连接测试：待确认';byId('email-status').textContent='测试邮件：待确认';byId('observation-rows').innerHTML='<tr><td colspan="6" class="empty">状态暂不可用，旧行情已隐藏。</td></tr>';byId('event-rows').innerHTML='<tr><td colspan="6" class="empty">记录暂不可用，请刷新重试。</td></tr>';updateControls();return}
  showError(state.error?notificationReason(state.error):'');
  byId('eligible-count').textContent=`可用标的：${state.items.filter(item=>item.eligible).length} / ${state.items.length}`;
  byId('mode').textContent=notificationLabel(state.mode);byId('mode').dataset.mode=state.mode;
  const details={OBSERVATION:'仅观察，不发送自动通知。配置并完成测试后，仍需你明确启用。',ENABLED:'通知已启用；只处理合格的 ETF 异动与监控故障，不下单。',PAUSED:'通知已暂停，后台仍可观察；不会自动恢复发送。',ERROR:'通知异常，当前操作已禁用。请检查本地服务并刷新。',READ_ONLY:'只读模式：未启用后台采集或通知写入，不能配置、测试或启用。'};
  byId('mode-detail').textContent=details[state.mode];
  byId('secret-status').textContent=state.secret_configured?'授权码：已在本机配置':'授权码：尚未配置';
  byId('connection-status').textContent=state.checks.connection?'连接测试：已通过（未发送邮件）':'连接测试：尚未通过';
  byId('email-status').textContent=state.checks.email?'测试邮件：服务器已接受（不代表送达）':'测试邮件：尚未通过';
  const session=typeof state.session==='string'?state.session:state.session?.status??state.session?.state;
  byId('last-checked').textContent=`最近后台检查：${state.last_checked_at??'—'} · ${notificationLabel(session)} · 页面每 10 秒刷新`;
  hydrate(state,{force:forceHydration});forceHydration=false;
  byId('observation-rows').innerHTML=notificationItemsMarkup(state.items);presentEvents(state.events);
  byId('record-page').textContent=`第 ${Math.floor(client.offset()/50)+1} 页 · 本页 ${state.events.length} 条 · 每页 50 条`;
  updateControls();
}
const client=createNotificationClient({onState:present,onError:showError});
async function runAction(pending,operation){
  if(actionBusy)return;
  actionBusy=true;showError('');byId('action-feedback').textContent=pending;updateControls();
  try{await operation()}catch(error){const message=error.message||'操作失败，请重试。';showError(message);byId('action-feedback').textContent=`操作未确认：${message}`}finally{actionBusy=false;updateControls()}
}
byId('config-form').addEventListener('submit',event=>{event.preventDefault();runAction('正在保存，尚未确认…',async()=>{
  const form={anomaly_enabled:byId('anomaly-enabled').checked,health_enabled:byId('health-enabled').checked};for(const field of ['sender','recipient','username','secret'])form[field]=byId(field).value;
  const rows=Array.from(document.querySelectorAll('[data-rule-symbol]')).map(row=>({symbol:row.dataset.ruleSymbol,excluded:row.querySelector('.rule-excluded').checked,threshold_pct:row.querySelector('.rule-threshold').value,cooldown_minutes:row.querySelector('.rule-cooldown').value}));
  const state=client.state();if(!state)throw Error(NOTIFICATION_STATE_ERROR);
  try{await client.post('config',notificationConfigUpdate(form,rows,state.config))}finally{secretInput.value='';form.secret=''}
  for(const field of ['sender','recipient','username'])byId(field).value='';
  forceHydration=true;const refreshed=await client.refresh();byId('action-feedback').textContent=refreshed?'配置已保存；未自动启用通知。邮箱变更后需重新测试。':'保存请求已处理，但最新状态待确认，请刷新。';
})});
byId('connection-test').addEventListener('click',()=>runAction('连接测试请求处理中…',async()=>{await client.post('connection-test',{confirmed:true});byId('action-feedback').textContent='连接测试已排队；不会发送邮件，等待后台检查结果。';await client.refresh()}));
byId('test-email').addEventListener('click',()=>{if(!window.confirm('将使用已保存的设置向收件邮箱发送一封测试邮件。是否继续？'))return;runAction('测试邮件请求处理中…',async()=>{await client.post('test-email',{confirmed:true});byId('action-feedback').textContent='测试邮件已排队；请等待检查状态更新。服务器接受不代表送达。';await client.refresh()})});
byId('enable').addEventListener('click',()=>{if(!window.confirm('确认启用 ETF 异动及监控故障邮件？本机需保持唤醒和联网；这不会下单。'))return;runAction('启用请求处理中，状态待确认…',async()=>{await client.post('enabled',{enabled:true,confirmed:true});await client.refresh();byId('action-feedback').textContent=client.state()?.mode==='ENABLED'?'通知已启用。':'启用结果待确认，请查看最新状态。'})});
byId('pause').addEventListener('click',()=>runAction('正在暂停通知，状态待确认…',async()=>{await client.post('enabled',{enabled:false,confirmed:true});await client.refresh();byId('action-feedback').textContent=client.state()&&client.state().mode!=='ENABLED'?'通知已暂停。':'暂停结果待确认，请刷新状态。'}));
byId('replay-form').addEventListener('submit',event=>{event.preventDefault();runAction('正在计算历史回放，不发送邮件…',async()=>{
  const symbol=byId('replay-symbol').value,date=byId('replay-date').value;if(!/^\d{6}$/.test(symbol)||!/^\d{4}-\d{2}-\d{2}$/.test(date))throw Error('请选择 ETF 与完整的历史交易日。');
  byId('replay-output').textContent='正在计算…';
  try{const result=await client.post('replay',{symbol,date});const fields=[['raw_crossings','原始越线次数'],['merged_events','合并后事件'],['invalid_samples','无效样本'],['sample_count','样本总数']];if(fields.some(([key])=>!Number.isInteger(result[key])||result[key]<0))throw Error('回放响应不完整，请重试。');byId('replay-output').innerHTML=`<p class="hint">${escapeNotificationText(symbol)} · ${escapeNotificationText(date)} · 历史模拟，不发送邮件</p><div class="replay-results">${fields.map(([key,label])=>`<div><span>${label}</span><strong>${notificationNumber(result[key],0)}</strong></div>`).join('')}</div>`;byId('action-feedback').textContent='历史回放完成，没有发送邮件。'}catch(error){byId('replay-output').textContent=`回放未完成：${error.message||'请稍后重试。'}`;throw error}
})});
async function refreshPage(offset){if(recordBusy||actionBusy)return;recordBusy=true;updateControls();try{await client.refresh(offset)}finally{recordBusy=false;updateControls()}}
byId('refresh').addEventListener('click',()=>refreshPage());byId('previous-page').addEventListener('click',()=>refreshPage(Math.max(0,client.offset()-50)));byId('next-page').addEventListener('click',()=>refreshPage(client.offset()+50));
async function poll(){clearTimeout(pollTimer);if(!document.hidden)await refreshPage();pollTimer=setTimeout(poll,10000)}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});window.addEventListener('pagehide',()=>{clearTimeout(pollTimer);secretInput.value=''});poll();
</script>
</body>
</html>
"""
