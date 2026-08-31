# 本地 ETF 做 T 监控

这是一个只读、本地运行的 ETF 分钟行情监控器。它采集公开行情，校验并保存已完成分钟，识别市场状态，输出中性的做 T 候选，并提供库存约束下的做 T 回测。系统不连接券商、不保存账号，也不会提交交易委托。

## 安全边界

- 固定为 `MONITOR_ONLY`，所有交易写请求都会被拒绝。
- 页面中的 `BUY_CANDIDATE`、`SELL_CANDIDATE` 仅表示满足候选门槛，不是交易指令。
- 行情延迟、断流、午休、收盘、状态未确认或分钟未完成时不会展示候选。
- 估值数据只提供中长期背景，不参与分钟候选门控。
- 回测结果用于核对信号和记账逻辑，不构成收益承诺。

## 目录与数据边界

代码和静态配置可以进入 Git：

- `src/etf_rotation/market_data.py`：分钟完成判定、行情校验、健康状态和 schema v3 历史存储。
- `src/etf_rotation/regime.py`：震荡、趋势和不确定状态识别。
- `src/etf_rotation/t_strategy.py`：候选门控和成本覆盖检查。
- `src/etf_rotation/t_backtest.py`：底仓、T 仓、可卖库存和配对账本回测。
- `src/etf_rotation/t_web.py`：单一后台生产者、只读 API 和 SSE。
- `src/etf_rotation/t_page.py`：增量页面、状态证据、图表和 tooltip。
- `data/monitor/watchlist.json`：监控标的和格宽。
- `data/monitor/etf_metadata.json`：ETF、指数和交易属性。
- `data/monitor/market_calendar.json`：休市日。
- `data/monitor/valuation.json`：可选的只读指数估值快照。

运行数据统一放在被 Git 忽略的 `var/monitor/`：

- `quotes.json`：最新行情快照。
- `quotes.jsonl`：schema v3 分钟总历史。
- `alerts.jsonl`：候选提示历史。
- `history/YYYY-MM-DD/`：按交易日拆分的行情和提示。
- `monitor.pid`、`monitor.out.log`、`monitor.err.log`：进程与日志。

Python 字节码、日志、PID、临时文件和整个 `var/` 均由 `.gitignore` 排除。

## 分钟行情和历史校验

系统只保存观测时间至少晚于分钟时间一分钟的已完成分钟。主键为标准化到 `Asia/Shanghai` 的：

```text
symbol + timestamp
```

后到的更晚观测可以覆盖同一主键的早期记录。每条历史记录包含：

- `schema_version = 3`
- `trading_date`
- `timestamp`
- `observed_at`
- `is_complete = true`
- 昨收、OHLC、VWAP 近似值、成交量和成交额
- 数据来源

写入前执行昨收日内一致性、涨跌幅与最小价位、OHLC、量价、连续交易时段和时区检查。总历史与按日历史通过事务日志、临时文件及原子替换一起更新；中断后的下一次读写会先恢复事务。

## 行情健康状态

每个标的独立分类，页面和候选门控使用同一时钟：

- `REALTIME`：实时；
- `DELAYED`：延迟；
- `OUTAGE`：断流、采集或校验失败；
- `LUNCH_BREAK`：午间休市；
- `CLOSED`：盘前、盘后、周末或休市日；
- `MISSING` / `UNKNOWN`：缺少行情或无法确认。

任何非 `REALTIME` 状态都会立即撤销旧候选。页面断线、SSE 错误或分钟接口失败也会同步撤销候选，恢复必须重新取得有效摘要和对应标的分钟行情。

## 市场状态识别

状态只使用连续的已完成分钟，不能跨午休、交易日或分钟缺口。

确认 `RANGE` 的硬条件包括最近 20 个连续分钟、有效穿越 VWAP 至少 2 次、上下两侧均有停留、单侧比例不高于约 70%、ER 较低、VWAP 斜率较小，并连续 3 个窗口成立。

确认 `UPTREND` / `DOWNTREND` 时要求 ER、VWAP 斜率、单侧比例和高低点推进方向一致，并连续 2 个窗口成立。其余状态统一为 `UNCERTAIN`。

页面展示 ER、单侧比例、穿越次数、VWAP 斜率、两侧停留数、震荡/趋势确认次数和阻断原因，便于追溯状态结论。

## 做 T 候选门控

只有同时满足下列条件才输出 `BUY_CANDIDATE` 或 `SELL_CANDIDATE`：

1. 行情健康为 `REALTIME`；
2. 使用的分钟均已完成；
3. `RANGE` 已连续确认；
4. 价格相对 VWAP 的偏离达到配置阈值；
5. 偏离开始收窄；
6. 相对昨收的距离满足风险门槛；
7. 预期毛边际覆盖买卖佣金、最低佣金和双边滑点。

未通过全部门槛时只显示 `DEVIATION_OBSERVE`（页面文案“偏离观察”）。默认一格来自唯一常量：

```text
grid_width_pct = 0.002 = 0.20%
三格 = 0.60%
五格 = 1.00%
```

图表分别绘制价格、VWAP、昨收，以及围绕每分钟 VWAP 动态计算的正负三格和正负五格轨道。

## 做 T 回测与信号粗回放

`/api/t-backtest` 是库存感知的做 T 回测：

- 每个 ETF 使用相同的初始底仓作为策略和基准；
- T 仓额度独立受限，配置支持底仓金额/份额和 T 仓比例/份额覆盖；
- 隔夜可卖份额与当日买入份额分账，按 ETF 元数据执行可卖延迟；
- 高抛后回补、低吸后卖出分别按 FIFO 等量配对；
- 信号只在下一个已完成分钟成交，不跨午休或交易日执行陈旧信号；
- 约束整手、成交量参与率、零成交、现金和可卖库存；
- 计算佣金、最低佣金、双边滑点、未完成腿和卖飞损失；
- 与“持有同样底仓且完全不操作”的期末权益比较。

没有完成配对时不会宣称跑赢。存在未完成腿时按期末价盯市并单独报告。

`/api/signal-replay` 是“信号粗回放”，只复查候选出现和下一分钟执行顺序，不输出收益或跑赢结论。旧 `/api/backtest` 暂时指向真正的做 T 回测，并带 `deprecated_alias=true`。

## 增量 API

- `GET /health`：服务健康和只读模式。
- `GET /api/snapshot`：轻量摘要，不含分钟 `points`。
- `GET /api/quotes?symbol=510300&since=0`：选中 ETF 的当日分钟增量。
- `GET /api/t-backtest`：库存感知做 T 回测。
- `GET /api/signal-replay`：无绩效结论的信号粗回放。
- `GET /api/history/dates`：历史交易日。
- `GET /api/history/quotes?date=YYYY-MM-DD&symbol=510300`：按日分钟。
- `GET /api/alerts?symbol=510300&limit=30`：候选历史。
- `GET /api/etf/510300/valuation`：ETF 关联指数和只读估值。
- `GET /api/events`：带 revision ID 的 SSE 摘要、增量和 reset 事件。

`since=0` 返回当日权威全集并标记 `reset=true`。跨交易日、点集缩减、行情缺失、服务重启或游标超出缓存时也返回 reset，浏览器先清空旧分钟再应用权威数据。普通 revision 只返回按 `symbol+timestamp` 合并后的 upsert。

## ETF 交易元数据和估值

`etf_metadata.json` 为每个标的保存交易所、资产类别、是否允许日内回转、可卖延迟天数、整手、最小价位、涨跌幅限制、成交量单位和关联指数。回测不再统一假设所有 ETF 的当日回转规则。

估值按指数代码旁路保存，包含 PE-TTM、PB、股息率、五年/十年分位、数据日期、来源和等级。缺少或过期估值会降级显示，不会阻断行情、候选或回测，也不会用示例数字代替真实数据。

## 清理并重建历史

旧历史已被视为不可信，不应用于评估策略。用验证过的收盘快照重建：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m etf_rotation.cli rebuild-history `
  --input data/monitor/quotes.json `
  --output var/monitor/quotes.jsonl `
  --metadata data/monitor/etf_metadata.json
```

迁移先在同一文件系统的临时目录中完整解析、校验、生成总历史和按日历史并执行审计；全部成功后才交换运行目录。失败时保留原目录。

本次提交只停止继续跟踪敏感运行数据，不能自动清除旧 Git 提交。若仓库曾公开，请先阅读 [Git 历史清理说明](docs/git-history-cleanup.md)，取得明确授权并备份后再单独处理。

## 启动、停止和测试

在项目根目录执行：

```powershell
.\scripts\start-monitor.ps1
.\scripts\stop-monitor.ps1
.\scripts\restart-monitor.ps1
```

脚本从自身位置推导项目根目录，静态配置读取 `data/monitor`，运行文件写入 `var/monitor`。

运行测试：

```powershell
.\scripts\run-tests.ps1
```
