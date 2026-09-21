# SWING_V11 深度波段优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留 SWING_V1 回滚基线的前提下，实现可解释、只读、人工确认的 SWING_V11_SHADOW ETF 波段策略，并接入数据质量、服务快照和波段页面。

**Architecture:** 新建聚焦的 `swing_v11.py` 规则模块，输入已完成日线、已完成周线和 14:45 准收盘证据，输出不可执行的结构化决策。服务层只负责组装数据和发布 read-model，页面只展示状态与证据。旧 V1/V2 代码不直接删除，先并行回放比较。

**Tech Stack:** Python 3.12+, dataclasses/enum/json；现有 `DailyBar`、`SwingService`、unittest 测试框架和内嵌 HTML 页面。

---

### Task 1: 锁定 V11 配置和数据契约

**Files:**
- Create: `src/etf_rotation/swing_v11.py`
- Create: `data/swing/v11_strategy.json`
- Test: `tests/test_swing_v11.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_load_v11_config_exposes_manual_capital_and_risk_defaults():
    config = load_v11_config(PROJECT_ROOT / "data/swing/v11_strategy.json")
    assert config.strategy_version == "SWING_V11_SHADOW"
    assert config.target_order_cny == 2000.0
    assert config.max_order_cny == 5000.0
    assert config.shadow_risk_rate == 0.003

def test_v11_decision_is_never_executable():
    decision = evaluate_v11([], config=load_test_config(), context=V11Context())
    assert decision.executable is False
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `py -3.12 -m unittest tests.test_swing_v11 -v`

Expected: import failure because `swing_v11.py` and the V11 API do not yet exist.

- [ ] **Step 3: Implement the minimal contract**

Define frozen dataclasses `V11Config`, `V11Context`, `V11Decision`, enums `V11State`, `V11Setup`, and strict JSON loader. The loader must reject unknown keys, non-finite numbers, target order below one lot, max order below target order, and any non-shadow strategy version. `evaluate_v11` must return `DATA_UNAVAILABLE` for empty input and always set `executable=False`.

Create JSON with `schema_version=1`, `strategy_version=SWING_V11_SHADOW`, `target_order_cny=2000`, `max_order_cny=5000`, `shadow_risk_rate=0.003`, `formal_risk_rate=0.01`, `minimum_daily_bars=250`, `weekly_confirmation_days=2`, `pullback_window_min=5`, `pullback_window_max=15`, `box_days=25`, `cooldown_sessions=5`.

- [ ] **Step 4: Run the focused test and verify it passes**

Run: `py -3.12 -m unittest tests.test_swing_v11 -v`

Expected: the two contract tests pass.

- [ ] **Step 5: Commit the contract**

```powershell
git add src/etf_rotation/swing_v11.py data/swing/v11_strategy.json tests/test_swing_v11.py
git commit -m "feat: add swing v11 decision contract"
```

### Task 2: Implement completed-week and indicator evidence

**Files:**
- Modify: `src/etf_rotation/swing_indicators.py`
- Modify: `src/etf_rotation/swing_v11.py`
- Test: `tests/test_swing_indicators.py`
- Test: `tests/test_swing_v11.py`

- [ ] **Step 1: Write failing tests**

```python
def test_weekly_context_excludes_incomplete_current_week():
    bars = bars_ending_on_thursday_with_current_week()
    snapshot = calculate_v11_indicators(bars)
    assert snapshot["weekly"]["as_of_trading_date"] == previous_friday

def test_volume_ratio_uses_prior_completed_twenty_day_average():
    bars = bars_with_last_day_volume_spike()
    snapshot = calculate_v11_indicators(bars)
    assert snapshot["volume"]["ratio20"] > 1.4

def test_kdj_is_diagnostic_and_not_a_required_entry_trigger():
    decision = evaluate_v11(bars_with_macd_recovery_only(), config, context)
    assert decision.evidence["macd_trigger"] is True
    assert decision.evidence["kdj_required"] is False
```

- [ ] **Step 2: Run tests to verify the new tests fail**

Run: `py -3.12 -m unittest tests.test_swing_indicators tests.test_swing_v11 -v`

Expected: current weekly and volume behavior fails at least one assertion, and the V11 indicator function is missing.

- [ ] **Step 3: Implement the smallest indicator changes**

Add an explicit `completed_weekly` builder that drops the last ISO week unless its Friday is present in the completed trading calendar. Add a V11 indicator context that returns MA10/20/60/250, BIAS20, Bollinger, ATR14, MACD event fields, RSI recovery fields, KDJ diagnostic fields, prior-20-day volume ratio, 20-day return, and data timestamps. Preserve existing indicator API behavior for V1/V2 callers.

- [ ] **Step 4: Run focused tests to green**

Run: `py -3.12 -m unittest tests.test_swing_indicators tests.test_swing_v11 -v`

Expected: all focused indicator tests pass.

- [ ] **Step 5: Commit indicator evidence**

```powershell
git add src/etf_rotation/swing_indicators.py src/etf_rotation/swing_v11.py tests/test_swing_indicators.py tests/test_swing_v11.py
git commit -m "feat: add completed-week and v11 indicator evidence"
```

### Task 3: Implement environment, A/B entries, and vetoes

**Files:**
- Modify: `src/etf_rotation/swing_v11.py`
- Modify: `data/monitor/etf_metadata.json`
- Test: `tests/test_swing_v11.py`

- [ ] **Step 1: Write failing behavior tests**

```python
def test_a_setup_accepts_one_macd_or_rsi_or_volume_confirmation():
    decision = evaluate_v11(a_pullback_with_macd_only(), config, healthy_context())
    assert decision.setup is V11Setup.A_PULLBACK
    assert decision.state is V11State.TECHNICAL_CANDIDATE

def test_b_setup_requires_twenty_five_day_box_and_zero_axis_macd():
    decision = evaluate_v11(breakout_without_box(), config, healthy_context())
    assert "BOX_NOT_CONFIRMED" in decision.blocked_reasons

def test_uncertain_environment_cannot_be_a_candidate():
    decision = evaluate_v11(valid_a_setup(), config, context_with_environment("UNKNOWN"))
    assert decision.state is V11State.UNCERTAIN
    assert "ENVIRONMENT_UNKNOWN" in decision.blocked_reasons

def test_relative_strength_between_negative_three_and_zero_only_halves_size():
    decision = evaluate_v11(valid_a_setup(rs20=-1.0), config, healthy_context())
    assert decision.evidence["forced_half_size"] is True
```

- [ ] **Step 2: Run the tests and confirm they fail for missing V11 behavior**

Run: `py -3.12 -m unittest tests.test_swing_v11 -v`

Expected: the new state/setup fields and reason codes are absent.

- [ ] **Step 3: Implement pure rule evaluation**

Implement explicit helpers for market health, A setup, B setup, relative-strength bands, liquidity/size/ex-date/holiday/cooldown vetoes, and reason-code accumulation. Missing evidence must fail closed, never become a passing zero. Add metadata fields `category`, `environment_index`, `correlation_group`, `fund_size_cny`, `avg_amount20_cny`, and `dividend_dates` with backward-compatible defaults that mark the symbol unverified rather than inventing values.

- [ ] **Step 4: Run tests to green**

Run: `py -3.12 -m unittest tests.test_swing_v11 -v`

Expected: all environment, A/B, veto, and uncertainty tests pass.

- [ ] **Step 5: Commit the rule engine**

```powershell
git add src/etf_rotation/swing_v11.py data/monitor/etf_metadata.json tests/test_swing_v11.py
git commit -m "feat: implement swing v11 environment and entries"
```

### Task 4: Implement risk sizing and position action state machine

**Files:**
- Modify: `src/etf_rotation/swing_v11.py`
- Test: `tests/test_swing_v11.py`

- [ ] **Step 1: Write failing tests**

```python
def test_order_size_is_lot_aligned_and_never_over_five_thousand():
    decision = evaluate_v11(valid_a_setup(price=4.6, stop=4.3), config, healthy_context(cash=10000))
    assert decision.planned_shares % 100 == 0
    assert decision.planned_shares * 4.6 <= 5000

def test_stop_distance_over_category_cap_blocks_instead_of_moving_stop():
    decision = evaluate_v11(valid_a_setup(stop_distance_pct=0.0711), config, healthy_context())
    assert "STOP_WIDTH_OVER_CAP" in decision.blocked_reasons

def test_action_priority_exit_beats_reduce_and_entry():
    action = evaluate_v11_position(position_with_stop_and_profit_signal(), config, healthy_context())
    assert action.action == "EXIT"

def test_t_plus_one_uses_sellable_shares_not_total_shares():
    action = evaluate_v11_position(position(shares=500, sellable_shares=100), config, healthy_context())
    assert action.planned_shares <= 100
```

- [ ] **Step 2: Run tests and verify red**

Run: `py -3.12 -m unittest tests.test_swing_v11 -v`

Expected: sizing and position API are not implemented.

- [ ] **Step 3: Implement sizing and actions**

Add lot-floor sizing that applies risk budget, 2,000 target notional, 5,000 hard notional, cash, symbol/group/exposure, and minimum-lot constraints. Add stop computation for A/B setups and reject over-cap stops. Add action evaluators for stop, environment, reduce, top-up, time rules, and entry, returning one highest-priority action with structured evidence. Never lower a stop; never use total shares when sellable shares are supplied.

- [ ] **Step 4: Run tests to green**

Run: `py -3.12 -m unittest tests.test_swing_v11 -v`

Expected: all sizing, stop, priority, and T+1 tests pass.

- [ ] **Step 5: Commit risk/action state machine**

```powershell
git add src/etf_rotation/swing_v11.py tests/test_swing_v11.py
git commit -m "feat: add swing v11 risk and position actions"
```

### Task 5: Integrate V11 into the service read model

**Files:**
- Modify: `src/etf_rotation/swing_service.py`
- Test: `tests/test_swing_service.py`
- Test: `tests/test_swing_health_contract.py`

- [ ] **Step 1: Write failing integration tests**

```python
def test_snapshot_contains_v11_shadow_without_replacing_formal_v1():
    snapshot = service_snapshot()
    assert snapshot["strategy"] == "SWING_V1"
    assert snapshot["v11"]["strategy_version"] == "SWING_V11_SHADOW"
    assert snapshot["v11"]["executable"] is False

def test_v11_uses_separate_quasi_close_and_completed_daily_dates():
    item = service_snapshot()["items"][0]
    assert item["v11"]["as_of_kind"] in {"COMPLETED_DAILY", "QUASI_CLOSE_1445"}
    assert item["v11"]["signal_data_date"] <= item["v11"]["generated_at_date"]
```

- [ ] **Step 2: Run the integration tests to verify red**

Run: `py -3.12 -m unittest tests.test_swing_service tests.test_swing_health_contract -v`

Expected: the read model has no `v11` section.

- [ ] **Step 3: Integrate read-only V11**

Add a `_v11_snapshot` service method that assembles completed bars, completed-week evidence, latest 14:45 evidence, metadata and holdings context. Publish it under each item as `v11`, and add aggregate `v11_summary` diagnostics. Do not replace `formal`, `shadow`, `alerts`, or portfolio ledger behavior. Any missing provider data must produce explicit `DATA_UNAVAILABLE` or `QUASI_CLOSE_UNAVAILABLE` reasons.

- [ ] **Step 4: Run integration tests to green**

Run: `py -3.12 -m unittest tests.test_swing_service tests.test_swing_health_contract -v`

Expected: V1 remains unchanged and V11 is present, read-only, and fail-closed.

- [ ] **Step 5: Commit service integration**

```powershell
git add src/etf_rotation/swing_service.py tests/test_swing_service.py tests/test_swing_health_contract.py
git commit -m "feat: publish swing v11 shadow read model"
```

### Task 6: Update the swing page for action-oriented evidence

**Files:**
- Modify: `src/etf_rotation/swing_page.py`
- Test: `tests/test_swing_page.py`

- [ ] **Step 1: Write failing page tests**

```python
def test_page_renders_v11_action_workbench_and_reason_codes():
    assert "SWING_V11_SHADOW" in PAGE
    assert "今日行动" in PAGE
    assert "数据时点" in PAGE
    assert "阻断原因" in PAGE

def test_page_never_labels_unverified_v11_as_buy():
    assert "可执行候选" in PAGE
    assert "自动下单" in PAGE
```

- [ ] **Step 2: Run the page tests and verify red**

Run: `py -3.12 -m unittest tests.test_swing_page -v`

Expected: the page source does not contain the V11 workbench labels.

- [ ] **Step 3: Implement the read-only UI**

Add a compact action summary, V11 state badge, A/B checklist, data-as-of badge, planned target/max notional, risk/stop cards, sellable-share display, and expandable reason-code explanation. Keep V1 formal state visible as rollback baseline. Render “观察/技术候选/可执行候选/持仓动作” instead of “买入” unless V11 explicitly passes every hard gate; V11 must always display manual confirmation and no auto-trade notice.

- [ ] **Step 4: Run page tests to green**

Run: `py -3.12 -m unittest tests.test_swing_page -v`

Expected: page tests pass and existing V1 page tests remain green.

- [ ] **Step 5: Commit page changes**

```powershell
git add src/etf_rotation/swing_page.py tests/test_swing_page.py
git commit -m "feat: add swing v11 action workbench"
```

### Task 7: Replay comparison and release gate

**Files:**
- Modify: `scripts/run_swing_shadow.py`
- Create: `docs/superpowers/reports/2026-09-21-swing-v11-shadow-validation.md`
- Test: `tests/test_swing_shadow_backtest.py`

- [ ] **Step 1: Write failing comparison test**

```python
def test_shadow_report_marks_v11_unverified_when_amount_or_crosscheck_is_missing():
    report = run_shadow_report(strategy_versions=("SWING_V1", "SWING_V2_SHADOW", "SWING_V11_SHADOW"))
    assert report["performance_claim_allowed"] is False
    assert "DATA_QUALITY" in report["blocking_reasons"]
```

- [ ] **Step 2: Run the test and verify red**

Run: `py -3.12 -m unittest tests.test_swing_shadow_backtest -v`

Expected: the report runner does not include V11 or the new release gate.

- [ ] **Step 3: Implement replay comparison**

Extend the existing shadow runner to include V11 decisions and counts, while keeping all claims blocked unless data is crosschecked, amount quality is actual, two OOS windows exist, and the test has no look-ahead. Record candidate count, blocked-reason distribution, average planned notional, stop-width rejection count, and missing-data count; do not turn shadow decisions into portfolio trades.

- [ ] **Step 4: Run targeted and full available tests**

Run: `py -3.12 -m unittest tests.test_swing_v11 tests.test_swing_shadow tests.test_swing_shadow_backtest tests.test_swing_service tests.test_swing_page -v`

Expected: all targeted tests pass. If the repository does not have a Python 3.12 runtime, record that environment blocker rather than claiming a passing suite.

- [ ] **Step 5: Write the validation report**

Record exact data coverage, current quality blockers, V1/V2/V11 candidate counts, and the fact that V11 remains shadow-only. Do not report a return or profitability conclusion until the data release gate is satisfied.

