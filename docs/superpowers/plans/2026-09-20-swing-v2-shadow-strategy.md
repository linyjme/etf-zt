# ETF 波段 V2 影子策略 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变 `SWING_V1` 正式策略和成交账本的前提下，完成 ETF 日线数据分级、指标方向增强、5 日机会事件、V2-A/B/C 影子回放及页面展示。

**Architecture:** `SWING_V1` 继续作为正式只读策略；新增研究质量层、机会事件层和影子策略层。影子层读取同一份已完成日线，但通过独立版本化快照返回，不写入正式成交计划、不覆盖正式状态。只有样本外验证通过并经用户审核后，才讨论切换正式策略。

**Tech Stack:** Python 3.12、现有 `DailyBar`/`SwingBacktester`、JSONL/JSON 版本化读写、现有 HTTP 服务和内嵌 HTML/JavaScript 页面、unittest discovery。

---

## 文件地图

- Create: `src/etf_rotation/swing_research.py` — 研究数据质量、研究分级、manifest 摘要哈希。
- Create: `src/etf_rotation/swing_opportunities.py` — 5 日回调机会事件和幂等生命周期。
- Create: `src/etf_rotation/swing_shadow.py` — V2-A、V2-B、V2-C 只读评估器。
- Create: `src/etf_rotation/swing_shadow_backtest.py` — 影子策略批量回放和对照报告。
- Create: `scripts/build_swing_research_manifest.py` — 生成研究 manifest，不修改运行时历史。
- Create: `scripts/run_swing_shadow.py` — 生成带数据哈希的影子回放结果。
- Create: `data/swing/shadow_strategy.json` — V2 影子参数，严格版本化，不能被 V1 配置读取。
- Create: `data/swing/research_manifest.json` — 每只 ETF 的研究分级和校验结果。
- Create: `data/swing/shadow/` — 机会事件和影子运行结果目录。
- Create: `tests/test_swing_research.py` — 研究质量分类、哈希和 manifest。
- Create: `tests/test_swing_indicator_reference.py` — 指标方向、交叉、版本和无未来数据。
- Create: `tests/test_swing_opportunities.py` — 机会事件生命周期。
- Create: `tests/test_swing_shadow.py` — V2-A/B/C 条件和解释字段。
- Create: `tests/test_swing_shadow_backtest.py` — 回放执行模型和指标对照。
- Modify: `src/etf_rotation/swing_data.py` — 暴露稳定的完成日线迭代和质量字段读取。
- Modify: `src/etf_rotation/swing_indicators.py` — 增加历史方向和交叉派生字段。
- Modify: `src/etf_rotation/swing_service.py` — 返回独立 `formal`/`shadow` 读模型。
- Modify: `src/etf_rotation/swing_page.py` — 展示正式策略与影子候选的差异。
- Modify: `tests/test_swing_service.py`、`tests/test_swing_page.py`、`tests/test_swing_web.py` — API、页面和回归覆盖。

## 执行约束

- `data/swing/strategy.json` 保持 `SWING_V1`，不在本计划中修改其交易阈值。
- 影子结果不能生成成交账本事件、可执行份额或自动通知委托。
- 现有工作区有大量未提交改动；每次提交只加入本任务明确列出的文件。
- 每个任务先写失败测试，再写最小实现，再运行聚焦测试，最后提交。
- 研究输出包含数据摘要哈希；输入历史变化时必须生成新运行 ID。

## Task 1: 研究质量类型和历史 manifest

**Files:**
- Create: `src/etf_rotation/swing_research.py`
- Create: `tests/test_swing_research.py`
- Modify: `src/etf_rotation/swing_data.py:DailyHistoryStore`（只暴露已完成记录读取，不改变 upsert 规则）

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date
from etf_rotation.swing_research import assess_history, ResearchStatus
from tests.swing_helpers import swing_strategy_bars

def test_history_with_unique_completed_bars_is_usable_with_warnings():
    result = assess_history(
        swing_strategy_bars(130),
        crosscheck_status="PENDING",
        adjustment_status="UNKNOWN",
        amount_quality="ESTIMATED",
    )
    assert result.status is ResearchStatus.USABLE_WITH_WARNINGS
    assert result.bar_count == 130
    assert result.duplicate_dates == ()
    assert result.data_version.startswith("sha256:")

def test_duplicate_date_is_excluded_and_is_reported():
    bars = list(swing_strategy_bars(130))
    bars[1] = bars[0]
    result = assess_history(bars, crosscheck_status="PASSED", adjustment_status="VERIFIED", amount_quality="PROVIDER_REPORTED")
    assert result.status is ResearchStatus.EXCLUDED
    assert result.duplicate_dates

def test_short_history_is_not_a_walk_forward_sample():
    result = assess_history(swing_strategy_bars(257), crosscheck_status="PASSED", adjustment_status="VERIFIED", amount_quality="PROVIDER_REPORTED")
    assert result.status is ResearchStatus.SHORT_SAMPLE
    assert result.walk_forward_eligible is False
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' -m unittest tests.test_swing_research -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'etf_rotation.swing_research'`.

- [ ] **Step 3: Implement the research result and classifier**

Add a frozen result type and deterministic classifier:

```python
class ResearchStatus(StrEnum):
    VERIFIED = "VERIFIED"
    USABLE_WITH_WARNINGS = "USABLE_WITH_WARNINGS"
    SHORT_SAMPLE = "SHORT_SAMPLE"
    EXCLUDED = "EXCLUDED"

@dataclass(frozen=True)
class ResearchAssessment:
    status: ResearchStatus
    bar_count: int
    history_start: date | None
    history_end: date | None
    duplicate_dates: tuple[str, ...]
    warnings: tuple[str, ...]
    walk_forward_eligible: bool
    data_version: str

def assess_history(bars, *, crosscheck_status, adjustment_status, amount_quality,
                   minimum_walk_forward_bars=630) -> ResearchAssessment:
    materialized = tuple(bars)
    symbol = materialized[0].symbol if materialized else None
    duplicate_dates = tuple(sorted(
        str(day) for day, count in Counter(
            item.trading_date for item in materialized
        ).items() if count > 1
    ))
    canonical = [item.to_dict() for item in materialized]
    data_version = "sha256:" + sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    warnings = tuple(sorted(_quality_warnings(
        duplicate_dates, crosscheck_status, adjustment_status, amount_quality,
    )))
    count = len(materialized)
    status = _classify_status(count, duplicate_dates, warnings, minimum_walk_forward_bars)
    return ResearchAssessment(
        status=status, bar_count=count,
        history_start=materialized[0].trading_date if materialized else None,
        history_end=materialized[-1].trading_date if materialized else None,
        duplicate_dates=duplicate_dates, warnings=warnings,
        walk_forward_eligible=status is ResearchStatus.VERIFIED,
        data_version=data_version,
    )
```

The implementation must validate `DailyBar` values, require one symbol, preserve duplicate dates in the warning, and hash canonical UTF-8 JSON with sorted keys. It must not write files.

- [ ] **Step 4: Run the focused tests and verify success**

Run the same command. Expected: 3 tests PASS.

- [ ] **Step 5: Commit the isolated research module**

```powershell
git add src/etf_rotation/swing_research.py src/etf_rotation/swing_data.py tests/test_swing_research.py
git commit -m "feat: classify swing research history"
```

## Task 2: Build the data manifest and Wind cross-check receipt

**Files:**
- Create: `scripts/build_swing_research_manifest.py`
- Create: `data/swing/research_manifest.json`
- Modify: `src/etf_rotation/wind_history.py` only if a stable archival manifest field is missing
- Test: `tests/test_swing_research.py`

- [ ] **Step 1: Add a failing manifest test**

```python
def test_manifest_contains_every_enabled_symbol_without_promoting_wind_archive(tmp_path):
    manifest = build_manifest(history_path, watchlist_path, metadata_path, output_path=tmp_path / "manifest.json")
    assert {item["symbol"] for item in manifest["items"]} == enabled_symbols
    assert manifest["runtime_history_unchanged"] is True
    assert all(item["data_version"].startswith("sha256:") for item in manifest["items"])
```

- [ ] **Step 2: Run the test and verify the missing builder failure**

Run:

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' -m unittest tests.test_swing_research.ResearchManifestTests.test_manifest_contains_every_enabled_symbol_without_promoting_wind_archive -v
```

Expected: FAIL because `build_manifest` is not defined.

- [ ] **Step 3: Implement the manifest builder**

The script must load the existing watchlist and metadata, group `DailyBar` records, call `assess_history`, and atomically write exactly one JSON document containing `schema_version`, `generated_at`, `runtime_history_unchanged`, `history_path`, `items`, and `source_receipts`. It must classify existing Tencent/legacy data as `USABLE_WITH_WARNINGS` or `SHORT_SAMPLE` unless an independent receipt exists; it must never infer `VERIFIED` from a source label alone.

Wind receipts are read from `var/swing/wind/<batch>/manifest.json` when present. Missing Wind receipts remain `PENDING`; they do not silently become passed.

- [ ] **Step 4: Run the builder against the repository data**

Run:

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' scripts/build_swing_research_manifest.py --end-date 2026-09-18 --output data/swing/research_manifest.json
```

Expected: exit code 0, 33 enabled symbols in the manifest, no changes to `var/swing/daily_quotes.jsonl`, and explicit short-sample/warning statuses.

- [ ] **Step 5: Commit the manifest tool and generated research manifest**

```powershell
git add scripts/build_swing_research_manifest.py data/swing/research_manifest.json tests/test_swing_research.py
git commit -m "feat: add swing research manifest"
```

## Task 3: Add indicator direction and cross-event context

**Files:**
- Modify: `src/etf_rotation/swing_indicators.py`
- Create: `tests/test_swing_indicator_reference.py`
- Modify: `tests/test_swing_indicators.py`

- [ ] **Step 1: Write failing direction tests**

```python
def test_indicator_context_exposes_recent_direction_without_lookahead():
    bars = swing_strategy_bars(140)
    context = calculate_indicator_context(bars, lookback=3)
    assert context["indicator_version"] == "INDICATORS_V1"
    assert len(context["recent"]) == 3
    assert context["latest"]["as_of_trading_date"] == bars[-1].trading_date.isoformat()
    assert "macd_histogram_rising_days" in context["latest"]

def test_indicator_context_changes_when_only_the_last_completed_bar_changes():
    bars = swing_strategy_bars(140)
    first = calculate_indicator_context(bars[:-1], lookback=3)
    second = calculate_indicator_context(bars, lookback=3)
    assert first["latest"]["as_of_trading_date"] != second["latest"]["as_of_trading_date"]
```

- [ ] **Step 2: Run the focused test and verify failure**

Run:

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' -m unittest tests.test_swing_indicator_reference -v
```

Expected: FAIL with an undefined `calculate_indicator_context`.

- [ ] **Step 3: Implement `calculate_indicator_context`**

Add a pure function with this interface:

```python
def calculate_indicator_context(
    bars: Sequence[DailyBar], *, lookback: int = 3,
) -> dict[str, object]:
    """Return latest and recent completed-bar indicator evidence."""
```

Reuse the existing formula functions, calculate all series from the full completed sequence, expose only the requested trailing window, and calculate crossing age from the newest completed bar backward. Return `status`, `reason`, `indicator_version`, `price_basis`, `as_of_trading_date`, `bar_count`, `recent`, and `latest`. Reject non-ascending dates exactly as the existing snapshot function does.

- [ ] **Step 4: Add reference fixtures and run focused tests**

Record the Wind 2026-09-18 `512170.SH` MACD/KDJ values in a test fixture with source and period labels. Do not compare Wind RSI6/RSI12 to internal RSI14. Run:

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' -m unittest tests.test_swing_indicators tests.test_swing_indicator_reference -v
```

Expected: all existing indicator tests and new direction tests PASS.

- [ ] **Step 5: Commit the indicator context**

```powershell
git add src/etf_rotation/swing_indicators.py tests/test_swing_indicators.py tests/test_swing_indicator_reference.py
git commit -m "feat: expose swing indicator direction context"
```

## Task 4: Implement the 5-day opportunity event lifecycle

**Files:**
- Create: `src/etf_rotation/swing_opportunities.py`
- Create: `tests/test_swing_opportunities.py`
- Create: `data/swing/shadow/opportunities.jsonl`

- [ ] **Step 1: Write failing event tests**

```python
def test_pullback_event_recovers_once_and_is_idempotent():
    first = update_opportunity(previous=None, observation=pullback_observation("2026-09-10"))
    second = update_opportunity(previous=first, observation=recovery_observation("2026-09-12"))
    repeat = update_opportunity(previous=second, observation=recovery_observation("2026-09-12"))
    assert second.opportunity_id == repeat.opportunity_id
    assert second.status is OpportunityStatus.TECHNICAL_CANDIDATE

def test_event_expires_after_five_completed_sessions():
    event = update_opportunity(previous=None, observation=pullback_observation("2026-09-10"))
    expired = update_opportunity(previous=event, observation=neutral_observation("2026-09-18"))
    assert expired.status is OpportunityStatus.EXPIRED
    assert expired.cancel_reason == "RECOVERY_WINDOW_EXPIRED"

def test_event_does_not_read_future_bars():
    event = update_opportunity(previous=None, observation=pullback_observation("2026-09-10"))
    assert event.recovery_date is None
```

- [ ] **Step 2: Run the tests and verify failure**

Run:

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' -m unittest tests.test_swing_opportunities -v
```

Expected: FAIL because the opportunity module and statuses do not exist.

- [ ] **Step 3: Implement immutable event types and transitions**

Use these stable names:

```python
class OpportunityStatus(StrEnum):
    PULLBACK_WATCH = "PULLBACK_WATCH"
    RECOVERY_WATCH = "RECOVERY_WATCH"
    TECHNICAL_CANDIDATE = "TECHNICAL_CANDIDATE"
    PUBLISHED = "PUBLISHED"
    WINDOW_OPEN = "WINDOW_OPEN"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    SKIPPED = "SKIPPED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"

@dataclass(frozen=True)
class OpportunityEvent:
    opportunity_id: str
    symbol: str
    strategy_version: str
    pullback_start_date: date
    recovery_date: date | None
    expiry_date: date
    status: OpportunityStatus
    conditions: Mapping[str, bool | float | int | str | None]
    cancel_reason: str | None
    data_version: str
```

`opportunity_id` is a deterministic SHA-256 prefix of symbol, pullback start, price basis, ATR algorithm, strategy version, and data version. Transitions accept only completed observations and never mutate an existing event.

- [ ] **Step 4: Run focused event tests**

Run the same command. Expected: 3 tests PASS.

- [ ] **Step 5: Commit the event lifecycle**

```powershell
git add src/etf_rotation/swing_opportunities.py tests/test_swing_opportunities.py data/swing/shadow/opportunities.jsonl
git commit -m "feat: track swing opportunity events"
```

## Task 5: Add V2-A, V2-B and V2-C shadow evaluators

**Files:**
- Create: `src/etf_rotation/swing_shadow.py`
- Create: `data/swing/shadow_strategy.json`
- Create: `tests/test_swing_shadow.py`

- [ ] **Step 1: Write failing variant tests**

```python
def test_v2a_uses_one_momentum_trigger_and_one_confirmation():
    decision = evaluate_shadow(bars, variant=ShadowVariant.V2_A, config=shadow_config, context=flat_context)
    assert decision.strategy_version == "SWING_V2_SHADOW"
    assert decision.executable is False
    assert decision.evidence["momentum_trigger_count"] <= 1
    assert decision.evidence["confirmation_count"] <= 1

def test_v2a_does_not_require_macd_rsi_and_kdj_simultaneously():
    decision = evaluate_shadow(bars_with_macd_and_rsi_only, variant=ShadowVariant.V2_A, config=shadow_config, context=flat_context)
    assert decision.state in {ShadowState.TECHNICAL_CANDIDATE, ShadowState.OBSERVE}
    assert decision.evidence["all_three_indicators_required"] is False

def test_snapshot_only_and_unknown_quality_are_never_executable():
    decision = evaluate_shadow(bars, variant=ShadowVariant.V2_A, config=shadow_config, context=snapshot_only_context)
    assert decision.executable is False
    assert "SNAPSHOT_ONLY" in decision.blocked_reasons
```

- [ ] **Step 2: Run focused tests and verify failure**

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' -m unittest tests.test_swing_shadow -v
```

Expected: FAIL because `swing_shadow.py` and `shadow_strategy.json` do not exist.

- [ ] **Step 3: Add strict shadow configuration**

Create `data/swing/shadow_strategy.json` with this exact initial shape:

```json
{
  "schema_version": 1,
  "strategy_version": "SWING_V2_SHADOW",
  "event_window_sessions": 5,
  "rsi_lower": 45.0,
  "rsi_upper": 65.0,
  "kdj_j_max": 85.0,
  "atr_distance_max": 1.0,
  "macd_histogram_rising_days": 2,
  "min_walk_forward_bars": 630,
  "variants": ["V2_A", "V2_B", "V2_C"]
}
```

The loader must reject extra keys, missing keys, non-positive windows, invalid percentages, and unknown variants. It must never load this file as `SwingStrategyConfig`.

- [ ] **Step 4: Implement the three read-only evaluators**

Expose:

```python
def evaluate_shadow(
    bars: Sequence[DailyBar], *, variant: ShadowVariant,
    config: ShadowConfig, context: ShadowContext,
) -> ShadowDecision:
```

V2-A uses the 5-day opportunity event, current V1 trend evidence, one primary momentum trigger, one secondary confirmation, anti-chase, data quality, cost and account gates. V2-B returns a score and explanations but still fails closed on health gates. V2-C selects trend/range/uncertain mode and refuses a trend entry in range or uncertain mode. All decisions include `state`, `executable=False`, `blocked_reasons`, `evidence`, `data_version`, and `indicator_version`.

- [ ] **Step 5: Run the focused shadow tests**

Run the same command. Expected: all new variant tests PASS.

- [ ] **Step 6: Commit the shadow evaluator**

```powershell
git add src/etf_rotation/swing_shadow.py data/swing/shadow_strategy.json tests/test_swing_shadow.py
git commit -m "feat: add swing shadow strategy variants"
```

## Task 6: Add unified shadow backtesting and report generation

**Files:**
- Create: `src/etf_rotation/swing_shadow_backtest.py`
- Create: `scripts/run_swing_shadow.py`
- Create: `tests/test_swing_shadow_backtest.py`
- Modify: `src/etf_rotation/swing_backtest.py` only for a shared, backward-compatible execution helper

- [ ] **Step 1: Write failing no-lookahead and cost-parity tests**

```python
def test_shadow_replay_uses_next_trading_day_execution():
    result = replay_variant(history, variant=ShadowVariant.V2_A, costs=costs)
    assert all(trade.execution_date > trade.signal_date for trade in result.trades)

def test_all_variants_use_identical_cost_assumptions():
    results = replay_all_variants(history, costs=costs)
    assert {result.execution_assumptions for result in results.values()} == {results["V1"].execution_assumptions}

def test_short_history_is_inconclusive_not_profitable():
    result = replay_variant(history[:257], variant=ShadowVariant.V2_A, costs=costs)
    assert result.validation_status == "INSUFFICIENT_SAMPLE"
    assert result.performance_claim_allowed is False
```

- [ ] **Step 2: Run the tests and verify failure**

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' -m unittest tests.test_swing_shadow_backtest -v
```

Expected: FAIL because the shadow replay API does not exist.

- [ ] **Step 3: Implement the replay result and variant runner**

Expose:

```python
def replay_variant(history_by_symbol, *, variant, costs, initial_equity=100000.0):
    validated = validate_shadow_history(history_by_symbol)
    events = build_shadow_events(validated, variant=variant)
    trades = execute_next_session(events, validated, costs=costs, initial_equity=initial_equity)
    return summarize_shadow_result(trades, variant=variant, costs=costs)

def replay_all_variants(history_by_symbol, *, costs, initial_equity=100000.0):
    return {
        variant.value: replay_variant(
            history_by_symbol, variant=variant, costs=costs,
            initial_equity=initial_equity,
        )
        for variant in (ShadowVariant.V1, ShadowVariant.V2_A, ShadowVariant.V2_B, ShadowVariant.V2_C)
    }
```

Use the existing `SwingBacktester` execution assumptions, signal only through the completed signal date, execute at the next valid trading day, honor ETF metadata for lot size and intraday turnaround, and record fees, spread, slippage, incomplete legs, rejected trades, and validation status. Do not treat a daily open as an afternoon fill.

- [ ] **Step 4: Implement walk-forward folds and acceptance fields**

For each eligible symbol, create 504-session train and 126-session test folds with 126-session steps. For fewer than 630 common sessions return `INSUFFICIENT_SAMPLE`. Report V1 and each shadow variant side by side with net result, maximum drawdown, trade count, average holding days, fees, spread, slippage, and rejection counts.

- [ ] **Step 5: Add the command-line report**

The script must write a new `outputs/swing-research/<run_id>/shadow-report.json` and print a summary without mutating `data/swing/strategy.json`, `var/swing/daily_quotes.jsonl`, or the trade ledger.

Run:

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' scripts/run_swing_shadow.py --manifest data/swing/research_manifest.json --end-date 2026-09-18
```

Expected: exit code 0; each short-history result says `INSUFFICIENT_SAMPLE`; no strategy switch occurs.

- [ ] **Step 6: Run focused backtest tests and commit**

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' -m unittest tests.test_swing_shadow_backtest tests.test_swing_backtest -v
git add src/etf_rotation/swing_shadow_backtest.py scripts/run_swing_shadow.py tests/test_swing_shadow_backtest.py src/etf_rotation/swing_backtest.py
git commit -m "feat: replay swing shadow variants with costs"
```

Expected: focused tests PASS.

## Task 7: Return formal and shadow layers from the service

**Files:**
- Modify: `src/etf_rotation/swing_service.py:_build_snapshot`
- Modify: `src/etf_rotation/swing_service.py` configuration loading
- Modify: `tests/test_swing_service.py`
- Modify: `tests/test_swing_web.py`

- [ ] **Step 1: Add failing API contract tests**

```python
def test_snapshot_contains_formal_and_shadow_without_executable_shadow_order(service):
    item = next(item for item in service.snapshot()["items"] if item["symbol"] == "512170")
    assert item["formal"]["strategy_version"] == "SWING_V1"
    assert item["shadow"]["strategy_version"] == "SWING_V2_SHADOW"
    assert item["shadow"]["executable"] is False

def test_snapshot_only_blocks_shadow_execution(service):
    item = next(item for item in service.snapshot()["items"] if item["symbol"] == "512170")
    assert "SNAPSHOT_ONLY" in item["shadow"]["blocked_reasons"]
```

- [ ] **Step 2: Run focused service tests and verify failure**

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' -m unittest tests.test_swing_service tests.test_swing_web -v
```

Expected: FAIL because `formal` and `shadow` fields are absent.

- [ ] **Step 3: Load shadow configuration independently**

Add an optional `shadow_config_path` to the service constructor. If the file is absent or invalid, retain V1 service health and return a shadow item with `status="UNAVAILABLE"`, an error code, and `executable=false`; never disable V1.

- [ ] **Step 4: Add the independent read model**

For every enabled symbol, return:

```python
"formal": {"strategy_version": "SWING_V1", "state": formal.state.value, "decision": formal.to_dict()},
"shadow": {"strategy_version": "SWING_V2_SHADOW", "variants": variants, "opportunity": opportunity, "blocked_reasons": reasons, "executable": False},
```

Keep the existing top-level `formal_state`, `formal_decision`, `blocked_reasons`, and `indicators` fields for backward compatibility. Shadow results must not be written to alert history or trade ledger.

- [ ] **Step 5: Run service and web tests, then commit**

Expected: all focused tests PASS and existing JSON clients continue reading the legacy fields.

```powershell
git add src/etf_rotation/swing_service.py tests/test_swing_service.py tests/test_swing_web.py
git commit -m "feat: expose formal and shadow swing layers"
```

## Task 8: Update the wave-monitor page and presentation tests

**Files:**
- Modify: `src/etf_rotation/swing_page.py`
- Modify: `tests/test_swing_page.py`
- Modify: `tests/test_swing_account_layout.py`

- [ ] **Step 1: Add failing HTML assertions**

```python
def test_page_separates_formal_and_shadow_labels():
    html = render_page()
    assert "正式策略" in html
    assert "影子策略" in html
    assert "技术候选" in html
    assert "仅技术观察，不生成可执行买入" in html

def test_page_does_not_render_shadow_candidate_as_buy():
    html = render_page_with_shadow_candidate()
    assert "正式买入" not in html
    assert "SWING_V2_SHADOW" in html
```

- [ ] **Step 2: Run page tests and verify failure**

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' -m unittest tests.test_swing_page tests.test_swing_account_layout -v
```

Expected: FAIL because the new section labels are absent.

- [ ] **Step 3: Add formal/shadow sections without changing existing controls**

Add a compact shadow summary below the existing formal evidence. Use the existing `escapeHtml`, numeric formatters, and safe status helpers. Render:

- variant state and score;
- opportunity ID and expiry date;
- MACD/RSI/KDJ direction evidence;
- data quality and valuation status;
- exact blocking reason;
- explicit `executable=false` label.

Do not hide or relabel the existing formal strategy plan. Do not render a shadow share count, order button, or active alert.

- [ ] **Step 4: Verify the page in the running service**

Run:

```powershell
Invoke-WebRequest http://127.0.0.1:8765/swing -UseBasicParsing | Select-Object -ExpandProperty Content | Select-String '正式策略|影子策略|技术候选'
```

Expected: all three labels appear, and the page still shows the current formal V1 status.

- [ ] **Step 5: Run page tests and commit**

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' -m unittest tests.test_swing_page tests.test_swing_account_layout -v
git add src/etf_rotation/swing_page.py tests/test_swing_page.py tests/test_swing_account_layout.py
git commit -m "feat: show swing shadow candidates separately"
```

Expected: focused page tests PASS.

## Task 9: Notifications, full regression, and rollout gate

**Files:**
- Modify: `src/etf_rotation/notification_rules.py` to label shadow events as research notifications
- Create: `tests/test_swing_shadow_notifications.py`
- Create: `docs/superpowers/reports/2026-09-20-swing-v2-shadow-validation.md`

- [ ] **Step 1: Write notification isolation tests**

```python
def test_shadow_candidate_is_not_a_formal_trade_alert():
    alerts = build_alerts(formal_state="PULLBACK_WATCH", shadow_state="TECHNICAL_CANDIDATE")
    assert alerts.formal == []
    assert alerts.shadow[0].kind == "SHADOW_RESEARCH"
    assert alerts.shadow[0].executable is False

def test_unknown_data_suppresses_shadow_notification():
    alerts = build_alerts(data_status="UNKNOWN", shadow_state="TECHNICAL_CANDIDATE")
    assert alerts.shadow == []
```

- [ ] **Step 2: Run focused notification tests and verify failure**

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' -m unittest tests.test_swing_shadow_notifications -v
```

Expected: FAIL until shadow notifications are separated.

- [ ] **Step 3: Implement read-only shadow notification filtering**

Shadow notifications must include `kind=SHADOW_RESEARCH`, `strategy_version`, `opportunity_id`, `blocked_reasons`, and `executable=false`. Unknown, stale, incomplete, or `SNAPSHOT_ONLY` states suppress email delivery while retaining the page explanation. Formal V1 alert behavior remains unchanged.

- [ ] **Step 4: Run the complete test suite and quality checks**

Run:

```powershell
$env:PYTHONPATH='F:\plan\money\zt\src'
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' -m unittest discover -s tests -v
& 'C:\Users\linyongjie\.workbuddy\binaries\python\envs\default\Scripts\python.exe' -m compileall -q src tests scripts
git diff --check
```

Expected: all new and existing tests pass, compileall exits 0, and `git diff --check` reports no whitespace errors. Any pre-existing unrelated failure must be recorded with its exact test name rather than hidden.

- [ ] **Step 5: Generate the validation report**

Run the manifest and shadow commands, copy their run IDs and hashes into `docs/superpowers/reports/2026-09-20-swing-v2-shadow-validation.md`, and state explicitly whether the result is `VALIDATED`, `INSUFFICIENT_SAMPLE`, or `BLOCKED_DATA_QUALITY`. The report must not claim strategy profitability when the required sample or folds are missing.

- [ ] **Step 6: Commit the rollout-gate changes**

```powershell
git add src/etf_rotation/notification_rules.py src/etf_rotation/swing_service.py tests/test_swing_shadow_notifications.py docs/superpowers/reports/2026-09-20-swing-v2-shadow-validation.md
git commit -m "feat: isolate swing shadow research notifications"
```

## Task 10: Final verification and handoff

**Files:**
- Review only: all files changed by Tasks 1–9

- [ ] **Step 1: Verify V1 rollback and data immutability**

Run:

```powershell
git diff HEAD~9 -- data/swing/strategy.json var/swing/daily_quotes.jsonl
```

Expected: no strategy threshold rewrite and no direct rewrite of the canonical history from the shadow implementation.

- [ ] **Step 2: Verify API safety**

Run:

```powershell
$snapshot = Invoke-RestMethod http://127.0.0.1:8765/api/swing/snapshot
$snapshot.items | ForEach-Object { if ($_.shadow.executable -ne $false) { throw "shadow executable flag is not false" } }
```

Expected: every shadow result remains non-executable.

- [ ] **Step 3: Verify browser presentation**

Open `http://127.0.0.1:8765/swing` and confirm one item can show all of: formal V1 state, shadow variant states, data quality, indicator direction, valuation status, and `SNAPSHOT_ONLY` blocking. Confirm no shadow section exposes a buy button or recommended share count.

- [ ] **Step 4: Prepare handoff summary**

Report changed files, test commands, run IDs, sample coverage, validation status, known data warnings, and the exact command/config needed to disable the shadow layer. Do not recommend switching to V2 until the user reviews the out-of-sample report.
