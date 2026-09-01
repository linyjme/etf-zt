# Index ETF Swing Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an isolated `/swing` page that monitors six verified broad-index ETFs on completed daily bars, maintains a local manual portfolio ledger, emits risk-gated swing candidates, and supplies realistic single-symbol and portfolio backtests without affecting the existing intraday T monitor.

**Architecture:** Keep one HTTP process, but compose a separate `SwingService` behind `/api/swing/*`. Daily data, strategy state, portfolio events, alerts, revisions, page state, and backtests live in focused modules and under `var/swing`; only ETF metadata and the market calendar are shared with the T monitor. Completed daily bars determine formal state, while current intraday quotes may only create retractable proximity or predefined-stop overlays.

**Tech Stack:** Python 3.12 standard library, `unittest`, `ThreadingHTTPServer`, JSON/JSONL atomic persistence, Server-Sent Events, vanilla HTML/CSS/JavaScript, PowerShell launch scripts.

---

## Execution preflight

The checkout contained unrelated uncommitted changes in `data/monitor/etf_metadata.json`, `data/monitor/watchlist.json`, `src/etf_rotation/t_web.py`, and `tests/test_t_monitor.py` when this plan was written. Do not stage, overwrite, or discard them.

- [ ] Use `superpowers:using-git-worktrees` before implementation and create an isolated worktree from the committed `main` state.
- [ ] Record `git status --short` in both the original checkout and the worktree.
- [ ] Run the baseline suite in the worktree:

```powershell
.\scripts\run-tests.ps1
```

Expected: the committed baseline passes before any swing changes.

- [ ] Keep each task commit limited to the files listed for that task. Before every commit, run `git diff --cached --name-only` and reject unexpected files.

## File map

New production files:

- `src/etf_rotation/swing_config.py`: versioned strategy and watchlist configuration.
- `src/etf_rotation/swing_data.py`: daily-bar model, validation, adjusted/raw series, and atomic history store.
- `src/etf_rotation/eastmoney_client.py`: shared Eastmoney transport and security-market mapping.
- `src/etf_rotation/swing_collector.py`: raw and adjusted daily K-line collection.
- `src/etf_rotation/swing_strategy.py`: indicators, formal state machine, intraday overlays, and position sizing.
- `src/etf_rotation/swing_portfolio.py`: append-only account events and reconstructed portfolio projection.
- `src/etf_rotation/swing_alerts.py`: formal and intraday alert lifecycle.
- `src/etf_rotation/swing_service.py`: single producer, published snapshot, revisions, health, and orchestration.
- `src/etf_rotation/swing_page.py`: standalone `/swing` page.
- `src/etf_rotation/swing_backtest.py`: single-symbol and shared-cash portfolio backtests.
- `data/swing/watchlist.json`: independent six-ETF swing watchlist.
- `data/swing/strategy.json`: `SWING_V1` risk and indicator parameters.

New test files:

- `tests/swing_helpers.py`
- `tests/test_swing_config.py`
- `tests/test_swing_data.py`
- `tests/test_swing_collector.py`
- `tests/test_swing_strategy.py`
- `tests/test_swing_portfolio.py`
- `tests/test_swing_alerts.py`
- `tests/test_swing_service.py`
- `tests/test_swing_web.py`
- `tests/test_swing_page.py`
- `tests/test_swing_backtest.py`

Existing files modified only at integration tasks:

- `src/etf_rotation/quote_collector.py`
- `src/etf_rotation/t_web.py`
- `src/etf_rotation/t_page.py`
- `src/etf_rotation/cli.py`
- `scripts/start-monitor.ps1`
- `tests/test_market_data.py`
- `tests/test_run_tests_script.py`
- `tests/test_runtime_api.py`
- `README.md`

### Task 1: Versioned swing configuration and deterministic fixtures

**Files:**

- Create: `src/etf_rotation/swing_config.py`
- Create: `data/swing/watchlist.json`
- Create: `data/swing/strategy.json`
- Create: `tests/swing_helpers.py`
- Create: `tests/test_swing_config.py`

- [ ] **Step 1: Write failing configuration tests**

Create tests that require an independent six-symbol watchlist, exact `SWING_V1` defaults, strict ASCII codes, and cross-checking against ETF metadata:

```python
from pathlib import Path
import tempfile
import unittest

from etf_rotation.swing_config import SwingConfigError, load_strategy, load_watchlist


ROOT = Path(__file__).resolve().parents[1]


class SwingConfigTests(unittest.TestCase):
    def test_repository_defaults_are_versioned_and_exact(self) -> None:
        strategy = load_strategy(ROOT / "data" / "swing" / "strategy.json")
        watchlist = load_watchlist(
            ROOT / "data" / "swing" / "watchlist.json",
            ROOT / "data" / "monitor" / "etf_metadata.json",
        )
        self.assertEqual(strategy.strategy_version, "SWING_V1")
        self.assertEqual(strategy.minimum_daily_bars, 70)
        self.assertEqual(strategy.short_ma_days, 20)
        self.assertEqual(strategy.long_ma_days, 60)
        self.assertEqual(strategy.long_ma_slope_lookback, 10)
        self.assertEqual(strategy.atr_days, 14)
        self.assertEqual(strategy.pullback_atr_distance, 0.5)
        self.assertEqual(strategy.entry_zone_atr_half_width, 0.25)
        self.assertEqual(strategy.anti_chase_atr_distance, 1.5)
        self.assertEqual(strategy.breakout_days, 20)
        self.assertEqual(strategy.add_profit_r, 1.0)
        self.assertEqual(strategy.reduce_profit_r, 2.0)
        self.assertEqual(strategy.risk_per_trade, 0.0075)
        self.assertEqual(strategy.max_symbol_weight, 0.40)
        self.assertEqual(strategy.max_equity_weight, 0.80)
        self.assertEqual(strategy.max_portfolio_risk, 0.02)
        self.assertEqual(strategy.initial_stop_atr, 2.0)
        self.assertEqual(strategy.trailing_stop_atr, 3.0)
        self.assertEqual(strategy.cooldown_days, 5)
        self.assertEqual(strategy.walk_forward_train_days, 504)
        self.assertEqual(strategy.walk_forward_test_days, 126)
        self.assertEqual(strategy.walk_forward_step_days, 126)
        self.assertEqual(strategy.max_volume_participation, 0.10)
        self.assertEqual(
            [item.symbol for item in watchlist],
            ["510300", "510500", "563360", "512100", "159915", "588000"],
        )

    def test_unicode_code_unknown_metadata_and_unknown_key_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = root / "watchlist.json"
            bad.write_text(
                '{"schema_version":1,"items":[{"symbol":"５１０３００","enabled":true}]}',
                encoding="utf-8",
            )
            with self.assertRaises(SwingConfigError):
                load_watchlist(bad, ROOT / "data" / "monitor" / "etf_metadata.json")
```

- [ ] **Step 2: Run the focused test and verify RED**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_config -v
```

Expected: `ModuleNotFoundError: No module named 'etf_rotation.swing_config'`.

- [ ] **Step 3: Implement strict configuration models**

Create immutable public models and reject missing, extra, boolean-as-number, non-finite, and out-of-range fields:

```python
@dataclass(frozen=True)
class SwingWatchItem:
    symbol: str
    enabled: bool


@dataclass(frozen=True)
class SwingStrategyConfig:
    schema_version: int
    strategy_version: str
    minimum_daily_bars: int
    short_ma_days: int
    long_ma_days: int
    long_ma_slope_lookback: int
    atr_days: int
    pullback_atr_distance: float
    entry_zone_atr_half_width: float
    anti_chase_atr_distance: float
    breakout_days: int
    add_profit_r: float
    reduce_profit_r: float
    risk_per_trade: float
    max_symbol_weight: float
    max_equity_weight: float
    max_portfolio_risk: float
    initial_stop_atr: float
    trailing_stop_atr: float
    cooldown_days: int
    walk_forward_train_days: int
    walk_forward_test_days: int
    walk_forward_step_days: int
    max_volume_participation: float


def load_watchlist(path: Path, metadata_path: Path) -> tuple[SwingWatchItem, ...]:
    payload = _load_object(path)
    _require_exact_keys(payload, {"schema_version", "items"}, "波段观察列表")
    if payload["schema_version"] != 1 or not isinstance(payload["items"], list):
        raise SwingConfigError("波段观察列表schema无效")
    metadata = EtfMetadataStore(metadata_path).load()
    result: list[SwingWatchItem] = []
    seen: set[str] = set()
    for record in payload["items"]:
        _require_exact_keys(record, {"symbol", "enabled"}, "波段观察项")
        symbol = _ascii_code(record["symbol"])
        if type(record["enabled"]) is not bool:
            raise SwingConfigError(f"{symbol}的enabled必须是布尔值")
        if symbol not in metadata:
            raise SwingConfigError(f"缺少交易元数据: {symbol}")
        if symbol in seen:
            raise SwingConfigError(f"波段观察代码重复: {symbol}")
        seen.add(symbol)
        result.append(SwingWatchItem(symbol, record["enabled"]))
    return tuple(result)
```

Write `strategy.json` with exactly the fields represented by `SwingStrategyConfig` and the values asserted above. Reject unknown keys and validate `short_ma_days < long_ma_days <= minimum_daily_bars`, all day counts as positive non-boolean integers, all rates as finite numbers in `(0, 1]`, and all ATR/R multiples as finite positive numbers. Write `watchlist.json` with the six confirmed symbols only. Add `completed_daily_bars()` and `metadata_fixture()` builders to `tests/swing_helpers.py`; every generated timestamp must include `+08:00`.

- [ ] **Step 4: Run configuration tests and full config regressions**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_config tests.test_defaults -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the configuration boundary**

```powershell
git add -- data/swing/watchlist.json data/swing/strategy.json src/etf_rotation/swing_config.py tests/swing_helpers.py tests/test_swing_config.py
git diff --cached --check
git commit -m "feat: add swing monitor configuration"
```

### Task 2: Daily-bar model and cross-field validation

**Files:**

- Create: `src/etf_rotation/swing_data.py`
- Create: `tests/test_swing_data.py`

- [ ] **Step 1: Write failing model and validator tests**

Cover aware observation times, final bars, raw and adjusted OHLC, previous-close continuity, price limits, calendar dates, volume/amount, and adjustment consistency:

```python
from datetime import date, datetime
import math
import unittest

from etf_rotation.etf_metadata import EtfMetadataStore
from etf_rotation.swing_data import DailyBar, DailyBarValidator, SwingDataError
from tests.swing_helpers import daily_bar_mapping, metadata_fixture


class DailyBarValidatorTests(unittest.TestCase):
    def test_valid_final_raw_and_adjusted_bar_is_accepted(self) -> None:
        metadata_path = metadata_fixture(self)
        metadata = EtfMetadataStore(metadata_path).get("510300")
        record = DailyBar.from_mapping(daily_bar_mapping("2026-08-31"))
        DailyBarValidator(set()).validate(record, metadata)

    def test_incomplete_naive_and_invalid_ohlc_are_rejected(self) -> None:
        cases = (
            {"is_final": False},
            {"observed_at": "2026-08-31T15:10:00"},
            {"high": 4.60, "close": 4.70},
            {"adjusted_close": math.nan},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                payload = daily_bar_mapping("2026-08-31")
                payload.update(changes)
                with self.assertRaises(SwingDataError):
                    DailyBar.from_mapping(payload)

    def test_price_limit_move_is_rejected_by_metadata_validator(self) -> None:
        metadata = EtfMetadataStore(metadata_fixture(self)).get("510300")
        record = DailyBar.from_mapping(daily_bar_mapping("2026-08-31", close=5.30))
        with self.assertRaisesRegex(SwingDataError, "涨跌幅"):
            DailyBarValidator(set()).validate(record, metadata)

    def test_previous_close_must_match_prior_raw_close(self) -> None:
        first = DailyBar.from_mapping(daily_bar_mapping("2026-08-28", close=4.60))
        second = DailyBar.from_mapping(
            daily_bar_mapping("2026-08-31", previous_close=4.59)
        )
        metadata = EtfMetadataStore(metadata_fixture(self)).load()
        with self.assertRaisesRegex(SwingDataError, "前收盘"):
            DailyBarValidator(set()).validate_sequence((first, second), metadata)
```

- [ ] **Step 2: Run the focused test and verify RED**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_data.DailyBarValidatorTests -v
```

Expected: import failure for `etf_rotation.swing_data`.

- [ ] **Step 3: Implement `DailyBar` and `DailyBarValidator`**

Use a schema-v1 immutable record. Raw values drive fills, previous-close checks, limits, cash, and valuation; adjusted values drive MA and ATR signals:

```python
@dataclass(frozen=True)
class DailyBar:
    schema_version: int
    symbol: str
    trading_date: date
    observed_at: datetime
    source: str
    open: float
    high: float
    low: float
    close: float
    previous_close: float
    volume: float
    amount: float
    adjusted_open: float
    adjusted_high: float
    adjusted_low: float
    adjusted_close: float
    is_final: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "DailyBar":
        _require_exact_keys(value, DAILY_BAR_FIELDS, "日线")
        record = cls(
            schema_version=_exact_int(value["schema_version"], "schema_version"),
            symbol=_ascii_code(value["symbol"]),
            trading_date=date.fromisoformat(_text(value["trading_date"], "trading_date")),
            observed_at=datetime.fromisoformat(_text(value["observed_at"], "observed_at")),
            source=_text(value["source"], "source"),
            open=_positive(value["open"], "open"),
            high=_positive(value["high"], "high"),
            low=_positive(value["low"], "low"),
            close=_positive(value["close"], "close"),
            previous_close=_positive(value["previous_close"], "previous_close"),
            volume=_nonnegative(value["volume"], "volume"),
            amount=_nonnegative(value["amount"], "amount"),
            adjusted_open=_positive(value["adjusted_open"], "adjusted_open"),
            adjusted_high=_positive(value["adjusted_high"], "adjusted_high"),
            adjusted_low=_positive(value["adjusted_low"], "adjusted_low"),
            adjusted_close=_positive(value["adjusted_close"], "adjusted_close"),
            is_final=_exact_bool(value["is_final"], "is_final"),
        )
        record._validate_shape()
        return record
```

`DailyBarValidator.validate_sequence()` must sort nothing silently: require strictly increasing dates, reject configured closed dates, require every pair of adjacent expected trading days to be present, and compare `previous_close` to the preceding raw close within one `price_tick`.

- [ ] **Step 4: Run data-model tests**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_data.DailyBarValidatorTests -v
```

Expected: all validator tests pass.

- [ ] **Step 5: Commit the daily model**

```powershell
git add -- src/etf_rotation/swing_data.py tests/test_swing_data.py tests/swing_helpers.py
git diff --cached --check
git commit -m "feat: validate completed swing daily bars"
```

### Task 3: Atomic daily history with authoritative upsert

**Files:**

- Modify: `src/etf_rotation/swing_data.py`
- Modify: `tests/test_swing_data.py`

- [ ] **Step 1: Write failing store tests**

Require primary-key upsert, later-observation precedence, all-before-any-write validation, inter-instance serialization, and rollback after replacement failure:

```python
class DailyHistoryStoreTests(unittest.TestCase):
    def test_later_final_observation_upserts_symbol_and_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "daily_quotes.jsonl"
            store = DailyHistoryStore(path, self.metadata, self.closed_dates)
            early = DailyBar.from_mapping(daily_bar_mapping("2026-08-31", close=4.60))
            late = DailyBar.from_mapping(
                daily_bar_mapping(
                    "2026-08-31",
                    close=4.61,
                    observed_at="2026-08-31T15:20:00+08:00",
                )
            )
            store.upsert((early,))
            store.upsert((late,))
            self.assertEqual(store.query("510300")[-1].close, 4.61)
            self.assertEqual(len(store.query("510300")), 1)

    def test_invalid_batch_does_not_change_existing_file(self) -> None:
        before = self.path.read_bytes()
        with self.assertRaises(SwingDataError):
            self.store.upsert((self.valid_bar, self.invalid_bar))
        self.assertEqual(self.path.read_bytes(), before)

    def test_replace_failure_preserves_previous_history(self) -> None:
        with patch("etf_rotation.swing_data.os.replace", side_effect=OSError("replace")):
            with self.assertRaises(OSError):
                self.store.upsert((self.next_bar,))
        self.assertEqual(self.store.query("510300"), (self.valid_bar,))
```

- [ ] **Step 2: Run store tests and verify RED**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_data.DailyHistoryStoreTests -v
```

Expected: `DailyHistoryStore` is missing.

- [ ] **Step 3: Implement locked atomic replacement**

Expose this interface:

```python
class DailyHistoryStore:
    def __init__(
        self,
        path: Path,
        metadata: Mapping[str, EtfMetadata],
        closed_dates: set[date],
    ) -> None:
        self.path = Path(path)
        self.metadata = dict(metadata)
        self.validator = DailyBarValidator(closed_dates)

    def load(self) -> tuple[DailyBar, ...]:
        with _SiblingFileLock(self.path, shared=True):
            return self._read_unlocked()

    def query(self, symbol: str) -> tuple[DailyBar, ...]:
        return tuple(bar for bar in self.load() if bar.symbol == symbol)

    def upsert(self, records: Sequence[DailyBar]) -> tuple[DailyBar, ...]:
        incoming = tuple(records)
        self._validate_batch(incoming)
        with _SiblingFileLock(self.path, shared=False):
            current = self._read_unlocked()
            merged = self._merge(current, incoming)
            self._validate_all(merged)
            self._atomic_replace(merged)
            return merged
```

`_merge()` must use `(symbol, trading_date)` and only replace an existing record when `incoming.observed_at >= existing.observed_at`. `_atomic_replace()` must write UTF-8 JSONL to a sibling temporary file, flush, `os.fsync`, and call `os.replace`; cleanup must never delete the canonical path.

- [ ] **Step 4: Run store and validator tests**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_data -v
```

Expected: all swing data tests pass, including concurrent two-store updates.

- [ ] **Step 5: Commit atomic history**

```powershell
git add -- src/etf_rotation/swing_data.py tests/test_swing_data.py
git diff --cached --check
git commit -m "feat: persist authoritative swing daily history"
```

### Task 4: Shared transport and raw-plus-adjusted daily collection

**Files:**

- Create: `src/etf_rotation/eastmoney_client.py`
- Create: `src/etf_rotation/swing_collector.py`
- Create: `tests/test_swing_collector.py`
- Modify: `src/etf_rotation/quote_collector.py`
- Modify: `tests/test_t_monitor.py`

- [ ] **Step 1: Lock current minute-collector behavior with regression tests**

Add a transport contract test before moving shared helpers:

```python
class SharedEastmoneyClientTests(unittest.TestCase):
    def test_market_mapping_and_source_labels_remain_stable(self) -> None:
        self.assertEqual(market_for_symbol("159915"), 0)
        self.assertEqual(market_for_symbol("510300"), 1)
        self.assertEqual(
            source_label(TRENDS2_ENDPOINT),
            "东方财富 trends2 (push2his.eastmoney.com)",
        )
```

- [ ] **Step 2: Write failing daily collector tests**

Use deterministic raw (`fqt=0`) and adjusted (`fqt=1`) fixture payloads. Require aligned dates, one batch observation time, primary/fallback behavior, no current incomplete day, and no partial batch:

```python
class EastmoneyDailyCollectorTests(unittest.TestCase):
    def test_raw_and_adjusted_series_are_joined_by_date(self) -> None:
        transport = FixtureTransport(raw_kline_payload(), adjusted_kline_payload())
        bars = EastmoneyDailyCollector(
            transport=transport,
            now=lambda: datetime.fromisoformat("2026-09-01T15:20:00+08:00"),
        ).collect((SwingWatchItem("510300", True),), date(2026, 9, 1))
        self.assertEqual(bars[-1].trading_date, date(2026, 9, 1))
        self.assertEqual(bars[-1].close, 4.68)
        self.assertEqual(bars[-1].adjusted_close, 5.12)
        self.assertTrue(all(bar.is_final for bar in bars))

    def test_date_mismatch_or_one_missing_symbol_rejects_entire_batch(self) -> None:
        with self.assertRaisesRegex(SwingDataError, "完整性"):
            self.collector.collect(self.two_symbols, date(2026, 9, 1))
        self.assertEqual(self.store_path.read_bytes(), self.before)
```

- [ ] **Step 3: Run collector tests and verify RED**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_collector -v
```

Expected: import failure for `swing_collector`.

- [ ] **Step 4: Extract transport and implement the daily collector**

Move `_default_transport` and `market_for_symbol` without semantic changes into `eastmoney_client.py`, then import them from both collectors. The new collector uses these exact endpoints and daily K-line fields:

```python
KLINE_ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
KLINE_FALLBACK_ENDPOINT = "https://push2delay.eastmoney.com/api/qt/stock/kline/get"
FIELDS1 = "f1,f2,f3,f4,f5,f6"
FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


class EastmoneyDailyCollector:
    def collect(
        self,
        watchlist: Sequence[SwingWatchItem],
        last_completed_date: date,
        count: int = 260,
    ) -> tuple[DailyBar, ...]:
        enabled = tuple(item for item in watchlist if item.enabled)
        observed_at = self.now()
        try:
            return self._collect_batch(
                enabled, KLINE_ENDPOINT, observed_at, last_completed_date, count
            )
        except DailyRequestFailure as primary_error:
            try:
                return self._collect_batch(
                    enabled, KLINE_FALLBACK_ENDPOINT, observed_at,
                    last_completed_date, count,
                )
            except (DailyRequestFailure, SwingDataError) as fallback_error:
                raise SwingDataError(
                    f"日线主备端点均失败: 主端点 {primary_error}; 备用端点 {fallback_error}"
                ) from fallback_error
```

Parse `f51` through `f57` as date, open, close, high, low, volume, and amount. Require the raw and adjusted responses for a symbol to contain exactly the same trading-date set. For the first retained raw bar, take `previous_close` from Eastmoney's `data.preKPrice`; for every later raw bar, use the preceding raw close. Reject the symbol when the first previous close is missing/non-positive or when continuity breaks. Do not derive raw fills from adjusted values. Filter every date after `last_completed_date`. If any enabled symbol fails, return no batch.

- [ ] **Step 5: Run collector plus existing minute tests**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_collector tests.test_t_monitor.Trends2QuoteCollectorTests -v
```

Expected: all daily and minute collector tests pass.

- [ ] **Step 6: Commit shared transport and daily collection**

```powershell
git add -- src/etf_rotation/eastmoney_client.py src/etf_rotation/quote_collector.py src/etf_rotation/swing_collector.py tests/test_swing_collector.py tests/test_t_monitor.py
git diff --cached --check
git commit -m "feat: collect validated swing daily bars"
```

### Task 5: Indicators, formal state machine, and intraday overlays

**Files:**

- Create: `src/etf_rotation/swing_strategy.py`
- Create: `tests/test_swing_strategy.py`

- [ ] **Step 1: Write failing indicator and state tests**

Use deterministic adjusted bars and test every hard boundary:

```python
class SwingStrategyTests(unittest.TestCase):
    def test_exactly_seventy_bars_can_confirm_trial_entry(self) -> None:
        decision = evaluate_swing(
            completed_daily_bars(count=70, pattern="pullback_reclaim"),
            strategy_config(),
            PortfolioContext.empty(100_000.0),
        )
        self.assertEqual(decision.state, SwingState.TRIAL_ENTRY_CANDIDATE)
        self.assertEqual(decision.as_of_trading_date.isoformat(), "2026-08-31")
        self.assertGreater(decision.planned_shares, 0)

    def test_sixty_nine_bars_and_falling_ma60_are_blocked(self) -> None:
        short = evaluate_swing(
            completed_daily_bars(count=69, pattern="pullback_reclaim"),
            strategy_config(),
            PortfolioContext.empty(100_000.0),
        )
        falling = evaluate_swing(
            completed_daily_bars(count=80, pattern="falling_ma60"),
            strategy_config(),
            PortfolioContext.empty(100_000.0),
        )
        self.assertEqual(short.state, SwingState.DATA_UNAVAILABLE)
        self.assertEqual(falling.state, SwingState.TREND_BLOCKED)

    def test_intraday_price_only_changes_overlay(self) -> None:
        formal = evaluate_swing(self.bars, self.config, self.portfolio)
        current = evaluate_intraday_overlay(formal, price=formal.planned_entry_low)
        self.assertEqual(current.formal_state, formal.state)
        self.assertEqual(current.overlay, IntradayOverlay.APPROACHING_ENTRY_ZONE)
```

Also add named tests for the MA20 distance boundary, previous-high confirmation, 1.5-ATR anti-chase gate, 1R add gate, 20-day breakout, 2R half reduction, two-close MA20 exit, one-close MA60 exit, trailing stop, insufficient cash, minimum lot, and five-day cooldown.

- [ ] **Step 2: Run strategy tests and verify RED**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_strategy -v
```

Expected: import failure for `swing_strategy`.

- [ ] **Step 3: Implement explicit decisions and evidence**

Expose immutable state and evidence rather than UI strings:

```python
class SwingState(StrEnum):
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    TREND_BLOCKED = "TREND_BLOCKED"
    UPTREND_WATCH = "UPTREND_WATCH"
    PULLBACK_WATCH = "PULLBACK_WATCH"
    TRIAL_ENTRY_CANDIDATE = "TRIAL_ENTRY_CANDIDATE"
    HOLDING = "HOLDING"
    ADD_CANDIDATE = "ADD_CANDIDATE"
    REDUCE_CANDIDATE = "REDUCE_CANDIDATE"
    EXIT_CANDIDATE = "EXIT_CANDIDATE"
    COOLDOWN = "COOLDOWN"


@dataclass(frozen=True)
class SwingDecision:
    symbol: str
    strategy_version: str
    as_of_trading_date: date | None
    state: SwingState
    evidence: Mapping[str, float | int | bool | str | None]
    blocked_reasons: tuple[str, ...]
    planned_entry_low: float | None
    planned_entry_high: float | None
    planned_stop: float | None
    planned_shares: int
    planned_risk_rate: float
    first_reduce_price: float | None
    valid_for_trading_date: date | None
```

Compute MA and ATR only from `adjusted_*`. On the latest completed bar define `raw_scale = close / adjusted_close`; map adjusted MA and ATR values back to raw-price display using this scale. Define the entry zone as `MA20 ± 0.25 * ATR14`, use the mapped raw upper bound as the conservative sizing price, set the raw initial stop two mapped ATR units below it, and set 2R relative to that same reference. Keep every gate in a named pure function; return all individual booleans and numeric distances in `evidence`.

- [ ] **Step 4: Run all strategy tests**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_strategy -v
```

Expected: all state, sizing, and overlay tests pass.

- [ ] **Step 5: Commit the strategy engine**

```powershell
git add -- src/etf_rotation/swing_strategy.py tests/test_swing_strategy.py tests/swing_helpers.py
git diff --cached --check
git commit -m "feat: add risk-gated swing state machine"
```

### Task 6: Append-only portfolio ledger and reconstruction

**Files:**

- Create: `src/etf_rotation/swing_portfolio.py`
- Create: `tests/test_swing_portfolio.py`

- [ ] **Step 1: Write failing event and projection tests**

Test account initialization, buy/sell cash, weighted cost, fees, T+1 inventory, realized P&L, idempotency, reversal, and reconstruction:

```python
class SwingPortfolioTests(unittest.TestCase):
    def test_buy_rollover_sell_and_rebuild_are_exact(self) -> None:
        ledger = PortfolioLedger(self.path, self.metadata)
        ledger.initialize("波段账户", cash=100_000.0, idempotency_key="init-1")
        ledger.record_trade(
            TradeInput("510300", "BUY", 1000, 4.60, 5.0, self.tuesday),
            idempotency_key="buy-1",
        )
        same_day = ledger.project(self.tuesday, {"510300": 4.60})
        self.assertEqual(same_day.positions["510300"].sellable_shares, 0)
        next_day = ledger.project(self.wednesday, {"510300": 4.70})
        self.assertEqual(next_day.positions["510300"].sellable_shares, 1000)
        ledger.record_trade(
            TradeInput("510300", "SELL", 500, 4.80, 5.0, self.wednesday),
            idempotency_key="sell-1",
        )
        rebuilt = PortfolioLedger(self.path, self.metadata).project(
            self.wednesday, {"510300": 4.80}
        )
        self.assertEqual(rebuilt.positions["510300"].shares, 500)
        self.assertAlmostEqual(rebuilt.realized_pnl, 92.5)

    def test_duplicate_idempotency_key_is_not_appended_twice(self) -> None:
        first = self.ledger.record_trade(self.trade, idempotency_key="trade-1")
        second = self.ledger.record_trade(self.trade, idempotency_key="trade-1")
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(len(self.ledger.load_events()), 2)

    def test_missing_or_corrupt_projection_is_rebuilt_from_events(self) -> None:
        self.projection_path.write_text("broken", encoding="utf-8")
        rebuilt = self.ledger.load_or_rebuild_projection(
            self.projection_path, self.wednesday, {"510300": 4.80}
        )
        self.assertEqual(rebuilt.positions["510300"].shares, 500)
        on_disk = json.loads(self.projection_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["positions"]["510300"]["shares"], 500)
```

- [ ] **Step 2: Run portfolio tests and verify RED**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_portfolio -v
```

Expected: import failure for `swing_portfolio`.

- [ ] **Step 3: Implement event types and atomic append**

Use one locked read-validate-rewrite operation so duplicate keys and concurrent writes cannot race:

```python
class PortfolioEventType(StrEnum):
    ACCOUNT_INITIALIZED = "ACCOUNT_INITIALIZED"
    BUY_CONFIRMED = "BUY_CONFIRMED"
    SELL_CONFIRMED = "SELL_CONFIRMED"
    TRADE_REVERSED = "TRADE_REVERSED"


class PortfolioLedger:
    def initialize(
        self, name: str, cash: float, idempotency_key: str,
    ) -> PortfolioEvent:
        event = PortfolioEvent.initialize(name, cash, idempotency_key, self.clock())
        return self._append_idempotent(event)

    def record_trade(
        self, trade: TradeInput, idempotency_key: str,
    ) -> PortfolioEvent:
        current = self.project(trade.executed_at.date(), {})
        self._validate_trade(current, trade)
        event = PortfolioEvent.from_trade(trade, idempotency_key, self.clock())
        return self._append_idempotent(event)

    def reverse(self, event_id: str, idempotency_key: str) -> PortfolioEvent:
        events = self.load_events()
        target = self._reversible_target(events, event_id)
        return self._append_idempotent(
            PortfolioEvent.reversal(target, idempotency_key, self.clock())
        )

    def load_or_rebuild_projection(
        self,
        projection_path: Path,
        trading_date: date,
        marks: Mapping[str, float],
    ) -> PortfolioProjection:
        projected = self.project(trading_date, marks)
        _atomic_write_json(projection_path, projected.to_dict())
        return projected
```

Generate every portfolio `event_id` with lowercase canonical `uuid.uuid4()` text, and validate that exact shape before accepting a reversal path. Idempotency keys remain caller-supplied opaque strings and are never reused as event IDs.

Buying beyond cash and selling beyond sellable inventory are invalid. A trade that exceeds the strategy risk limit remains recordable when cash, shares, lot, and T+1 rules are valid; add `RISK_LIMIT_EXCEEDED` to projection warnings.

- [ ] **Step 4: Run portfolio tests**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_portfolio -v
```

Expected: all ledger tests pass, including reversal and concurrent idempotency.

- [ ] **Step 5: Commit the ledger**

```powershell
git add -- src/etf_rotation/swing_portfolio.py tests/test_swing_portfolio.py
git diff --cached --check
git commit -m "feat: add local swing portfolio ledger"
```

### Task 7: Alert lifecycle, deduplication, and retraction

**Files:**

- Create: `src/etf_rotation/swing_alerts.py`
- Create: `tests/test_swing_alerts.py`

- [ ] **Step 1: Write failing alert lifecycle tests**

Require deterministic formal IDs, one notification per state/day/version, persisted acknowledgement and ignore events, retractable intraday overlays, and formal-alert survival across intraday outage:

```python
class SwingAlertStoreTests(unittest.TestCase):
    def test_formal_alert_is_deduplicated_and_acknowledged(self) -> None:
        alert = formal_alert(
            trading_date=date(2026, 8, 31),
            symbol="510300",
            state="TRIAL_ENTRY_CANDIDATE",
            strategy_version="SWING_V1",
        )
        first = self.store.publish_formal(alert)
        second = self.store.publish_formal(alert)
        self.assertEqual(first.alert_id, second.alert_id)
        self.assertEqual(len(self.store.current()), 1)
        self.store.acknowledge(first.alert_id, "ack-1")
        self.assertTrue(self.store.current()[0].acknowledged)

    def test_intraday_outage_retracts_only_intraday_overlay(self) -> None:
        formal = self.store.publish_formal(self.formal)
        overlay = self.store.publish_overlay(self.stop_touched)
        self.store.retract_overlays("INTRADAY_FEED_UNAVAILABLE")
        current = {item.alert_id: item for item in self.store.current(include_retracted=True)}
        self.assertFalse(current[formal.alert_id].retracted)
        self.assertTrue(current[overlay.alert_id].retracted)
        self.assertEqual(current[overlay.alert_id].retraction_reason, "INTRADAY_FEED_UNAVAILABLE")
```

- [ ] **Step 2: Run alert tests and verify RED**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_alerts -v
```

Expected: import failure for `swing_alerts`.

- [ ] **Step 3: Implement append-only alert events**

Use these event and projection boundaries:

```python
class AlertEventType(StrEnum):
    FORMAL_PUBLISHED = "FORMAL_PUBLISHED"
    OVERLAY_PUBLISHED = "OVERLAY_PUBLISHED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IGNORED = "IGNORED"
    RETRACTED = "RETRACTED"


def formal_alert_id(
    trading_date: date, symbol: str, state: str, strategy_version: str,
) -> str:
    identity = f"{trading_date.isoformat()}|{symbol}|{state}|{strategy_version}"
    return hashlib.sha256(identity.encode("ascii")).hexdigest()[:24]


class SwingAlertStore:
    def publish_formal(self, alert: AlertInput) -> AlertProjection:
        return self._append_once(AlertEvent.formal(alert, self.clock()))

    def publish_overlay(self, alert: AlertInput) -> AlertProjection:
        return self._append_once(AlertEvent.overlay(alert, self.clock()))

    def acknowledge(self, alert_id: str, idempotency_key: str) -> AlertProjection:
        return self._transition(alert_id, AlertEventType.ACKNOWLEDGED, idempotency_key)

    def ignore(self, alert_id: str, idempotency_key: str) -> AlertProjection:
        return self._transition(alert_id, AlertEventType.IGNORED, idempotency_key)

    def retract_overlays(self, reason: str) -> tuple[AlertProjection, ...]:
        return self._retract_matching(lambda item: item.scope == "INTRADAY", reason)
```

Formal alerts are never retracted by an intraday transport failure. An ignored formal alert remains part of history but is omitted from active notifications.

- [ ] **Step 4: Run alert tests**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_alerts -v
```

Expected: all alert tests pass.

- [ ] **Step 5: Commit alert persistence**

```powershell
git add -- src/etf_rotation/swing_alerts.py tests/test_swing_alerts.py
git diff --cached --check
git commit -m "feat: persist swing alert lifecycle"
```

### Task 8: Single swing producer, health, snapshots, and revisions

**Files:**

- Create: `src/etf_rotation/swing_service.py`
- Create: `tests/test_swing_service.py`
- Modify: `tests/swing_helpers.py`

- [ ] **Step 1: Write failing service bootstrap and producer tests**

Cover read-only bootstrap, one collector invocation, post-close scheduling, failed batch preservation, formal-plan validity, intraday overlay withdrawal, and fault isolation:

```python
class SwingServiceTests(unittest.TestCase):
    def test_bootstrap_reads_history_without_collecting_or_writing(self) -> None:
        service = self.make_service(collector=None)
        before = self.paths.daily.read_bytes()
        snapshot = service.snapshot()
        self.assertEqual(snapshot["revision"], 0)
        self.assertEqual(snapshot["as_of_trading_date"], "2026-08-31")
        self.assertEqual(self.paths.daily.read_bytes(), before)

    def test_post_close_refresh_publishes_once_after_atomic_history_commit(self) -> None:
        collector = StaticDailyCollector(self.final_bars)
        service = self.make_service(collector=collector)
        self.assertTrue(service.refresh_once(datetime.fromisoformat("2026-09-01T15:10:00+08:00")))
        self.assertEqual(collector.calls, 1)
        self.assertEqual(service.snapshot()["revision"], 1)
        self.assertEqual(service.snapshot()["as_of_trading_date"], "2026-09-01")

    def test_intraday_failure_retracts_overlays_and_pauses_formal_plan(self) -> None:
        service = self.make_service(intraday_provider=FailingIntradayProvider())
        snapshot = service.refresh_intraday()
        item = next(item for item in snapshot["items"] if item["symbol"] == "510300")
        self.assertEqual(item["execution_status"], "PAUSED_MARKET_NOT_REALTIME")
        self.assertIsNone(item["intraday_overlay"])
        self.assertEqual(item["formal_state"], "TRIAL_ENTRY_CANDIDATE")
```

- [ ] **Step 2: Run service tests and verify RED**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_service -v
```

Expected: import failure for `swing_service`.

- [ ] **Step 3: Implement service paths and published state**

Use injected dependencies so tests never call the network:

```python
@dataclass(frozen=True)
class SwingPaths:
    watchlist: Path
    strategy: Path
    daily_history: Path
    portfolio_snapshot: Path
    trades: Path
    alerts: Path
    metadata: Path
    calendar: Path
    backtests: Path


class SwingService:
    def __init__(
        self,
        paths: SwingPaths,
        collector: DailyCollector | None,
        intraday_provider: Callable[[], Mapping[str, object]],
        intraday_points_provider: Callable[[str], Mapping[str, object]],
        clock: Callable[[], datetime],
        refresh_interval: float = 60.0,
        event_limit: int = 128,
    ) -> None:
        self.paths = paths
        self.collector = collector
        self.intraday_provider = intraday_provider
        self.intraday_points_provider = intraday_points_provider
        self.clock = clock
        self.producer_lock = threading.Lock()
        self.publish_condition = threading.Condition(threading.Lock())
        self.events: deque[dict[str, object]] = deque(maxlen=event_limit)
        self.revision = 0
        self.published = self._bootstrap()

    def snapshot(self) -> dict[str, object]:
        with self.publish_condition:
            return copy.deepcopy(self.published)

    def refresh_once(self, now: datetime | None = None) -> bool:
        cycle_time = now or self.clock()
        with self.producer_lock:
            return self._refresh_completed_daily(cycle_time)

    def refresh_intraday(self) -> dict[str, object]:
        with self.producer_lock:
            return self._refresh_intraday_overlay(self.clock())
```

`_refresh_completed_daily()` must obtain the last completed trading date from the calendar, collect all enabled symbols, cross-check each available complete minute series through `intraday_points_provider`, validate the whole batch, commit history, then publish. Minute absence is recorded as `MINUTE_CROSSCHECK_UNAVAILABLE`; a present minute series whose aggregate OHLC differs by more than one price tick blocks the entire daily batch. On failure the service publishes component health and no new formal decision. `_refresh_intraday_overlay()` may only update overlay, current price/time, and execution status.

During `_bootstrap()`, call `ledger.load_or_rebuild_projection()` before computing any position-dependent decision. After every successful account initialization, trade append, or reversal, rebuild and atomically persist the projection before publishing the next revision. If the event log itself is invalid, mark portfolio health blocked and suppress entry/add/reduce candidates; never reconstruct it from the projection file.

When history does not contain the latest completed trading date, a producer cycle is due even during lunch, after close, or on the next closed session. After one successful backfill it stops requesting that date. Before 15:10 on a trading day, the current day is never considered completed.

- [ ] **Step 4: Add revision wait/reset tests and pass service suite**

Add exact tests for `daily_quotes(symbol, since)`, stale/ahead cursor resets, cross-date authoritative reset, bounded retained events, blocked wait waking on stop, and no prior-date delta after rollover.

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_service -v
```

Expected: all producer, health, overlay, revision, and isolation tests pass.

- [ ] **Step 5: Commit the swing service**

```powershell
git add -- src/etf_rotation/swing_service.py tests/test_swing_service.py tests/swing_helpers.py
git diff --cached --check
git commit -m "feat: add isolated swing monitor producer"
```

### Task 9: HTTP routing and local-write APIs

**Files:**

- Modify: `src/etf_rotation/t_web.py`
- Create: `tests/test_swing_web.py`
- Modify: `tests/test_runtime_api.py`

- [ ] **Step 1: Write failing real-server route tests**

Start a real ephemeral HTTP server with temporary swing paths. Verify `/swing`, snapshot, daily cursor validation, swing watchlist enablement, portfolio initialization, trade idempotency, reversal, alert transitions, method rejection, request-size limits, and no broker endpoint:

```python
class SwingWebTests(unittest.TestCase):
    def test_swing_page_and_snapshot_are_independent_from_t_snapshot(self) -> None:
        with urlopen(self.base + "/swing", timeout=2) as response:
            self.assertIn("指数ETF波段监控", response.read().decode("utf-8"))
        with urlopen(self.base + "/api/swing/snapshot", timeout=2) as response:
            swing = json.loads(response.read())
        with urlopen(self.base + "/api/snapshot", timeout=2) as response:
            intraday = json.loads(response.read())
        self.assertEqual(swing["mode"], "MONITOR_ONLY")
        self.assertNotEqual(swing["strategy"], intraday.get("strategy"))

    def test_trade_post_requires_idempotency_key_and_never_calls_broker(self) -> None:
        request = Request(
            self.base + "/api/swing/trades",
            data=json.dumps(self.valid_trade).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as captured:
            urlopen(request, timeout=2)
        self.assertEqual(captured.exception.code, 400)
        self.assertFalse(hasattr(self.server, "broker"))
```

- [ ] **Step 2: Run web tests and verify RED**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_web -v
```

Expected: `/swing` returns 404.

- [ ] **Step 3: Attach `SwingService` without embedding swing logic in the T application**

Change the server container and factory boundary:

```python
class MonitorServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        application: MonitorApplication,
        swing_application: SwingService,
    ) -> None:
        super().__init__(address, MonitorRequestHandler)
        self.application = application
        self.swing_application = swing_application

    def server_close(self) -> None:
        self.application.stop_refresh()
        self.swing_application.stop_refresh()
        super().server_close()
```

Extend `create_server()` with optional `swing_paths`, `swing_collector`, and `swing_clock` arguments. Production CLI always supplies explicit paths. Existing tests that omit `swing_paths` must receive an isolated default rooted at `quotes_path.parent / "swing"`, never the repository `var/swing`. Build the T application first, then inject `application.snapshot` and `lambda symbol: application.quotes(symbol, 0)` as SwingService's intraday providers.

- [ ] **Step 4: Implement strict route delegation**

Add exact GET routes from the design and regex-match only these dynamic POST routes:

```python
if path == "/swing":
    self._send(HTTPStatus.OK, SWING_PAGE.encode("utf-8"), "text/html; charset=utf-8")
elif path == "/api/swing/snapshot":
    self._json(HTTPStatus.OK, self.server.swing_application.snapshot())
elif path == "/api/swing/watchlist":
    self._json(HTTPStatus.OK, self.server.swing_application.watchlist())
elif path == "/api/swing/daily-quotes":
    self._swing_daily_quotes()
elif path == "/api/swing/events":
    self._swing_events()
elif path == "/api/swing/portfolio":
    self._json(HTTPStatus.OK, self.server.swing_application.portfolio())
elif path == "/api/swing/alerts":
    self._swing_alerts()
```

Delegate POST requests only through this explicit allowlist; return HTTP 405 for known read-only resources and HTTP 404 for every other path:

```python
if path == "/api/swing/watchlist":
    self._swing_update_watchlist()
elif path == "/api/swing/portfolio/initialize":
    self._swing_initialize_portfolio()
elif path == "/api/swing/trades":
    self._swing_record_trade()
elif re.fullmatch(
    r"/api/swing/trades/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}/reverse",
    path,
):
    self._swing_reverse_trade(path.split("/")[4])
elif re.fullmatch(r"/api/swing/alerts/[0-9a-f]{24}/acknowledge", path):
    self._swing_acknowledge_alert(path.split("/")[4])
elif re.fullmatch(r"/api/swing/alerts/[0-9a-f]{24}/ignore", path):
    self._swing_ignore_alert(path.split("/")[4])
else:
    self._reject_unknown_or_read_only_post(path)
```

Use one `_read_json_object(max_bytes=16_384)` helper. Require `Content-Type: application/json`, strict content length, UTF-8 JSON object, and `Idempotency-Key` for account, trade, reversal, acknowledge, and ignore writes. Swing watchlist POST may only enable or disable an ETF already present in verified metadata; it cannot create metadata. These are local monitor records, not broker operations. The backtest route is added after the real engine in Task 12, so this task must not return fabricated or temporary performance data.

- [ ] **Step 5: Run swing and existing runtime HTTP tests**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_web tests.test_runtime_api -v
```

Expected: all new routes pass and all existing T routes retain their behavior.

- [ ] **Step 6: Commit HTTP integration**

```powershell
git add -- src/etf_rotation/t_web.py tests/test_swing_web.py tests/test_runtime_api.py
git diff --cached --check
git commit -m "feat: expose isolated swing monitor APIs"
```

### Task 10: Standalone swing page, chart, ledger actions, and notifications

**Files:**

- Create: `src/etf_rotation/swing_page.py`
- Create: `tests/test_swing_page.py`
- Modify: `src/etf_rotation/t_page.py`
- Modify: `tests/test_t_monitor.py`

- [ ] **Step 1: Write failing page contract tests**

Require navigation, risk-first hierarchy, no example values, accessible forms, evidence, overlays, incremental daily quotes, SSE reset, and browser notification permission:

```python
class SwingPageContractTests(unittest.TestCase):
    def test_page_has_confirmed_layout_and_no_demo_numbers(self) -> None:
        self.assertIn('href="/"', SWING_PAGE)
        self.assertIn("组合权益", SWING_PAGE)
        self.assertIn("计划总风险", SWING_PAGE)
        self.assertIn("波段标的", SWING_PAGE)
        self.assertIn("MA20", SWING_PAGE)
        self.assertIn("MA60", SWING_PAGE)
        self.assertIn("阻断原因", SWING_PAGE)
        self.assertIn("成交账本", SWING_PAGE)
        self.assertIn("波段回测", SWING_PAGE)
        self.assertIn("初始化波段账户", SWING_PAGE)
        self.assertIn("/api/swing/watchlist", SWING_PAGE)
        self.assertNotIn("100,000", SWING_PAGE)
        self.assertNotIn("4.700", SWING_PAGE)

    def test_page_uses_daily_cursor_sse_and_permission_gated_notifications(self) -> None:
        self.assertIn("/api/swing/daily-quotes", SWING_PAGE)
        self.assertIn("/api/swing/events", SWING_PAGE)
        self.assertIn("new EventSource", SWING_PAGE)
        self.assertIn("Notification.requestPermission", SWING_PAGE)
        self.assertIn("PREDEFINED_STOP_TOUCHED", SWING_PAGE)
```

- [ ] **Step 2: Run page tests and verify RED**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_page -v
```

Expected: import failure for `swing_page`.

- [ ] **Step 3: Implement semantic page shell and responsive chart**

Create `SWING_PAGE` with these stable DOM regions:

```html
<nav aria-label="监控模式"><a href="/">做T监控</a><a href="/swing" aria-current="page">波段监控</a></nav>
<section id="portfolio-risk" aria-label="组合风险"></section>
<aside><h2>波段标的</h2><nav id="swing-watchlist" aria-label="波段标的"></nav></aside>
<main id="swing-detail"><div class="empty">正在载入波段状态</div></main>
<div id="swing-errors" role="alert"></div>
<div id="swing-live-status" aria-live="polite"></div>
```

Render the selected symbol only. The 120-bar SVG must include labeled date and raw-price axes, raw close, adjusted MA20/MA60 mapped to raw display scale, entry zone, hard stop, trailing stop, and a keyboard/touch tooltip. At widths below 850px stack sidebar, chart, and plan; below 600px use two-column evidence and full-width action buttons.

- [ ] **Step 4: Implement state updates and local-write forms**

Use authoritative reset before upsert and preserve the selected symbol:

```javascript
function applyDailyPayload(payload) {
  if (payload.reset) dailyBySymbol.set(payload.symbol, new Map());
  const bars = dailyBySymbol.get(payload.symbol) || new Map();
  for (const bar of payload.upserts || []) bars.set(bar.trading_date, bar);
  dailyBySymbol.set(payload.symbol, bars);
  dailyRevision = Number(payload.revision || 0);
  renderSelected();
}

async function postLocalRecord(path, payload) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': crypto.randomUUID(),
    },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.message || result.error || '保存失败');
  return result;
}
```

Account initialization, trade confirmation, reversal, acknowledge, and ignore forms must show inline success/error and retain failed input. None may call a broker URL.

The watchlist control may only enable or disable the six verified first-version symbols. It must not offer a free-form unknown ETF add path; future expansion remains metadata-first.

- [ ] **Step 5: Implement notification gating and feed failure revocation**

Only notify new active alerts after explicit permission. On SSE error or snapshot/daily failure, clear `APPROACHING_ENTRY_ZONE` and `PREDEFINED_STOP_TOUCHED`, mark execution paused, and keep the last formal plan visible with its date.

```javascript
function notifyAlert(alert) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  if (notifiedAlertIds.has(alert.alert_id)) return;
  notifiedAlertIds.add(alert.alert_id);
  new Notification(`波段提醒 · ${alert.symbol}`, {body: alert.label});
}
```

- [ ] **Step 6: Run both page contract suites**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_page tests.test_t_monitor.MonitorWebTests -v
```

Expected: the swing page passes and the T page contains a working `/swing` navigation link without changing its monitoring semantics.

- [ ] **Step 7: Commit the page**

```powershell
git add -- src/etf_rotation/swing_page.py src/etf_rotation/t_page.py tests/test_swing_page.py tests/test_t_monitor.py
git diff --cached --check
git commit -m "feat: add swing monitoring page"
```

### Task 11: Single-symbol next-open swing backtest

**Files:**

- Create: `src/etf_rotation/swing_backtest.py`
- Create: `tests/test_swing_backtest.py`

- [ ] **Step 1: Write failing fill and benchmark tests**

Require adjusted indicators, raw next-open fills, actual gap-through-stop execution, fees, slippage, lot constraints, T+1, and equal initial cash benchmark:

```python
class SingleSymbolSwingBacktestTests(unittest.TestCase):
    def test_signal_executes_at_next_raw_open_without_lookahead(self) -> None:
        result = SwingBacktester(self.config, self.trading).run_symbol(
            bars=backtest_bars_with_trial_signal(),
            initial_cash=100_000.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.signal_date.isoformat(), "2026-06-30")
        self.assertEqual(trade.execution_date.isoformat(), "2026-07-01")
        self.assertEqual(trade.raw_reference_price, 4.72)
        self.assertGreater(trade.fill_price, trade.raw_reference_price)

    def test_gap_below_stop_uses_executable_open_not_ideal_stop(self) -> None:
        result = self.backtester.run_symbol(gap_below_stop_bars(), 100_000.0)
        exit_trade = next(trade for trade in result.trades if trade.side == "SELL")
        self.assertLess(exit_trade.fill_price, exit_trade.planned_stop)
        self.assertEqual(exit_trade.reason, "GAP_THROUGH_STOP")

    def test_no_completed_trade_reports_insufficient_sample(self) -> None:
        result = self.backtester.run_symbol(no_signal_bars(), 100_000.0)
        self.assertEqual(result.status, "INSUFFICIENT_SAMPLE")
        self.assertIsNone(result.outperformance)
```

- [ ] **Step 2: Run backtest tests and verify RED**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_backtest.SingleSymbolSwingBacktestTests -v
```

Expected: import failure for `swing_backtest`.

- [ ] **Step 3: Implement fills and accounting**

Expose stable result types:

```python
@dataclass(frozen=True)
class SwingFill:
    symbol: str
    side: str
    shares: int
    signal_date: date
    execution_date: date
    raw_reference_price: float
    fill_price: float
    fee: float
    planned_stop: float | None
    reason: str


class SwingBacktester:
    def run_symbol(
        self, bars: Sequence[DailyBar], initial_cash: float,
    ) -> SwingBacktestResult:
        account = BacktestAccount(initial_cash, self.trading)
        for index in range(self.config.minimum_daily_bars - 1, len(bars) - 1):
            signal_bars = tuple(bars[: index + 1])
            execution_bar = bars[index + 1]
            decision = evaluate_swing(signal_bars, self.config, account.context())
            account.execute(decision, execution_bar)
            account.mark(execution_bar)
        return account.result(bars, initial_cash)
```

Apply buy slippage above and sell slippage below raw execution price. Enforce available volume participation, T+1 sellable inventory, cash, lot, and price-limit executability. Compute buy-and-hold from the same first executable date, cash, fee, slippage, and lot size.

- [ ] **Step 4: Add metric and determinism tests**

Test cumulative/annualized return, drawdown, Calmar, Sharpe, win rate, payoff ratio, average holding days, utilization, longest losing streak, fees, slippage, and rejection counts. Re-running identical input must produce byte-identical JSON serialization.

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_backtest.SingleSymbolSwingBacktestTests -v
```

Expected: all single-symbol tests pass.

- [ ] **Step 5: Commit single-symbol backtesting**

```powershell
git add -- src/etf_rotation/swing_backtest.py tests/test_swing_backtest.py
git diff --cached --check
git commit -m "feat: backtest swing signals at next open"
```

### Task 12: Shared-cash portfolio backtest and sample checks

**Files:**

- Modify: `src/etf_rotation/swing_backtest.py`
- Modify: `src/etf_rotation/swing_strategy.py`
- Modify: `src/etf_rotation/swing_service.py`
- Modify: `src/etf_rotation/t_web.py`
- Modify: `tests/test_swing_backtest.py`
- Modify: `tests/test_swing_strategy.py`
- Modify: `tests/test_swing_service.py`
- Modify: `tests/test_swing_web.py`

- [ ] **Step 1: Add a deterministic trend score to decisions**

Write a failing strategy test, then add `trend_score: float` to `SwingDecision`. Define it exactly as `max(0, (MA60[t] / MA60[t-10] - 1) / 0.01) + max(0, (adjusted_close[t] / MA60[t] - 1) / 0.05)`. It is ranking evidence, not a candidate gate.

```python
def test_trend_score_is_deterministic_and_does_not_change_candidate_gate(self) -> None:
    first = evaluate_swing(self.bars, self.config, self.portfolio)
    second = evaluate_swing(self.bars, self.config, self.portfolio)
    self.assertEqual(first.state, second.state)
    self.assertEqual(first.trend_score, second.trend_score)
    self.assertGreaterEqual(first.trend_score, 0.0)
```

- [ ] **Step 2: Write failing shared-cash and priority tests**

```python
class PortfolioSwingBacktestTests(unittest.TestCase):
    def test_candidates_compete_for_one_cash_and_risk_budget(self) -> None:
        result = self.backtester.run_portfolio(
            bars_by_symbol=two_simultaneous_candidates(),
            initial_cash=100_000.0,
        )
        self.assertLessEqual(result.max_equity_weight, 0.80 + 1e-12)
        self.assertLessEqual(result.max_planned_risk, 0.02 + 1e-12)
        self.assertGreater(result.rejections["PORTFOLIO_RISK_LIMIT"], 0)

    def test_exit_reduce_add_and_trial_priority_is_stable(self) -> None:
        actions = self.backtester.rank_actions(priority_fixture())
        self.assertEqual(
            [action.kind for action in actions],
            ["EXIT", "REDUCE", "ADD", "TRIAL_ENTRY"],
        )

    def test_equal_weight_baseline_uses_common_valid_date_range(self) -> None:
        result = self.backtester.run_portfolio(staggered_history_fixture(), 100_000.0)
        self.assertEqual(result.common_start_date.isoformat(), "2024-01-02")
        self.assertAlmostEqual(sum(result.baseline_weights.values()), 1.0)
```

- [ ] **Step 3: Run portfolio backtest tests and verify RED**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_backtest.PortfolioSwingBacktestTests -v
```

Expected: `run_portfolio` or `trend_score` is missing.

- [ ] **Step 4: Implement portfolio simulation and stability output**

```python
def rank_actions(self, actions: Sequence[PendingAction]) -> tuple[PendingAction, ...]:
    priority = {"EXIT": 0, "REDUCE": 1, "ADD": 2, "TRIAL_ENTRY": 3}
    return tuple(sorted(
        actions,
        key=lambda action: (
            priority[action.kind],
            -action.trend_score,
            action.symbol,
        ),
    ))
```

Simulate one shared cash balance, one portfolio risk budget, real per-symbol lot metadata, and one event sequence per completed date. The equal-weight baseline buys all symbols at their first raw open in the common valid range using the same fees, slippage, and total cash.

Use exact rolling windows from configuration: 504 training days, 126 untouched test days, and a 126-day step. The neighborhood report must evaluate all combinations of short MA `{18, 20, 22}`, long MA `{55, 60, 65}`, initial stop ATR `{1.75, 2.0, 2.25}`, and trailing stop ATR `{2.75, 3.0, 3.25}`. Report all 81 variants in stable parameter order and never select only the best result.

- [ ] **Step 5: Wire real backtests into the service and HTTP route**

Add `SwingService.backtest(symbol: str | None, scope: str)`. `scope="symbol"` requires an enabled six-digit symbol; `scope="portfolio"` rejects a symbol. Cache by `(scope, symbol, strategy_version, latest_trading_date, history_digest)` under `var/swing/backtests` and invalidate only when one key component changes.

Add `GET /api/swing/backtest?scope=symbol&symbol=510300` and `GET /api/swing/backtest?scope=portfolio`. Map invalid query shapes to HTTP 400 and data/sample failures to a structured HTTP 200 result with `status="INSUFFICIENT_SAMPLE"` or `status="DATA_UNAVAILABLE"`; never synthesize outperformance.

```python
def test_backtest_routes_use_real_engine_and_strict_query_shapes(self) -> None:
    symbol = self.get_json("/api/swing/backtest?scope=symbol&symbol=510300")
    portfolio = self.get_json("/api/swing/backtest?scope=portfolio")
    self.assertEqual(symbol["scope"], "symbol")
    self.assertEqual(portfolio["scope"], "portfolio")
    self.assertNotIn("deprecated_alias", symbol)
    self.assertNotIn("demo", json.dumps(portfolio))
```

- [ ] **Step 6: Run complete swing backtests and routes**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_swing_strategy tests.test_swing_backtest tests.test_swing_service tests.test_swing_web -v
```

Expected: all strategy and backtest tests pass.

- [ ] **Step 7: Commit portfolio backtesting**

```powershell
git add -- src/etf_rotation/swing_backtest.py src/etf_rotation/swing_strategy.py src/etf_rotation/swing_service.py src/etf_rotation/t_web.py tests/test_swing_backtest.py tests/test_swing_strategy.py tests/test_swing_service.py tests/test_swing_web.py
git diff --cached --check
git commit -m "feat: add shared-cash swing portfolio backtest"
```

### Task 13: CLI, launch paths, documentation, and runtime privacy

**Files:**

- Modify: `src/etf_rotation/cli.py`
- Modify: `scripts/start-monitor.ps1`
- Modify: `tests/test_market_data.py`
- Modify: `tests/test_run_tests_script.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing CLI and launch-script contract tests**

```python
def test_monitor_cli_has_independent_swing_paths(self) -> None:
    parser = _parser()
    arguments = parser.parse_args(["monitor"])
    self.assertEqual(arguments.swing_watchlist.name, "watchlist.json")
    self.assertEqual(arguments.swing_strategy.name, "strategy.json")
    self.assertEqual(arguments.swing_daily_history.parts[-2:], ("swing", "daily_quotes.jsonl"))
    self.assertEqual(arguments.swing_trades.parts[-2:], ("swing", "trades.jsonl"))


def test_start_monitor_passes_all_swing_paths_to_same_process(self) -> None:
    script = (ROOT / "scripts" / "start-monitor.ps1").read_text(encoding="utf-8")
    for argument in (
        "--swing-watchlist", "--swing-strategy", "--swing-daily-history",
        "--swing-portfolio", "--swing-trades", "--swing-alerts",
        "--swing-backtests",
    ):
        self.assertIn(argument, script)
    self.assertIn("var\\swing", script)
```

- [ ] **Step 2: Run CLI/script tests and verify RED**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_market_data.CliPathTests tests.test_run_tests_script -v
```

Expected: swing CLI arguments are missing.

- [ ] **Step 3: Add explicit paths and build `SwingPaths` in the CLI**

Add these parser defaults:

```python
SWING_DATA_ROOT = PROJECT_ROOT / "data" / "swing"
SWING_RUNTIME_ROOT = PROJECT_ROOT / "var" / "swing"

monitor.add_argument("--swing-watchlist", type=Path, default=SWING_DATA_ROOT / "watchlist.json")
monitor.add_argument("--swing-strategy", type=Path, default=SWING_DATA_ROOT / "strategy.json")
monitor.add_argument("--swing-daily-history", type=Path, default=SWING_RUNTIME_ROOT / "daily_quotes.jsonl")
monitor.add_argument("--swing-portfolio", type=Path, default=SWING_RUNTIME_ROOT / "portfolio.json")
monitor.add_argument("--swing-trades", type=Path, default=SWING_RUNTIME_ROOT / "trades.jsonl")
monitor.add_argument("--swing-alerts", type=Path, default=SWING_RUNTIME_ROOT / "alerts.jsonl")
monitor.add_argument("--swing-backtests", type=Path, default=SWING_RUNTIME_ROOT / "backtests")
```

Pass a `SwingPaths` object and an `EastmoneyDailyCollector` to `create_server()`. `--no-collect` must disable both network collectors but retain read-only local T and swing pages.

- [ ] **Step 4: Update the PowerShell launcher and README**

Create `var/swing` next to `var/monitor`, pass every explicit path, and keep one hidden Python process and one PID. Document `/swing`, local ledger privacy, daily finalization, API routes, formal versus intraday states, and both backtests. State that `var/` is already ignored and add this exact automated assertion:

```python
def test_all_swing_runtime_paths_are_git_ignored(self) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "var/swing/daily_quotes.jsonl",
         "var/swing/portfolio.json", "var/swing/trades.jsonl",
         "var/swing/alerts.jsonl", "var/swing/backtests/result.json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertEqual(len(result.stdout.splitlines()), 5)
```

- [ ] **Step 5: Run script, CLI, and documentation-adjacent tests**

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_market_data.CliPathTests tests.test_run_tests_script tests.test_swing_config -v
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit runtime integration**

```powershell
git add -- src/etf_rotation/cli.py scripts/start-monitor.ps1 tests/test_market_data.py tests/test_run_tests_script.py README.md
git diff --cached --check
git commit -m "docs: integrate swing monitor runtime"
```

### Task 14: Full regression, browser acceptance, and isolation evidence

**Files:**

- Modify only if verification exposes a defect in a file already owned by Tasks 1–13.
- Test: all `tests/test_*.py`

- [ ] **Step 1: Run the complete automated suite**

```powershell
.\scripts\run-tests.ps1
```

Expected: every existing and new test passes with zero failures and zero errors.

- [ ] **Step 2: Run static repository checks**

```powershell
git diff --check
git status --short
git check-ignore var/swing/daily_quotes.jsonl var/swing/portfolio.json var/swing/trades.jsonl var/swing/alerts.jsonl
```

Expected: no whitespace errors; only intended branch changes are present; every runtime swing path is ignored.

- [ ] **Step 3: Start one service and verify both pages**

Use a non-production test port and a network-capable local environment:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m etf_rotation.cli monitor --host 127.0.0.1 --port 8766
```

Expected: `/` and `/swing` both return HTTP 200 from the same process. `/api/snapshot` and `/api/swing/snapshot` have separate revisions and strategies.

- [ ] **Step 4: Verify the real page in the in-app browser**

Use `browser:control-in-app-browser` and check at desktop width and 390px width:

- Top risk metrics, six-symbol list, chart, evidence, plan, alerts, ledger, and backtest are readable without overlap.
- No sample price or account values appear when runtime data is absent.
- Failed account/trade forms retain input and show an inline error.
- Successful local records visibly update portfolio state after the server acknowledges them.
- Notification permission is requested only after a user action.
- SSE reconnect preserves the selected symbol and applies authoritative reset before daily upserts.

- [ ] **Step 5: Execute fault-isolation acceptance cases**

Use test-injected collectors/providers rather than changing production files:

- Daily collector failure leaves canonical daily history unchanged and produces no new formal state.
- Intraday failure retracts proximity and stop-touch overlays and marks formal plans paused.
- Corrupt portfolio projection is rebuilt from valid trades; corrupt trade history blocks position-dependent candidates.
- Swing backtest failure affects only its panel.
- Swing producer failure does not change `/api/snapshot` or T candidate revisions.
- T collector failure does not change completed daily formal swing state.

- [ ] **Step 6: Re-run the complete suite after any verification fix**

```powershell
.\scripts\run-tests.ps1
```

Expected: zero failures and zero errors after the final code change.

- [ ] **Step 7: Commit only verified fixes, if any**

```powershell
git add -- data/swing/watchlist.json data/swing/strategy.json src/etf_rotation/eastmoney_client.py src/etf_rotation/quote_collector.py src/etf_rotation/swing_alerts.py src/etf_rotation/swing_backtest.py src/etf_rotation/swing_collector.py src/etf_rotation/swing_config.py src/etf_rotation/swing_data.py src/etf_rotation/swing_page.py src/etf_rotation/swing_portfolio.py src/etf_rotation/swing_service.py src/etf_rotation/swing_strategy.py src/etf_rotation/t_page.py src/etf_rotation/t_web.py src/etf_rotation/cli.py scripts/start-monitor.ps1 tests/swing_helpers.py tests/test_market_data.py tests/test_run_tests_script.py tests/test_runtime_api.py tests/test_swing_alerts.py tests/test_swing_backtest.py tests/test_swing_collector.py tests/test_swing_config.py tests/test_swing_data.py tests/test_swing_page.py tests/test_swing_portfolio.py tests/test_swing_service.py tests/test_swing_strategy.py tests/test_swing_web.py tests/test_t_monitor.py README.md
git diff --cached --check
git commit -m "fix: harden swing monitor integration"
```

If Step 5 required no code changes, skip this commit. Never create an empty commit.

## Completion checklist

- [ ] Every task commit contains only its declared files.
- [ ] The complete test suite passes from a clean process.
- [ ] `/` and `/swing` run in one process and retain independent state/revisions.
- [ ] Formal swing state uses completed daily bars only.
- [ ] Intraday overlays retract immediately on feed failure.
- [ ] Local trades reconstruct the exact portfolio and never invoke a broker.
- [ ] Single-symbol and portfolio backtests use next-open raw fills and adjusted signals.
- [ ] No runtime account, trade, alert, backtest, or daily-history file is tracked by Git.
- [ ] The original checkout's pre-existing uncommitted changes remain untouched.
