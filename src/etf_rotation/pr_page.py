PR_PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>市赚率 PR 估值</title>
<style>body{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#f5f7fb;color:#182230;margin:0}.wrap{max-width:1280px;margin:auto;padding:28px}nav[aria-label="监控模式"]{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:24px}nav[aria-label="监控模式"] a{color:#2457a6;text-decoration:none;font-weight:600;border:1px solid #d5deeb;border-radius:999px;padding:6px 12px}.hero,.card{background:#fff;border:1px solid #e1e7f0;border-radius:14px;padding:22px;margin-bottom:18px;box-shadow:0 5px 18px #1f3b5d0d}.hero h1{margin:0 0 8px}.muted{color:#667085}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}.metric{font-size:28px;font-weight:700;color:#155eef}.warn{color:#b54708}.bad{color:#b42318}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid #edf0f5;white-space:nowrap}th{background:#f8fafc}.pill{padding:4px 9px;border-radius:99px;font-size:12px;font-weight:650}.stage-DEEP_VALUE{background:#ecfdf3;color:#027a48}.stage-VALUE{background:#eef4ff;color:#175cd3}.stage-FAIR{background:#f2f4f7;color:#344054}.stage-RICH{background:#fffaeb;color:#b54708}.stage-EXPENSIVE{background:#fef3f2;color:#b42318}.stage-UNAVAILABLE{background:#f8fafc;color:#667085}.empty{padding:24px;text-align:center;color:#667085}@media(max-width:700px){.wrap{padding:16px}table{font-size:13px}}</style></head>
<body><main class="wrap"><nav aria-label="监控模式"><a href="/swing">波段监控</a><a href="/">做T监控</a><a href="/pr" aria-current="page">PR估值</a><a href="/notifications">通知中心</a></nav>
<section class="hero"><h1>市赚率 PR 估值</h1><p class="muted">主口径是同日 PE² ÷ PB ÷ 100。PE ÷ 年化 ROE 只用来核对两者是否一致，不单独决定阶段。宽基和黄金在分位档与 PR 档相差两档及以上时取中间档。本页只读，不产生交易指令。</p></section>
<section class="card"><div id="summary" class="grid"><div><div class="muted">状态</div><div class="metric">加载中</div></div></div></section>
<section class="card"><h2>ETF与指数估值</h2><div id="table" class="empty">正在读取估值数据...</div></section>
<section class="card"><h2>阶段参数（只读提示）</h2><p class="muted">下表对应估值阶段的仓位和保护参数。服务默认不启用这些行为，观察统计完成前不会据此调整仓位或下单。</p>
<table><thead><tr><th>阶段</th><th>新开仓</th><th>回调 A</th><th>突破 B</th><th>补仓</th><th>无进展退出</th><th>浮盈减仓</th></tr></thead><tbody>
<tr><td><span class="pill stage-DEEP_VALUE">深度低估</span></td><td>1.0</td><td>允许</td><td>允许</td><td>允许</td><td>12 个交易日</td><td>不强制</td></tr>
<tr><td><span class="pill stage-VALUE">低估</span></td><td>1.0</td><td>允许</td><td>允许</td><td>允许</td><td>10 个交易日</td><td>不强制</td></tr>
<tr><td><span class="pill stage-FAIR">合理</span></td><td>1.0</td><td>允许</td><td>允许</td><td>允许</td><td>10 个交易日</td><td>不强制</td></tr>
<tr><td><span class="pill stage-RICH">偏贵</span></td><td>0.5</td><td>允许</td><td>允许</td><td>禁止</td><td>7 个交易日</td><td>1.5R</td></tr>
<tr><td><span class="pill stage-EXPENSIVE">昂贵</span></td><td>0.5</td><td>禁止</td><td>允许</td><td>禁止</td><td>5 个交易日</td><td>1.0R</td></tr>
</tbody></table></section>
<section class="card"><h2>口径说明</h2><p class="muted">PR = PE² ÷ PB ÷ 100。ROE 列展示年化值，并标注 H1、Q1 或 TTM；推断出的期间会标明“推断”。roe_consistent 只说明 PE/ROE 与 PE²/PB 是否落在 20% 以内，不阻挡 PR 阶段。分位优先使用 10 年，缺失时用 5 年。非宽基、非黄金只使用分位。</p></section></main>
<script>
const esc=v=>String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const fmt=(v,d=2)=>v==null||v===''?'—':Number(v).toFixed(d);
const stageLabel={DEEP_VALUE:'深度低估',VALUE:'低估',FAIR:'合理',RICH:'偏贵',EXPENSIVE:'昂贵',UNAVAILABLE:'不可用'};
function stageCell(stage){const name=stage&&stage.stage||'UNAVAILABLE';const label=stageLabel[name]||name;const conflict=stage&&stage.stage_conflict?' 冲突':'';return '<span class="pill stage-'+esc(name)+'">'+esc(label+conflict)+'</span>'}
function roeCell(v){if(!v||v.roe_annualized==null)return '—';const mark=v.roe_period_inferred?' 推断':'';return fmt(v.roe_annualized)+'%'+mark}
function periodCell(v){if(!v||!v.roe_period)return '—';return esc(v.roe_period)+(v.roe_period_inferred?' 推断':'')}
function percentileCell(v){const horizon=v&&v.percentile_horizon_used;if(!horizon)return '—';return fmt(v['pe_percentile_'+horizon],1)+' / '+fmt(v['pb_percentile_'+horizon],1)}
function consistentCell(v){if(!v||v.pr_pe_roe==null)return '—';return v.roe_consistent?'一致':'偏离'}
async function load(){const r=await fetch('/api/valuations');if(!r.ok)throw Error('HTTP '+r.status);const d=await r.json();const items=d.items||[];const valid=items.filter(x=>x.valuation&&x.valuation.pr_pe_pb!=null);document.querySelector('#summary').innerHTML='<div><div class="muted">指数数量</div><div class="metric">'+items.length+'</div></div><div><div class="muted">已有PR</div><div class="metric">'+valid.length+'</div></div><div><div class="muted">数据状态</div><div class="metric">只读</div></div>';
if(!items.length){document.querySelector('#table').textContent='暂无ETF估值映射数据';return}
document.querySelector('#table').innerHTML='<table><thead><tr><th>ETF</th><th>跟踪指数</th><th>阶段</th><th>PE</th><th>PB</th><th>年化ROE</th><th>期间</th><th>PR(PE²/PB)</th><th>PR(PE/ROE)</th><th>一致性</th><th>PE/PB分位</th><th>分位窗口</th><th>日期</th><th>状态</th></tr></thead><tbody>'+items.map(x=>{const v=x.valuation||{};const stage=x.stage||{};return '<tr><td>'+esc(x.symbol)+' '+esc(x.name)+'</td><td>'+esc(x.index?.code)+' '+esc(x.index?.name)+'</td><td>'+stageCell(stage)+'</td><td>'+fmt(v.pe_ttm)+'</td><td>'+fmt(v.pb)+'</td><td>'+roeCell(v)+'</td><td>'+periodCell(v)+'</td><td><b>'+fmt(v.pr_pe_pb)+'</b></td><td>'+fmt(v.pr_pe_roe)+'</td><td>'+consistentCell(v)+'</td><td>'+percentileCell(v)+'</td><td>'+esc(v.percentile_horizon_used||'—')+'</td><td>'+esc(v.as_of||'—')+'</td><td><span class="pill">'+esc(x.status||'UNKNOWN')+'</span></td></tr>'}).join('')+'</tbody></table>'}
load().catch(e=>{document.querySelector('#table').innerHTML='<div class="empty bad">估值接口暂不可用：'+esc(e.message)+'</div>'});
</script></body></html>'''
