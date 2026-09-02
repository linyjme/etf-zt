# ETF T Monitor Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a trustworthy ETF T-monitor whose persisted bars are finalized and validated, whose candidates require confirmed range conditions, and whose backtest models base inventory, T capacity, T+1 sellability, paired legs, and a no-trade baseline.

**Architecture:** Keep the standard-library Python HTTP server and auditable JSON/JSONL storage, but move market-data validation, regime classification, candidate gating, and T-account simulation into focused modules. A single background producer validates and persists data before atomically publishing a revisioned immutable snapshot; HTTP and SSE paths only read published state.

**Tech Stack:** Python 3.12+ standard library, `unittest`, `dataclasses`, `zoneinfo`, atomic filesystem replacement, HTML/CSS/vanilla JavaScript, PowerShell launch and verification scripts.

---

## File map

**Create**

- `src/etf_rotation/constants.py` — all strategy, cost, timing, and backtest defaults.
- `src/etf_rotation/market_data.py` — session boundaries, finalized-minute filtering, health classification, validation, schema v3 records, atomic history upsert.
- `src/etf_rotation/regime.py` — range/trend window metrics and consecutive confirmation.
- `src/etf_rotation/t_strategy.py` — candidate gate and blocked reasons.
- `src/etf_rotation/t_backtest.py` — inventory ledger, execution constraints, paired legs, benchmark, reporting.
- `src/etf_rotation/t_page.py` — embedded page and browser-side incremental rendering.
- `src/etf_rotation/history_migration.py` — validated one-time rebuild from a closing snapshot.
- `data/monitor/market_calendar.json` — configured exchange-closed weekdays for health classification.
- `tests/test_market_data.py` — completion, session, health, validation, and upsert tests.
- `tests/test_regime.py` — hard-gate and confirmation tests.
- `tests/test_t_strategy.py` — candidate-gate tests.
- `tests/test_t_backtest.py` — inventory, execution, pairing, and benchmark tests.
- `tests/test_runtime_api.py` — single-producer, incremental API, and SSE tests.
- `docs/git-history-cleanup.md` — separately authorized Git-history cleanup procedure.
- `.gitignore` — runtime/cache/log exclusions.

**Modify**

- `src/etf_rotation/t_monitor.py` — models, JSON adapter, watchlist defaults, compatibility facade, snapshot serialization.
- `src/etf_rotation/quote_collector.py` — observation metadata and validation integration.
- `src/etf_rotation/etf_metadata.py` — trading attributes next to index metadata.
- `src/etf_rotation/t_web.py` — single producer, revision cache, read-only APIs, true T backtest endpoints.
- `src/etf_rotation/cli.py` — runtime-root and rebuild-history commands.
- `data/monitor/etf_metadata.json` — six ETFs' exchange, limits, lot, tick, volume unit, and T+1 metadata.
- `tests/test_t_monitor.py` — compatibility assertions and new neutral wording.
- `scripts/run-tests.ps1` — portable Python discovery.
- `scripts/start-monitor.ps1` — project-relative runtime paths.
- `scripts/restart-monitor.ps1` — project-relative runtime paths.
- `scripts/stop-monitor.ps1` — project-relative runtime paths.
- `README.md` — schema, states, APIs, backtest meaning, migration, and relative commands.

**Remove from the tracked project state after migration**

- `data/monitor/quotes.json`
- `data/monitor/quotes.jsonl`
- `data/monitor/alerts.jsonl`
- `data/monitor/history/`
- tracked `src/**/__pycache__/*.pyc`
- tracked `tests/**/__pycache__/*.pyc`

### Task 1: Centralize defaults and make the test runner portable

**Files:**
- Create: `src/etf_rotation/constants.py`
- Create: `tests/test_defaults.py`
- Modify: `src/etf_rotation/t_monitor.py` (`load_watchlist`)
- Modify: `src/etf_rotation/t_web.py` (`MonitorApplication.add_watch_item`)
- Modify: `scripts/run-tests.ps1`

- [ ] **Step 1: Write failing default-value tests**

```python
# tests/test_defaults.py
import json
from pathlib import Path
import tempfile
import unittest

from etf_rotation.constants import DEFAULT_GRID_WIDTH_PCT
from etf_rotation.t_monitor import load_watchlist
from etf_rotation.t_web import MonitorApplication


class DefaultTests(unittest.TestCase):
    def test_one_grid_defaults_to_two_tenths_percent_everywhere(self) -> None:
        self.assertEqual(DEFAULT_GRID_WIDTH_PCT, 0.002)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watchlist = root / "watchlist.json"
            quotes = root / "quotes.json"
            watchlist.write_text(json.dumps({"watchlist": [{"symbol": "510300"}]}), encoding="utf-8")
            quotes.write_text(json.dumps({"quotes": []}), encoding="utf-8")
            self.assertEqual(load_watchlist(watchlist)[0].grid_width_pct, 0.002)
            app = MonitorApplication(quotes, watchlist)
            created = app.add_watch_item("159915", "创业板ETF")
            self.assertEqual(created["grid_width_pct"], 0.002)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_defaults -v
```

Expected: import failure for `etf_rotation.constants`.

- [ ] **Step 3: Add constants and replace both 2% defaults**

```python
# src/etf_rotation/constants.py
DEFAULT_GRID_WIDTH_PCT = 0.002
RANGE_WINDOW_MINUTES = 20
RANGE_CONFIRMATIONS = 3
TREND_CONFIRMATIONS = 2
VWAP_NEUTRAL_BAND_PCT = 0.0002
RANGE_MAX_ER = 0.30
RANGE_MAX_ONE_SIDE_RATIO = 0.70
RANGE_MAX_VWAP_SLOPE = 0.001
TREND_MIN_ER = 0.55
TREND_MIN_ONE_SIDE_RATIO = 0.80
TREND_MIN_VWAP_SLOPE = 0.001
BUY_COMMISSION_RATE = 0.00012
SELL_COMMISSION_RATE = 0.00012
SLIPPAGE_RATE = 0.001
MINIMUM_COMMISSION_CNY = 0.0
DEFAULT_BASE_NOTIONAL_CNY = 15_000.0
DEFAULT_T_CAPACITY_RATIO = 0.20
DEFAULT_VOLUME_PARTICIPATION = 0.10
REALTIME_MAX_AGE_SECONDS = 75
DELAYED_MAX_AGE_SECONDS = 180
```

In `load_watchlist`, use `raw_item.get("grid_width_pct", DEFAULT_GRID_WIDTH_PCT)`. In `add_watch_item`, use the same imported constant.

Replace `scripts/run-tests.ps1` with executable discovery that does not select a nonexistent Launcher version:

```powershell
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
if (Get-Command python -ErrorAction SilentlyContinue) {
    & python -m unittest discover -s (Join-Path $projectRoot 'tests') -v
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m unittest discover -s (Join-Path $projectRoot 'tests') -v
} else {
    throw 'Python 3 runtime not found'
}
exit $LASTEXITCODE
```

- [ ] **Step 4: Run focused and full tests and verify GREEN**

Run the focused command from Step 2, then:

```powershell
& '.\scripts\run-tests.ps1'
```

Expected: the focused test passes; the full suite passes when `python` or `py -3` is available. In the Codex runtime, run the bundled Python command if it is not on `PATH`.

- [ ] **Step 5: Commit**

```powershell
git add src/etf_rotation/constants.py src/etf_rotation/t_monitor.py src/etf_rotation/t_web.py scripts/run-tests.ps1 tests/test_defaults.py
git commit -m "fix: unify monitor defaults"
```

### Task 2: Add per-ETF trading metadata

**Files:**
- Modify: `src/etf_rotation/etf_metadata.py`
- Modify: `data/monitor/etf_metadata.json`
- Modify: `tests/test_t_monitor.py` (`EtfMetadataTests`)

- [ ] **Step 1: Extend the metadata test with exact trading attributes**

```python
def test_initial_mapping_contains_t_plus_one_trading_metadata(self) -> None:
    path = Path(__file__).resolve().parents[1] / "data" / "monitor" / "etf_metadata.json"
    items = EtfMetadataStore(path).load()
    self.assertEqual(set(items), {"510300", "510500", "563360", "512100", "159915", "588000"})
    for item in items.values():
        self.assertFalse(item.trading.intraday_turnaround)
        self.assertEqual(item.trading.sellable_delay_days, 1)
        self.assertEqual(item.trading.lot_size, 100)
        self.assertEqual(item.trading.price_tick, 0.001)
        self.assertEqual(item.trading.volume_unit_shares, 100)
    self.assertEqual(items["159915"].trading.price_limit_pct, 0.20)
    self.assertEqual(items["588000"].trading.price_limit_pct, 0.20)
    self.assertEqual(items["510300"].trading.price_limit_pct, 0.10)
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_t_monitor.EtfMetadataTests.test_initial_mapping_contains_t_plus_one_trading_metadata -v
```

Expected: `EtfMetadata` has no `trading` attribute.

- [ ] **Step 3: Implement strict trading metadata parsing**

```python
@dataclass(frozen=True)
class TradingMetadata:
    exchange: str
    asset_type: str
    intraday_turnaround: bool
    sellable_delay_days: int
    lot_size: int
    price_tick: float
    price_limit_pct: float
    volume_unit_shares: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EtfMetadata:
    symbol: str
    name: str
    index: IndexMetadata
    trading: TradingMetadata
```

Parse a required `trading` object and reject non-positive lot/tick/limit/unit values, non-boolean `intraday_turnaround`, negative `sellable_delay_days`, and exchanges outside `SSE`/`SZSE`. Upgrade `data/monitor/etf_metadata.json` to schema version 2 and add these exact values:

```json
{"exchange":"SSE","asset_type":"DOMESTIC_EQUITY_ETF","intraday_turnaround":false,"sellable_delay_days":1,"lot_size":100,"price_tick":0.001,"price_limit_pct":0.10,"volume_unit_shares":100}
```

Use `SZSE` and `0.20` for 159915; use `SSE` and `0.20` for 588000; use `SSE` and `0.10` for the other four.

- [ ] **Step 4: Run metadata and full tests**

Run:

```powershell
python -m unittest tests.test_t_monitor.EtfMetadataTests -v
& '.\scripts\run-tests.ps1'
```

Expected: all metadata and existing valuation tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/etf_rotation/etf_metadata.py data/monitor/etf_metadata.json tests/test_t_monitor.py
git commit -m "feat: record ETF trading constraints"
```

### Task 3: Validate finalized minutes and classify market health

**Files:**
- Create: `src/etf_rotation/market_data.py`
- Create: `data/monitor/market_calendar.json`
- Create: `tests/test_market_data.py`
- Modify: `src/etf_rotation/t_monitor.py` (`Quote`, `JsonQuoteAdapter`)
- Modify: `src/etf_rotation/quote_collector.py` (`_fetch`)

- [ ] **Step 1: Write completion, session, health, and validation tests**

```python
# tests/test_market_data.py
from datetime import date, datetime
import unittest
from zoneinfo import ZoneInfo

from etf_rotation.etf_metadata import TradingMetadata
from etf_rotation.market_data import MarketDataValidator, MarketHealthClassifier, finalized_points
from etf_rotation.t_monitor import MarketDataError, QuotePoint

SHANGHAI = ZoneInfo("Asia/Shanghai")


def point(minute: str, price: float = 10.0, volume: float = 100.0, amount: float = 100_000.0) -> QuotePoint:
    return QuotePoint(datetime.fromisoformat(f"2026-08-28T{minute}:00+08:00"), price, 10.0, price, price, price, volume, amount)


TRADING = TradingMetadata("SSE", "DOMESTIC_EQUITY_ETF", False, 1, 100, 0.001, 0.10, 100)


class MarketDataTests(unittest.TestCase):
    def test_only_minutes_older_than_one_minute_are_final(self) -> None:
        points = (point("09:30"), point("09:31"), point("09:32"))
        observed = datetime.fromisoformat("2026-08-28T09:32:30+08:00")
        self.assertEqual([item.timestamp.minute for item in finalized_points(points, observed)], [30, 31])

    def test_market_health_distinguishes_live_delay_outage_lunch_and_close(self) -> None:
        classifier = MarketHealthClassifier(closed_dates={date(2026, 10, 1)})
        last = datetime.fromisoformat("2026-08-28T10:00:00+08:00")
        self.assertEqual(classifier.classify(datetime.fromisoformat("2026-08-28T10:01:00+08:00"), last, None).status, "REALTIME")
        self.assertEqual(classifier.classify(datetime.fromisoformat("2026-08-28T10:02:00+08:00"), last, None).status, "DELAYED")
        self.assertEqual(classifier.classify(datetime.fromisoformat("2026-08-28T10:04:00+08:00"), last, None).status, "OUTAGE")
        self.assertEqual(classifier.classify(datetime.fromisoformat("2026-08-28T12:00:00+08:00"), last, None).status, "LUNCH_BREAK")
        self.assertEqual(classifier.classify(datetime.fromisoformat("2026-08-28T15:10:00+08:00"), last, None).status, "CLOSED")

    def test_configured_exchange_holiday_is_closed(self) -> None:
        closed = load_closed_dates(Path(__file__).resolve().parents[1] / "data" / "monitor" / "market_calendar.json")
        self.assertIn(date(2026, 10, 1), closed)
        classifier = MarketHealthClassifier(closed_dates=closed)
        result = classifier.classify(datetime.fromisoformat("2026-10-01T10:00:00+08:00"), None, None)
        self.assertEqual(result.status, "CLOSED")

    def test_validator_rejects_previous_close_ohlc_limit_and_amount_mismatches(self) -> None:
        validator = MarketDataValidator(TRADING)
        validator.validate_point(point("09:30"), previous_close=10.0)
        with self.assertRaisesRegex(MarketDataError, "OHLC"):
            validator.validate_point(QuotePoint(point("09:31").timestamp, 10.1, 10.0, 10.0, 10.0, 10.0, 100, 100_000), 10.0)
        with self.assertRaisesRegex(MarketDataError, "涨跌幅"):
            validator.validate_point(point("09:31", 11.2, 100, 112_000), 10.0)
        with self.assertRaisesRegex(MarketDataError, "量价"):
            validator.validate_point(point("09:31", 10.0, 100, 50_000), 10.0)
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_market_data -v
```

Expected: import failure for `etf_rotation.market_data`.

- [ ] **Step 3: Implement market-data primitives**

Implement these public interfaces:

```python
@dataclass(frozen=True)
class MarketHealth:
    status: str
    quote_age_seconds: float | None
    reason: str


def finalized_points(points: Sequence[QuotePoint], observed_at: datetime) -> tuple[QuotePoint, ...]:
    return tuple(item for item in points if observed_at >= item.timestamp + timedelta(minutes=1))


class MarketHealthClassifier:
    def __init__(self, closed_dates: set[date] | None = None):
        self.closed_dates = frozenset(closed_dates or ())

    def classify(self, now: datetime, last_quote_at: datetime | None, error: str | None) -> MarketHealth:
        local = now.astimezone(SHANGHAI)
        if local.weekday() >= 5 or local.date() in self.closed_dates or local.time() < time(9, 30) or local.time() > time(15, 0):
            return MarketHealth("CLOSED", None, "非连续交易时段")
        if time(11, 30) < local.time() < time(13, 0):
            return MarketHealth("LUNCH_BREAK", None, "午间休市")
        if error or last_quote_at is None:
            return MarketHealth("OUTAGE", None, error or "缺少当日行情")
        age = max(0.0, (local - last_quote_at.astimezone(SHANGHAI)).total_seconds())
        if age <= REALTIME_MAX_AGE_SECONDS:
            return MarketHealth("REALTIME", age, "行情实时")
        if age <= DELAYED_MAX_AGE_SECONDS:
            return MarketHealth("DELAYED", age, "行情延迟")
        return MarketHealth("OUTAGE", age, "行情断流")
```

`MarketDataValidator.validate_point` must enforce session time, finite positive prices, OHLC ordering, metadata price limit plus one tick, nonnegative volume/amount, zero-pair consistency, and `amount / (volume * volume_unit_shares)` inside `[low - tick, high + tick]`.

Implement `load_closed_dates(path)` with a required schema version 1 and ISO-date strings. Create `data/monitor/market_calendar.json` from the official 2026 exchange schedule with these weekday closure dates:

```json
{
  "schema_version": 1,
  "source": "https://www.sse.com.cn/disclosure/dealinstruc/closed/",
  "closed_dates": [
    "2026-01-01", "2026-01-02",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
    "2026-04-06",
    "2026-05-01", "2026-05-04", "2026-05-05",
    "2026-06-19",
    "2026-09-25",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"
  ]
}
```

Add `observed_at` and `source` to `Quote`; parse record `collected_at`/`observed_at` and source, defaulting only in test fixtures to the quote timestamp plus one minute. Collector records must use a single `observed_at` value per collection and retain schema/source.

- [ ] **Step 4: Run focused and existing adapter/collector tests**

Run:

```powershell
python -m unittest tests.test_market_data tests.test_t_monitor.JsonQuoteAdapterTests tests.test_t_monitor.Trends2QuoteCollectorTests -v
```

Expected: all pass without network access.

- [ ] **Step 5: Commit**

```powershell
git add src/etf_rotation/market_data.py src/etf_rotation/t_monitor.py src/etf_rotation/quote_collector.py data/monitor/market_calendar.json tests/test_market_data.py tests/test_t_monitor.py
git commit -m "feat: validate finalized market minutes"
```

### Task 4: Replace append-only history with atomic schema v3 upsert

**Files:**
- Modify: `src/etf_rotation/market_data.py`
- Modify: `src/etf_rotation/t_monitor.py` (`QuoteHistoryStore` compatibility alias)
- Modify: `tests/test_market_data.py`
- Modify: `tests/test_t_monitor.py` (`QuoteHistoryStoreTests`)

- [ ] **Step 1: Write an upsert regression test**

```python
def metadata_for_test() -> dict[str, EtfMetadata]:
    trading = TradingMetadata("SSE", "DOMESTIC_EQUITY_ETF", False, 1, 100, 0.001, 0.10, 100)
    metadata = EtfMetadata("510300", "沪深300ETF", IndexMetadata("000300", "沪深300", "中证指数"), trading)
    return {"510300": metadata}


def history_quote(price: float, previous_close: float, observed_at: str) -> Quote:
    timestamp = datetime.fromisoformat("2026-08-28T09:30:00+08:00")
    point = QuotePoint(
        timestamp=timestamp,
        price=price,
        average_price=price,
        open=price,
        high=price,
        low=price,
        volume=100.0,
        amount=price * 100.0 * 100.0,
    )
    return Quote(
        symbol="510300",
        name="沪深300ETF",
        price=price,
        average_price=price,
        previous_close=previous_close,
        timestamp=timestamp,
        points=(point,),
        observed_at=datetime.fromisoformat(observed_at),
        source="TEST",
    )


def test_history_upserts_later_final_observation_and_writes_schema_v3(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "quotes.jsonl"
        store = MinuteHistoryStore(path)
        early = history_quote(price=4.095, previous_close=4.095, observed_at="2026-08-28T09:31:01+08:00")
        final = history_quote(price=4.684, previous_close=4.691, observed_at="2026-08-28T17:56:09+08:00")
        store.upsert({"510300": early}, metadata_for_test())
        store.upsert({"510300": final}, metadata_for_test())
        records = store.query("2026-08-28", "510300")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["price"], 4.684)
        self.assertEqual(records[0]["previous_close"], 4.691)
        self.assertEqual(records[0]["schema_version"], 3)
        self.assertEqual(records[0]["trading_date"], "2026-08-28")
        self.assertEqual(records[0]["observed_at"], "2026-08-28T17:56:09+08:00")
        self.assertTrue(records[0]["is_complete"])


def test_history_does_not_persist_current_minute(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "quotes.jsonl"
        store = MinuteHistoryStore(path)
        current = history_quote(price=4.684, previous_close=4.691, observed_at="2026-08-28T09:30:30+08:00")
        store.upsert({"510300": current}, metadata_for_test())
        self.assertEqual(store.query("2026-08-28", "510300"), [])
```

The helper must build one 09:30 OHLC point with internally consistent amount/volume and the supplied quote-level observation time.

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_market_data.MarketDataTests.test_history_upserts_later_final_observation_and_writes_schema_v3 tests.test_market_data.MarketDataTests.test_history_does_not_persist_current_minute -v
```

Expected: `MinuteHistoryStore` is missing.

- [ ] **Step 3: Implement normalized atomic upsert**

```python
class MinuteHistoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def upsert(self, quotes: Mapping[str, Quote], metadata: Mapping[str, EtfMetadata]) -> int:
        with self._lock:
            indexed = {(item["symbol"], item["timestamp"]): item for item in self._read_path(self.path)}
            changed = 0
            for symbol, quote in quotes.items():
                validator = MarketDataValidator(metadata[symbol].trading)
                for point in finalized_points(quote.points, quote.observed_at):
                    validator.validate_point(point, quote.previous_close)
                    record = minute_record(quote, point)
                    key = (symbol, record["timestamp"])
                    old = indexed.get(key)
                    if old is None or record["observed_at"] > str(old.get("observed_at", "")):
                        indexed[key] = record
                        changed += 1
            ordered = [indexed[key] for key in sorted(indexed)]
            self._atomic_write(self.path, ordered)
            self._rewrite_daily(ordered)
            return changed
```

`_atomic_write` must create a UTF-8 temporary file in the destination directory, write one compact JSON object per line, flush, `os.fsync`, and `os.replace`. `_rewrite_daily` must derive each date file from the complete canonical record set, not append independently. Reject inconsistent `previous_close` values within `symbol + trading_date` before replacing any file.

Keep `QuoteHistoryStore = MinuteHistoryStore` or a thin method-compatible wrapper in `t_monitor.py`; change callers from `append` to `upsert` only in the producer task, not HTTP reads.

- [ ] **Step 4: Run all history tests**

Run:

```powershell
python -m unittest tests.test_market_data tests.test_t_monitor.QuoteHistoryStoreTests -v
```

Expected: late final values replace early values, no duplicate primary keys exist, and current minutes stay out of history.

- [ ] **Step 5: Commit**

```powershell
git add src/etf_rotation/market_data.py src/etf_rotation/t_monitor.py tests/test_market_data.py tests/test_t_monitor.py
git commit -m "fix: upsert finalized minute history"
```

### Task 5: Implement hard-gated regime classification

**Files:**
- Create: `src/etf_rotation/regime.py`
- Create: `tests/test_regime.py`
- Modify: `src/etf_rotation/t_monitor.py` (`MonitorSignal`, `snapshot_to_dict`)

- [ ] **Step 1: Write range, one-sided, lunch-reset, and trend tests**

```python
# tests/test_regime.py
import unittest

from etf_rotation.regime import RegimeDetector
from tests.regime_fixtures import alternating_points, one_sided_points, trending_points


class RegimeTests(unittest.TestCase):
    def test_one_sided_points_never_form_range(self) -> None:
        result = RegimeDetector().evaluate(one_sided_points(24))
        self.assertEqual(result.state, "UNCERTAIN")
        self.assertEqual(result.one_side_ratio, 1.0)
        self.assertEqual(result.vwap_crossings, 0)
        self.assertEqual(result.range_confirmation_count, 0)

    def test_range_requires_three_consecutive_windows(self) -> None:
        self.assertEqual(RegimeDetector().evaluate(alternating_points(21)).state, "UNCERTAIN")
        result = RegimeDetector().evaluate(alternating_points(22))
        self.assertEqual(result.state, "RANGE")
        self.assertEqual(result.range_confirmation_count, 3)
        self.assertGreaterEqual(result.vwap_crossings, 2)
        self.assertLessEqual(result.one_side_ratio, 0.70)

    def test_trend_requires_two_aligned_windows(self) -> None:
        result = RegimeDetector().evaluate(trending_points(21, direction=1))
        self.assertEqual(result.state, "UPTREND")
        self.assertEqual(result.trend_confirmation_count, 2)

    def test_lunch_break_resets_window(self) -> None:
        points = alternating_points(20, start="11:11") + alternating_points(10, start="13:00")
        result = RegimeDetector().evaluate(points)
        self.assertEqual(result.state, "UNCERTAIN")
        self.assertEqual(result.sample_count, 10)
```

Create `tests/regime_fixtures.py` with deterministic minute generators: alternating points cross a flat VWAP by 0.05% with a zig-zag path; one-sided points remain 0.10% above a flat VWAP; trending points advance price and VWAP together and advance both highs and lows in the second half.

Use this complete fixture implementation:

```python
from datetime import datetime, timedelta

from etf_rotation.t_monitor import Quote, QuotePoint


def _start(value: str) -> datetime:
    return datetime.fromisoformat(f"2026-08-28T{value}:00+08:00")


def _point(timestamp: datetime, price: float, average: float) -> QuotePoint:
    low = min(price, average) * 0.9999
    high = max(price, average) * 1.0001
    return QuotePoint(timestamp, price, average, price, high, low, 1000.0, price * 1000.0 * 100.0)


def alternating_points(count: int, start: str = "09:30") -> tuple[QuotePoint, ...]:
    origin = _start(start)
    average = 10.0
    return tuple(
        _point(origin + timedelta(minutes=index), average * (1.0005 if index % 2 == 0 else 0.9995), average)
        for index in range(count)
    )


def one_sided_points(count: int, start: str = "09:30") -> tuple[QuotePoint, ...]:
    origin = _start(start)
    return tuple(_point(origin + timedelta(minutes=index), 10.01, 10.0) for index in range(count))


def trending_points(count: int, direction: int, start: str = "09:30") -> tuple[QuotePoint, ...]:
    origin = _start(start)
    points = []
    for index in range(count):
        average = 10.0 + direction * index * 0.0015
        price = average * (1.001 if direction > 0 else 0.999)
        points.append(_point(origin + timedelta(minutes=index), price, average))
    return tuple(points)


def confirmed_range_quote(
    previous_deviation: float,
    current_deviation: float,
    previous_close_distance: float = 0.02,
) -> Quote:
    average = 10.0
    previous = _point(_start("10:00"), average * (1 + previous_deviation), average)
    current = _point(_start("10:01"), average * (1 + current_deviation), average)
    previous_close = current.price * (1 + previous_close_distance if current_deviation < 0 else 1 - previous_close_distance)
    return Quote(
        symbol="510300",
        name="沪深300ETF",
        price=current.price,
        average_price=current.average_price,
        previous_close=previous_close,
        timestamp=current.timestamp,
        points=(previous, current),
        observed_at=current.timestamp + timedelta(minutes=1),
        source="TEST",
    )
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_regime -v
```

Expected: import failure for `etf_rotation.regime`.

- [ ] **Step 3: Implement window metrics and consecutive confirmation**

```python
@dataclass(frozen=True)
class RegimeResult:
    state: str
    label: str
    sample_count: int
    path_efficiency: float | None
    one_side_ratio: float | None
    vwap_crossings: int | None
    vwap_slope: float | None
    above_vwap_count: int
    below_vwap_count: int
    range_confirmation_count: int
    trend_confirmation_count: int
    reasons: tuple[str, ...]


class RegimeDetector:
    def evaluate(self, points: Sequence[QuotePoint]) -> RegimeResult:
        segment = current_continuous_segment(points)
        windows = [segment[end - RANGE_WINDOW_MINUTES:end] for end in range(RANGE_WINDOW_MINUTES, len(segment) + 1)]
        if not windows:
            return uncertain_result(len(segment), "样本不足")
        metrics = [self._metrics(window) for window in windows]
        range_count = trailing_count(metrics, lambda item: item.range_ok)
        trend_direction = metrics[-1].trend_direction
        trend_count = trailing_count(metrics, lambda item: item.trend_direction == trend_direction and trend_direction != 0)
        if range_count >= RANGE_CONFIRMATIONS:
            return result_from(metrics[-1], "RANGE", range_count, 0)
        if trend_count >= TREND_CONFIRMATIONS:
            state = "UPTREND" if trend_direction > 0 else "DOWNTREND"
            return result_from(metrics[-1], state, 0, trend_count)
        return result_from(metrics[-1], "UNCERTAIN", range_count, trend_count)
```

`_metrics` must calculate ER, relative VWAP slope, neutral-band side counts, true side-to-side crossings, one-side ratio, and first-half/second-half high-low progression. `range_ok` is the conjunction of all five confirmed design gates. `trend_direction` is nonzero only when all four trend gates align.

Expose every metric and both confirmation counts through `MonitorSignal` and `snapshot_to_dict`.

- [ ] **Step 4: Run regime and monitor serialization tests**

Run:

```powershell
python -m unittest tests.test_regime tests.test_t_monitor.TMonitorEngineTests.test_regime_fields_are_exposed_and_short_history_is_uncertain -v
```

Expected: all pass; no single-score fallback can produce RANGE.

- [ ] **Step 5: Commit**

```powershell
git add src/etf_rotation/regime.py src/etf_rotation/t_monitor.py tests/test_regime.py tests/regime_fixtures.py tests/test_t_monitor.py
git commit -m "fix: harden market regime classification"
```

### Task 6: Gate neutral T candidates on health, narrowing deviation, and costs

**Files:**
- Create: `src/etf_rotation/t_strategy.py`
- Create: `tests/test_t_strategy.py`
- Modify: `src/etf_rotation/t_monitor.py` (`TMonitorEngine.evaluate`)
- Modify: `tests/test_t_monitor.py` (`TMonitorEngineTests`)

- [ ] **Step 1: Write candidate-gate tests**

```python
# tests/test_t_strategy.py
import unittest

from etf_rotation.market_data import MarketHealth
from etf_rotation.t_strategy import CandidateContext, TStrategy
from tests.regime_fixtures import confirmed_range_quote


class TStrategyTests(unittest.TestCase):
    def test_uncertain_delayed_and_widening_deviation_are_observation_only(self) -> None:
        quote = confirmed_range_quote(previous_deviation=-0.007, current_deviation=-0.006)
        strategy = TStrategy()
        uncertain = strategy.evaluate(CandidateContext(quote, "UNCERTAIN", MarketHealth("REALTIME", 10, "行情实时"), 0.002))
        delayed = strategy.evaluate(CandidateContext(quote, "RANGE", MarketHealth("DELAYED", 100, "行情延迟"), 0.002))
        widening_quote = confirmed_range_quote(previous_deviation=-0.005, current_deviation=-0.006)
        widening = strategy.evaluate(CandidateContext(widening_quote, "RANGE", MarketHealth("REALTIME", 10, "行情实时"), 0.002))
        self.assertEqual(uncertain.action, "DEVIATION_OBSERVE")
        self.assertEqual(delayed.action, "DEVIATION_OBSERVE")
        self.assertEqual(widening.action, "DEVIATION_OBSERVE")
        self.assertIn("REGIME_NOT_RANGE", uncertain.blocked_reasons)
        self.assertIn("MARKET_NOT_REALTIME", delayed.blocked_reasons)
        self.assertIn("DEVIATION_NOT_NARROWING", widening.blocked_reasons)

    def test_confirmed_range_narrowing_and_cost_coverage_produces_candidate(self) -> None:
        quote = confirmed_range_quote(previous_deviation=-0.008, current_deviation=-0.007)
        decision = TStrategy().evaluate(CandidateContext(quote, "RANGE", MarketHealth("REALTIME", 10, "行情实时"), 0.002))
        self.assertEqual(decision.action, "BUY_CANDIDATE")
        self.assertGreater(decision.expected_net_edge_pct, 0)
        self.assertEqual(decision.blocked_reasons, ())

    def test_edge_below_round_trip_cost_is_observation_only(self) -> None:
        quote = confirmed_range_quote(previous_deviation=-0.0023, current_deviation=-0.0022, previous_close_distance=0.02)
        decision = TStrategy().evaluate(CandidateContext(quote, "RANGE", MarketHealth("REALTIME", 10, "行情实时"), 0.0007))
        self.assertEqual(decision.action, "DEVIATION_OBSERVE")
        self.assertIn("COST_NOT_COVERED", decision.blocked_reasons)
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_t_strategy -v
```

Expected: import failure for `etf_rotation.t_strategy`.

- [ ] **Step 3: Implement the conjunction gate**

```python
@dataclass(frozen=True)
class CandidateDecision:
    action: str
    label: str
    expected_gross_edge_pct: float
    round_trip_cost_pct: float
    expected_net_edge_pct: float
    blocked_reasons: tuple[str, ...]


class TStrategy:
    def evaluate(self, context: CandidateContext) -> CandidateDecision:
        latest, previous = context.quote.points[-1], context.quote.points[-2]
        current_deviation = latest.price / latest.average_price - 1
        previous_deviation = previous.price / previous.average_price - 1
        grid_size = latest.average_price * context.grid_width_pct
        deviation_grids = abs(latest.price - latest.average_price) / grid_size
        close_grids = abs(latest.price - context.quote.previous_close) / grid_size
        gross = abs(latest.price - latest.average_price) / latest.price
        cost = BUY_COMMISSION_RATE + SELL_COMMISSION_RATE + 2 * SLIPPAGE_RATE
        reasons: list[str] = []
        if context.health.status != "REALTIME": reasons.append("MARKET_NOT_REALTIME")
        if context.regime_state != "RANGE": reasons.append("REGIME_NOT_RANGE")
        if deviation_grids < 3: reasons.append("DEVIATION_BELOW_3_GRIDS")
        if close_grids < 5: reasons.append("PREVIOUS_CLOSE_DISTANCE_BELOW_5_GRIDS")
        if current_deviation * previous_deviation <= 0 or abs(current_deviation) >= abs(previous_deviation): reasons.append("DEVIATION_NOT_NARROWING")
        if gross <= cost: reasons.append("COST_NOT_COVERED")
        if fast_rise_grids(context.quote, grid_size) >= 5: reasons.append("FAST_RISE")
        if reasons:
            action = "DEVIATION_OBSERVE" if deviation_grids >= 3 else "WAIT"
            return CandidateDecision(action, "偏离观察" if action == "DEVIATION_OBSERVE" else "等待", gross, cost, gross - cost, tuple(reasons))
        action = "SELL_CANDIDATE" if current_deviation > 0 else "BUY_CANDIDATE"
        return CandidateDecision(action, "做T候选", gross, cost, gross - cost, ())
```

`TMonitorEngine.evaluate` must filter to finalized points, obtain `RegimeDetector` output, call `TStrategy`, and serialize neutral candidate names, edge fields, metrics, confirmations, and blocked reasons. Remove the old asymmetric trend-only blockers and the `GOLDEN` signal level.

- [ ] **Step 4: Run strategy and engine tests**

Run:

```powershell
python -m unittest tests.test_t_strategy tests.test_t_monitor.TMonitorEngineTests -v
```

Expected: candidate tests pass; update old reminder expectations to candidate or observation based on confirmed-range fixtures.

- [ ] **Step 5: Commit**

```powershell
git add src/etf_rotation/t_strategy.py src/etf_rotation/t_monitor.py tests/test_t_strategy.py tests/test_t_monitor.py tests/regime_fixtures.py
git commit -m "fix: require confirmed mean reversion candidates"
```

### Task 7: Make the background runtime the only producer

**Files:**
- Modify: `src/etf_rotation/t_web.py` (`MonitorApplication`)
- Create: `tests/test_runtime_api.py`
- Modify: `tests/test_t_monitor.py` (`MonitorRefreshTests`, `MonitorWebTests`)

- [ ] **Step 1: Write single-writer and stale-candidate tests**

```python
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from etf_rotation.t_monitor import MarketDataError
from etf_rotation.t_web import MonitorApplication


def test_metadata_document() -> dict[str, object]:
    return {
        "schema_version": 2,
        "items": [{
            "symbol": "510300",
            "name": "沪深300ETF",
            "index": {"code": "000300", "name": "沪深300", "provider": "中证指数"},
            "trading": {
                "exchange": "SSE",
                "asset_type": "DOMESTIC_EQUITY_ETF",
                "intraday_turnaround": False,
                "sellable_delay_days": 1,
                "lot_size": 100,
                "price_tick": 0.001,
                "price_limit_pct": 0.10,
                "volume_unit_shares": 100,
            },
        }],
    }


def valid_completed_quote_payload() -> dict[str, object]:
    points = []
    for minute, price in (("09:30", 10.0), ("09:31", 10.01)):
        points.append({
            "timestamp": f"2026-08-28T{minute}:00+08:00",
            "price": price,
            "average_price": price,
            "open": price,
            "high": price,
            "low": price,
            "volume": 100.0,
            "amount": price * 100.0 * 100.0,
        })
    return {
        "collected_at": "2026-08-28T10:01:00+08:00",
        "source": {"name": "TEST"},
        "quotes": [{
            "symbol": "510300",
            "name": "沪深300ETF",
            "price": 10.01,
            "average_price": 10.01,
            "previous_close": 10.0,
            "timestamp": "2026-08-28T09:31:00+08:00",
            "observed_at": "2026-08-28T10:01:00+08:00",
            "source": "TEST",
            "points": points,
        }],
    }


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_runtime_fixture(self) -> tuple[MonitorApplication, SimpleNamespace]:
        root = Path(self.temporary.name)
        paths = SimpleNamespace(
            quotes=root / "quotes.json",
            watchlist=root / "watchlist.json",
            history=root / "quotes.jsonl",
            alerts=root / "alerts.jsonl",
            metadata=root / "etf_metadata.json",
            calendar=root / "market_calendar.json",
        )
        paths.watchlist.write_text(json.dumps({"watchlist": [{"symbol": "510300", "name": "ETF", "grid_width_pct": 0.002}]}), encoding="utf-8")
        paths.metadata.write_text(json.dumps(test_metadata_document()), encoding="utf-8")
        paths.calendar.write_text(json.dumps({"schema_version": 1, "closed_dates": []}), encoding="utf-8")
        paths.alerts.write_text("", encoding="utf-8")
        collector = StaticCollector(valid_completed_quote_payload())
        app = MonitorApplication(
            quotes_path=paths.quotes,
            watchlist_path=paths.watchlist,
            history_path=paths.history,
            collector=collector,
            refresh_interval=5.0,
            alert_history_path=paths.alerts,
            metadata_path=paths.metadata,
            valuation_path=None,
            calendar_path=paths.calendar,
            clock=lambda: datetime.fromisoformat("2026-08-28T10:02:00+08:00"),
        )
        return app, paths

    def test_snapshot_reads_published_state_without_writing_history_or_alerts(self) -> None:
        app, paths = self.make_runtime_fixture()
        self.assertTrue(app.refresh_once())
        history_before = paths.history.read_bytes()
        alerts_before = paths.alerts.read_bytes()
        first = app.snapshot()
        second = app.snapshot()
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(paths.history.read_bytes(), history_before)
        self.assertEqual(paths.alerts.read_bytes(), alerts_before)

    def test_failed_refresh_immediately_revokes_published_candidate(self) -> None:
        app, paths = self.make_runtime_fixture()
        app._published = {
            "revision": 1,
            "items": [{"symbol": "510300", "action": "BUY_CANDIDATE", "health_status": "REALTIME", "blocked_reasons": []}],
        }
        app.collector = FailingCollector("断流")
        self.assertFalse(app.refresh_once())
        item = app.snapshot()["items"][0]
        self.assertEqual(item["health_status"], "OUTAGE")
        self.assertNotIn(item["action"], {"BUY_CANDIDATE", "SELL_CANDIDATE"})
        self.assertIn("MARKET_NOT_REALTIME", item["blocked_reasons"])


class StaticCollector:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def collect_to_file(self, watchlist: object, path: Path) -> dict[str, object]:
        path.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")
        return self.payload


class FailingCollector:
    def __init__(self, message: str):
        self.message = message

    def collect_to_file(self, watchlist: object, path: Path) -> dict[str, object]:
        raise MarketDataError(self.message)
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_runtime_api.RuntimeTests -v
```

Expected: history/alerts change during `snapshot()` or revision/health fields are missing.

- [ ] **Step 3: Refactor MonitorApplication publishing**

Add fields `_published: Mapping[str, Any]`, `_revision: int`, and a condition protected by `refresh_lock`. Implement:

```python
def refresh_once(self) -> bool:
    try:
        watchlist = load_watchlist(self.watchlist_path)
        payload = self.collector.collect_to_file(watchlist, self.quotes_path)
        quotes = JsonQuoteAdapter().parse(payload)
        metadata = self.metadata_store.load()
        self.history_store.upsert(quotes, metadata)
        health = self.health_classifier.classify(self.clock(), max(item.timestamp for item in quotes.values()), None)
        published = snapshot_to_dict(self.engine.evaluate(watchlist, quotes, health=health))
        self.alert_store.append_candidates(published)
    except (ValueError, OSError) as error:
        self._publish_outage(str(error))
        return False
    self._publish(published, payload)
    return True


def snapshot(self) -> dict[str, Any]:
    with self.refresh_lock:
        return copy.deepcopy(dict(self._published))
```

Add `calendar_path` and injectable `clock` fields to `MonitorApplication`. In `__post_init__`, load closed dates with `load_closed_dates(calendar_path)` and construct `MarketHealthClassifier`; invalid calendar configuration must fail startup rather than silently treating holidays as outages. Update `create_server` and CLI construction using keyword arguments so the current metadata and valuation paths remain correctly associated.

`_publish` increments revision exactly once after history and alerts succeed. `_publish_outage` keeps last prices and metrics, changes health and actions to safe values, appends no candidate alert, increments revision, and stores the original error. If no collector exists, bootstrap a read-only published snapshot once during application initialization; do not persist from request methods.

- [ ] **Step 4: Run runtime and Web tests**

Run:

```powershell
python -m unittest tests.test_runtime_api.RuntimeTests tests.test_t_monitor.MonitorRefreshTests tests.test_t_monitor.MonitorWebTests -v
```

Expected: repeated GET-style snapshots do not change files; a failed refresh removes the candidate in the next revision.

- [ ] **Step 5: Commit**

```powershell
git add src/etf_rotation/t_web.py tests/test_runtime_api.py tests/test_t_monitor.py
git commit -m "refactor: publish monitor state from one producer"
```

### Task 8: Implement the T-account ledger and true backtest

**Files:**
- Create: `src/etf_rotation/t_backtest.py`
- Create: `tests/test_t_backtest.py`
- Modify: `src/etf_rotation/t_web.py` (`t_backtest`, `signal_replay`)

- [ ] **Step 1: Write T+1 ledger, paired-leg, volume, and benchmark tests**

```python
# tests/test_t_backtest.py
import unittest

from etf_rotation.t_backtest import FillBar, TAccount, TBacktester


class TAccountTests(unittest.TestCase):
    def test_low_buy_then_sell_uses_overnight_inventory(self) -> None:
        account = TAccount.create(base_shares=10_000, t_capacity_shares=2_000, first_price=10.0, lot_size=100, intraday_turnaround=False)
        account.execute("BUY_CANDIDATE", FillBar("2026-08-28T10:00:00+08:00", 9.8, 50_000), requested_shares=2_000)
        self.assertEqual(account.today_bought_shares, 2_000)
        self.assertEqual(account.overnight_sellable_shares, 10_000)
        account.execute("SELL_CANDIDATE", FillBar("2026-08-28T10:30:00+08:00", 10.1, 50_000), requested_shares=2_000)
        self.assertEqual(account.today_bought_shares, 2_000)
        self.assertEqual(account.overnight_sellable_shares, 8_000)
        self.assertEqual(len(account.completed_pairs), 1)
        self.assertEqual(account.total_shares, 10_000)

    def test_high_sell_then_buyback_restores_total_inventory(self) -> None:
        account = TAccount.create(base_shares=10_000, t_capacity_shares=2_000, first_price=10.0, lot_size=100, intraday_turnaround=False)
        account.execute("SELL_CANDIDATE", FillBar("2026-08-28T10:00:00+08:00", 10.2, 50_000), 2_000)
        account.execute("BUY_CANDIDATE", FillBar("2026-08-28T10:30:00+08:00", 9.9, 50_000), 2_000)
        self.assertEqual(account.total_shares, 10_000)
        self.assertEqual(len(account.completed_pairs), 1)
        self.assertGreater(account.completed_pairs[0].net_pnl, 0)

    def test_zero_volume_and_participation_limit_prevent_impossible_fills(self) -> None:
        account = TAccount.create(base_shares=10_000, t_capacity_shares=2_000, first_price=10.0, lot_size=100, intraday_turnaround=False)
        rejected = account.execute("SELL_CANDIDATE", FillBar("2026-08-28T10:00:00+08:00", 10.2, 0), 2_000)
        self.assertEqual(rejected.reason, "ZERO_VOLUME")
        limited = account.execute("SELL_CANDIDATE", FillBar("2026-08-28T10:01:00+08:00", 10.2, 20), 2_000)
        self.assertEqual(limited.shares, 200)

    def test_no_completed_pairs_never_claims_outperformance(self) -> None:
        result = TBacktester().summarize_no_trade(base_shares=1_000, reserve_cash=2_000, first_price=10.0, last_price=9.0)
        self.assertEqual(result.status, "NO_COMPLETED_PAIRS")
        self.assertEqual(result.t_net_gain_cny, 0.0)
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_t_backtest -v
```

Expected: import failure for `etf_rotation.t_backtest`.

- [ ] **Step 3: Implement explicit inventory and execution**

Define immutable `FillBar`, `Fill`, `TradePair`, `RejectedFill`, and `BacktestResult` records. `TAccount.execute` must:

```python
def executable_shares(self, bar: FillBar, requested_shares: int) -> int:
    if bar.volume_lots <= 0:
        return 0
    market_capacity = floor_to_lot(int(bar.volume_lots * self.volume_unit_shares * DEFAULT_VOLUME_PARTICIPATION), self.lot_size)
    return min(floor_to_lot(requested_shares, self.lot_size), market_capacity, self.remaining_t_capacity_shares)
```

For SELL, cap again by `overnight_sellable_shares + (today_bought_shares if intraday_turnaround else 0)`. For BUY, cap by available cash after slipped price and commission. Pair only equal economic quantities; never decrement T+1 `today_bought_shares` for a same-day sale. Use next completed bar in `TBacktester.run`.

Initialize base shares as `floor_to_lot(int(DEFAULT_BASE_NOTIONAL_CNY / first_price), lot_size)` and T capacity as 20% rounded down unless explicit watchlist values override them. Give both strategy and benchmark the same base shares and reserve cash. Mark open legs to market; compute sell-fly loss for an unclosed sell leg; return zero net gain for a true no-trade account.

Replace `MonitorApplication.backtest` internals with `t_backtest()`. Keep the prior signal walk only as `signal_replay()` and remove performance claims from it.

- [ ] **Step 4: Run ledger and Web integration tests**

Run:

```powershell
python -m unittest tests.test_t_backtest tests.test_t_monitor.MonitorWebTests.test_backtest_endpoint_returns_independent_summary_per_enabled_item -v
```

Expected: ledger tests pass; update the Web assertion to expect baseline equity, T net gain, completed/open legs, inventory, costs, and execution mode.

- [ ] **Step 5: Commit**

```powershell
git add src/etf_rotation/t_backtest.py src/etf_rotation/t_web.py tests/test_t_backtest.py tests/test_t_monitor.py
git commit -m "feat: add inventory-aware T backtest"
```

### Task 9: Add lightweight snapshot, incremental quote, backtest, and replay APIs

**Files:**
- Modify: `src/etf_rotation/t_web.py` (`MonitorRequestHandler`, event publishing)
- Modify: `tests/test_runtime_api.py`
- Modify: `tests/test_t_monitor.py` (`MonitorWebTests`)

- [ ] **Step 1: Write API payload and endpoint tests**

```python
def test_snapshot_is_lightweight_and_quotes_are_incremental(self) -> None:
    status, snapshot = self.get("/api/snapshot")
    self.assertEqual(status, 200)
    self.assertNotIn("points", snapshot["items"][0])
    revision = snapshot["revision"]
    status, quotes = self.get("/api/quotes?symbol=510300&since=0")
    self.assertEqual(status, 200)
    self.assertEqual(quotes["symbol"], "510300")
    self.assertEqual(quotes["revision"], revision)
    self.assertTrue(quotes["upserts"])


def test_true_backtest_replay_and_compatibility_alias_are_distinct(self) -> None:
    self.assertEqual(self.get("/api/t-backtest")[1]["mode"], "T_BACKTEST")
    self.assertEqual(self.get("/api/signal-replay")[1]["mode"], "SIGNAL_ROUGH_REPLAY")
    alias = self.get("/api/backtest")[1]
    self.assertEqual(alias["mode"], "T_BACKTEST")
    self.assertTrue(alias["deprecated_alias"])
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_runtime_api tests.test_t_monitor.MonitorWebTests -v
```

Expected: missing `/api/quotes`, `/api/t-backtest`, and `/api/signal-replay`; snapshot still contains points.

- [ ] **Step 3: Implement revisioned read-only endpoints**

Route:

```python
elif path == "/api/quotes":
    self._quotes()
elif path == "/api/t-backtest":
    self._json(HTTPStatus.OK, self.server.application.t_backtest())
elif path == "/api/signal-replay":
    self._json(HTTPStatus.OK, self.server.application.signal_replay())
elif path == "/api/backtest":
    payload = self.server.application.t_backtest()
    payload["deprecated_alias"] = True
    self._json(HTTPStatus.OK, payload)
```

Store a bounded deque of revision deltas. `/api/snapshot` returns summary items without `points`. `/api/quotes` validates a six-digit enabled symbol and nonnegative integer `since`; return all current-day points if the revision is older than the delta cache, otherwise return only appended or revised primary keys. SSE event IDs equal revision; honor `Last-Event-ID` and send only delta payloads after the initial summary.

- [ ] **Step 4: Run API tests and measure fixture payload**

Run:

```powershell
python -m unittest tests.test_runtime_api tests.test_t_monitor.MonitorWebTests -v
```

Expected: all pass. Add an assertion that serialized summary size is less than 10% of the equivalent full-points fixture.

- [ ] **Step 5: Commit**

```powershell
git add src/etf_rotation/t_web.py tests/test_runtime_api.py tests/test_t_monitor.py
git commit -m "feat: stream incremental monitor updates"
```

### Task 10: Rebuild the page around candidates, metrics, axes, and incremental data

**Files:**
- Create: `src/etf_rotation/t_page.py`
- Modify: `src/etf_rotation/t_web.py` (import `PAGE`)
- Modify: `tests/test_t_monitor.py` (page tests)

- [ ] **Step 1: Replace old golden-window assertions with required UI behavior**

```python
def test_page_uses_candidate_language_and_exposes_regime_evidence(self) -> None:
    self.assertNotIn("黄金窗口", PAGE)
    self.assertNotIn("回补提醒", PAGE)
    self.assertNotIn("减仓提醒", PAGE)
    self.assertIn("做T候选", PAGE)
    for field in ("path_efficiency", "one_side_ratio", "vwap_crossings", "vwap_slope", "range_confirmation_count", "blocked_reasons"):
        self.assertIn(field, PAGE)


def test_page_has_axes_thresholds_tooltip_and_incremental_fetch(self) -> None:
    self.assertIn('class="x-axis"', PAGE)
    self.assertIn('class="y-axis"', PAGE)
    self.assertIn('id="chart-tooltip"', PAGE)
    self.assertIn("three_grid", PAGE)
    self.assertIn("five_grid", PAGE)
    self.assertIn("/api/quotes?symbol=", PAGE)
    self.assertIn("三格 0.60%", PAGE)
    self.assertIn("五格 1.00%", PAGE)
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_t_monitor.MonitorWebTests.test_page_uses_candidate_language_and_exposes_regime_evidence tests.test_t_monitor.MonitorWebTests.test_page_has_axes_thresholds_tooltip_and_incremental_fetch -v
```

Expected: old language remains and required chart elements are missing.

- [ ] **Step 3: Move PAGE and implement incremental rendering**

Move the complete HTML string to `t_page.py`. Keep existing watchlist and valuation behavior. Replace the full-points SSE path with:

```javascript
async function loadQuoteUpdates(symbol, since) {
  const response = await fetch(`/api/quotes?symbol=${encodeURIComponent(symbol)}&since=${since}`, {cache: 'no-store'});
  if (!response.ok) throw new Error('分钟行情不可用');
  const payload = await response.json();
  const points = quotePoints.get(symbol) || new Map();
  for (const point of payload.upserts || []) points.set(point.timestamp, point);
  quotePoints.set(symbol, points);
  quoteRevisions.set(symbol, payload.revision);
  return [...points.values()].sort((left, right) => left.timestamp.localeCompare(right.timestamp));
}
```

Render SVG axes with five labeled y ticks and session-aware x ticks. Draw price, VWAP, previous close, three-grid, and five-grid lines. Add an overlay that finds the nearest point on pointer movement and updates `#chart-tooltip` with time, OHLC, price, VWAP, volume, deviation, and state reason.

Map `BUY_CANDIDATE`/`SELL_CANDIDATE` to the neutral heading “做T候选”; map all blocked deviations to “偏离观察”. Show health status and reason. Show ER, one-side ratio, crossings, slope, side counts, confirmation counts, gross edge, costs, net edge, and blocked reasons. Rename panels to “做T回测” and “信号粗回放”.

The add form must initialize and persist `DEFAULT_GRID_WIDTH_PCT` through the server and display calculated text `三格 0.60% · 五格 1.00%`.

- [ ] **Step 4: Run all page and HTTP tests**

Run:

```powershell
python -m unittest tests.test_t_monitor.MonitorWebTests tests.test_runtime_api -v
```

Expected: all pass; no old action wording exists in `PAGE`.

- [ ] **Step 5: Commit**

```powershell
git add src/etf_rotation/t_page.py src/etf_rotation/t_web.py tests/test_t_monitor.py tests/test_runtime_api.py
git commit -m "feat: explain T candidates in the monitor UI"
```

### Task 11: Migrate clean history and remove tracked runtime artifacts

**Files:**
- Create: `src/etf_rotation/history_migration.py`
- Modify: `src/etf_rotation/cli.py`
- Create: `.gitignore`
- Create: `docs/git-history-cleanup.md`
- Modify: `README.md`
- Modify: `scripts/start-monitor.ps1`
- Modify: `scripts/restart-monitor.ps1`
- Modify: `scripts/stop-monitor.ps1`
- Test: `tests/test_market_data.py`

- [ ] **Step 1: Write an all-or-nothing migration test**

```python
def test_closing_snapshot_rebuilds_clean_schema_v3_history(self) -> None:
    metadata = Path(__file__).resolve().parents[1] / "data" / "monitor" / "etf_metadata.json"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "closing-snapshot.json"
        output = root / "monitor" / "quotes.jsonl"
        source.write_text(json.dumps({
            "collected_at": "2026-08-28T17:56:09+08:00",
            "quotes": [{
                "symbol": "510300",
                "name": "沪深300ETF",
                "price": 4.685,
                "average_price": 4.6845,
                "previous_close": 4.691,
                "timestamp": "2026-08-28T09:31:00+08:00",
                "observed_at": "2026-08-28T17:56:09+08:00",
                "source": "TEST",
                "points": [
                    {"timestamp":"2026-08-28T09:30:00+08:00","price":4.684,"average_price":4.684,"open":4.684,"high":4.684,"low":4.684,"volume":100.0,"amount":46840.0},
                    {"timestamp":"2026-08-28T09:31:00+08:00","price":4.685,"average_price":4.6845,"open":4.685,"high":4.685,"low":4.685,"volume":100.0,"amount":46850.0}
                ]
            }]
        }), encoding="utf-8")
        count = rebuild_history(source, output, metadata)
        records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(count, 2)
        self.assertEqual(len({(item["symbol"], item["timestamp"]) for item in records}), 2)
        first = next(item for item in records if item["symbol"] == "510300" and item["timestamp"].startswith("2026-08-28T09:30"))
        self.assertEqual(first["price"], 4.684)
        self.assertEqual(first["previous_close"], 4.691)
        self.assertTrue(all(item["schema_version"] == 3 and item["observed_at"] and item["trading_date"] for item in records))


def test_failed_rebuild_does_not_replace_existing_output(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "bad.json"
        output = root / "monitor" / "quotes.jsonl"
        source.write_text('{"quotes":[{"symbol":"510300"}]}', encoding="utf-8")
        output.parent.mkdir()
        output.write_text("preserve\n", encoding="utf-8")
        with self.assertRaises(MarketDataError):
            metadata = Path(__file__).resolve().parents[1] / "data" / "monitor" / "etf_metadata.json"
            rebuild_history(source, output, metadata)
        self.assertEqual(output.read_text(encoding="utf-8"), "preserve\n")
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_market_data.MarketDataTests.test_closing_snapshot_rebuilds_clean_schema_v3_history tests.test_market_data.MarketDataTests.test_failed_rebuild_does_not_replace_existing_output -v
```

Expected: `rebuild_history` is missing.

- [ ] **Step 3: Implement and execute validated migration**

```python
def rebuild_history(source: Path, output: Path, metadata_path: Path) -> int:
    quotes = JsonQuoteAdapter().load(source)
    metadata = EtfMetadataStore(metadata_path).load()
    missing = sorted(set(quotes) - set(metadata))
    if missing:
        raise MarketDataError("缺少交易元数据: " + ",".join(missing))
    destination_root = output.parent
    destination_root.parent.mkdir(parents=True, exist_ok=True)
    staged_root = Path(tempfile.mkdtemp(prefix=".monitor-rebuild-", dir=destination_root.parent))
    backup_root = destination_root.parent / f".monitor-backup-{uuid.uuid4().hex}"
    staged_output = staged_root / output.name
    replaced_old = False
    try:
        store = MinuteHistoryStore(staged_output)
        count = store.upsert(quotes, metadata)
        audit_history(staged_output, expected_symbols=set(quotes))
        if destination_root.exists():
            os.replace(destination_root, backup_root)
            replaced_old = True
        try:
            os.replace(staged_root, destination_root)
        except OSError:
            if replaced_old:
                os.replace(backup_root, destination_root)
            raise
        if replaced_old:
            shutil.rmtree(backup_root)
        return count
    finally:
        if staged_root.exists():
            shutil.rmtree(staged_root)
```

Add CLI command:

```powershell
python -m etf_rotation.cli rebuild-history --input data/monitor/quotes.json --output var/monitor/quotes.jsonl --metadata data/monitor/etf_metadata.json
```

The monitor command also accepts `--calendar`, defaulting to `data/monitor/market_calendar.json`, and passes it to `create_server` by keyword.

Run it before removing the tracked source. Expected: `1446 finalized minutes rebuilt` and `var/monitor/history/2026-08-28/quotes.jsonl` exists. The directory swap preserves the previous runtime root as a recoverable backup until the validated staged root is in place.

- [ ] **Step 4: Move runtime defaults, ignore artifacts, and remove tracked runtime files**

Use `PROJECT_ROOT / "var" / "monitor"` for live quote, history, alert, PID, and log defaults. Keep watchlist, metadata, and valuation under `data/monitor`.

Add:

```gitignore
__pycache__/
*.py[cod]
.coverage
.pytest_cache/
.mypy_cache/
var/
*.log
*.pid
*.tmp
.env
.venv/
.idea/
.vscode/
```

Remove the tracked runtime data and bytecode listed in the file map. Do not remove the newly rebuilt ignored `var/monitor` files. Update PowerShell scripts to derive `$projectRoot = Split-Path -Parent $PSScriptRoot`; no script or README command may contain a machine-specific project-root path.

Document the schema, health states, candidate gate, APIs, true backtest, rough replay, migration, and relative launch commands in README. In `docs/git-history-cleanup.md`, require a remote backup and explicit authorization before `git filter-repo`, explain rewritten commit IDs, and show verification without executing history rewriting.

- [ ] **Step 5: Run migration audit, repository scans, and full tests**

Run:

```powershell
$records = Get-Content -LiteralPath 'var\monitor\quotes.jsonl' | ForEach-Object { $_ | ConvertFrom-Json }
[pscustomobject]@{Records=$records.Count; Keys=(($records | Group-Object symbol,timestamp).Count); Schema3=($records | Where-Object schema_version -eq 3).Count} | Format-List
rg -n "<project-root>|黄金窗口|回补提醒|减仓提醒" README.md scripts src tests
git ls-files | rg "(__pycache__|\.pyc$|data/monitor/(quotes|alerts|history))"
& '.\scripts\run-tests.ps1'
```

Expected: migration reports 1446 records; both `rg` repository-hygiene scans return no matches; all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add .gitignore README.md docs/git-history-cleanup.md src/etf_rotation/history_migration.py src/etf_rotation/cli.py scripts data/monitor src tests
git commit -m "chore: rebuild clean runtime history"
```

Before committing, use `git diff --cached --name-status` to confirm valuation and ETF-index work is included only where intentionally integrated, and unrelated user changes are not staged.

### Task 12: Final verification and local UI smoke test

**Files:**
- Modify only if verification exposes a defect; every defect must start with a failing regression test in the owning test file.

- [ ] **Step 1: Run the complete automated suite with the available Python runtime**

Run:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest discover -s tests -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 2: Audit rebuilt history**

Run:

```powershell
$records = Get-Content -LiteralPath 'var\monitor\quotes.jsonl' | ForEach-Object { $_ | ConvertFrom-Json }
$duplicates = $records | Group-Object symbol,timestamp | Where-Object Count -gt 1
$badSchema = $records | Where-Object { $_.schema_version -ne 3 -or -not $_.trading_date -or -not $_.observed_at -or -not $_.is_complete }
$bad510300 = $records | Where-Object { $_.symbol -eq '510300' -and ($_.previous_close -eq 4.095 -or ($_.timestamp -like '*T09:30:*' -and $_.price -ne 4.684)) }
[pscustomobject]@{Records=$records.Count; DuplicateGroups=$duplicates.Count; BadSchema=$badSchema.Count; Bad510300=$bad510300.Count} | Format-List
```

Expected: `Records=1446`, `DuplicateGroups=0`, `BadSchema=0`, `Bad510300=0`.

- [ ] **Step 3: Start the server and verify read-only endpoints**

Run:

```powershell
& '.\scripts\start-monitor.ps1'
```

Then request `/api/snapshot`, `/api/quotes?symbol=510300&since=0`, `/api/t-backtest`, `/api/signal-replay`, and `/api/etf/510300/valuation`. Expected: HTTP 200; summary has no `points`; quote response has schema v3 upserts; T backtest includes inventory/baseline fields; signal replay has no return claim; valuation remains read-only.

- [ ] **Step 4: Smoke-test the page in the in-app browser**

Open `http://127.0.0.1:8765/` and verify:

- six ETF navigation entries render;
- health says `CLOSED` after market close and no candidate remains visible;
- chart shows axes, tooltip, price, VWAP, previous close, three-grid, and five-grid lines;
- state evidence and blocked reasons render;
- add-form text says `三格 0.60% · 五格 1.00%`;
- panels read “做T回测” and “信号粗回放”;
- switching symbols fetches only that symbol's minute data.

- [ ] **Step 5: Inspect Git scope**

Run:

```powershell
git status --short
git diff --check
git log --oneline --decorate -12
```

Expected: no whitespace errors; only intentional implementation changes remain; original unrelated user modifications have not been discarded.

- [ ] **Step 6: Request code review and address only verified findings**

Use the `requesting-code-review` skill. For each actionable finding, reproduce it with a failing test, make the smallest fix, rerun the focused test, then rerun the complete suite. Finish with the `verification-before-completion` skill before reporting success.
