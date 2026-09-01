# Market-Aware Quote Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure the live monitor exposes only the current Shanghai calendar day's finalized minutes, collects only when a trading-session policy requires it, preserves lunch/closed semantics, backs off active-session failures, and treats normal SSE disconnects quietly.

**Architecture:** Add a pure market-session classifier to `market_data.py`, then make the single producer in `t_web.py` consult that classifier before collection and when publishing failures. Filter quotes before engine evaluation and filter revision payloads against `generated_at`, while leaving schema v3 history untouched. Keep the current Eastmoney transport and standard-library HTTP server; change only scheduling, live-data boundaries, failure semantics, and expected disconnect handling.

**Tech Stack:** Python 3.12+ standard library, `dataclasses`, `datetime`/`zoneinfo`, `threading.Event`, `unittest`, vanilla JavaScript, PowerShell launch scripts.

---

## File map

**Modify**

- `src/etf_rotation/market_data.py` — pure Shanghai market-session classification.
- `src/etf_rotation/quote_collector.py` — Task 6 approved same-interface fallback semantics and actual-host provenance (already covered by focused tests).
- `src/etf_rotation/t_monitor.py` — missing-current-day items inherit the actual market phase instead of forcing `OUTAGE`.
- `src/etf_rotation/t_web.py` — current-day filtering, session-aware producer scheduling, bounded failure backoff, market-aware failure publication, SSE disconnect handling.
- `src/etf_rotation/t_page.py` — show “暂无当日行情” and preserve `CLOSED`/`LUNCH_BREAK` presentation for missing current-day data.
- `src/etf_rotation/constants.py` — one 60-second minute-feed refresh default and a five-minute backoff ceiling.
- `src/etf_rotation/cli.py` — consume the shared refresh default.
- `scripts/start-monitor.ps1` — use the 60-second production refresh interval.
- `README.md` — document current-day isolation, session scheduling, network requirements, and failure behavior.
- `tests/test_market_data.py` — session-boundary classification tests.
- `tests/test_runtime_api.py` — old-day isolation, catch-up, pause, failure, backoff, reset, and SSE regression tests.
- `tests/test_t_monitor.py` — page wording/presentation and missing-quote market-state tests.
- `tests/test_defaults.py` — shared refresh default test.
- `tests/test_run_tests_script.py` — production launch interval assertion.

**Do not modify, except for the Task 6 approved correction below**

- `src/etf_rotation/quote_collector.py` field mapping and validation remain strict; only the documented same-interface host fallback/provenance correction is permitted.
- Strategy, regime, T-account, backtest, valuation, ETF metadata, or schema v3 persistence semantics.
- Existing historical JSONL records.

### Task 1: Add a pure Shanghai market-session policy

**Files:**
- Modify: `src/etf_rotation/market_data.py`
- Modify: `tests/test_market_data.py`

- [ ] **Step 1: Write failing session-policy tests**

Add the import and test class below to `tests/test_market_data.py`:

```python
from etf_rotation.market_data import market_session_state


class MarketSessionStateTests(unittest.TestCase):
    def test_trading_day_boundaries_control_collection(self) -> None:
        cases = (
            ("2026-08-28T09:29:59+08:00", "PRE_OPEN", "CLOSED", False, False),
            ("2026-08-28T09:30:00+08:00", "MORNING", "REALTIME", True, False),
            ("2026-08-28T11:30:00+08:00", "MORNING", "REALTIME", True, False),
            ("2026-08-28T11:30:01+08:00", "LUNCH_BREAK", "LUNCH_BREAK", False, True),
            ("2026-08-28T13:00:00+08:00", "AFTERNOON", "REALTIME", True, False),
            ("2026-08-28T15:00:00+08:00", "AFTERNOON", "REALTIME", True, False),
            ("2026-08-28T15:00:01+08:00", "CLOSED", "CLOSED", False, True),
        )
        for value, phase, health, active, catch_up in cases:
            with self.subTest(value=value):
                state = market_session_state(datetime.fromisoformat(value), set())
                self.assertEqual(state.phase, phase)
                self.assertEqual(state.health_status, health)
                self.assertEqual(state.active, active)
                self.assertEqual(state.catch_up_allowed, catch_up)

    def test_weekend_and_calendar_closure_never_collect(self) -> None:
        weekend = market_session_state(
            datetime.fromisoformat("2026-08-29T10:00:00+08:00"), set(),
        )
        holiday = market_session_state(
            datetime.fromisoformat("2026-10-01T10:00:00+08:00"),
            {date(2026, 10, 1)},
        )
        for state in (weekend, holiday):
            self.assertEqual(state.phase, "CLOSED")
            self.assertEqual(state.health_status, "CLOSED")
            self.assertFalse(state.active)
            self.assertFalse(state.catch_up_allowed)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
$env:PYTHONPATH = 'src'
& 'C:\Users\linyongjie\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_market_data.MarketSessionStateTests -v
```

Expected: FAIL because `market_session_state` does not exist.

- [ ] **Step 3: Implement the immutable session state and classifier**

Add this block next to `MarketHealth` in `src/etf_rotation/market_data.py`:

```python
@dataclass(frozen=True)
class MarketSessionState:
    phase: str
    health_status: str
    active: bool
    catch_up_allowed: bool


def market_session_state(
    now: datetime,
    closed_dates: set[date] | frozenset[date] | None = None,
) -> MarketSessionState:
    local = _aware_time(now, "当前时间").astimezone(SHANGHAI)
    local_time = local.time().replace(tzinfo=None)
    closures = frozenset(closed_dates or ())
    if local.weekday() >= 5 or local.date() in closures:
        return MarketSessionState("CLOSED", "CLOSED", False, False)
    if local_time < _MORNING_START:
        return MarketSessionState("PRE_OPEN", "CLOSED", False, False)
    if local_time <= _MORNING_END:
        return MarketSessionState("MORNING", "REALTIME", True, False)
    if local_time < _AFTERNOON_START:
        return MarketSessionState("LUNCH_BREAK", "LUNCH_BREAK", False, True)
    if local_time <= _AFTERNOON_END:
        return MarketSessionState("AFTERNOON", "REALTIME", True, False)
    return MarketSessionState("CLOSED", "CLOSED", False, True)
```

Refactor `MarketHealthClassifier.classify` to call `market_session_state` first and return `CLOSED` or `LUNCH_BREAK` before examining errors. Keep its `REALTIME`/`DELAYED`/`OUTAGE` age thresholds unchanged.

Use this complete method body:

```python
    def classify(
        self,
        now: datetime,
        last_quote_at: datetime | None,
        error: str | None,
    ) -> MarketHealth:
        local = _aware_time(now, "当前时间").astimezone(SHANGHAI)
        session = market_session_state(local, self.closed_dates)
        if session.health_status == "CLOSED":
            return MarketHealth("CLOSED", None, "非连续交易时段")
        if session.health_status == "LUNCH_BREAK":
            return MarketHealth("LUNCH_BREAK", None, "午间休市")
        if error is not None:
            return MarketHealth("OUTAGE", None, error or "行情采集失败")
        if last_quote_at is None:
            return MarketHealth("OUTAGE", None, "缺少当日行情")
        quote_time = _aware_time(last_quote_at, "行情时间").astimezone(SHANGHAI)
        age = max(0.0, (local - quote_time).total_seconds())
        if age <= REALTIME_MAX_AGE_SECONDS:
            return MarketHealth("REALTIME", age, "行情实时")
        if age <= DELAYED_MAX_AGE_SECONDS:
            return MarketHealth("DELAYED", age, "行情延迟")
        return MarketHealth("OUTAGE", age, "行情断流")
```

- [ ] **Step 4: Run session and health tests and verify GREEN**

Run:

```powershell
$env:PYTHONPATH = 'src'
& 'C:\Users\linyongjie\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_market_data.MarketSessionStateTests tests.test_market_data.MarketHealthTests -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the policy**

```powershell
git add -- src/etf_rotation/market_data.py tests/test_market_data.py
git commit -m "feat: classify market collection sessions"
```

### Task 2: Isolate the live view to the current Shanghai date

**Files:**
- Modify: `src/etf_rotation/t_monitor.py`
- Modify: `src/etf_rotation/t_web.py`
- Modify: `src/etf_rotation/t_page.py`
- Modify: `tests/test_runtime_api.py`
- Modify: `tests/test_t_monitor.py`

- [ ] **Step 1: Write failing runtime tests for old-day isolation**

Add this reusable date-shift helper above `RuntimeTests` in `tests/test_runtime_api.py`:

```python
def quote_payload_for_date(value: str) -> dict[str, object]:
    payload = copy.deepcopy(valid_completed_quote_payload())
    target = datetime.fromisoformat(value).date()
    quote = payload["quotes"][0]
    source_date = datetime.fromisoformat(quote["timestamp"]).date()
    shift = timedelta(days=(target - source_date).days)
    for field in ("timestamp", "observed_at"):
        quote[field] = (datetime.fromisoformat(quote[field]) + shift).isoformat()
    for point in quote["points"]:
        point["timestamp"] = (
            datetime.fromisoformat(point["timestamp"]) + shift
        ).isoformat()
    payload["collected_at"] = quote["observed_at"]
    return payload
```

Add these tests to `RuntimeTests`:

```python
    def test_bootstrap_never_exposes_previous_day_as_live_data(self) -> None:
        self.paths.quotes.write_text(
            json.dumps(valid_completed_quote_payload(), ensure_ascii=False),
            encoding="utf-8",
        )
        app = self.make_runtime_fixture(collector=None)
        app.clock = lambda: datetime.fromisoformat("2026-08-31T10:02:00+08:00")
        app._bootstrap(increment_revision=True)

        item = app.snapshot()["items"][0]
        self.assertEqual(item["status"], "MISSING_QUOTE")
        self.assertIsNone(item["price"])
        self.assertIsNone(item["timestamp"])
        quotes = app.quotes("510300", since=0)
        self.assertTrue(quotes["reset"])
        self.assertEqual(quotes["upserts"], [])

    def test_quote_cursor_uses_generated_date_not_latest_point_date(self) -> None:
        app = self.make_runtime_fixture()
        self.assertTrue(app.refresh_once())
        app.clock = lambda: datetime.fromisoformat("2026-08-31T10:02:00+08:00")
        app._bootstrap(increment_revision=True)

        result = app.quotes("510300", since=0)
        self.assertEqual(result["upserts"], [])
        self.assertTrue(result["reset"])
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
$env:PYTHONPATH = 'src'
& 'C:\Users\linyongjie\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_runtime_api.RuntimeTests.test_bootstrap_never_exposes_previous_day_as_live_data tests.test_runtime_api.RuntimeTests.test_quote_cursor_uses_generated_date_not_latest_point_date -v
```

Expected: FAIL because the old 2026-08-28 quote and its points remain published.

- [ ] **Step 3: Filter `Quote` objects before engine evaluation**

Import `date` from `datetime` and import `Quote` from `t_monitor` in `src/etf_rotation/t_web.py`, then add these methods to `MonitorApplication`:

```python
    @staticmethod
    def _quote_for_date(quote: Quote, trading_date: date) -> Quote | None:
        points = tuple(
            point for point in quote.points
            if point.timestamp.astimezone(SHANGHAI).date() == trading_date
        )
        if not points:
            return None
        latest = points[-1]
        return Quote(
            symbol=quote.symbol,
            name=quote.name,
            price=latest.price,
            average_price=latest.average_price,
            previous_close=quote.previous_close,
            timestamp=latest.timestamp,
            points=points,
            observed_at=quote.observed_at,
            source=quote.source,
        )

    @classmethod
    def _quotes_for_now(
        cls, quotes: Mapping[str, Quote], now: datetime,
    ) -> dict[str, Quote]:
        trading_date = now.astimezone(SHANGHAI).date()
        result: dict[str, Quote] = {}
        for symbol, quote in quotes.items():
            current = cls._quote_for_date(quote, trading_date)
            if current is not None:
                result[symbol] = current
        return result
```

In `_bootstrap`, replace the parse/evaluate sequence with this ordering:

```python
            raw = json.loads(self.quotes_path.read_text(encoding="utf-8"))
            all_quotes = JsonQuoteAdapter().parse(raw)
            payload = raw if isinstance(raw, dict) else {}
        except (ValueError, OSError) as failure:
            all_quotes = {}
            payload = {}
            error = str(failure)
        if error is None and self.history_store is not None:
            try:
                self._validate_quotes(all_quotes, self.metadata_store.load())
            except (ValueError, OSError) as failure:
                error = str(failure)
        now = self.clock()
        quotes = self._quotes_for_now(all_quotes, now)
```

In the successful refresh path, retain `all_quotes = JsonQuoteAdapter().parse(payload)`, validate and persist `all_quotes`, then use:

```python
                        now = self.clock()
                        quotes = self._quotes_for_now(all_quotes, now)
                        health = self._health_by_symbol(quotes, now)
                        published = snapshot_to_dict(self.engine.evaluate(
                            watchlist, quotes, generated_at=now, health=health,
                        ))
```

- [ ] **Step 4: Make quote deltas use `generated_at` as the authoritative date**

Replace `_current_day_points` in `src/etf_rotation/t_web.py` with:

```python
    @classmethod
    def _current_day_points(
        cls, published: Mapping[str, Any], symbol: str,
    ) -> list[dict[str, Any]]:
        generated = datetime.fromisoformat(str(published.get("generated_at", "")))
        if generated.tzinfo is None or generated.utcoffset() is None:
            return []
        current_date = generated.astimezone(SHANGHAI).date().isoformat()
        for item in published.get("items", []):
            if item.get("symbol") != symbol:
                continue
            points = [
                cls._normalize_point(point)
                for point in item.get("points") or []
                if str(point.get("trading_date") or "") == current_date
            ]
            points.sort(key=lambda point: point["timestamp"])
            return points
        return []
```

- [ ] **Step 5: Preserve closed/lunch health for a missing current-day quote**

In the `quote is None` branch of `TMonitorEngine.evaluate`, derive health before creating the signal:

```python
            if quote is None:
                item_health = self.health_classifier.classify(current, None, None)
                signals.append(MonitorSignal(
                    item.symbol, item.name, "MISSING_QUOTE", "UNAVAILABLE",
                    "缺少当日行情", None, None, None, None, None,
                    item.grid_width_pct, None, None, None, None, None,
                    health_status=item_health.status,
                    health_reason=item_health.reason,
                    blocked_reasons=("MISSING_QUOTE",),
                ))
                continue
```

This keeps active-session missing data unsafe while allowing `CLOSED` and `LUNCH_BREAK` to remain truthful.

- [ ] **Step 6: Add the explicit empty-current-day page state**

Change `marketPresentation` in `src/etf_rotation/t_page.py` so missing data only overrides an active/unknown health state:

```javascript
function marketPresentation(item,nowMs=Date.now(),feed={ready:true,message:''}){
  const health=item.health_status||'UNKNOWN',missing=item.status==='MISSING_QUOTE'||health==='MISSING',missingOverrides=!['CLOSED','LUNCH_BREAK'].includes(health),healthKey=missing&&missingOverrides?'MISSING':health;
  const marketAt=item.timestamp?new Date(item.timestamp):null,marketMs=marketAt&&marketAt.getTime(),age=Number.isFinite(marketMs)?nowMs-marketMs:Infinity;
  const realtime=healthKey==='REALTIME'&&item.status==='OK',stale=realtime&&(age<0||age>STALE_AFTER_MS),candidateAction=item.action==='BUY_CANDIDATE'||item.action==='SELL_CANDIDATE',candidate=Boolean(feed.ready)&&realtime&&!stale&&candidateAction;
  const states={REALTIME:{statusText:'实时监控中',dotClass:'live'},DELAYED:{statusText:'行情延迟',dotClass:'delayed'},OUTAGE:{statusText:'行情断流',dotClass:'outage'},LUNCH_BREAK:{statusText:'午间休市',dotClass:'paused'},CLOSED:{statusText:'已收盘',dotClass:'closed'},MISSING:{statusText:'行情缺失',dotClass:'missing'},UNKNOWN:{statusText:'行情状态未知',dotClass:'unknown'}};
  const state=!feed.ready?{statusText:feed.message||'行情连接中断',dotClass:'outage'}:stale?{statusText:'当前行情已过期',dotClass:'stale'}:(states[healthKey]||states.UNKNOWN);
  const unsafe=!feed.ready||!realtime||stale;
  return {...state,health,healthKey,realtime,stale,candidateAction,candidate,displayLabel:candidate?'做T候选':candidateAction||item.action==='DEVIATION_OBSERVE'?'偏离观察':item.label,marketAt,unsafe,warning:unsafe?state.statusText+'，候选提醒已撤销':''};
}
```

Replace the missing-data card text with:

```javascript
missing?'<div class="empty">暂无当日行情，历史数据请从日期选择器查看</div>':chart(item)
```

Add assertions in `tests/test_t_monitor.py` that `PAGE` contains this exact message and that a `MISSING_QUOTE` item with `health_status:'CLOSED'` yields `statusText:'已收盘'`, `candidate:false`.

- [ ] **Step 7: Run focused tests and verify GREEN**

Run:

```powershell
$env:PYTHONPATH = 'src'
& 'C:\Users\linyongjie\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_runtime_api.RuntimeTests.test_bootstrap_never_exposes_previous_day_as_live_data tests.test_runtime_api.RuntimeTests.test_quote_cursor_uses_generated_date_not_latest_point_date tests.test_t_monitor.MonitorWebTests.test_page_market_state_hard_gates_candidates_and_connection_status -v
```

Expected: all focused tests PASS.

- [ ] **Step 8: Commit live-data isolation**

```powershell
git add -- src/etf_rotation/t_monitor.py src/etf_rotation/t_web.py src/etf_rotation/t_page.py tests/test_runtime_api.py tests/test_t_monitor.py
git commit -m "fix: isolate live quotes to current trading date"
```

### Task 3: Make the single producer session-aware with bounded backoff

**Files:**
- Modify: `src/etf_rotation/constants.py`
- Modify: `src/etf_rotation/t_web.py`
- Modify: `tests/test_runtime_api.py`
- Modify: `tests/test_defaults.py`

- [ ] **Step 1: Write failing producer-policy tests**

Add these tests to `RuntimeTests`:

```python
    def test_closed_session_with_current_data_does_not_collect(self) -> None:
        self.paths.quotes.write_text(
            json.dumps(valid_completed_quote_payload(), ensure_ascii=False),
            encoding="utf-8",
        )
        collector = StaticCollector(valid_completed_quote_payload())
        app = self.make_runtime_fixture(collector)
        app.clock = lambda: datetime.fromisoformat("2026-08-28T15:10:00+08:00")
        self.assertFalse(app.collection_due())
        self.assertEqual(collector.calls, 0)

    def test_closed_session_without_current_data_allows_one_catch_up(self) -> None:
        current = quote_payload_for_date("2026-08-31")
        collector = StaticCollector(current)
        app = self.make_runtime_fixture(collector)
        app.clock = lambda: datetime.fromisoformat("2026-08-31T15:10:00+08:00")
        self.assertTrue(app.collection_due())
        self.assertTrue(app.refresh_once())
        self.assertFalse(app.collection_due())
        self.assertEqual(collector.calls, 1)

    def test_weekend_never_collects_even_without_current_data(self) -> None:
        app = self.make_runtime_fixture()
        app.clock = lambda: datetime.fromisoformat("2026-08-29T10:00:00+08:00")
        self.assertFalse(app.collection_due())

    def test_active_failure_is_outage_but_closed_failure_stays_closed(self) -> None:
        app = self.make_runtime_fixture()
        self.assertTrue(app.refresh_once())
        app.collector = FailingCollector("断流")
        app.clock = lambda: datetime.fromisoformat("2026-08-28T10:03:00+08:00")
        self.assertFalse(app.refresh_once())
        self.assertEqual(app.snapshot()["items"][0]["health_status"], "OUTAGE")
        app.clock = lambda: datetime.fromisoformat("2026-08-28T15:10:00+08:00")
        self.assertFalse(app.refresh_once())
        closed = app.snapshot()
        self.assertEqual(closed["items"][0]["health_status"], "CLOSED")
        self.assertEqual(closed["errors"], [])

    def test_failure_backoff_is_bounded_and_resets_after_success(self) -> None:
        app = self.make_runtime_fixture()
        app.refresh_interval = 60.0
        self.assertEqual(
            [app.refresh_delay(count) for count in range(1, 6)],
            [60.0, 120.0, 240.0, 300.0, 300.0],
        )
```

Add to `tests/test_defaults.py`:

```python
    def test_minute_feed_defaults_to_sixty_seconds(self) -> None:
        from etf_rotation.constants import DEFAULT_REFRESH_INTERVAL_SECONDS
        self.assertEqual(DEFAULT_REFRESH_INTERVAL_SECONDS, 60.0)
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
$env:PYTHONPATH = 'src'
& 'C:\Users\linyongjie\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_runtime_api.RuntimeTests.test_closed_session_with_current_data_does_not_collect tests.test_runtime_api.RuntimeTests.test_closed_session_without_current_data_allows_one_catch_up tests.test_runtime_api.RuntimeTests.test_weekend_never_collects_even_without_current_data tests.test_runtime_api.RuntimeTests.test_active_failure_is_outage_but_closed_failure_stays_closed tests.test_runtime_api.RuntimeTests.test_failure_backoff_is_bounded_and_resets_after_success tests.test_defaults -v
```

Expected: FAIL because the scheduling API and shared defaults do not exist, and closed failures publish `OUTAGE`.

- [ ] **Step 3: Add shared refresh and backoff constants**

Add to `src/etf_rotation/constants.py`:

```python
DEFAULT_REFRESH_INTERVAL_SECONDS = 60.0
MAX_REFRESH_BACKOFF_SECONDS = 300.0
```

Use `constants.DEFAULT_REFRESH_INTERVAL_SECONDS` as the `MonitorApplication.refresh_interval` and `create_server` default.

- [ ] **Step 4: Implement collection eligibility and delay**

Import `market_session_state` and add these methods to `MonitorApplication`:

```python
    def _has_complete_current_day(self, now: datetime) -> bool:
        current_date = now.astimezone(SHANGHAI).date().isoformat()
        enabled = {
            item.symbol for item in load_watchlist(self.watchlist_path) if item.enabled
        }
        items = {
            str(item.get("symbol")): item
            for item in self.snapshot().get("items", [])
        }
        return bool(enabled) and all(
            symbol in items
            and items[symbol].get("status") == "OK"
            and str(items[symbol].get("timestamp") or "").startswith(current_date)
            for symbol in enabled
        )

    def collection_due(self) -> bool:
        now = self.clock()
        state = market_session_state(now, self.health_classifier.closed_dates)
        if state.active:
            return True
        return state.catch_up_allowed and not self._has_complete_current_day(now)

    def refresh_delay(self, failure_count: int) -> float:
        exponent = max(0, int(failure_count) - 1)
        return min(
            self.refresh_interval * (2 ** exponent),
            constants.MAX_REFRESH_BACKOFF_SECONDS,
        )
```

- [ ] **Step 5: Make failure publication respect the completion-time session**

Add this method:

```python
    def _publish_collection_failure(self, message: str) -> None:
        now = self.clock()
        state = market_session_state(now, self.health_classifier.closed_dates)
        if state.active:
            self._publish_outage(message)
            return
        self._bootstrap(increment_revision=True)
```

Replace all collector/validation failure calls to `_publish_outage(str(error))` in `_refresh_once` with `_publish_collection_failure(str(error))`. Keep primary commit and derived persistence failures on their existing paths because those are local integrity failures, not harmless closed-session source failures.

- [ ] **Step 6: Apply eligibility and backoff in the single loop**

Replace `_refresh_loop` with:

```python
    def _refresh_loop(self, generation: int) -> None:
        thread = threading.current_thread()
        failure_count = 0
        try:
            while True:
                with self.lifecycle_gate:
                    if self._generation_cancelled(generation):
                        break
                attempted = self.collection_due()
                if attempted:
                    success = self._refresh_once(generation)
                    failure_count = 0 if success else failure_count + 1
                else:
                    success = True
                    failure_count = 0
                delay = self.refresh_delay(failure_count if attempted else 0)
                if self._stop_event.wait(delay):
                    break
        finally:
            with self.lifecycle_gate:
                if self._refresh_thread is thread:
                    self._refresh_thread = None
```

The existing generation checks and producer lock remain authoritative; no second producer or timer is introduced.

- [ ] **Step 7: Run focused and lifecycle tests and verify GREEN**

Run:

```powershell
$env:PYTHONPATH = 'src'
& 'C:\Users\linyongjie\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_runtime_api.RuntimeTests.test_closed_session_with_current_data_does_not_collect tests.test_runtime_api.RuntimeTests.test_closed_session_without_current_data_allows_one_catch_up tests.test_runtime_api.RuntimeTests.test_weekend_never_collects_even_without_current_data tests.test_runtime_api.RuntimeTests.test_active_failure_is_outage_but_closed_failure_stays_closed tests.test_runtime_api.RuntimeTests.test_failure_backoff_is_bounded_and_resets_after_success tests.test_runtime_api.RuntimeTests.test_start_refresh_is_idempotent tests.test_runtime_api.RuntimeTests.test_stop_cancels_blocked_generation_without_any_commit_side_effect tests.test_defaults -v
```

Expected: all tests PASS and the stop test completes without waiting for the full backoff.

- [ ] **Step 8: Commit producer scheduling**

```powershell
git add -- src/etf_rotation/constants.py src/etf_rotation/t_web.py tests/test_runtime_api.py tests/test_defaults.py
git commit -m "fix: schedule quote collection by market session"
```

### Task 4: Quiet expected SSE disconnects without hiding real errors

**Files:**
- Modify: `src/etf_rotation/t_web.py`
- Modify: `tests/test_runtime_api.py`

- [ ] **Step 1: Write the failing Windows abort regression test**

Add this stream helper and test to `tests/test_runtime_api.py`:

```python
class AbortedStream:
    def write(self, value: bytes) -> int:
        raise ConnectionAbortedError(10053, "client closed")

    def flush(self) -> None:
        raise AssertionError("flush must not run after aborted write")


class ExplodingStream:
    def write(self, value: bytes) -> int:
        raise RuntimeError("server defect")

    def flush(self) -> None:
        return None
```

```python
    def test_sse_treats_windows_client_abort_as_normal_disconnect(self) -> None:
        handler = object.__new__(MonitorRequestHandler)
        handler.server = SimpleNamespace(application=ScriptedEventApplication())
        handler.headers = {}
        handler.wfile = AbortedStream()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler._events()

    def test_sse_does_not_hide_unexpected_write_errors(self) -> None:
        handler = object.__new__(MonitorRequestHandler)
        handler.server = SimpleNamespace(application=ScriptedEventApplication())
        handler.headers = {}
        handler.wfile = ExplodingStream()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        with self.assertRaisesRegex(RuntimeError, "server defect"):
            handler._events()
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
$env:PYTHONPATH = 'src'
& 'C:\Users\linyongjie\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_runtime_api.RuntimeTests.test_sse_treats_windows_client_abort_as_normal_disconnect tests.test_runtime_api.RuntimeTests.test_sse_does_not_hide_unexpected_write_errors -v
```

Expected: the Windows-abort test errors with `ConnectionAbortedError`; the unexpected-error test already passes.

- [ ] **Step 3: Add only the expected Windows exception**

Change the `_events` exception tuple in `src/etf_rotation/t_web.py` to:

```python
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
```

- [ ] **Step 4: Run SSE tests and verify GREEN**

Run:

```powershell
$env:PYTHONPATH = 'src'
& 'C:\Users\linyongjie\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_runtime_api.RuntimeTests.test_sse_treats_windows_client_abort_as_normal_disconnect tests.test_runtime_api.RuntimeTests.test_sse_does_not_hide_unexpected_write_errors tests.test_runtime_api.RuntimeTests.test_sse_uses_revision_ids_cursor_and_heartbeat_without_sleeping tests.test_runtime_api.RuntimeTests.test_sse_restart_cursor_ahead_of_current_gets_full_snapshot -v
```

Expected: all four tests PASS.

- [ ] **Step 5: Commit SSE handling**

```powershell
git add -- src/etf_rotation/t_web.py tests/test_runtime_api.py
git commit -m "fix: ignore expected SSE client aborts"
```

### Task 5: Align production defaults and operator documentation

**Files:**
- Modify: `src/etf_rotation/cli.py`
- Modify: `scripts/start-monitor.ps1`
- Modify: `tests/test_market_data.py`
- Modify: `tests/test_run_tests_script.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing CLI and launch-script default tests**

Add to `CliPathTests` in `tests/test_market_data.py`:

```python
    def test_monitor_defaults_to_minute_refresh_interval(self) -> None:
        from etf_rotation.cli import _parser
        arguments = _parser().parse_args(["monitor"])
        self.assertEqual(arguments.refresh_interval, 60.0)
```

Add to `RunTestsScriptTests` in `tests/test_run_tests_script.py`:

```python
    def test_start_monitor_uses_minute_feed_interval(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "start-monitor.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("'--refresh-interval', '60'", script)
        self.assertNotIn("'--refresh-interval', '5'", script)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
$env:PYTHONPATH = 'src'
& 'C:\Users\linyongjie\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_market_data.CliPathTests.test_monitor_defaults_to_minute_refresh_interval tests.test_run_tests_script.RunTestsScriptTests.test_start_monitor_uses_minute_feed_interval -v
```

Expected: both assertions report the existing five-second default.

- [ ] **Step 3: Use the shared default in CLI and production launch**

In `src/etf_rotation/cli.py`, import `constants` and replace the parser default with:

```python
    monitor.add_argument(
        "--refresh-interval",
        type=float,
        default=constants.DEFAULT_REFRESH_INTERVAL_SECONDS,
    )
```

In `scripts/start-monitor.ps1`, use:

```powershell
    '--refresh-interval', '60',
```

- [ ] **Step 4: Document exact runtime semantics**

Add a “实时数据与采集时段” section to `README.md` containing these statements:

```markdown
## 实时数据与采集时段

- 实时页面和 `/api/quotes` 只返回上海时区当前自然日的已完成分钟；上一交易日数据只从历史日期入口查看。
- 连续交易时段按 60 秒基础间隔采集；连续失败最多退避到 300 秒，并在首次失败时撤销候选。
- 午休暂停采集；午间首次启动且缺少当天数据时补采，成功后停止，失败时按上限退避重试。
- 收盘后已有当天收盘数据时停止采集；收盘后首次启动且缺少当天数据时补采，成功后停止，失败时按上限退避重试。
- 周末、休市日和盘前不请求行情源。午休和收盘状态不会被无关网络错误覆盖成断流。
- 采集进程必须具有正常主机网络权限；受限沙箱可运行只读页面，但不能被当作实时采集部署方式。
```

- [ ] **Step 5: Run script parsing and focused tests and verify GREEN**

Run:

```powershell
$null = [scriptblock]::Create((Get-Content -LiteralPath 'scripts/start-monitor.ps1' -Raw))
$env:PYTHONPATH = 'src'
& 'C:\Users\linyongjie\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_market_data.CliPathTests.test_monitor_defaults_to_minute_refresh_interval tests.test_run_tests_script -v
```

Expected: PowerShell parsing succeeds and all focused tests PASS.

- [ ] **Step 6: Commit defaults and documentation**

```powershell
git add -- src/etf_rotation/cli.py scripts/start-monitor.ps1 tests/test_market_data.py tests/test_run_tests_script.py README.md
git commit -m "docs: align monitor runtime with minute sessions"
```

### Task 6 correction: findings approved during real-host verification

This section supersedes the earlier “do not modify/fallback” boundary; it is an approved correction from Task 6现场验证, not a second quote-source feature.

- Both endpoints are the same Eastmoney `trends2` field interface. Retry the entire batch on `push2delay.eastmoney.com` only after a transport/decode failure from `push2his.eastmoney.com`; a parsed business error from the primary must fail without fallback.
- Never mix hosts inside one batch. Persist the actual successful host through `Quote.source` into schema v3 history.
- Source fallback never bypasses the existing quote-age gate. Stale fallback data remains `DELAYED`/`OUTAGE`, candidates are revoked, and fallback is rejected for field, previous-close, minute-count, date, OHLC, limit, or volume/amount inconsistencies.
- Volume/amount validation permits only `±1` source volume unit around the exact volume, with at least one share for non-zero trades; OHLC, price-limit, and zero-pair checks stay strict. Evidence: fallback record 510500 at `2026-09-01T10:49:00+08:00` had `low=7.878`, `high=7.881`, `volume=1253`, `amount=986874`, and `volume_unit_shares=100`. Its direct implied price was `7.8760893855`; the `volume-1` bound was `7.8823801917`, so one source-unit rounding explains the discrepancy.
- Cross-date hardening is two-layered: each producer cycle publishes at most one empty current-date reset even when collection is not due, while snapshot/quote reads independently suppress stale-day values without changing revision.
- Successful batches schedule from completion to the next Shanghai minute boundary (`:05` waits 55 seconds; exact `:00` waits 60). Failure delays remain 60/120/240/300 seconds.
- Request-line disconnects are quiet only at the read boundary. Application/handler `ConnectionAbortedError` must still reach the standard server `handle_error` path.

### Task 6: Full regression, clean-archive, and live-service verification

**Files:**
- Verify: all tracked files from Tasks 1–5
- Runtime only: `var/monitor/quotes.json`, logs, PID, and history remain ignored

- [ ] **Step 1: Run the complete working-tree suite**

Run:

```powershell
$env:PYTHONPATH = 'src'
& 'C:\Users\linyongjie\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -v
```

Expected: all tests PASS with zero failures and zero errors.

- [ ] **Step 2: Run repository hygiene checks**

Run:

```powershell
git diff --check
git status --short
git ls-files | Where-Object { $_ -match '(^|/)__pycache__/|\.pyc$|^var/' }
```

Expected: `git diff --check` has no output; no runtime or bytecode files are tracked; status contains only intentional task changes before their commit.

- [ ] **Step 3: Run the full suite from a pure Git archive**

Run:

```powershell
$workspace = (Resolve-Path -LiteralPath '.').Path
$name = '.codex-market-session-archive-' + [guid]::NewGuid().ToString('N')
$root = Join-Path $workspace $name
$zip = Join-Path $workspace ($name + '.zip')
New-Item -ItemType Directory -Path $root | Out-Null
try {
    git archive --format=zip --output=$zip HEAD
    if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
    Expand-Archive -LiteralPath $zip -DestinationPath $root
    Push-Location $root
    try {
        $env:PYTHONPATH = 'src'
        & 'C:\Users\linyongjie\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { throw 'archive suite failed' }
    } finally {
        Pop-Location
    }
} finally {
    if (Test-Path -LiteralPath $root) {
        $resolvedRoot = (Resolve-Path -LiteralPath $root).Path
        if (-not $resolvedRoot.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar)) { throw "unsafe archive directory: $resolvedRoot" }
        Remove-Item -LiteralPath $resolvedRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $zip) {
        $resolvedZip = (Resolve-Path -LiteralPath $zip).Path
        if (-not $resolvedZip.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar)) { throw "unsafe archive file: $resolvedZip" }
        Remove-Item -LiteralPath $resolvedZip -Force
    }
}
```

Expected: the same complete test count passes without local ignored files.

- [ ] **Step 4: Restart the real service with host network permission**

Resolve and stop only the Python process verified as listening on `127.0.0.1:8765`:

```powershell
$line = netstat -ano -p tcp | Select-String -Pattern '127\.0\.0\.1:8765\s+.*LISTENING\s+(\d+)$' | Select-Object -First 1
if ($line) {
    $pidValue = [int]$line.Matches[0].Groups[1].Value
    $process = Get-Process -Id $pidValue -ErrorAction Stop
    if ($process.ProcessName -ne 'python') { throw "refusing to stop $($process.ProcessName) pid $pidValue" }
    Stop-Process -Id $pidValue -Force
}
```

Then run:

```powershell
& '.\scripts\start-monitor.ps1'
```

Run the launch command in the normal Windows user context with host network permission, not the offline sandbox. Expected: PID is written only after the process survives startup.

- [ ] **Step 5: Verify live API behavior**

Check `/health`, `/api/snapshot`, and all six `/api/quotes?symbol=<symbol>&since=0` responses. Derive the expected date and phase from the current Shanghai clock and `market_calendar.json`; do not hardcode the historical 2026-08-31 observation. The acceptance rules are:

```text
/health and item health agree with the current session and quote age
snapshot exposes no date earlier than the current Shanghai calendar date
each nonempty quote stream contains only the current Shanghai date and finalized schema v3 points
PRE_OPEN/weekend/explicit closure returns an empty reset rather than the previous close
inactive sessions do not repeatedly change revision
candidate count is zero whenever health is not REALTIME
```

Run:

```powershell
$shanghaiNow = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), 'China Standard Time')
$today = $shanghaiNow.ToString('yyyy-MM-dd')
$health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 5
$snapshot = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/snapshot' -TimeoutSec 5
if (@($snapshot.items).Count -ne 6) { throw 'snapshot does not contain six ETFs' }
foreach ($item in $snapshot.items) {
    if ($null -ne $item.timestamp -and -not ([string]$item.timestamp).StartsWith($today)) {
        throw "stale snapshot timestamp for $($item.symbol): $($item.timestamp)"
    }
}
foreach ($symbol in '510300','510500','563360','512100','159915','588000') {
    $quotes = Invoke-RestMethod -Uri ("http://127.0.0.1:8765/api/quotes?symbol=$symbol&since=0") -TimeoutSec 5
    $dates = @($quotes.upserts | ForEach-Object trading_date | Sort-Object -Unique)
    if ($dates.Count -gt 1 -or ($dates.Count -eq 1 -and $dates[0] -ne $today)) { throw "$symbol leaked another date" }
    if (@($quotes.upserts | Where-Object { $_.schema_version -ne 3 -or $_.is_complete -ne $true }).Count -ne 0) {
        throw "$symbol contains invalid live-minute metadata"
    }
}
```

Use this read-only check:

```powershell
$before = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 5
Start-Sleep -Seconds 65
$after = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 5
if (($after.health_statuses -join ',') -ne ($before.health_statuses -join ',')) { throw 'health phase changed during stability check' }
if (($before.health_statuses -join ',') -ne 'REALTIME' -and $after.revision -ne $before.revision) { throw 'inactive-session producer published another revision' }
```

Expected: revision is unchanged across more than one refresh interval and no collector error appears.

- [ ] **Step 6: Verify the stale-start scenario**

Using a temporary runtime fixture, start with a valid 2026-08-28 quote and a 2026-08-31 clock. Expected: `/api/snapshot` reports missing current-day data, `/api/quotes` returns a reset with zero upserts, and the page contains no 2026-08-28 live chart. The historical date endpoint must still expose 2026-08-28.

- [ ] **Step 7: Browser smoke test**

Open `http://127.0.0.1:8765/`, switch among all six symbols, and verify:

```text
No red outage banner after close
Status is 已收盘
No old-day chart appears in 实时行情
历史日期 still renders 2026-08-28
No console errors
Closing/reloading the tab does not append ConnectionAbortedError to monitor.err.log
```

- [ ] **Step 8: Request independent code review and commit any verified corrections**

Review against `docs/superpowers/specs/2026-08-31-market-aware-collector-design.md`. Any correction must receive its own failing regression test, focused verification, full-suite verification, and commit before completion is claimed.
