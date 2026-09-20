# ETF 波段 V2 影子策略与数据质量设计

> 设计日期：2026-09-20  
> 状态：已获用户确认，等待进入实现计划  
> 适用范围：仅 ETF 波段监控，不接入自动交易，不改变现有长期持仓账本

## 1. 目标与非目标

### 1.1 目标

在保留 `SWING_V1` 正式规则的前提下，增加一个可审计的 `SWING_V2_SHADOW` 影子层，依次完成：

1. 清理并分级仓库内 ETF 日线数据；
2. 固化 MACD、KDJ、RSI、均线的计算口径，并补充方向、交叉和持续天数；
3. 将单日回调条件升级为可追踪的 5 个交易日机会事件；
4. 并行回放 V1、V2-A、V2-B、V2-C；
5. 在波段页面清晰区分正式策略、影子策略、技术观察和可执行状态；
6. 在至少两个样本外窗口通过验证后，才允许讨论是否切换正式策略。

### 1.2 非目标

- 不自动下单，不连接券商委托接口；
- 不把技术指标直接变成单独买卖信号；
- 不因为信号太少而绕过行情健康、账户状态、成本、整手和止损门控；
- 不覆盖已有成交、持仓快照、历史事件或已发布计划；
- 不把短历史 ETF 与完整历史 ETF 混合成一个收益结论；
- 不在数据质量未达标时调参或宣称策略有效。

## 2. 当前问题基线

2026-09-20 的只读复盘证据保存在：

- `outputs/swing-audit-evidence-20260920.json`
- `outputs/audit-swing-20260920.py`

复盘结果：

- 33 只已启用 ETF 没有发现无效日线或重复交易日；
- 共同有效样本为 257 个交易日，而现有 walk-forward 验证要求 630 个交易日；
- 33 只 ETF 均存在复权口径或独立交叉校验警告；
- 27 只 ETF 的成交额为估算值；
- 最新正式状态为 28 只 `TREND_BLOCKED`、4 只 `PULLBACK_WATCH`、1 只 `TRIAL_ENTRY_OBSERVE`；
- `SWING_V1` 正式状态没有使用 MACD、KDJ、RSI 作为买入硬门槛；当前页面指标属于观察层；
- `SNAPSHOT_ONLY` 会阻断全部可执行买入，技术观察和可执行计划必须分开显示。

因此，V2 首先是研究和解释层，不是放宽 V1 的补丁。

## 3. 数据层设计

### 3.1 数据分层

现有 `var/swing/daily_quotes.jsonl` 保留为运行时日线输入，不覆盖历史记录。新增研究清单：

```text
data/swing/research_manifest.json
data/swing/shadow/opportunities.jsonl
data/swing/shadow/runs/<run_id>.json
outputs/swing-research/<run_id>/
```

`research_manifest.json` 每个 ETF 至少记录：

```json
{
  "symbol": "512170",
  "history_start": "2023-08-09",
  "history_end": "2026-09-18",
  "bar_count": 756,
  "source": "...",
  "price_basis": "adjusted_ohlc",
  "crosscheck_status": "PENDING|PASSED|FAILED",
  "adjustment_status": "VERIFIED|REVIEW|UNKNOWN",
  "amount_quality": "PROVIDER_REPORTED|ESTIMATED|UNKNOWN",
  "research_status": "VERIFIED|USABLE_WITH_WARNINGS|EXCLUDED",
  "data_version": "sha256:..."
}
```

### 3.2 校验规则

研究数据接受前必须通过：

- `symbol + trading_date` 唯一；
- 只使用 `is_final=true` 的完成日线；
- 交易日严格递增；
- `low <= open/close <= high`；
- 昨收、涨跌幅和异常跳变被记录并可解释；
- 成交量、成交额及单位一致；
- 前复权比例变化被标记，不静默忽略；
- 至少一份独立来源交叉核验记录；
- 每次研究运行保存数据摘要哈希，历史修订生成新版本而不是覆盖旧结果。

异常价格变动不自动删除。它们进入 `REVIEW` 队列，由来源、复权和真实行情共同判断。

### 3.3 样本分组

- `FULL_SAMPLE`：单标的满足 630 个以上完成交易日，可以进入 walk-forward；
- `SHORT_SAMPLE`：满足指标计算但不足 630 日，只展示和做描述性分析；
- `EXCLUDED`：数据质量不满足研究要求，不进入收益结论。

ETF 和对应指数代理必须分开统计，不能用指数历史直接伪装成 ETF 实盘收益。

## 4. 指标层设计

### 4.1 固定计算口径

指标版本初始固定为 `INDICATORS_V1`：

- MACD：EMA12、EMA26、DEA9，柱体为 `2 * (DIF - DEA)`；
- KDJ：9/3/3，初始 K/D 为 50；
- RSI：Wilder RSI14；
- 均线：MA5、MA10、MA20、MA60；
- 所有指标只使用完成日线及同一复权 OHLC。

不直接把 Wind 的不同 RSI 周期字段与内部 RSI14 混用。外部参考值必须记录周期和来源。

### 4.2 新增派生字段

每个指标快照增加：

- `macd_histogram_previous_1/2`；
- `macd_histogram_rising_days`；
- `macd_dif_above_dea`；
- `macd_cross_age`；
- `rsi_previous_1/2`；
- `rsi_rising_days`；
- `kdj_k_above_d`；
- `kdj_cross_age`；
- `ma20_slope_pct_5d`；
- `ma60_slope_pct_10d`；
- `close_ma20_atr_distance`；
- `price_basis`、`indicator_version`、`data_version`。

指标历史不足时使用 `null` 和明确原因，不填充为 0。

## 5. 机会事件层

### 5.1 事件生命周期

事件状态固定为：

```text
PULLBACK_WATCH
  → RECOVERY_WATCH
  → TECHNICAL_CANDIDATE
  → PUBLISHED
  → WINDOW_OPEN
  → FILLED / PARTIAL / SKIPPED / EXPIRED / CANCELLED
```

事件只由完成日线建立，盘中数据只能更新窗口状态，不得重新创建历史事件。

### 5.2 机会事件字段

每个事件至少包含：

- `opportunity_id`；
- `symbol`；
- `strategy_version`；
- `indicator_version`；
- `data_version`；
- `pullback_start_date`；
- `recovery_date`；
- `expiry_date`；
- `entry_zone_low/high`；
- `anti_chase_limit`；
- `protective_stop`；
- 每个条件的布尔结果和实际数值；
- `status`；
- `cancel_reason` 或 `terminal_reason`。

同一标的同一回调只能产生一个机会 ID。历史数据修订后生成新版本事件，不覆盖已经发布的事件。

### 5.3 V2-A 规则

V2-A 是推荐的第一候选：

1. 中期趋势满足价格、MA20、MA60、MA60 斜率中的基本趋势条件；
2. 5 个交易日机会事件内出现回调；
3. 选择一个主触发：
   - MACD 柱连续两日改善且 DIF 不再下行；或
   - RSI 从 45 附近向上或重新站回 50；或
   - K>D 且 J 不超过 85；
4. 选择一个辅助确认：
   - 收盘重新站上 MA20；
   - 距离 MA20 不超过约 1 ATR；
   - 未出现明显追高；
5. 费用、流动性、资金、整手、账户状态和止损门控全部通过。

MACD、RSI、KDJ 不全部硬叠加。零值、缺失值或未知状态不能视为通过。

### 5.4 V2-B 评分策略

仅用于影子比较：

- 趋势 40 分；
- 回调位置 25 分；
- 动能 25 分；
- 估值与数据质量 10 分。

70 分以上为技术候选，55—69 分为观察，低于 55 分为暂不参与。数据健康、账户未知和止损约束仍是硬门槛，不能被评分覆盖。

### 5.5 V2-C 状态分离

影子识别趋势、震荡和不确定状态：

- 趋势状态使用趋势回调规则；
- 震荡状态不套用趋势突破买入规则；
- 不确定状态只显示观察，不生成候选。

## 6. 回放和验证设计

### 6.1 统一执行模型

V1、V2-A、V2-B、V2-C 使用同一执行模型：

- 日线收盘产生信号；
- 下一交易日执行；
- 默认观察和持有 5—20 个交易日；
- 佣金、点差、滑点、最低费用和整手约束统一；
- 单笔目标约 2,000 元，硬上限 5,000 元；
- 不使用未来价格或未来指标；
- 当日回转属性按 ETF 元数据处理。

只有具备可靠分钟数据时才评价具体午后执行窗口。只有日线时，结果标注为“执行时点近似”，不得当作真实成交收益。

### 6.2 比较结果

每次影子运行输出：

- 信号数、完成往返数和未完成腿；
- 扣费后净收益、最大回撤、胜率、盈亏比；
- 平均和尾部持仓天数；
- 假突破、错过上涨和过期事件；
- 每周提醒次数、人工处理次数和数据阻断次数；
- 各 ETF 分项结果和组合结果。

### 6.3 验收标准

正式切换前必须满足：

1. 至少两个样本外 walk-forward 窗口；
2. 研究数据不是 `EXCLUDED`；
3. 无未来数据泄露；
4. 扣除成本后收益不劣于 V1；
5. 最大回撤不明显恶化；
6. 完成往返少于 20 次时标记为样本不足；
7. 交易次数、人工负担和提醒质量同时达标；
8. V1 能够一键恢复。

若任一条件不满足，V2 保持影子状态，不得因为“提醒更多”而上线。

## 7. 页面与服务设计

### 7.1 API

`/api/swing/snapshot` 增加独立字段，不覆盖现有正式字段：

```json
{
  "formal": {"strategy_version": "SWING_V1", "state": "..."},
  "shadow": {
    "strategy_version": "SWING_V2_SHADOW",
    "variants": {"A": {}, "B": {}, "C": {}},
    "opportunity": {},
    "executable": false
  }
}
```

正式状态、影子状态、数据质量和账户状态分别返回，避免影子候选覆盖正式策略。

### 7.2 页面

页面分为：

- 正式策略：V1 状态、账户、风险、正式提醒；
- 影子策略：V2-A/B/C、触发理由、阻断理由、指标方向和回放结果；
- 数据质量：数据日期、来源、复权状态、交叉校验和样本分类。

文案统一：

- “推荐买入”改为“技术候选”；
- “试仓观察”改为“观察中，尚未完成确认”；
- `SNAPSHOT_ONLY` 明确显示“仅技术观察，不生成可执行买入”；
- 数据未知、行情过期、历史不足时不得显示“黄金窗口”或可执行数量。

### 7.3 通知

影子策略只发送研究通知，不发送成交指令。只有数据健康、完成日线、账户状态可用、成本和风险门控通过时，才允许进入候选通知；未知状态只发送阻断原因。

## 8. 文件与模块边界

计划新增或修改：

- `src/etf_rotation/swing_data.py`：研究数据校验和清单；
- `src/etf_rotation/swing_indicators.py`：指标方向和交叉事件；
- `src/etf_rotation/swing_strategy.py`：保持 V1，抽取可复用条件；
- `src/etf_rotation/swing_opportunities.py`：机会事件生命周期；
- `src/etf_rotation/swing_shadow.py`：V2-A/B/C；
- `src/etf_rotation/swing_backtest.py`：统一回放执行模型；
- `src/etf_rotation/swing_service.py`：正式与影子读模型；
- `src/etf_rotation/swing_page.py`：页面并排展示；
- `tests/test_swing_data_quality.py`；
- `tests/test_swing_indicator_reference.py`；
- `tests/test_swing_opportunities.py`；
- `tests/test_swing_shadow.py`；
- `tests/test_swing_shadow_backtest.py`；
- 对应 API、页面和回归测试。

模块之间通过只读快照和版本化 JSON 交换，不把影子序列塞入 `SwingDecision.evidence`。

## 9. 错误处理、回滚与发布策略

- 单个 ETF 数据失败只隔离该 ETF，不影响其他标的；
- 指标计算失败返回 `DATA_ERROR`，不填充 0；
- 影子策略异常时保留 V1 正常运行；
- `shadow_strategy_enabled` 默认开启只读展示；
- `formal_strategy_version` 默认保持 `SWING_V1`；
- 所有影子结果带策略、指标和数据版本；
- 删除或回滚影子文件不会修改成交账本和历史行情；
- 只有用户审核样本外报告后，才讨论将 V2 设为正式策略。

## 10. 测试计划

### 单元测试

- 指标公式、边界、缺失值和版本字段；
- MACD、RSI、KDJ 方向及交叉持续天数；
- 日线唯一性、OHLC、异常收益和复权状态；
- 机会事件创建、恢复、过期、取消和幂等；
- V2-A/B/C 条件互斥和解释字段；
- 无未来数据访问。

### 集成测试

- 33 只 ETF 快照序列化；
- 单个 ETF 数据损坏不影响其他 ETF；
- `SNAPSHOT_ONLY` 不生成可执行数量；
- V1 与影子结果并行返回；
- 页面不将影子候选渲染成正式买入。

### 回归测试

- 现有波段、做 T、持仓、通知和数据质量测试全部通过；
- `compileall` 通过；
- 页面浏览器检查正式/影子/阻断三类状态；
- 生成可复现的研究运行摘要和数据哈希。

## 11. 实施顺序

1. 第一阶段：数据清单、质量校验、来源和版本；
2. 第二阶段：指标方向、参考值和事件字段；
3. 第三阶段：V2-A/B/C 影子策略、机会生命周期和统一回放；
4. 第四阶段：API、页面、通知、测试和回滚开关；
5. 输出样本外报告，等待用户审核后再决定是否切换正式策略。

本设计不承诺收益，只保证信号口径、数据质量、解释能力和回滚边界清晰。
