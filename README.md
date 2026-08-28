# 本地做T监控

本目录只保留本地ETF做T监控系统，不连接券商、不自动下单，仅提供真实分钟行情采集、策略观察、提示追溯和只读回测。

## 安全边界

- 系统模式固定为 `MONITOR_ONLY`。
- 不保存券商账号，不连接券商接口，不提交交易委托。
- `BUY_REMINDER`、`SELL_REMINDER`、图表 `B/S` 都是观察提示，不代表实际成交。
- 页面只允许管理本地监控列表，其他写请求会被拒绝。

## 目录

- `src/etf_rotation/t_monitor.py`：行情模型、网格策略、状态识别、B/S观察点和历史存储
- `src/etf_rotation/quote_collector.py`：东方财富分钟行情采集和快照原子写入
- `src/etf_rotation/t_web.py`：本地网页、HTTP API、SSE实时刷新和只读回测
- `src/etf_rotation/cli.py`：监控服务启动入口及默认路径
- `data/monitor/`：当前快照、监控列表、行情历史和提示历史
- `tests/test_t_monitor.py`：做T监控测试
- `scripts/`：启动、停止、重启和测试脚本

## 核心策略

### 白线、黄线和昨收轴

- 白线：分钟价格 `price`。
- 黄线：当日累计成交均价 `average_price`，作为VWAP近似值。
- 昨收轴：`previous_close`。
- 每个标的使用黄线均价乘以 `grid_width_pct` 得到一格价格宽度。

```text
grid_size = average_price × grid_width_pct
白黄偏离格数 = |price - average_price| ÷ grid_size
距昨收格数 = |price - previous_close| ÷ grid_size
```

当前六只ETF在 `data/monitor/watchlist.json` 中均配置：

```text
grid_width_pct = 0.002，即每格0.2%
```

因此静态近似下，三格约为0.6%，五格约为1.0%；实际阈值随当时黄线均价变化。

### 基础提醒条件

策略先计算快速上冲，再检查三格/五格双阈值：

1. 最近两个行情点间隔不超过5分钟，且价格快速上冲达到5格：输出 `OBSERVE`，避免直接追随极端波动。
2. 白黄偏离至少3格，并且距昨收至少5格：进入方向判断。
3. 价格高于黄线：产生 `SELL_REMINDER`。
4. 价格低于黄线：产生 `BUY_REMINDER`。
5. 未满足条件：输出 `WAIT`。

### 20分钟市场状态识别

状态识别使用最近20个分钟点。少于20点时为 `UNCERTAIN`。

主要特征包括：

- 路径效率ER：首尾净位移除以逐点绝对变化总和。
- 黄线斜率：窗口首尾累计均价变化率。
- 单侧停留比例：价格持续位于黄线上方或下方的比例，中性带为黄线上下0.02%。
- 有效穿越次数：价格跨越黄线且偏离超过中性带才计数。
- 高低点推进：比较前后半窗口的高点与低点是否同向推进。
- K线重叠：相邻分钟K线区间交集占并集的比例。
- 失败突破：突破前半窗口区间后又快速收回。
- 量价停滞：后半窗口明显放量，但价格没有有效推进。

### 趋势判定

趋势候选包含四项，每项一分：

- 单侧停留比例不低于80%；
- 黄线斜率与价格方向一致，绝对推进达到0.1%；
- 路径效率ER不低于0.55；
- 高低点同向推进。

趋势分至少3分，并且单侧停留比例不低于80%，才判定为：

- `UPTREND`：上涨趋势；
- `DOWNTREND`：下跌趋势。

### 震荡判定

震荡候选包含六项，每项一分：

- 黄线斜率绝对值低于0.1%；
- 有效穿越黄线至少2次；
- K线重叠比例不低于50%，且没有高低点推进；
- 出现失败突破；
- 出现放量但价格不推进；
- 路径效率ER不高于0.30。

至少满足3项才判定为 `RANGE`。既不满足趋势条件，也不满足震荡条件时为 `UNCERTAIN`。

### 趋势状态阻断

状态识别完成后会修正逆势均值回归提醒：

- `UPTREND + SELL_REMINDER` 转为 `OBSERVE`，避免上涨趋势中过早高抛。
- `DOWNTREND + BUY_REMINDER` 转为 `OBSERVE`，避免下跌趋势中逆势低吸。

当前实现需要注意：`UNCERTAIN` 会显示“状态未确认”，但后端尚未强制阻断所有买卖提醒；这是后续需要继续收紧的策略边界。

## 图表B/S观察点

B/S只在 `RANGE` 状态下生成，并使用最近20个分钟点：

- `B`：上一点位于黄线下方至少1%，随后偏离开始向黄线收敛，但仍位于黄线下方至少0.02%。
- `S`：上一点位于黄线上方至少1%，随后偏离开始向黄线收敛，但仍位于黄线上方至少0.02%。

B/S是分时图反转观察点，不检查距昨收五格，不直接参与回测撮合，也不等同于 `BUY_REMINDER` 或 `SELL_REMINDER`。

## 只读回测

回测按每个ETF独立使用15,000元初始资金：

- 使用前一时点及以前的历史生成信号。
- 在下一分钟点成交，执行方式为 `NEXT_POINT`，避免未来函数。
- 买入按100份整数倍，接近全仓。
- 卖出时一次性卖出全部持仓。
- 买入佣金率：万1.2。
- 卖出佣金率：暂按万1.2。
- 最低佣金：0元，即免五。
- 单边滑点：0.1%。
- 输出累计收益、最大回撤、买入持有收益、相对持有超额收益、交易记录和已平仓胜率。

当前成本模型没有另外计算印花税、过户费、经手费或期末强制平仓；结果仅用于策略观察，不构成收益承诺。

## 行情采集和持久化

分钟行情来源为东方财富趋势接口，采集字段包括：

- 时间、价格、累计均价；
- 开盘价、最高价、最低价；
- 成交量、成交额；
- 昨收和采集时间。

当前快照使用临时文件、`fsync` 和原子替换写入，采集失败时不会用损坏内容覆盖旧快照。

### 数据文件

- `data/monitor/quotes.json`：最近行情快照
- `data/monitor/watchlist.json`：监控标的和格宽配置
- `data/monitor/quotes.jsonl`：行情总历史
- `data/monitor/alerts.jsonl`：提示总历史
- `data/monitor/history/YYYY-MM-DD/quotes.jsonl`：按交易日行情
- `data/monitor/history/YYYY-MM-DD/alerts.jsonl`：按交易日提示

行情去重键：

```text
symbol + timestamp
```

提示只保存 `BUY_REMINDER`、`SELL_REMINDER` 和 `OBSERVE`，去重键为：

```text
symbol + timestamp + action + strategy_version
```

提示可按交易日期、标的、动作和数量上限查询。总历史服务于策略恢复和回测，按日历史服务于页面日期查询和追溯。

## 页面与接口

服务默认监听：

```text
http://127.0.0.1:8765/
```

主要接口：

- `GET /health`：健康状态和只读模式
- `GET /api/snapshot`：当前六只ETF的行情、状态和提示
- `GET /api/history/dates`：可追溯交易日期
- `GET /api/history/quotes?date=YYYY-MM-DD&symbol=510300`：指定日期、标的分钟行情
- `GET /api/alerts?symbol=510300&limit=30`：指定标的提示历史
- `GET /api/backtest?symbol=510300`：指定标的只读回测
- `GET /events`：SSE实时快照

页面将行情时间、采集时间和页面刷新时间分开显示。行情超过60秒时前端显示过期警告，并隐藏黄金窗口展示。

## 启动、停止和重启

在 `F:\plan\money\zt` 下执行：

```powershell
.\scripts\start-monitor.ps1
.\scripts\stop-monitor.ps1
.\scripts\restart-monitor.ps1
```

脚本固定使用本目录的 `src` 和 `data\monitor`，避免误用旧项目路径。

## 测试

```powershell
.\scripts\run-tests.ps1
```

测试覆盖行情解析、快照保留、历史去重、策略阈值、趋势阻断、HTTP接口、提示追溯和回测费用。
