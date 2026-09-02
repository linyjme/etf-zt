# 本地 ETF 做 T 监控

这是一个本地运行的 ETF 监控器：根页面 `/` 用于分钟级做 T 观察，独立页面 `/swing` 用于日线波段计划、提醒、本地手工持仓账本和回测。两者共用一个服务进程，但数据、状态和 revision 相互隔离。系统不连接券商，不读取券商账号，也不会提交交易委托。

## 安全边界

- 固定为 `MONITOR_ONLY`，所有交易写请求都会被拒绝。
- 页面中的 `BUY_CANDIDATE`、`SELL_CANDIDATE` 仅表示满足候选门槛，不是交易指令。
- 行情延迟、断流、午休、收盘、状态未确认或分钟未完成时不会展示候选。
- 估值数据只提供中长期背景，不参与分钟候选门控。
- 回测结果用于核对信号和记账逻辑，不构成收益承诺。
- `/swing` 的建仓、加仓、减仓和退出均为规则候选；用户只能记录自己已经在外部完成的成交，任何页面操作都不会自动下单。

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

波段运行数据独立放在同样被忽略的 `var/swing/`：完成日线为 `daily_quotes.jsonl`，账户投影为 `portfolio.json`，手工成交为追加式 `trades.jsonl`，提醒为 `alerts.jsonl`，回测缓存和本机签名材料位于 `backtests` 相关运行路径。这里可能包含真实资金、持仓和交易记录，不应提交、公开或通过日志分享；升级、迁移和清理前应先备份整个 `var/swing/`。静态观察列表与策略参数仍位于 `data/swing/`。

## 独立波段监控

波段正式状态只使用通过校验的已完成日线。交易日下午 15:10（上海时区）之后，系统才把当日视为可完成日线并更新正式状态；采集失败、数据不完整、公司行动无法可靠识别或历史审计失败时保持 fail-closed，不生成新的正式候选。当前盘中行情只可形成“接近计划区间”或“触及预设止损”等临时 overlay，不能改变正式日线结论；行情延迟、过期或断流时，临时 overlay 会立即撤销，正式计划标记为暂停执行。

首次使用应在页面初始化本地账户，填写现金和已有持仓；之后只记录用户已经成交的 BUY、SELL 或止损退出。`portfolio.json` 是可重建投影，`trades.jsonl` 是本地追加账本；损坏的投影可从有效账本重建，账本损坏时依赖持仓的候选会被阻断。初始化和成交接口要求明确确认与幂等键，仍然只写本机文件，不存在券商或自动交易端点。

波段回测提供两种只读口径：单标的回测使用调整后日线产生信号、下一交易日原始开盘价成交；组合回测让已启用标的共享现金，并使用 81 组参数进行 walk-forward 样本外检验。两者均计入手续费、最低佣金、买卖价差、滑点、整手、成交量参与率、资金和持仓约束，以完全不操作的可比基准衡量。公司行动证据不足时回测不可用，不会猜测复权换算。首次完整计算可能约需 35 秒；相同版本、输入数据和假设会读取本地校验缓存。

Wind 数据指南仅供开发人员在需要人工取数或核验时参考；当前运行服务不依赖 Wind，不会读取或保存 Wind API Key。生产行情仍由代码中配置的采集器提供，任何金融事实应以实际数据源返回为准。

波段 API（全部同源、本机 HTTP）包括：

- `GET /api/swing/snapshot`、`GET /api/swing/watchlist`：权威摘要和独立观察列表；
- `GET /api/swing/daily-quotes?symbol=...&since=...&limit=...`：已完成日线；
- `GET /api/swing/events`：独立 revision 的 SSE 更新；
- `GET /api/swing/portfolio`、`GET /api/swing/alerts`：本地账户投影与提醒历史；
- `GET /api/swing/backtest?scope=symbol&symbol=...` 或 `scope=portfolio`：单标的或共享现金组合回测；
- `POST /api/swing/watchlist`：启用或停用已验证标的；
- `POST /api/swing/portfolio/initialize`、`POST /api/swing/trades`、`POST /api/swing/trades/{event_id}/reverse`：本地账本写入与冲正；
- `POST /api/swing/alerts/{alert_id}/acknowledge`、`POST /api/swing/alerts/{alert_id}/ignore`：本地提醒处置。

除观察列表外的写入接口要求 `Idempotency-Key`；所有请求均为本地记账或提醒状态变更，没有下单接口。

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

## 实时数据与采集时段

实时页面和 `/api/quotes` 只提供上海时区当前自然日的已完成分钟；上一交易日及更早数据只能从历史接口按交易日读取，不会混入当日实时视图。

采集器按上海交易时段运行：

- 交易活动时段默认每 60 秒采集一次。首次采集失败会立即撤销已有候选，并按 60、120、240、300 秒指数退避；后续失败最多等待 300 秒，成功后恢复正常间隔。
- 午休期间暂停常规采集；若当天数据尚不完整，则继续按有界退避补采，补采成功后停止，等待下午开盘。
- 收盘后若已有完整的 15:00 分钟则停止采集；若缺少，则继续按有界退避补采，成功取得完整数据后停止。
- 盘前、周末和配置的休市日不发起行情请求。

午休或收盘阶段发生与行情展示无关的网络错误，不会把 `LUNCH_BREAK` 或 `CLOSED` 覆盖成 `OUTAGE`。实时采集必须运行在具备正常主机网络权限的环境中；受限沙箱中的只读进程可用于检查和测试，但不能作为实时采集部署方式。

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

脚本从自身位置推导项目根目录，在同一个隐藏 Python 进程中同时提供 `/` 和 `/swing`，并只写一个 PID。静态配置读取 `data/monitor` 与 `data/swing`，运行文件分别写入 `var/monitor` 与 `var/swing`，所有路径均以项目根目录为基准显式传入。

需要在无网络环境检查已有本地数据和两个页面时，可直接运行：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m etf_rotation.cli monitor --no-collect
```

`--no-collect` 同时禁用分钟行情与波段日线网络采集器；服务仍可只读加载本地数据、打开两个页面以及使用本地账本，但不会尝试访问行情网络。

旧的分钟历史或波段日线一旦确认受污染，不要继续用于提醒或评估策略。先停止服务并备份 `var/`，再用可信源重建对应历史；不要把删除当前文件误当成 Git 历史清理，也不要在未授权和未备份时重写仓库历史。

运行测试：

```powershell
.\scripts\run-tests.ps1
```
