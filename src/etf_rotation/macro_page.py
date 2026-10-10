MACRO_PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>股债性价比 与 成长/红利 风格</title>
<style>body{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#f5f7fb;color:#182230;margin:0}.wrap{max-width:1280px;margin:auto;padding:28px}nav[aria-label="监控模式"]{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:24px}nav[aria-label="监控模式"] a{color:#2457a6;text-decoration:none;font-weight:600;border:1px solid #d5deeb;border-radius:999px;padding:6px 12px}.hero,.card{background:#fff;border:1px solid #e1e7f0;border-radius:14px;padding:22px;margin-bottom:18px;box-shadow:0 5px 18px #1f3b5d0d}.hero h1{margin:0 0 8px}.muted{color:#667085}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}.metric{font-size:28px;font-weight:700;color:#155eef}.sub{font-size:13px;color:#667085}.warn{color:#b54708}.bad{color:#b42318}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid #edf0f5;vertical-align:top}th{background:#f8fafc;white-space:nowrap}.pill{padding:4px 9px;border-radius:99px;font-size:12px;font-weight:650;white-space:nowrap}.tier-VERY_CHEAP,.tier-GROWTH_WEAK{background:#ecfdf3;color:#027a48}.tier-CHEAP,.tier-GROWTH_FAVORED{background:#eef4ff;color:#175cd3}.tier-NEUTRAL{background:#f2f4f7;color:#344054}.tier-RICH,.tier-GROWTH_WARM{background:#fffaeb;color:#b54708}.tier-VERY_RICH,.tier-GROWTH_HOT{background:#fef3f2;color:#b42318}.tier-UNAVAILABLE{background:#f8fafc;color:#667085}tr.current td{background:#f0f6ff}.empty{padding:24px;text-align:center;color:#667085}.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:18px}@media(max-width:700px){.wrap{padding:16px}table{font-size:13px}.two{grid-template-columns:1fr}}</style></head>
<body><main class="wrap"><nav aria-label="监控模式"><a href="/swing">波段监控</a><a href="/">做T监控</a><a href="/pr">PR估值</a><a href="/macro" aria-current="page">股债与风格</a><a href="/notifications">通知中心</a></nav>
<section class="hero"><h1>股债性价比 与 成长/红利 风格</h1><p class="muted">股债性价比管总仓位：ERP = 100 ÷ 沪深300 PE-TTM − 10 年期国债收益率，看 10 年滚动分位；当 ERP 分位比 PE 便宜度分位高出 15 个点以上，视为利率推高而非股票便宜，档位下调一档。风格比值管成长与红利的分配：创业板ETF ÷ 中证红利ETF 的复权序列（全收益口径），看 3 年滚动分位；高位只降低新增，切换要等比值跌破 20 日均线。本页只读，不产生交易指令。</p></section>
<section class="card"><h2>综合结论</h2><div id="combined" class="grid"><div><div class="muted">状态</div><div class="metric">加载中</div></div></div></section>
<div class="two">
<section class="card"><h2>指标一：股债性价比（ERP）</h2><div id="erp" class="grid"></div><div id="erp-tiers"></div></section>
<section class="card"><h2>指标二：成长 / 红利 风格比值</h2><div id="style" class="grid"></div><div id="style-tiers"></div></section>
</div>
<section class="card"><h2>组合矩阵（先定总量，再分风格）</h2>
<table><thead><tr><th>ERP ＼ 比值</th><th>&lt; 30%（偏成长）</th><th>30%~70%（中性）</th><th>&gt; 70%（偏红利）</th></tr></thead><tbody id="matrix">
<tr data-row="ADD"><th>&gt; 70%（便宜）</th><td data-col="GROWTH">加仓，新增资金主投成长</td><td data-col="BALANCED">加仓，按原比例</td><td data-col="DIVIDEND">加仓，新增资金主投红利</td></tr>
<tr data-row="HOLD"><th>30%~70%（中性）</th><td data-col="GROWTH">按计划，成长 1.2× 红利 0.9×</td><td data-col="BALANCED">按计划</td><td data-col="DIVIDEND">按计划，成长 0.8× 红利 1.1×</td></tr>
<tr data-row="REDUCE"><th>&lt; 30%（贵）</th><td data-col="GROWTH">减仓，先减红利留成长</td><td data-col="BALANCED">减仓，按比例</td><td data-col="DIVIDEND">减仓，先减成长留红利</td></tr>
</tbody></table></section>
<section class="card"><h2>数据来源与口径</h2><div id="coverage" class="muted">加载中</div></section></main>
<script>
const esc=v=>String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const fmt=(v,d=2)=>v==null||v===''?'—':Number(v).toFixed(d);
const pill=(tier,label)=>'<span class="pill tier-'+esc(tier||'UNAVAILABLE')+'">'+esc(label||'不可用')+'</span>';
const cell=(k,v,sub)=>'<div><div class="muted">'+esc(k)+'</div><div class="metric">'+v+'</div>'+(sub?'<div class="sub">'+esc(sub)+'</div>':'')+'</div>';
const unavailable=x=>'<div class="empty">指标不可用：'+esc(x.reason||'未知')+(x.sample_days!=null?'（样本 '+esc(x.sample_days)+' / 需 '+esc(x.minimum_days)+' 日）':'')+'</div>';
function erpView(x){if(x.status!=='OK')return unavailable(x);
 const corr=x.rate_driven?'<span class="warn">利率推高，已下调一档（原档 '+esc(x.raw_tier)+'）</span>':'无需修正';
 return cell('档位',pill(x.tier,x.tier_label),x.action)+cell('ERP',fmt(x.erp_pct)+'%','10 年分位 '+fmt(x.erp_percentile,1)+'%')+cell('沪深300 PE-TTM',fmt(x.pe_ttm),'PE 分位 '+fmt(x.pe_percentile,1)+'%，便宜度 '+fmt(x.pe_cheapness_percentile,1)+'%')+cell('10 年期国债',fmt(x.cn10y_yield_pct,3)+'%',x.cn10y_as_of)+cell('利率修正','<span style="font-size:16px">'+corr+'</span>','分位差 '+fmt(x.rate_correction_gap,1)+' 个点，阈值 15')+cell('权益目标仓位',esc(x.equity_target_pct)+'%','定投倍数 '+fmt(x.dca_multiplier,1)+'×')+cell('样本',esc(x.sample_days)+' 日',x.sample_start+' ~ '+x.as_of)}
function styleView(x){if(x.status!=='OK')return unavailable(x);
 const trend=(x.above_ma20?'在 20 日均线上方':'跌破 20 日均线')+(x.ma20_falling?'，均线向下':'，均线未向下');
 const sw=x.switch_confirmed?'<span class="bad">切换条件已满足</span>':'未触发';
 return cell('档位',pill(x.tier,x.tier_label),x.switch_rule)+cell('比值（首日=1）',fmt(x.ratio_normalized,3),'3 年分位 '+fmt(x.ratio_percentile,1)+'%')+cell('趋势','<span style="font-size:16px">'+esc(trend)+'</span>','20 日均线 '+fmt(x.ratio_ma20,3))+cell('距区间高点',fmt(x.from_high_pct,1)+'%','高点 '+x.window_high_date+'，低点 '+x.window_low_date)+cell('成长线倍数',fmt(x.growth_multiplier,1)+'×','红利线倍数 '+fmt(x.dividend_multiplier,1)+'×')+cell('存量切换','<span style="font-size:16px">'+sw+'</span>','代理 '+esc(x.growth_symbol)+' ÷ '+esc(x.dividend_symbol))+cell('样本',esc(x.sample_days)+' 日',x.sample_start+' ~ '+x.as_of)}
function tiers(rows,current,cols){return '<table><thead><tr><th>分位下限</th><th>档位</th>'+cols.map(c=>'<th>'+esc(c[0])+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr'+(r.tier===current?' class="current"':'')+'><td>≥ '+esc(r.min_percentile)+'%</td><td>'+pill(r.tier,r.label)+'</td>'+cols.map(c=>'<td>'+esc(c[1](r))+'</td>').join('')+'</tr>').join('')+'</tbody></table>'}
async function load(){const r=await fetch('/api/macro');if(!r.ok)throw Error('HTTP '+r.status);const d=await r.json();const erp=d.erp||{},st=d.style_ratio||{},c=d.combined||{};
 document.querySelector('#combined').innerHTML=c.status==='OK'?cell('操作',esc(c.summary),'总量 '+esc(c.total_action)+' · 风格 '+esc(c.style_lean))+cell('定投总倍数',fmt(c.dca_total_multiplier,1)+'×','由 ERP 档位决定')+cell('成长线',fmt(c.growth_multiplier,1)+'×','由风格比值决定')+cell('红利线',fmt(c.dividend_multiplier,1)+'×','由风格比值决定')+cell('生成时间','<span style="font-size:16px">'+esc(d.generated_at)+'</span>','只读 · 不下单'):'<div class="empty">任一指标不可用时不给出综合结论</div>';
 document.querySelector('#erp').innerHTML=erpView(erp);document.querySelector('#style').innerHTML=styleView(st);
 document.querySelector('#erp-tiers').innerHTML=tiers(d.erp_tiers||[],erp.tier,[['权益仓位',r=>r.equity_target_pct+'%'],['定投倍数',r=>r.dca_multiplier+'×'],['动作',r=>r.action]]);
 document.querySelector('#style-tiers').innerHTML=tiers(d.style_tiers||[],st.tier,[['成长线',r=>r.growth_multiplier+'×'],['红利线',r=>r.dividend_multiplier+'×'],['存量切换',r=>r.switch_rule]]);
 if(c.status==='OK'){const td=document.querySelector('#matrix tr[data-row="'+c.total_action+'"] td[data-col="'+c.style_lean+'"]');if(td)td.style.background='#f0f6ff',td.style.fontWeight='700'}
 const cov=d.series_coverage||{},errs=d.errors||{};const line=(name,k,src)=>'<p><b>'+esc(name)+'</b>：'+esc(src)+'；已存 '+esc(cov[k]?.count??0)+' 日（'+esc(cov[k]?.start??'—')+' ~ '+esc(cov[k]?.end??'—')+'）'+(errs[k]?' <span class="bad">最近刷新失败：'+esc(errs[k])+'</span>':'')+'</p>';
 document.querySelector('#coverage').innerHTML=line('10 年期国债收益率','CN10Y','东方财富数据中心 RPTA_WEB_TREASURYYIELD，字段 EMM00166466，日频，含非交易日')+line('沪深300 PE-TTM','CSI300_PE_TTM','中证指数公司 index-perf 的 peg 字段（滚动市盈率），日频；按自然年分段抓取')+'<p><b>风格比值</b>：仓库已核验的 ETF 复权日线（创业板ETF 159915 ÷ 中证红利ETF 515180），等价于全收益口径，不随价格指数漏掉分红而漂移'+(errs.history?' <span class="bad">读取失败：'+esc(errs.history)+'</span>':'')+'</p><p class="muted">外部序列每小时最多刷新一次，且仅在存量落后于前一日时才请求；刷新失败沿用已存数据并在此处标注。</p>'}
load().catch(e=>{document.querySelector('#combined').innerHTML='<div class="empty bad">接口暂不可用：'+esc(e.message)+'</div>'});
</script></body></html>'''
