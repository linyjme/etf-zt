# ETF 波段 V1.1 审查问题修复计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复审查文档中阻断干净克隆、估值口径、V1.1 规则一致性、持仓安全和数据质量门控问题，使仓库可复现、规则可验证，并且只有在数据质量达标时才产生候选提醒。

**Architecture:** 先恢复可发布的源码完整性，再把估值计算、估值阶段和 V1.1 决策解耦。估值仅调整仓位和利润保护，不绕过技术硬门槛；数据质量作为独立的 fail-closed 门控，在服务入口统一生成并传入 V11Context。每一阶段先增加回归测试，再改实现，最后在干净克隆中运行全量测试和服务冒烟验证。

**Tech Stack:** Python 3.11+、unittest、现有 etf_rotation 服务、JSON 监控数据、PowerShell/Windows 与 Linux 兼容测试。

---

## 1. 范围、约束与现状基线

本计划依据审查文档和当前仓库状态编写。当前 main 工作树有大量历史遗留的未跟踪文件，本次实施只允许添加或修改计划中列出的文件，不得批量 git add .、删除未跟踪文件或重置用户改动。

需要保持的安全不变量：

- 项目仍然是只读监控和提醒系统，不自动下单；所有 V1.1 决策的 executable 必须为 False。
- 技术硬否决（数据缺失、趋势不成立、止损无效、T+1 不可卖）不能被估值阶段放宽。
- 未验证的数据只能产生证据或 OBSERVE，不能产生 TECHNICAL_CANDIDATE、建仓、补仓或持仓动作。
- 不使用系统当前时间直接写入业务判断；所有日期/时间都通过可注入的 today、交易日历和 Asia/Shanghai 时区计算。

当前审查基线：干净克隆中约 595 个测试被收集，但由于 3 个源码文件未提交导致导入错误；V1.1 专项测试约 73 个可通过。Linux 下依赖 Windows 批处理的 5 个 test_run_tests_script 失败属于测试平台策略问题，不应掩盖源码缺失问题。

## 2. 文件地图与职责边界

实施前先核对实际代码行号；以下是职责映射，不允许把未跟踪运行时数据混入源码提交。

| 领域 | 主要文件 | 目标 |
|---|---|---|
| 发布完整性 | src/etf_rotation/holdings_snapshot.py、swing_minutes.py、pr_page.py | 作为源码纳入 Git，保证干净克隆可导入 |
| 估值 | src/etf_rotation/valuation.py、data/monitor/valuation.json | ROE 期间、PR canonical、历史分位、过期状态 |
| 估值阶段 | valuation.py、swing_v11.py、swing_shadow.py | UNAVAILABLE/DEEP_VALUE/VALUE/FAIR/RICH/EXPENSIVE，只影响仓位和保护 |
| V1.1 核心规则 | src/etf_rotation/swing_v11.py | 环境、T/A/B、RSI、止损、E3/T25、S/C/E 动作 |
| 服务编排 | src/etf_rotation/swing_service.py | 指标扁平化契约、指数环境、RS20、估值阶段、数据质量门控 |
| 数据质量 | src/etf_rotation/swing_quality.py、必要时新增同目录小模块 | VERIFIED/UNVERIFIED 与可解释原因码 |
| 影子回放 | src/etf_rotation/swing_shadow.py、swing_shadow_backtest.py | 复用完整证据，禁止旧字段回退导致假信号 |
| 测试 | tests/test_swing_v11*.py、test_swing_service.py、test_swing_valuation.py、test_swing_shadow*.py、新增针对性测试 | 每个修复都有最小回归测试 |
| 测试脚本 | scripts/、tests/test_run_tests_script.py | 明确 Windows 执行与 Linux 收集策略 |

## 3. 依赖关系与交付顺序

必须按以下顺序推进，不能在源码未进入干净克隆前调策略阈值：

1. P0 源码完整性和跨平台测试策略。
2. P1 估值数据契约与 canonical PR。
3. P1 估值阶段先做证据层，不立即改变实盘候选行为。
4. P2 V1.1 规则与持仓动作的逐条对齐。
5. P3 数据质量 VERIFIED 门控，完成后才允许候选状态公开。
6. P4 影子观察、回归、文档和发布验证。

每个阶段都必须在阶段末运行目标测试；阶段之间使用独立提交，便于回滚和审查。

## 4. P0：恢复干净克隆可运行（阻断级）

### Task 0.1 — 建立“源码必须被跟踪”的回归测试

- [ ] 新增 tests/test_repository_completeness.py，只检查运行时必需的模块路径，不扫描或要求所有用户未跟踪文件。
- [ ] 测试明确断言以下路径可由 git ls-files 找到：
  - src/etf_rotation/holdings_snapshot.py
  - src/etf_rotation/swing_minutes.py
  - src/etf_rotation/pr_page.py
- [ ] 测试使用模块导入检查 etf_rotation.swing_service、etf_rotation.t_web，失败时给出缺失路径，不把本机绝对路径写入断言。
- [ ] 先运行测试确认当前基线能复现缺失源码问题，再进入下一步。

验证命令和预期：

~~~powershell
python -m unittest tests.test_repository_completeness -v
# 基线应明确报告 3 个未被 Git 跟踪的源码文件；修复后全部通过
~~~

### Task 0.2 — 只跟踪 3 个源码文件

- [ ] 检查 3 个文件内容只包含源码、模板和常量，不含本机路径、Wind key、运行时行情、提醒数据或 .pyc。
- [ ] 只对这 3 个精确路径执行 git add，禁止 git add .。
- [ ] 在隔离临时目录中用本地仓库创建干净克隆，确认三文件存在且可导入。
- [ ] 创建专门提交，例如 fix: track runtime source modules；不混入估值或策略改动。

验证命令和预期：

~~~powershell
git ls-files --error-unmatch src/etf_rotation/holdings_snapshot.py src/etf_rotation/swing_minutes.py src/etf_rotation/pr_page.py
git clone --no-local (git rev-parse --show-toplevel) $env:TEMP\etf-zt-clean-p0
Set-Location $env:TEMP\etf-zt-clean-p0
python -m unittest tests.test_repository_completeness -v
python -m unittest discover -s tests -t .
# 不再出现三模块导入错误；其余失败只能是已记录的跨平台测试策略项或真实回归
~~~

### Task 0.3 — 做依赖闭包审计，避免只补 3 个文件仍无法启动

- [ ] 在临时克隆中扫描 etf_rotation 下所有被服务入口、CLI、路由和测试引用的相对 import。
- [ ] 对每一个被 import 的路径执行 git ls-files --error-unmatch；若属于已交付功能的源码或测试，列入同一发布提交；若只是运行时缓存、实验脚本或用户本地资料，移出发布入口并补充可选依赖处理。
- [ ] 依赖闭包审计只在临时克隆或只读清单中进行，不对当前工作区执行 git clean。
- [ ] 把依赖闭包清单保存到发布报告，避免把“3 个文件存在”误判为整个服务可重建。

验证命令和预期：

~~~powershell
python -m unittest discover -s tests -t . -v
python -c "import etf_rotation.swing_service, etf_rotation.t_web"
# 无 ModuleNotFoundError；若有新的缺口，先补齐或明确隔离后再进入 P1
~~~

### Task 0.4 — 明确 Windows 脚本测试策略

- [ ] 检查 tests/test_run_tests_script.py 是否把“必须执行 .ps1”误当成所有平台的通用契约。
- [ ] Linux/macOS 下将 Windows 专属断言标记为跳过或单独分组；Windows 下仍必须真实运行脚本并检查退出码。
- [ ] README 或测试说明写清两类命令：跨平台 Python 测试、Windows 脚本集成测试。
- [ ] 不把“Linux 忽略 5 个失败”作为源码完整性的替代；干净克隆仍需零导入错误。

验证命令：

~~~powershell
python -m unittest tests.test_run_tests_script -v
# 当前平台只报告明确的 skip 或通过；Windows CI/本机执行脚本路径必须通过
~~~

## 5. P1：修复估值口径，并建立估值阶段（先证据后行为）

### Task 1.1 — ROE 期间与 PR canonical 的测试先行

- [ ] 在 tests/test_swing_valuation.py 增加以下案例：
  - H1 ROE 年化系数为 2.0，FY/TTM 为 1.0，Q1 为 4.0，Q3 为 4/3。
  - 未知 roe_period 不参与 pr_pe_roe，状态为不可用，而不是静默当 TTM。
  - PR_PE_PB = PE²/(PB*100) 是公开 canonical PR；pr_pe_roe 只在期间一致且与 PB 交叉结果误差不超过 20% 时保留。
  - roe_consistent=False 时仍保留原始输入和 pr_pe_pb，但不把不一致的 ROE 公式用于分级。
- [ ] 测试必须注入固定日期和输入记录，不能读取系统当前日期。

预期命令：

~~~powershell
python -m unittest tests.test_swing_valuation -v
# 新增案例在实现前失败；实现后全部通过，并保留已有公式回归
~~~

### Task 1.2 — 实现 Wind/NeoData 期间解析和数据修正

- [ ] 在 valuation.py 增加 ROE_PERIOD_FACTOR、期间校验和 roe_annualized 字段。
- [ ] Wind 的 38 条记录按来源实际期间补充 roe_period=H1（不得凭空把 H1 标为 TTM）；NeoData 记录标为 TTM，无法确认的记录显式标为未知并进入不可用状态。
- [ ] 公开结构同时保留原始 roe_ttm 字段以兼容页面，但所有计算使用规范化后的值。
- [ ] 更新 t_web.py、PR 页面和相关序列化测试，使其展示“原始 ROE、期间、年化 ROE、canonical PR、交叉一致性”。

### Task 1.3 — 过期状态和历史分位统一

- [ ] 为估值记录增加 as_of/status 的统一状态函数，签名包含可注入的 today 和 stale_days=10。
- [ ] 状态优先级：MISSING/INSUFFICIENT 保留；日期超过 10 个自然日为 STALE；有有效分位才为 OK；其余为 UNKNOWN。
- [ ] 5 年和 10 年分位不得平均：优先 10 年，缺失时回退 5 年，并输出 percentile_horizon_used。
- [ ] 行业、成长和跨境在没有历史分位时不得用宽基绝对 PR 阈值；阶段必须为 UNAVAILABLE。
- [ ] 增加过期、边界日、缺失分位和 10 年优先回退测试。

### Task 1.4 — 引入不可变 ValuationStage

- [ ] 在 valuation.py 或同职责的小模块定义不可变结构，至少包含：
  - stage：UNAVAILABLE/DEEP_VALUE/VALUE/FAIR/RICH/EXPENSIVE
  - size_multiplier
  - allow_topup
  - allow_b_breakout
  - s1_bias_limit
  - e3_session
  - reduce_at_r
- [ ] 分级优先使用历史分位：<=10 深值、<=30 价值、<70 合理、<90 偏贵、其余昂贵；无分位时只有宽基/黄金且 PR 期间一致才允许绝对阈值备用：<=0.8/1.2/2/3。
- [ ] 估值阶段只改变仓位、补仓、止盈保护和时间规则，不放宽趋势、回调、量价、数据质量等技术硬门槛。
- [ ] 为每个阶段写边界测试，确保 UNAVAILABLE 的默认行为是标准仓位、允许正常补仓、E3=10，不会误报便宜。

阶段行为矩阵必须固定并测试，避免只有枚举没有实际语义：

| 阶段 | 入场/补仓 | 仓位与保护 | 时间/退出 |
|---|---|---|---|
| UNAVAILABLE | 保持现有技术规则 | 标准仓位，允许标准补仓 | E3=10，使用现有保护 |
| DEEP_VALUE | 技术通过时补仓优先 | 不额外放大单笔风险 | E3=12；T25 低利润时优先跟踪而非机械退出 |
| VALUE/FAIR | 标准 A/B | 标准仓位和保护 | 现有 E3/T25 |
| RICH | A 可用，B 仅半仓 | size=0.5，禁止补仓，S1 阈值按类别扩大 | E3=7，1.5R 开始利润保护 |
| EXPENSIVE | 禁止 A；仅有量 B 半仓 | size=0.5，禁止补仓 | E3=5、盈利 1R 保护、MA10 失守退出、持仓上限 15 日 |

阶段矩阵只调节已通过技术硬门槛的结果；任何阶段都不能把缺失证据改成通过。

### Task 1.5 — 接入 V1.1，但先只展示证据

- [ ] 给 V11Context 增加可选 valuation_stage，保持旧构造器兼容。
- [ ] _v11_snapshot 读取关联指数估值并生成阶段和证据；不再用旧的 ATTRACTIVE/NEUTRAL 词汇匹配 LOW/NORMAL。
- [ ] 页面展示阶段、来源日期、分位口径和“仅影响仓位/保护，不覆盖技术否决”。
- [ ] 影子报告记录阶段分布和潜在动作，但默认配置不改变 live 候选输出；增加一周交易日观察的统计脚本/报告格式。

## 6. P2：逐条对齐 V1.1 核心规则

### Task 2.1 — 删除死代码并修复证据语义

- [ ] swing_v11.py 删除早期无效的 forced_half 计算，把 NaN/缺失 RS 判断放在最终计算之前。
- [ ] evidence 的 t4_return_60d 只有在 return_60d is not None and return_60d > 0 时为真；缺失必须同时产生 T4_EVIDENCE_UNAVAILABLE。
- [ ] UNVERIFIED 或缺失 category 立即产生 CATEGORY_UNVERIFIED 并 fail-closed；不再被当作行业类别进入 BIAS、止损或防守例外分支。
- [ ] 相关测试检查“证据显示值”和“实际 gate”一致，避免页面看起来满足但决策被另一套字段否决。

### Task 2.2 — 环境、相对强度和类别

- [ ] 服务层环境只读取 000300 与 000852 的独立历史，不再把当前 ETF 自身指标传给 classify_v11_environment。
- [ ] 两个指数至少一个健康时按手册判定 NEUTRAL；价格低于 MA60 且证据完整时为 DEFENSE；只有缺失必要证据才为 UNKNOWN。保留现有测试并新增单指数缺失、一个健康和两个不健康案例。
- [ ] 服务把 RS20 真实传入 V11Context.relative_strength_20，并测试 -3%~0 的半仓规则确实可达。
- [ ] 为全部 35 个监控标的补齐至少 BROAD/SECTOR/CROSS_BORDER/GOLD 四类；GROWTH/SMALL_CAP/DIVIDEND 若保留必须明确规则分支，不得只存标签不使用。
- [ ] 对 513050 等跨境标的补充跨境溢价/防守例外的元数据测试。

### Task 2.3 — T/A/B 入场规则与指标触发

- [ ] 实现/补齐 T3：周 MA10 近 3 个已完成周不能持续下降；T4：60 日涨幅必须大于 0。
- [ ] A1/A2 使用手册定义的回落 3%/5% 或 MA10×1.01 触发；A3 量能使用前 5 日与前 20 日、不含当天；A4 支持近 3 日站回 MA10 或 MA20，并加入长上影过滤。
- [ ] A6 RSI 改为“回调未破 40 且重新上穿 50”，删除旧的 35<=RSI<=60 and rising 兼容分支；A6 MACD 触发以规则手册为准，不额外隐式要求 DIF>DEA，除非测试和手册同时要求。
- [ ] A7 零轴下 MACD 只允许半仓，并在 evidence 中明确原因。
- [ ] B1 使用 MA250 斜率或 250 日涨幅大于 0；B2 检查后半段低点不创新低；B6 收盘不能高于布林上轨 1%；B8 突破首仓强制半仓。
- [ ] A/B 仍然是互为替代路径，不互相阻断；每个条目至少有通过、失败、证据缺失三个测试。

### Task 2.4 — 周线聚合和指标回退契约

- [ ] 修复 _completed_weekly：除最后一组外所有 ISO 周均视为已完成；最后一组只有在周五交易完成或交易日历确认本周无剩余交易日时才纳入。
- [ ] 使用现有 market_calendar.json，覆盖周五休市、节假日周、跨年周和当前未完成周。
- [ ] 将嵌套指标到扁平指标的转换放到 calculate_v11_indicators 或 evaluator 入口统一完成；删除 swing_service.py 中重复的约 40 行手工扁平化。
- [ ] evaluate_v11(bars, config, context_without_indicator) 必须和服务传入预计算 indicator 得出同样结论；新增直接调用回归测试。

### Task 2.5 — 止损、R 值和持仓动作

- [ ] 入场止损恢复为 max(回调低点×0.99, 入场价−2ATR)，再执行品类止损上限；止损价大于等于入场价直接拒绝。
- [ ] 1R 后止损上移到成本，2R 后转均线跟踪，不得重复每天产生同一 REDUCE；为跟踪线、保护止损和持久化 tracking_price 增加测试。
- [ ] E3 使用 holding_session >= stage.e3_session 且未创新高、profit_r < 1；之后才评估 T25。
- [ ] 给 V11Position 增加可选 entry_environment；只有“入场 ATTACK、当前 NEUTRAL、浮亏”才触发 E4。
- [ ] S1/S2 极端乖离或 RSI 过热不再要求 profit_r > 1；S1–S4、E1–E4、C1–C7 的优先级和互斥关系各有测试。
- [ ] profit_r 必须与 (current-entry)/initial_risk_per_share 一致（容差 0.05），或统一由内部风险字段计算；不一致时返回证据错误，不静默使用调用方任意 R。
- [ ] 服务 _position_context 在 planned risk 为 0 时使用 RISK_UNKNOWN，禁止用平均价的 1% 猜测 R；风险未知时不产生减仓/补仓判断。
- [ ] 防守期持仓路径补齐手册豁免：GOLD/CROSS_BORDER 不因普通防守 S5 被误减仓，但仍受自身数据质量、溢价率和止损硬门槛约束。

### Task 2.6 — 影子回放证据闭包与组合层约束

- [ ] 影子回放必须传入完整的 P1 指标证据（T3/T4、A3/A4、B1/B2/B6、MACD/RSI、量能、布林和周线），禁止缺字段时回退到旧的宽松字段；缺证据必须产生对应阻断原因。
- [ ] 增加影子回放与正式 evaluator 的同输入同结论测试，特别覆盖 B1/B6、A3/A4 和 RSI exact 规则，防止页面/回放显示候选而正式决策否决。
- [ ] 统一 14:45 准收盘判断的时区入口：任何带时区或无时区时间先转换到 Asia/Shanghai，再校验交易日、分钟完成状态和收盘窗口；增加跨时区边界测试。
- [ ] 增加组合级只读校验：单只 ETF 不超过 30%、持仓数量不超过 4 只、总仓位按 ATTACK/NEUTRAL/DEFENSE 配置限制；超过上限只能返回 `PORTFOLIO_LIMIT`，不自动平仓或下单。
- [ ] 补齐第五节的 TOP_UP 优先级：把补仓作为显式动作和 evidence，受估值阶段、风险已知、T+1、组合上限和冷却期共同约束，不能只在字符串中出现。

## 7. P3：建立真实 VERIFIED 数据质量门控

### Task 3.1 — 设计质量结果结构、证据收据和原因码

- [ ] 在 swing_quality.py 增加不可变质量结果（或等价结构），包含 status、reasons、checked_at、bar_count、last_completed_date、amount_quality、metadata_status、environment_history_status。
- [ ] 为每个数据源持久化校验收据：来源、校验时间、样本范围、成交额交叉核验结果、复权基准、计算版本。没有收据即使其他字段齐全也不能 VERIFIED。
- [ ] VERIFIED 必须同时满足：至少 250 根已完成日线；最后一根已完成 K 线为上一个有效交易日，或日历明确证明市场已收盘且没有遗漏；amount 为 PROVIDER_REPORTED；元数据校验通过；000300 与 000852 环境历史均可用；时间戳已统一到 Asia/Shanghai；收据无 warnings。
- [ ] 任一条件不满足则为 UNVERIFIED，并输出稳定的 DATA_QUALITY_* 原因码；不要只显示一个泛化的 UNVERIFIED。
- [ ] 质量判断接收注入的 today、calendar 和数据快照，禁止直接调用 datetime.now()。

### Task 3.2 — 服务接线并移除硬编码

- [ ] 删除 swing_service.py 中把 data_quality 固定写成 UNVERIFIED 的路径。
- [ ] _v11_snapshot 统一调用质量门控，再构造 V11Context；服务、影子回放和 API 返回使用同一个状态和原因集合。
- [ ] 只有 VERIFIED 才允许评估候选；STALE、MISSING_AMOUNT、MISSING_ENVIRONMENT 等必须显示为观察/暂停，不产生黄金窗口、建仓、补仓或持仓动作。
- [ ] 页面显示“已完成分钟/日线、最后有效日期、量能来源、指数环境数据、阻断原因”，让用户能区分行情断流、历史不足和元数据问题。

### Task 3.3 — 质量门控测试矩阵

- [ ] 增加完整通过案例，确认可以从 UNVERIFIED 到 VERIFIED。
- [ ] 增加逐项失败案例：249 根、最新日期落后、周末/节假日、amount 未提供、元数据错误、000300 缺失、000852 缺失、时区不一致、校验收据缺失。
- [ ] 增加 fail-closed 集成测试：即使技术指标全部满足，只要质量不通过，决策仍为 OBSERVE，且 executable=False。
- [ ] 增加 API/页面契约测试，确认原因码和状态不会被序列化丢失。

## 8. P4：证据观察、回归与发布

### Task 4.1 — 先影子观察，再启用估值行为

- [ ] 默认 valuation_stage_enforcement=false，先记录至少 5–7 个有效交易日的 stage、技术否决、候选数量和假设仓位变化。
- [ ] 观察报告按标的、环境、估值阶段统计：A/B 通过率、质量阻断率、S1–S4 触发率、候选后的实际收益和最大不利波动。
- [ ] 只有报告确认估值没有放宽硬否决、阶段边界无异常后，才允许通过配置启用 size/top-up/protection 行为；启用动作单独提交并可回滚。

### Task 4.2 — 全量测试和干净克隆验证

- [ ] 在当前工作树运行专项测试：

~~~powershell
python -m unittest tests.test_swing_v11 tests.test_swing_v11_p1 tests.test_swing_v11_p2 tests.test_swing_v11_p3 tests.test_swing_valuation tests.test_swing_service tests.test_swing_shadow tests.test_swing_shadow_backtest -v
~~~

- [ ] 创建全新临时克隆，只带已提交文件，运行：

~~~powershell
python -m unittest discover -s tests -t . -v
~~~

- [ ] 结果必须没有导入错误；平台专属脚本只能按 Task 0.4 显示 skip 或在对应平台通过。
- [ ] 启动服务并冒烟检查 /health、/api/valuations、/api/swing、波段页面和 PR 页面，确认无本机绝对路径、无密钥输出、无运行时文件依赖。
- [ ] 对所有候选接口断言 executable=False，对质量未验证的夹具断言没有 TECHNICAL_CANDIDATE。

### Task 4.3 — 文档、审计和提交

- [ ] README 增加：源码完整性、跨平台测试命令、估值期间口径、数据质量门控、影子观察流程和“无自动交易”声明。
- [ ] 在 docs/superpowers/reports/ 记录修复前后测试计数、失败分类、质量门控覆盖率和影子观察起止日期。
- [ ] 使用 git diff --check、敏感信息扫描和 git status --short；确认只提交计划内文件。
- [ ] 按下列顺序提交，提交信息保持单一职责：
  1. fix: track runtime source modules
  2. fix: normalize valuation periods and freshness
  3. feat: add valuation stage evidence
  4. fix: align v11 rules and position safety
  5. feat: enforce verified data quality gate
  6. docs: record review remediation and validation

## 9. 风险、回滚和验收标准

主要风险及处理：

- Wind H1 ROE 年化会显著改变部分指数 PR；先保留原始字段和 pr_pe_pb，使用影子报告确认，不能直接用历史旧阈值比较新值。
- 类别缺失改为 fail-closed 会减少候选；这是安全预期，先补齐元数据和原因展示，不通过放宽门槛解决。
- 环境从 ETF 自身改为 000300/000852 后，部分标的可能从 ATTACK 变为 NEUTRAL/DEFENSE；必须在影子回放中比较，不得为了增加信号回退旧逻辑。
- 质量门控可能在数据源不完整时长期保持 UNVERIFIED；页面必须显示具体缺项，数据补齐后能在固定测试中转为 VERIFIED。
- 估值阶段默认只做证据，任何行为启用都必须有独立配置和独立提交，便于回滚。

最终验收必须全部满足：

- 临时 fresh clone 的 git ls-files 可找到三项运行时源码；import etf_rotation.swing_service 和 etf_rotation.t_web 成功；/health、/pr 和相关 API 可用。
- 595 个测试按平台拆分：Python 业务测试 0 fail/error；Windows PowerShell 脚本测试在 Windows 运行，Linux 明确 skip/不统计。
- Wind H1 年化后，只有与 PB/PE 一致性在 20% 内才保留 pr_pe_roe；旧记录无 roe_period 不产生该值。
- 跨窗口分位不混用；行业、成长、跨境无历史分位时保持 UNAVAILABLE。
- 估值阶段只作为 evidence/position modifiers，任何技术或数据否决不可被绕过。
- 质量收据缺任一项仍为 UNVERIFIED；完整收据才为 VERIFIED。
- 关键边界测试覆盖 NaN RS、缺 T4、下 MA60 但斜率上升、E3 >=10、E4 entry environment、S1/S2 小于 1R、精确 RSI、RISK_UNKNOWN。
- 影子观察一周后才开启阶段行为；正式 V1 回归通过且所有决策 executable=False。
- 没有本机绝对路径、密钥、运行时行情或 .pyc 被提交；远端 main 可从全新 clone 重建服务。
