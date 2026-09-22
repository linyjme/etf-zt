from __future__ import annotations

from .constants import (
    DEFAULT_GRID_WIDTH_PCT,
    DELAYED_MAX_AGE_SECONDS,
    RANGE_CONFIRMATIONS,
    RANGE_WINDOW_MINUTES,
    REALTIME_MAX_AGE_SECONDS,
    TREND_CONFIRMATIONS,
)


_PAGE_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>本地做T监控</title>
<style>
:root{color-scheme:dark;--bg:#08111f;--panel:#111d2e;--line:#26364e;--text:#e6edf7;--muted:#8fa1b8;--up:#ff5964;--down:#32d296;--wait:#f4bf4f;--stale:#ff3b30}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#12233b,var(--bg) 44%);color:var(--text);font:14px system-ui,"Microsoft YaHei",sans-serif}a{color:#acd1ff}.shell{max-width:1380px;margin:auto;padding:28px 18px}nav[aria-label="监控模式"]{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}nav[aria-label="监控模式"] a{border:1px solid var(--line);border-radius:999px;padding:6px 12px;text-decoration:none}nav[aria-label="监控模式"] a[aria-current=page]{border-color:#5c8fc5;background:#162b45;color:white}header{display:flex;justify-content:space-between;gap:16px;align-items:end;margin-bottom:18px}h1{font-size:26px;margin:0 0 6px}.sub,.time,.hint{color:var(--muted)}.badge{display:inline-block;border:1px solid #365271;border-radius:99px;padding:5px 10px;color:#9ac8ff}.status{display:flex;align-items:center;gap:8px}.dot{width:9px;height:9px;border-radius:50%;background:var(--wait)}.dot.live{background:var(--down);box-shadow:0 0 12px var(--down)}.status.stale,.status.outage,.status.missing{color:var(--stale);font-weight:700}.status.delayed,.status.paused,.status.closed,.status.unknown{color:var(--wait);font-weight:700}.dot.stale,.dot.outage,.dot.missing{background:var(--stale);box-shadow:0 0 14px var(--stale)}.dot.delayed{background:#f4bf4f}.dot.paused{background:#6ea8d9}.dot.closed{background:#75859b}.dot.unknown{background:#a58acb}#errors,#form-error{color:#ff9b9b;margin:8px 0}#form-error.success{color:#74f0c4}.layout{display:grid;grid-template-columns:280px minmax(0,1fr);gap:18px;align-items:start}.card,.sidebar{background:linear-gradient(145deg,#132238,#0d1727);border:1px solid var(--line);border-radius:14px;padding:17px;box-shadow:0 12px 40px #0004}.sidebar{position:sticky;top:18px;padding:14px}.sidebar-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}.sidebar h2{font-size:17px;margin:0}.watch-count{color:var(--muted);font-size:12px}.watch-list{display:grid;gap:6px;max-height:52vh;overflow:auto;margin-bottom:14px}.watch-item{width:100%;display:flex;align-items:center;justify-content:space-between;gap:8px;border:1px solid transparent;border-radius:9px;background:#081321;color:var(--text);padding:9px 10px;text-align:left;cursor:pointer}.watch-item:hover{border-color:#365271}.watch-item.active{border-color:#5c8fc5;background:#162b45}.watch-item-main{min-width:0}.watch-item-name{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:700}.watch-item-symbol{display:block;color:var(--muted);font-size:11px;margin-top:2px}.watch-item-state{flex:none;width:8px;height:8px;border-radius:50%;background:var(--wait)}.watch-item-state.live{background:var(--down)}.watch-item-state.delayed{background:#f4bf4f}.watch-item-state.outage,.watch-item-state.missing{background:var(--stale)}.watch-item-state.paused{background:#6ea8d9}.watch-item-state.closed{background:#75859b}.watch-item-state.unknown{background:#a58acb}.sidebar-controls{display:flex;align-items:center;justify-content:space-between;gap:8px;border-top:1px solid var(--line);padding-top:12px}.missing-toggle{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px}.compact-form{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) auto;gap:6px;margin-top:10px}.compact-form input{min-width:0;width:100%;border:1px solid var(--line);border-radius:7px;background:#081321;color:var(--text);padding:8px;font:inherit;outline:none}.compact-form input:focus{border-color:#5c8fc5}.compact-form button{border:0;border-radius:7px;background:#2f7ed8;color:white;padding:8px 10px;font:700 13px inherit;cursor:pointer}.compact-form button:disabled{cursor:wait;opacity:.6}.history-picker{display:grid;gap:5px;margin-top:12px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}.history-picker select{width:100%;border:1px solid var(--line);border-radius:7px;background:#081321;color:var(--text);padding:8px}.detail{min-width:0;overflow-anchor:none}.top{display:flex;justify-content:space-between;gap:12px}.symbol{color:var(--muted);font-size:12px}.name{font-size:18px;font-weight:700;margin-top:2px}.price{text-align:right;font:700 24px ui-monospace,monospace}.pct{font:13px ui-monospace,monospace}.up{color:var(--up)}.down{color:var(--down)}.wait{color:var(--wait)}.signal{margin:14px 0 8px;font-weight:700}.candidate-alert{display:flex;align-items:center;gap:10px;margin:10px 0 12px;padding:11px 13px;border:1px solid #f4bf4f;border-radius:9px;background:#3b2b0c;color:#ffd76a;font-weight:700}.regime-alert{margin:10px 0;padding:10px 12px;border:1px solid #365271;border-radius:9px;background:#0b1d31;font-weight:700}.regime-alert.uptrend{border-color:#ff5964;color:#ff9da5}.regime-alert.downtrend{border-color:#32d296;color:#74f0c4}.regime-alert.range{border-color:#f4bf4f;color:#ffd76a}.regime-alert.uncertain{color:#b8c5d8}.watch-item.opportunity{border-color:#f4bf4f;box-shadow:inset 3px 0 #f4bf4f}.watch-item-state.opportunity{background:#f4bf4f;box-shadow:0 0 10px #f4bf4f}.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px}.meta div{background:#081321;border-radius:8px;padding:8px;overflow-wrap:anywhere}.meta span{display:block;color:var(--muted);font-size:11px;margin-bottom:3px}.market-time{margin-top:16px}.market-time.stale{color:var(--stale);font-weight:700}.stale-banner{display:none;color:var(--stale);font-weight:700;margin:10px 0}.stale-banner.visible{display:block}.backtest,.replay,.alerts,.valuation{border-top:1px solid var(--line);margin-top:16px;padding-top:14px}.backtest h3,.replay h3,.alerts h3,.valuation h3{font-size:15px;margin:0 0 8px}.alert-list{display:grid;gap:6px;max-height:240px;overflow:auto}.alert-row{background:#081321;border-radius:8px;padding:8px;font-size:12px}.alert-row strong{color:#ffd76a}.alert-row span{color:var(--muted);display:block;margin-top:3px}.chart-wrap{position:relative;margin:8px 0 12px}svg{width:100%;height:260px;display:block;overflow:visible}.price-line{fill:none;stroke:#fff;stroke-width:2;vector-effect:non-scaling-stroke}.average-line{fill:none;stroke:#f4d03f;stroke-width:2;vector-effect:non-scaling-stroke}.zero-line{stroke:#92a3ba;stroke-dasharray:4 4}.three-grid{fill:none;stroke:#5c8fc5;stroke-dasharray:5 4}.five-grid{fill:none;stroke:#b679ff;stroke-dasharray:7 4}.axis-grid{stroke:#26364e;stroke-width:1}.x-axis text,.y-axis text{fill:#8fa1b8;font-size:11px}.x-axis line,.y-axis line{stroke:#50647f}.chart-overlay{fill:transparent;pointer-events:all}.crosshair{display:none;stroke:#8fa1b8;stroke-dasharray:3 3}.chart-tooltip{display:none;position:absolute;z-index:2;pointer-events:none;min-width:205px;padding:8px 10px;border:1px solid #496580;border-radius:8px;background:#07111eea;color:var(--text);font:12px ui-monospace,monospace;box-shadow:0 8px 28px #0008;white-space:pre-line}.empty{padding:40px;text-align:center;color:var(--muted)}@media(max-width:850px){.layout{grid-template-columns:1fr}.sidebar{position:static}.watch-list{max-height:260px}}@media(max-width:600px){header{align-items:start;flex-direction:column}.meta{grid-template-columns:1fr 1fr}.compact-form{grid-template-columns:1fr auto}.compact-form #watch-name{grid-column:1/-1;grid-row:2}}
.status.data-error{color:var(--stale);font-weight:700}.dot.data-error,.watch-item-state.data-error{background:var(--stale)}.quality-notice{margin:10px 0 16px;padding:10px 12px;border:1px solid #775f36;border-radius:9px;background:#252315;color:#f2d69b;overflow-wrap:anywhere}.quality-notice div,.quality-notice details{margin-top:5px}.quality-notice summary{cursor:pointer}.quality-notice ul{margin:6px 0;padding-left:20px;max-height:160px;overflow:auto}
</style>
</head>
<body><main class="shell"><nav aria-label="监控模式"><a href="/" aria-current="page">做T监控</a><a href="/swing">波段监控</a><a href="/pr">PR估值</a><a href="/notifications">通知中心</a></nav><header><div><h1>本地做T监控</h1><div class="sub">候选观察 · 增量行情 · 只读交易</div></div><div><span class="badge">仅监控，不自动交易</span><div id="connection-status" class="status"><i id="dot" class="dot"></i><span id="status">正在连接</span></div></div></header><div id="errors"></div><section id="quality-notice" class="quality-notice" role="status" hidden></section><div id="stale-banner" class="stale-banner" role="alert">当前行情数据已过期，请勿按对应价格操作</div><div class="layout"><aside class="sidebar"><div class="sidebar-head"><h2>已监控</h2><span id="watch-count" class="watch-count">0 项</span></div><nav id="watch-list" class="watch-list" aria-label="已监控标的"><div class="empty">正在载入</div></nav><div class="sidebar-controls"><strong>添加标的</strong><label class="missing-toggle"><input id="show-missing" type="checkbox" checked>显示无行情</label></div><form id="watch-form" class="compact-form"><input id="watch-symbol" name="symbol" inputmode="numeric" maxlength="6" pattern="[0-9]{6}" placeholder="代码" aria-label="代码" required><input id="watch-name" name="name" maxlength="50" placeholder="名称（可选）" aria-label="名称"><button id="watch-submit" type="submit">添加</button></form><div id="form-error" role="alert"></div><div class="hint">默认一格 __ONE_GRID_DISPLAY__ · 震荡两格 __TWO_GRID_DISPLAY__ · 三格 __THREE_GRID_DISPLAY__ · 五格 __FIVE_GRID_DISPLAY__；仅保存监控标的。</div><label class="history-picker">历史行情日期<select id="history-date"><option value="">实时行情</option></select></label></aside><section id="detail" class="detail"><div class="card empty">正在载入行情</div></section></div><div id="refresh-time" class="time">页面刷新时间：尚未刷新</div></main>
<script>
const detail=document.querySelector('#detail'),watchList=document.querySelector('#watch-list'),watchCount=document.querySelector('#watch-count'),showMissing=document.querySelector('#show-missing'),connectionStatus=document.querySelector('#connection-status'),statusNode=document.querySelector('#status'),dot=document.querySelector('#dot'),errors=document.querySelector('#errors'),refreshTimeNode=document.querySelector('#refresh-time'),staleBanner=document.querySelector('#stale-banner'),watchForm=document.querySelector('#watch-form'),watchSymbol=document.querySelector('#watch-symbol'),watchName=document.querySelector('#watch-name'),watchSubmit=document.querySelector('#watch-submit'),formError=document.querySelector('#form-error'),historyDate=document.querySelector('#history-date');
const STALE_AFTER_MS=60000,DEFAULT_GRID_WIDTH_PCT=__DEFAULT_GRID_WIDTH_PCT__;
const num=(v,digits=3)=>v==null||!Number.isFinite(Number(v))?'—':Number(v).toFixed(digits),pct=v=>v==null||!Number.isFinite(Number(v))?'—':(Number(v)*100).toFixed(2)+'%';
let latestData=null,backtests=new Map(),replays=new Map(),selectedSymbol=null,alertHistoryCache=new Map(),dailyHistoryCache=new Map(),valuationCache=new Map(),alertRequestSequence=0,dailyRequestSequence=0,valuationRequestSequence=0,quoteRequestSequence=0;
const quotePoints=new Map(),quoteRevisions=new Map();
let feedState={ready:false,summaryReady:false,message:'正在连接'};

/* PAGE_HELPERS_START */
const REALTIME_MAX_AGE_SECONDS=__REALTIME_MAX_AGE_SECONDS__,DELAYED_MAX_AGE_SECONDS=__DELAYED_MAX_AGE_SECONDS__;
const RANGE_WINDOW_MINUTES=__RANGE_WINDOW_MINUTES__,RANGE_CONFIRMATIONS=__RANGE_CONFIRMATIONS__,TREND_CONFIRMATIONS=__TREND_CONFIRMATIONS__;
const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function nextFeedState(current,event,message=''){
  const state=current||{ready:false,summaryReady:false,message:''};
  if(event==='FAIL')return {ready:false,summaryReady:false,message:message||'行情连接中断'};
  if(event==='SUMMARY')return {...state,summaryReady:true};
  if(event==='QUOTES')return state.ready||state.summaryReady?{ready:true,summaryReady:true,message:''}:state;
  if(event==='POLL')return {ready:true,summaryReady:true,message:''};
  return state;
}

function marketPresentation(item,nowMs=Date.now(),feed={ready:true,message:''}){
  const health=item.health_status||'UNKNOWN',missing=item.status==='MISSING_QUOTE'||health==='MISSING',missingOverrides=!['CLOSED','LUNCH_BREAK','DATA_ERROR'].includes(health),healthKey=missing&&missingOverrides?'MISSING':health;
  const legacy=!Object.prototype.hasOwnProperty.call(item,'timestamp_basis'),minuteStart=item.timestamp_basis==='MINUTE_START';
  const marketAt=typeof item.timestamp==='string'&&item.timestamp?new Date(item.timestamp):null,marketMs=marketAt&&marketAt.getTime();
  // Only the quote minute can age the signal; collection and page times cannot renew it.
  const age=Number.isFinite(marketMs)?nowMs-(marketMs+(minuteStart?60000:0)):Infinity,validTime=(legacy||minuteStart)&&Number.isFinite(age)&&age>=0;
  const realtime=healthKey==='REALTIME'&&item.status==='OK',stale=realtime&&(!validTime||(legacy?(age<0||age>STALE_AFTER_MS):age>REALTIME_MAX_AGE_SECONDS*1000)),candidateAction=item.action==='BUY_CANDIDATE'||item.action==='SELL_CANDIDATE',candidate=Boolean(feed.ready)&&realtime&&!stale&&candidateAction;
  const states={
    REALTIME:{statusText:'实时监控中',dotClass:'live'},
    DELAYED:{statusText:'行情延迟',dotClass:'delayed'},
    OUTAGE:{statusText:'行情断流',dotClass:'outage'},
    DATA_ERROR:{statusText:'数据校验异常',dotClass:'data-error'},
    LUNCH_BREAK:{statusText:'午间休市',dotClass:'paused'},
    CLOSED:{statusText:'已收盘',dotClass:'closed'},
    MISSING:{statusText:'行情缺失',dotClass:'missing'},
    UNKNOWN:{statusText:'行情状态未知',dotClass:'unknown'},
  };
  let displayHealth=healthKey;
  if(!legacy&&['REALTIME','DELAYED'].includes(healthKey)){
    if(!validTime)displayHealth='UNKNOWN';
    else if(age>DELAYED_MAX_AGE_SECONDS*1000)displayHealth='OUTAGE';
    else if(age>REALTIME_MAX_AGE_SECONDS*1000)displayHealth='DELAYED';
  }
  const state=!feed.ready?{statusText:feed.message||'行情连接中断',dotClass:'outage'}:legacy&&stale?{statusText:'当前行情已过期',dotClass:'stale'}:(Object.prototype.hasOwnProperty.call(states,displayHealth)?states[displayHealth]:states.UNKNOWN);
  const unsafe=!feed.ready||!realtime||stale;
  return {...state,health,healthKey,realtime,stale,candidateAction,candidate,displayLabel:candidate?'做T候选':candidateAction||item.action==='DEVIATION_OBSERVE'?'偏离观察':item.label,marketAt,unsafe,warning:unsafe?state.statusText+'，候选提醒已撤销':''};
}

// Rendering may revoke an accepted quote, but only a new summary/quote pair may restore it.
const marketSafety=new Map();
let marketSummaryGeneration=0;

function acceptMarketSummary(items,revision,nowMs=Date.now()){
  const generation=++marketSummaryGeneration;
  for(const item of items){
    const previous=marketSafety.get(item.symbol),state=marketPresentation(item,nowMs);
    const previousState=previous&&marketPresentation(previous.item,Math.max(previous.checkedAt,nowMs));
    const previouslyUnsafe=previous&&(previous.blocked||(previousState.unsafe&&!['CLOSED','LUNCH_BREAK'].includes(previousState.healthKey)));
    const rejected=state.unsafe&&!['CLOSED','LUNCH_BREAK'].includes(state.healthKey),blocked=Boolean(previouslyUnsafe)||rejected;
    marketSafety.set(item.symbol,{item,generation,revision,checkedAt:nowMs,blocked,pending:blocked&&!state.unsafe,
      rejection:rejected?state:blocked?{statusText:'等待行情重新核验',dotClass:'unknown',stale:false}:null});
  }
}

function currentMarketPresentation(item,nowMs=Date.now(),feed={ready:true,message:''}){
  if(!marketSafety.has(item.symbol))acceptMarketSummary([item],null,nowMs);
  const guard=marketSafety.get(item.symbol);
  guard.checkedAt=Math.max(guard.checkedAt,nowMs);
  const state=marketPresentation(item,guard.checkedAt,feed),quoteState=marketPresentation(item,guard.checkedAt);
  if(guard.item!==item||(quoteState.unsafe&&!['CLOSED','LUNCH_BREAK'].includes(quoteState.healthKey))){
    guard.blocked=true;guard.pending=false;
    const rejected=guard.item!==item?{statusText:'等待行情重新核验',dotClass:'unknown',stale:false}:quoteState;
    const severity={live:0,paused:0,closed:0,delayed:1,stale:2,unknown:3,missing:4,outage:4,'data-error':5};
    if(!guard.rejection||severity[rejected.dotClass]>=severity[guard.rejection.dotClass])guard.rejection=rejected;
  }
  if(!guard.blocked||!feed.ready||['CLOSED','LUNCH_BREAK'].includes(state.healthKey))return state;
  const rejected=guard.rejection||{statusText:'等待行情重新核验',dotClass:'unknown',stale:false};
  return {...state,statusText:rejected.statusText,dotClass:rejected.dotClass,stale:state.stale||rejected.stale,candidate:false,unsafe:true,
    displayLabel:state.candidateAction||item.action==='DEVIATION_OBSERVE'?'偏离观察':item.label,warning:rejected.statusText+'，候选提醒已撤销'};
}

function confirmMarketSummary(symbol,generation,points,revision,nowMs=Date.now()){
  const guard=marketSafety.get(symbol);
  if(!guard||guard.generation!==generation||!guard.pending)return false;
  currentMarketPresentation(guard.item,nowMs);
  if(!guard.pending||marketPresentation(guard.item,guard.checkedAt).unsafe)return false;
  if(!Number.isInteger(revision)||!Number.isInteger(guard.revision)||revision<guard.revision)return false;
  if(!points.some(point=>point.timestamp===guard.item.timestamp&&point.is_complete===true))return false;
  guard.blocked=false;guard.pending=false;guard.rejection=null;
  return true;
}

function rejectMarketFeed(items,message,nowMs=Date.now()){
  for(const item of items){
    currentMarketPresentation(item,nowMs);
    const guard=marketSafety.get(item.symbol);
    guard.blocked=true;guard.pending=false;
    guard.rejection=marketPresentation(item,guard.checkedAt,{ready:false,message});
  }
}

const qualityNoticeMarkupCache=new WeakMap();
function renderQualityNotice(data,node){
  if(!node)return;
  const issues=new Map(),append=(issue,symbol)=>{
    if(!issue||typeof issue!=='object'||Array.isArray(issue))return;
    const record=[issue.symbol||symbol||'未知标的',issue.timestamp||'分钟未知',issue.reason||'数据未通过校验'];
    issues.set(JSON.stringify(record),record);
  };
  for(const issue of Array.isArray(data?.validation_issues)?data.validation_issues:[])append(issue);
  for(const item of Array.isArray(data?.items)?data.items:[])for(const issue of Array.isArray(item?.validation_issues)?item.validation_issues:[])append(issue,item.symbol);
  const coverage=data?.intraday_coverage&&typeof data.intraday_coverage==='object'?data.intraday_coverage:null;
  const missing=Array.isArray(coverage?.missing_symbols)?coverage.missing_symbols:[];
  const coveragePercent=Number(coverage?.coverage_pct);
  const coverageMarkup=missing.length?`<strong>盘中数据覆盖不完整 · ${Number.isFinite(coveragePercent)?coveragePercent.toFixed(1)+'%':'—'}</strong><div>缺少 ${missing.length} 个标的：${missing.map(esc).join('、')}；缺失标的不产生候选提醒。</div>`:'';
  const validationMarkup=issues.size?`<strong>数据校验异常 · 异常分钟已隔离</strong><div>仅相关标的提醒暂停，其他有效行情继续。</div><details><summary>查看已隔离分钟（${issues.size}）</summary><ul>${[...issues.values()].map(record=>`<li>${record.map(esc).join(' · ')}</li>`).join('')}</ul></details>`:'';
  const markup=[coverageMarkup,validationMarkup].filter(Boolean).join('');
  node.hidden=!markup;
  if(qualityNoticeMarkupCache.get(node)!==markup){node.innerHTML=markup;qualityNoticeMarkupCache.set(node,markup)}
}

function shanghaiMinute(timestamp){
  const epoch=Date.parse(timestamp);if(!Number.isFinite(epoch))return null;
  const local=new Date(epoch+8*3600000),minute=local.getUTCHours()*60+local.getUTCMinutes();
  return {epoch,date:local.toISOString().slice(0,10),minute,session:minute>=570&&minute<=690?'AM':minute>=780&&minute<=900?'PM':null};
}
function minutePathBreak(previous,current){
  const before=shanghaiMinute(previous.timestamp),after=shanghaiMinute(current.timestamp);
  if(!before||!after||!before.session||!after.session||before.date!==after.date||after.epoch<=before.epoch)return true;
  // Only the complete lunch boundary may bridge the compressed session axis.
  if(before.session!==after.session)return !(before.minute===690&&after.minute===780);
  return after.epoch-before.epoch>60000;
}

function reasonText(reasons){
  const labels={
    INSUFFICIENT_SAMPLES:'连续已完成分钟样本不足',INVALID_WINDOW_VALUES:'形态窗口数据无效',
    RANGE_CONFIRMED:'震荡形态已连续确认',UPTREND_CONFIRMED:'上涨趋势已连续确认',DOWNTREND_CONFIRMED:'下跌趋势已连续确认',
    RANGE_CONFIRMATION_PENDING:'震荡条件已满足，等待连续确认',UPTREND_CONFIRMATION_PENDING:'上涨趋势条件已满足，等待连续确认',DOWNTREND_CONFIRMATION_PENDING:'下跌趋势条件已满足，等待连续确认',
    RANGE_VWAP_CROSSINGS_INSUFFICIENT:'穿越均价线次数不足',RANGE_ABOVE_VWAP_SAMPLES_INSUFFICIENT:'均价线上方停留样本不足',RANGE_BELOW_VWAP_SAMPLES_INSUFFICIENT:'均价线下方停留样本不足',
    RANGE_ONE_SIDE_DOMINANT:'价格过于集中在均价线一侧',RANGE_PATH_TOO_EFFICIENT:'价格方向性偏强，不满足震荡条件',RANGE_VWAP_SLOPE_TOO_STEEP:'均价线斜率偏大，不满足震荡条件',
    TREND_PATH_NOT_EFFICIENT:'趋势方向性不足',TREND_VWAP_SLOPE_TOO_FLAT:'均价线斜率不足以确认趋势',TREND_PRICE_DIRECTION_FLAT:'价格方向尚不明确',
    TREND_PRICE_VWAP_DIRECTION_MISMATCH:'价格与均价线方向不一致',TREND_ONE_SIDE_NOT_DOMINANT:'趋势同侧停留比例不足',TREND_HIGH_LOW_NOT_ADVANCING:'高低点未沿趋势方向推进',
    MARKET_NOT_REALTIME:'行情非实时，暂停候选提醒',MARKET_DATA_INVALID:'存在未核验的异常分钟，暂停候选提醒',REGIME_NOT_RANGE:'尚未确认震荡形态',MISSING_QUOTE:'缺少当日行情',INSUFFICIENT_FINALIZED_POINTS:'已完成分钟不足，暂不能比较偏离变化',
    INVALID_GRID_WIDTH:'格宽设置无效',INVALID_PRICE_DATA:'价格数据无效',DEVIATION_BELOW_2_GRIDS:'震荡偏离均价不足两格',DEVIATION_BELOW_3_GRIDS:'偏离均价不足三格',PREVIOUS_CLOSE_DISTANCE_BELOW_5_GRIDS:'非震荡日距昨收不足五格',
    DEVIATION_NOT_NARROWING:'偏离尚未同侧收窄，等待反转确认',COST_NOT_COVERED:'预期毛边际不足以覆盖双边成本',FAST_RISE:'短时快速上涨，暂停逆势候选',
  };
  return (Array.isArray(reasons)?reasons:[]).map(reason=>Object.prototype.hasOwnProperty.call(labels,reason)?labels[reason]:'未识别原因（'+String(reason)+'）').join(' · ');
}

function regimePresentation(item){
  const state=item.regime_state,confirmed=['RANGE','UPTREND','DOWNTREND'].includes(state),reasons=Array.isArray(item.regime_reasons)?item.regime_reasons:[];
  const progress=(value,required)=>`${typeof value==='number'&&Number.isInteger(value)&&value>=0?value:'—'} / ${required}`;
  const heading=state==='UPTREND'?'上涨趋势日':state==='DOWNTREND'?'下跌趋势日':state==='RANGE'?'震荡日':'形态未确认';
  const className=state==='UPTREND'?'uptrend':state==='DOWNTREND'?'downtrend':state==='RANGE'?'range':'uncertain';
  let summary=item.regime_label||'形态未确认，暂停做T';
  if(!confirmed){
    if(reasons.includes('INSUFFICIENT_SAMPLES')||(typeof item.regime_sample_count==='number'&&item.regime_sample_count<RANGE_WINDOW_MINUTES))summary='样本不足，等待连续已完成分钟积累';
    else if(reasons.some(reason=>['RANGE_CONFIRMATION_PENDING','UPTREND_CONFIRMATION_PENDING','DOWNTREND_CONFIRMATION_PENDING'].includes(reason)))summary='条件已满足，等待连续确认';
    else if(reasons.includes('INVALID_WINDOW_VALUES'))summary='形态窗口数据无效，暂停做T';
    else if(reasons.some(reason=>typeof reason==='string'&&reason.startsWith('RANGE_'))&&reasons.some(reason=>typeof reason==='string'&&reason.startsWith('TREND_'))&&!reasonText(reasons).includes('未识别原因'))summary='震荡与趋势条件均未满足，继续观察';
  }
  return {heading,className,summary,sampleProgress:progress(item.regime_sample_count,RANGE_WINDOW_MINUTES),rangeProgress:progress(item.range_confirmation_count,RANGE_CONFIRMATIONS),trendProgress:progress(item.trend_confirmation_count,TREND_CONFIRMATIONS),reasonsText:reasonText(reasons)||'暂无形态依据',blockedText:reasonText(item.blocked_reasons)||'无'};
}

function regimeMarkup(item){
  const view=regimePresentation(item);
  return `<div class="regime-alert ${view.className}" role="status"><strong>${esc(view.heading)}</strong><span> · ${esc(view.summary)}</span><div class="hint">连续已完成分钟 ${esc(view.sampleProgress)} · 连续确认：震荡 ${esc(view.rangeProgress)} · 趋势 ${esc(view.trendProgress)}</div><div class="hint">形态依据：${esc(view.reasonsText)}</div><div class="hint">等待 / 阻断原因：${esc(view.blockedText)}</div></div>`;
}

function gridBandValue(point,gridWidth,multiple,direction){return Number(point.average_price)*(1+Number(gridWidth)*multiple*direction)}

function stageQuoteUpdate(current,payload){
  const staged=payload.reset?new Map():new Map(current||[]);
  for(const point of payload.upserts||[])staged.set(point.timestamp,point);
  return staged;
}

async function loadQuoteUpdates(symbol,since,sequence){
  const response=await fetch(`/api/quotes?symbol=${encodeURIComponent(symbol)}&since=${since}`,{cache:'no-store'});
  if(!response.ok)throw new Error('分钟行情不可用');
  const payload=await response.json();
  const staged=stageQuoteUpdate(quotePoints.get(symbol),payload),revision=Number(payload.revision);
  if(payload.symbol!==symbol)throw new Error('分钟行情标的不匹配');
  if(sequence!==quoteRequestSequence||symbol!==selectedSymbol)return null;
  if(!Number.isInteger(revision)||revision<0)throw new Error('分钟行情版本无效');
  if(!payload.reset&&revision<Number(quoteRevisions.get(symbol)||0))return null;
  quotePoints.set(symbol,staged);
  quoteRevisions.set(symbol,revision);
  return [...staged.values()].sort((left,right)=>left.timestamp.localeCompare(right.timestamp));
}
/* PAGE_HELPERS_END */

function marketMinute(timestamp){const minutes=shanghaiMinute(timestamp)?.minute??570;if(minutes<=690)return Math.max(0,minutes-570);return Math.min(240,120+Math.max(0,minutes-780))}
function sessionAwareTicks(points){
  const targets=[['09:30',0],['10:30',60],['11:30 / 13:00',120],['14:00',180],['15:00',240]];
  if(!points.length)return [];
  const occupied=new Set();
  return targets.filter(([,minute])=>{if(occupied.has(minute))return false;occupied.add(minute);return true});
}
function chart(item){
  const points=quotePoints.has(item.symbol)?[...quotePoints.get(item.symbol).values()].sort((a,b)=>a.timestamp.localeCompare(b.timestamp)):[];
  if(!points.length)return '<div class="empty">正在载入已完成分钟</div>';
  const w=760,h=260,left=58,right=18,top=16,bottom=34,plotW=w-left-right,plotH=h-top-bottom;
  const grid=Number(item.grid_width_pct)||DEFAULT_GRID_WIDTH_PCT,base=Number(item.previous_close);
  const thresholds=[base,...points.flatMap(point=>[gridBandValue(point,grid,3,-1),gridBandValue(point,grid,3,1),gridBandValue(point,grid,5,-1),gridBandValue(point,grid,5,1)])];
  const values=points.flatMap(point=>[point.low,point.high,point.price,point.average_price].map(Number)).concat(thresholds).filter(Number.isFinite),lo=Math.min(...values),hi=Math.max(...values),span=hi-lo||1;
  const x=point=>left+marketMinute(point.timestamp)*plotW/240,y=value=>top+(hi-Number(value))*plotH/span;
  const pathValue=valueForPoint=>points.map((point,index)=>(index&&!minutePathBreak(points[index-1],point)?'L':'M')+x(point).toFixed(1)+' '+y(valueForPoint(point)).toFixed(1)).join(' '),path=key=>pathValue(point=>point[key]);
  const horizontal=(cls,value,label)=>`<g><line class="${cls}" x1="${left}" y1="${y(value)}" x2="${w-right}" y2="${y(value)}"/><text x="${w-right-2}" y="${y(value)-3}" text-anchor="end" fill="#8fa1b8" font-size="10">${label}</text></g>`;
  const last=points[points.length-1],bandLabel=(value,label)=>`<text x="${w-right-2}" y="${y(value)-3}" text-anchor="end" fill="#8fa1b8" font-size="10">${label}</text>`;
  // five labeled y ticks
  const yTicks=Array.from({length:5},(_,index)=>{const value=hi-span*index/4,py=y(value);return `<g><line class="axis-grid" x1="${left}" y1="${py}" x2="${w-right}" y2="${py}"/><text x="${left-7}" y="${py+4}" text-anchor="end">${num(value)}</text></g>`}).join('');
  const xTicks=sessionAwareTicks(points).map(([label,minute])=>{const px=left+minute*plotW/240;return `<g><line x1="${px}" y1="${h-bottom}" x2="${px}" y2="${h-bottom+5}"/><text x="${px}" y="${h-8}" text-anchor="middle">${label}</text></g>`}).join('');
  const markers=(item.trade_markers||[]).map(marker=>{const point=points.find(current=>current.timestamp===marker.timestamp);if(!point)return '';const color=marker.type==='B'?'#32d296':'#ff5964';return `<circle cx="${x(point)}" cy="${y(marker.price)}" r="4" fill="${color}"/>`}).join('');
  return `<div class="chart-wrap"><svg viewBox="0 0 ${w} ${h}" aria-label="分时图，白线实时价，黄线均价，含昨收、三格和五格阈值"><g class="y-axis">${yTicks}</g><g class="x-axis">${xTicks}</g><path class="five-grid" d="${pathValue(point=>gridBandValue(point,grid,5,-1))}"/>${bandLabel(gridBandValue(last,grid,5,-1),'-5格')}<path class="five-grid" d="${pathValue(point=>gridBandValue(point,grid,5,1))}"/>${bandLabel(gridBandValue(last,grid,5,1),'+5格')}<path class="three-grid" d="${pathValue(point=>gridBandValue(point,grid,3,-1))}"/>${bandLabel(gridBandValue(last,grid,3,-1),'-3格')}<path class="three-grid" d="${pathValue(point=>gridBandValue(point,grid,3,1))}"/>${bandLabel(gridBandValue(last,grid,3,1),'+3格')}${horizontal('zero-line',base,'昨收')}<path class="average-line" d="${path('average_price')}"/><path class="price-line" d="${path('price')}"/>${markers}<line class="crosshair" x1="0" y1="${top}" x2="0" y2="${h-bottom}"/><rect class="chart-overlay" x="${left}" y="${top}" width="${plotW}" height="${plotH}" data-symbol="${esc(item.symbol)}"/></svg><div id="chart-tooltip" class="chart-tooltip" role="status"></div></div>`;
}

function attachChartTooltip(item){
  const overlay=document.querySelector('.chart-overlay'),tooltip=document.querySelector('#chart-tooltip'),crosshair=document.querySelector('.crosshair');
  if(!overlay||!tooltip||!crosshair)return;
  const points=[...(quotePoints.get(item.symbol)||new Map()).values()].sort((a,b)=>a.timestamp.localeCompare(b.timestamp));
  overlay.addEventListener('pointermove',event=>{const rect=overlay.getBoundingClientRect(),targetMinute=Math.max(0,Math.min(240,(event.clientX-rect.left)*240/Math.max(rect.width,1))),point=points.reduce((best,current)=>Math.abs(marketMinute(current.timestamp)-targetMinute)<Math.abs(marketMinute(best.timestamp)-targetMinute)?current:best,points[0]);if(!point)return;const deviation=Number(point.price)/Number(point.average_price)-1,reason=item.regime_label||item.health_reason||'—',px=58+marketMinute(point.timestamp)*(760-58-18)/240;crosshair.setAttribute('x1',px);crosshair.setAttribute('x2',px);crosshair.style.display='block';tooltip.style.display='block';tooltip.style.left=Math.min(event.offsetX+12,520)+'px';tooltip.style.top='8px';tooltip.textContent=`时间 ${new Date(point.timestamp).toLocaleTimeString()}\nOHLC ${num(point.open)} / ${num(point.high)} / ${num(point.low)} / ${num(point.price)}\n价格 ${num(point.price)} · VWAP ${num(point.average_price)}\n成交量 ${num(point.volume,0)} · 偏离 ${pct(deviation)}\n状态依据 ${reason}`});
  overlay.addEventListener('pointerleave',()=>{tooltip.style.display='none';crosshair.style.display='none'});
}

function backtestSummary(symbol){
  const data=backtests.get(symbol);
  if(!data)return '<section class="backtest"><h3>做T回测</h3><div class="hint">正在计算</div></section>';
  if(data.status==='MISSING_QUOTE'||data.status==='MISSING_METADATA')return `<section class="backtest"><h3>做T回测</h3><div class="hint">${esc(data.status)}</div></section>`;
  return `<section class="backtest"><h3>做T回测</h3><div class="meta"><div><span>状态</span>${esc(data.status)}</div><div><span>完成配对</span>${num(data.completed_pair_count,0)}</div><div><span>未完成腿</span>${num(data.open_leg_count,0)}</div><div><span>净增益</span>${num(data.t_net_gain_cny,2)} 元</div><div><span>基线权益</span>${num(data.baseline_equity_cny,2)}</div><div><span>策略权益</span>${num(data.strategy_equity_cny,2)}</div></div></section>`;
}
function replaySummary(symbol){
  const data=replays.get(symbol),actions=data&&Array.isArray(data.actions)?data.actions:[];
  if(!data)return '<section class="replay"><h3>信号粗回放</h3><div class="hint">正在载入</div></section>';
  return `<section class="replay"><h3>信号粗回放</h3><div class="meta"><div><span>状态</span>${esc(data.status||'OK')}</div><div><span>候选次数</span>${actions.length}</div><div><span>说明</span>仅复查信号，不评价收益</div></div></section>`;
}
function valuationSummary(symbol){const data=valuationCache.get(symbol);if(!data)return '<section class="valuation"><h3>关联指数与指数估值</h3><div class="hint">正在载入估值</div></section>';const index=data.index,valuation=data.valuation;if(!index)return `<section class="valuation"><h3>关联指数与指数估值</h3><div class="hint">${esc(data.status||'MISSING_METADATA')} · 未提供估值数据</div></section>`;if(!valuation)return `<section class="valuation"><h3>关联指数与指数估值</h3><div class="meta"><div><span>指数</span>${esc(index.name)}</div><div><span>代码</span>${esc(index.code)}</div><div><span>状态</span>${esc(data.status||'MISSING_VALUATION')}</div></div><div class="hint">未提供估值数据，不使用示例数字</div></section>`;const level={LOW:'低位',NORMAL:'正常',HIGH:'高位',UNKNOWN:'未知'}[valuation.level]||'未知';return `<section class="valuation"><h3>关联指数与指数估值</h3><div class="meta"><div><span>指数</span>${esc(index.name)} · ${esc(index.code)}</div><div><span>PE-TTM</span>${num(valuation.pe_ttm)}</div><div><span>PB</span>${num(valuation.pb)}</div><div><span>股息率</span>${pct(valuation.dividend_yield)}</div><div><span>5年分位</span>${num(valuation.pe_percentile_5y)} / ${num(valuation.pb_percentile_5y)}</div><div><span>10年分位</span>${num(valuation.pe_percentile_10y)} / ${num(valuation.pb_percentile_10y)}</div><div><span>估值等级</span>${level}</div><div><span>数据日期</span>${esc(valuation.as_of||'—')}</div><div><span>来源</span>${esc(valuation.source||'—')}</div></div></section>`}
async function loadValuation(){const symbol=selectedSymbol;if(!symbol)return;const sequence=++valuationRequestSequence;try{const response=await fetch(`/api/etf/${encodeURIComponent(symbol)}/valuation`,{cache:'no-store'});if(!response.ok)throw new Error('估值不可用');const data=await response.json();if(sequence!==valuationRequestSequence||symbol!==selectedSymbol)return;valuationCache.set(symbol,data);const node=document.querySelector('#valuation');if(node)node.outerHTML=valuationSummary(symbol)}catch(error){if(sequence!==valuationRequestSequence||symbol!==selectedSymbol)return;valuationCache.set(symbol,{symbol,status:'UNKNOWN',index:null,valuation:null});const node=document.querySelector('#valuation');if(node)node.outerHTML=valuationSummary(symbol)}}
function updateConnection(state){connectionStatus.className='status '+state.dotClass;dot.className='dot '+state.dotClass;statusNode.textContent=state.statusText;staleBanner.textContent=state.warning||'';staleBanner.classList.toggle('visible',Boolean(state.unsafe))}
function renderNav(items){
  watchCount.textContent=items.length+' 项';
  const markup=items.map(item=>{const state=currentMarketPresentation(item,Date.now(),feedState),opportunity=state.candidate;return `<button type="button" class="watch-item${item.symbol===selectedSymbol?' active':''}${opportunity?' opportunity':''}" data-symbol="${esc(item.symbol)}" aria-current="${item.symbol===selectedSymbol?'true':'false'}"><span class="watch-item-main"><span class="watch-item-name">${esc(item.name)}</span><span class="watch-item-symbol">${esc(item.symbol)} · <b class="watch-item-change ${item.change_pct!=null&&item.change_pct>=0?'up':'down'}">${item.change_pct==null||!Number.isFinite(Number(item.change_pct))?'—':(Number(item.change_pct)*100).toFixed(2)+'%'}</b></span></span><i class="watch-item-state ${state.dotClass}${opportunity?' opportunity':''}"></i></button>`}).join('')||'<div class="empty">监控列表为空</div>';
  if(markup!==renderNav.lastMarkup){watchList.innerHTML=markup;renderNav.lastMarkup=markup}
}

function refreshMarketState(){
  if(!latestData)return;
  const allItems=latestData.items||[],items=showMissing.checked?allItems:allItems.filter(item=>item.status!=='MISSING_QUOTE');
  renderNav(items);
  const item=items.find(current=>current.symbol===selectedSymbol);
  if(!item)return;
  const state=currentMarketPresentation(item,Date.now(),feedState);
  updateConnection(state);
  if(!state.candidate){
    detail.querySelector('.candidate-alert')?.remove();
    const signal=detail.querySelector('.signal');
    if(signal)signal.textContent=state.displayLabel||'等待';
  }
  detail.querySelector('.market-time')?.classList.toggle('stale',state.stale);
  const healthNode=detail.querySelector('#market-health');
  if(healthNode)healthNode.textContent=state.statusText+' · '+(state.unsafe?state.warning:item.health_reason||'行情实时');
}

function render(data){
  renderQualityNotice(data,document.querySelector('#quality-notice'));latestData=data;const refreshedAt=new Date(),allItems=data.items||[],items=showMissing.checked?allItems:allItems.filter(item=>item.status!=='MISSING_QUOTE');errors.textContent=(data.errors||[]).join(' · ');if(!items.some(item=>item.symbol===selectedSymbol))selectedSymbol=items.length?items[0].symbol:null;renderNav(items);const item=items.find(current=>current.symbol===selectedSymbol);
  if(!item){detail.innerHTML=`<div class="card empty">${allItems.length?'无可显示行情，请开启“显示无行情”':'监控列表为空'}</div>`;updateConnection({statusText:'暂无可用行情',dotClass:'missing',warning:'暂无可用行情，候选提醒已撤销',unsafe:true});refreshTimeNode.textContent='页面刷新时间：'+refreshedAt.toLocaleString();return}
  const state=currentMarketPresentation(item,refreshedAt.getTime(),feedState),marketAt=state.marketAt,stale=state.stale,candidate=state.candidate,displayLabel=state.displayLabel,cls='wait',move=item.change_pct!=null&&item.change_pct>=0?'up':'down',missing=item.status==='MISSING_QUOTE',candidateAlert=candidate?'<div class="candidate-alert" role="alert"><strong>做T候选</strong><span>方向仅用于配对记账，不构成交易指令</span></div>':'';
  const regimeAlert=regimeMarkup(item),regimeView=regimePresentation(item);
  const health_reason=item.health_reason||'行情状态未知',path_efficiency=item.path_efficiency,one_side_ratio=item.one_side_ratio,vwap_crossings=item.vwap_crossings,vwap_slope=item.vwap_slope,above_vwap_count=item.above_vwap_count,below_vwap_count=item.below_vwap_count,gross_edge_pct=item.expected_gross_edge_pct,cost_pct=item.round_trip_cost_pct,net_edge_pct=item.expected_net_edge_pct;
  detail.innerHTML=`<article class="card" data-symbol="${esc(item.symbol)}"><div class="top"><div><div class="symbol">${esc(item.symbol)}</div><div class="name">${esc(item.name)}</div></div><div><div class="price">${num(item.price)}</div><div class="pct ${move}">${pct(item.change_pct)}</div></div></div><div class="signal ${cls}">${esc(displayLabel)}</div>${regimeAlert}${candidateAlert}${missing?'<div class="empty">暂无当日行情，历史数据请从日期选择器查看</div>':chart(item)}<div class="meta"><div><span>行情健康</span><strong id="market-health">${esc(state.statusText)} · ${esc(state.unsafe?state.warning:health_reason)}</strong></div><div><span>ER / 路径效率</span>${num(path_efficiency,4)}</div><div><span>单侧比例</span>${pct(one_side_ratio)}</div><div><span>VWAP 穿越</span>${num(vwap_crossings,0)}</div><div><span>VWAP 斜率</span>${pct(vwap_slope)}</div><div><span>VWAP 两侧停留</span>上 ${num(above_vwap_count,0)} · 下 ${num(below_vwap_count,0)}</div><div><span>连续确认</span>震荡 ${esc(regimeView.rangeProgress)} · 趋势 ${esc(regimeView.trendProgress)}</div><div><span>预期毛边际</span>${pct(gross_edge_pct)}</div><div><span>双边成本</span>${pct(cost_pct)}</div><div><span>预期净边际</span>${pct(net_edge_pct)}</div><div><span>阻断原因</span>${esc(regimeView.blockedText)}</div><div><span>格宽阈值</span>三格 ${pct((item.grid_width_pct||DEFAULT_GRID_WIDTH_PCT)*3)} · 五格 ${pct((item.grid_width_pct||DEFAULT_GRID_WIDTH_PCT)*5)}</div></div><section class="alerts"><h3>按日历史行情</h3><div id="daily-history" class="alert-list">${dailyHistoryCache.get(`${historyDate.value}|${item.symbol}`)||'<div class="hint">选择日期后查看当日分钟行情</div>'}</div></section><section class="alerts"><h3>提示追溯</h3><div id="alert-list" class="alert-list">${alertHistoryCache.get(item.symbol)||'<div class="hint">正在载入提示历史</div>'}</div></section><div class="time market-time${stale?' stale':''}">行情数据时间：${marketAt&&Number.isFinite(marketAt.getTime())?marketAt.toLocaleString():'—'}</div><div id="valuation">${valuationSummary(item.symbol)}</div>${backtestSummary(item.symbol)}${replaySummary(item.symbol)}</article>`;
  refreshTimeNode.textContent='页面刷新时间：'+refreshedAt.toLocaleString();updateConnection(state);loadAlerts();loadDailyHistory();loadValuation();attachChartTooltip(item)
}

function failed(message){feedState=nextFeedState(feedState,'FAIL',message);if(latestData){rejectMarketFeed(latestData.items||[],message);render(latestData)}else updateConnection({statusText:message,dotClass:'outage',warning:message+'，候选提醒已撤销',unsafe:true})}
async function refreshSelectedQuotes(){const symbol=selectedSymbol;if(!symbol)return;const sequence=++quoteRequestSequence,generation=marketSafety.get(symbol)?.generation;try{const points=await loadQuoteUpdates(symbol,quoteRevisions.get(symbol)||0,sequence);if(points===null||sequence!==quoteRequestSequence||symbol!==selectedSymbol)return;confirmMarketSummary(symbol,generation,points,quoteRevisions.get(symbol));feedState=nextFeedState(feedState,'QUOTES');if(latestData)render(latestData)}catch(error){if(sequence===quoteRequestSequence)failed(error.message)}}
async function loadAlerts(){const symbol=selectedSymbol;if(!symbol)return;const sequence=++alertRequestSequence;try{const response=await fetch(`/api/alerts?symbol=${encodeURIComponent(symbol)}&limit=30`,{cache:'no-store'});if(!response.ok)throw new Error('提示历史不可用');const data=await response.json(),html=(data.items||[]).map(item=>`<div class="alert-row"><strong>${esc(item.action)} · ${esc(item.symbol)}</strong><span>${esc(item.timestamp||item.recorded_at||'—')} · ${esc(item.label||'')}</span><span>策略 ${esc(item.strategy_version||'T_V1')} · 偏离 ${item.deviation_pct==null?'—':pct(item.deviation_pct)} · 状态 ${esc(item.regime_state||item.trend_state||'—')}</span></div>`).join('')||'<div class="hint">当前标的暂无历史提示</div>';if(sequence!==alertRequestSequence||symbol!==selectedSymbol)return;alertHistoryCache.set(symbol,html);const node=document.querySelector('#alert-list');if(node)node.innerHTML=html}catch(error){if(sequence!==alertRequestSequence||symbol!==selectedSymbol)return;const html=`<div class="hint">${esc(error.message)}</div>`;alertHistoryCache.set(symbol,html);const node=document.querySelector('#alert-list');if(node)node.innerHTML=html}}
async function loadHistoryDates(){try{const selected=historyDate.value,response=await fetch('/api/history/dates',{cache:'no-store'}),data=await response.json();historyDate.innerHTML='<option value="">实时行情</option>'+data.dates.map(date=>`<option value="${esc(date)}">${esc(date)}</option>`).join('');if((data.dates||[]).includes(selected))historyDate.value=selected;loadDailyHistory()}catch(error){failed(error.message)}}
async function loadDailyHistory(){const node=document.querySelector('#daily-history');if(!node||!historyDate.value||!selectedSymbol){if(node)node.innerHTML='<div class="hint">选择日期后查看当日分钟行情</div>';return}try{const response=await fetch(`/api/history/quotes?date=${encodeURIComponent(historyDate.value)}&symbol=${encodeURIComponent(selectedSymbol)}`,{cache:'no-store'}),data=await response.json();if(!response.ok)throw new Error(data.error||'历史行情不可用');const records=data.records||[],first=records[0],last=records[records.length-1];node.innerHTML=records.length?`<div class="alert-row"><strong>${esc(data.date)} · ${esc(selectedSymbol)}</strong><span>共 ${records.length} 个分钟点</span><span>${esc(first.timestamp)} → ${esc(last.timestamp)}</span><span>开 ${num(first.open||first.price)} · 收 ${num(last.price)} · 最高 ${num(Math.max(...records.map(x=>Number(x.high||x.price))))} · 最低 ${num(Math.min(...records.map(x=>Number(x.low||x.price))))}</span></div>`:'<div class="hint">该日期没有此标的行情</div>'}catch(error){node.innerHTML=`<div class="hint">${esc(error.message)}</div>`}}
async function poll(){try{const response=await fetch('/api/snapshot',{cache:'no-store'});if(!response.ok)throw new Error('行情不可用');const data=await response.json();feedState=nextFeedState(feedState,'POLL');applySummary(data,true)}catch(error){failed(error.message)}}
async function loadBacktests(){try{const [backtestResponse,replayResponse]=await Promise.all([fetch('/api/t-backtest',{cache:'no-store'}),fetch('/api/signal-replay',{cache:'no-store'})]);if(!backtestResponse.ok||!replayResponse.ok)throw new Error('回放不可用');const backtestData=await backtestResponse.json(),replayData=await replayResponse.json();backtests=new Map((backtestData.items||[]).map(item=>[item.symbol,item]));replays=new Map((replayData.items||[]).map(item=>[item.symbol,item]));if(latestData)render(latestData)}catch(error){failed(error.message)}}
function applySummary(data,authoritative=false){
  feedState=nextFeedState(feedState,'SUMMARY');
  acceptMarketSummary(data.items||[],data.revision);
  if(authoritative||!latestData)latestData=data;
  else{const removed=new Set(data.removed_symbols||[]),changed=new Map((data.items||[]).map(item=>[item.symbol,item])),current=(latestData.items||[]).filter(item=>!removed.has(item.symbol)).map(item=>changed.get(item.symbol)||item),known=new Set(current.map(item=>item.symbol));for(const item of changed.values())if(!known.has(item.symbol))current.push(item);latestData={...latestData,...data,items:current}}
  render(latestData);refreshSelectedQuotes();
}
function applyReset(data){quoteRequestSequence+=1;quotePoints.clear();quoteRevisions.clear();feedState=nextFeedState(feedState,'FAIL','正在同步行情');applySummary(data,true)}
watchList.addEventListener('click',event=>{const button=event.target.closest('[data-symbol]');if(!button)return;selectedSymbol=button.dataset.symbol;render(latestData);refreshSelectedQuotes()});
historyDate.addEventListener('change',loadDailyHistory);
showMissing.addEventListener('change',()=>{if(latestData)render(latestData)});
watchForm.addEventListener('submit',async event=>{event.preventDefault();formError.textContent='';formError.className='';watchSubmit.disabled=true;const submittedSymbol=watchSymbol.value.trim();try{const response=await fetch('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:submittedSymbol,name:watchName.value,grid_width_pct:DEFAULT_GRID_WIDTH_PCT})}),payload=await response.json();if(!response.ok)throw new Error(payload.message||payload.error||'添加失败');watchForm.reset();formError.className='success';formError.textContent=`已添加 ${submittedSymbol}，等待下一轮行情刷新`;await poll();await loadBacktests()}catch(error){formError.className='error';formError.textContent=error.message}finally{watchSubmit.disabled=false}});
let timer;function fallback(){failed('轮询模式');if(timer)return;poll();timer=setInterval(poll,5000)}
setInterval(refreshMarketState,1000);
loadHistoryDates();loadBacktests();if(window.EventSource){const source=new EventSource('/api/events');source.addEventListener('summary',event=>applySummary(JSON.parse(event.data),true));source.addEventListener('snapshot',event=>applySummary(JSON.parse(event.data),true));source.addEventListener('delta',event=>applySummary(JSON.parse(event.data)));source.addEventListener('reset',event=>applyReset(JSON.parse(event.data)));source.addEventListener('monitor-error',event=>failed(JSON.parse(event.data).error));source.onerror=fallback}else fallback();
</script></body></html>"""


PAGE = (
    _PAGE_TEMPLATE
    .replace("__DEFAULT_GRID_WIDTH_PCT__", repr(DEFAULT_GRID_WIDTH_PCT))
    .replace("__REALTIME_MAX_AGE_SECONDS__", repr(REALTIME_MAX_AGE_SECONDS))
    .replace("__DELAYED_MAX_AGE_SECONDS__", repr(DELAYED_MAX_AGE_SECONDS))
    .replace("__RANGE_WINDOW_MINUTES__", repr(RANGE_WINDOW_MINUTES))
    .replace("__RANGE_CONFIRMATIONS__", repr(RANGE_CONFIRMATIONS))
    .replace("__TREND_CONFIRMATIONS__", repr(TREND_CONFIRMATIONS))
    .replace("__ONE_GRID_DISPLAY__", f"{DEFAULT_GRID_WIDTH_PCT:.2%}")
    .replace("__TWO_GRID_DISPLAY__", f"{DEFAULT_GRID_WIDTH_PCT * 2:.2%}")
    .replace("__THREE_GRID_DISPLAY__", f"{DEFAULT_GRID_WIDTH_PCT * 3:.2%}")
    .replace("__FIVE_GRID_DISPLAY__", f"{DEFAULT_GRID_WIDTH_PCT * 5:.2%}")
)
