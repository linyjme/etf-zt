# Watchlist Add Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject watchlist additions that lack verified ETF metadata, show a visible result for every add attempt, and remove the invalid `515180` entry that currently degrades the live monitor.

**Architecture:** Keep validation in `MonitorApplication.add_watch_item`, immediately before its atomic watchlist write, so all callers share one transactional boundary. Map the dedicated metadata error to a stable HTTP 422 response, while the browser renders success and error text beside the existing compact form. The request thread does not collect quotes; the existing background producer remains the only market-data producer.

**Tech Stack:** Python 3 standard library HTTP server, `unittest`, inline HTML/CSS/JavaScript, PowerShell launch scripts.

---

## File map

- Modify `src/etf_rotation/t_web.py`: define the missing-metadata business error, validate before persistence, and map it to HTTP 422.
- Modify `src/etf_rotation/t_page.py`: render explicit success/error form feedback.
- Modify `tests/test_t_monitor.py`: cover application/HTTP behavior and page feedback strings.
- Restore `data/monitor/watchlist.json`: remove only the invalid `515180` runtime entry; no new metadata is invented.

### Task 1: Make watchlist persistence reject missing metadata atomically

**Files:**
- Modify: `tests/test_t_monitor.py:922-946,1524-1570`
- Modify: `src/etf_rotation/t_web.py:46-54,1105-1127,1260-1278`

- [ ] **Step 1: Write the failing HTTP regression test**

Add a test next to the existing invalid and duplicate POST tests. It captures the exact file bytes to prove that rejection is transactional:

```python
def test_watchlist_post_rejects_missing_metadata_without_changing_file(self) -> None:
    original = self.watchlist.read_bytes()
    status, payload = self.post("/api/watchlist", {
        "symbol": "515180",
        "name": "中证红利",
    })
    self.assertEqual(status, 422)
    self.assertEqual(payload["error"], "missing_metadata")
    self.assertEqual(payload["message"], "缺少交易元数据，无法添加: 515180")
    self.assertEqual(self.watchlist.read_bytes(), original)
```

- [ ] **Step 2: Update the valid-add fixture so it represents a configured ETF**

At the start of `test_watchlist_post_persists_valid_item`, append metadata for `159915` before posting it:

```python
metadata = json.loads(self.metadata.read_text(encoding="utf-8"))
metadata["items"].append({
    "symbol": "159915",
    "name": "创业板ETF",
    "index": {"code": "399006", "name": "创业板指", "provider": "深交所"},
    "trading": {
        "exchange": "SZSE",
        "asset_type": "DOMESTIC_EQUITY_ETF",
        "intraday_turnaround": False,
        "sellable_delay_days": 1,
        "lot_size": 100,
        "price_tick": 0.001,
        "price_limit_pct": 0.20,
        "volume_unit_shares": 100,
    },
})
self.metadata.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
```

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_t_monitor.MonitorWebTests.test_watchlist_post_rejects_missing_metadata_without_changing_file tests.test_t_monitor.MonitorWebTests.test_watchlist_post_persists_valid_item -v
```

Expected: the missing-metadata test fails because the endpoint returns `201` and changes the file; the configured valid-add test still passes.

- [ ] **Step 4: Implement the dedicated error and pre-write validation**

Add near the module constants in `t_web.py`:

```python
class MissingWatchMetadataError(Exception):
    """Raised when a watch item has no verified trading metadata."""
```

Inside the existing `watchlist_lock`, preserve duplicate precedence and validate metadata immediately before `_atomic_write_watchlist`:

```python
with self.watchlist_lock:
    watchlist = load_watchlist(self.watchlist_path)
    if any(current.symbol == normalized_symbol for current in watchlist):
        raise FileExistsError(f"代码已在监控列表中: {normalized_symbol}")
    if normalized_symbol not in self.metadata_store.load():
        raise MissingWatchMetadataError(
            f"缺少交易元数据，无法添加: {normalized_symbol}",
        )
    self._atomic_write_watchlist([*watchlist, item])
```

Map it before the existing `ValueError` branch in `_add_watch_item`:

```python
except MissingWatchMetadataError as error:
    self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {
        "error": "missing_metadata",
        "message": str(error),
    })
```

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run the same command from Step 3.

Expected: both tests pass; the rejection test confirms byte-for-byte file preservation.

- [ ] **Step 6: Run the adjacent POST regression tests**

Run:

```powershell
python -m unittest tests.test_t_monitor.MonitorWebTests.test_watchlist_post_rejects_invalid_symbol_without_changing_file tests.test_t_monitor.MonitorWebTests.test_watchlist_post_rejects_duplicate_without_changing_file tests.test_t_monitor.MonitorWebTests.test_trading_post_is_still_rejected -v
```

Expected: 3 tests pass with existing 400, 409, and 405 behavior unchanged.

- [ ] **Step 7: Commit the backend transaction fix**

```powershell
git add -- src/etf_rotation/t_web.py tests/test_t_monitor.py
git commit -m "fix: validate metadata before adding watch items"
```

### Task 2: Show an explicit result beside the add form

**Files:**
- Modify: `tests/test_t_monitor.py:1150-1161`
- Modify: `src/etf_rotation/t_page.py:14-20,154`

- [ ] **Step 1: Write the failing page contract test**

Extend `test_page_has_left_watchlist_navigation_and_compact_add_form` with assertions that require both visual states and the success copy:

```python
self.assertIn("#form-error.success", PAGE)
self.assertIn("formError.className='success'", PAGE)
self.assertIn("formError.className='error'", PAGE)
self.assertIn("等待下一轮行情刷新", PAGE)
```

- [ ] **Step 2: Run the page test and verify RED**

Run:

```powershell
python -m unittest tests.test_t_monitor.MonitorWebTests.test_page_has_left_watchlist_navigation_and_compact_add_form -v
```

Expected: FAIL because the success class and success message do not exist.

- [ ] **Step 3: Implement minimal success/error rendering**

Keep the existing `#form-error` node and add a success override to the stylesheet:

```css
#errors,#form-error{color:#ff9b9b;margin:8px 0}
#form-error.success{color:#74f0c4}
```

Replace the submit handler with the same request flow plus explicit class and message updates:

```javascript
watchForm.addEventListener('submit',async event=>{
  event.preventDefault();
  formError.textContent='';
  formError.className='';
  watchSubmit.disabled=true;
  const submittedSymbol=watchSymbol.value.trim();
  try{
    const response=await fetch('/api/watchlist',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        symbol:submittedSymbol,
        name:watchName.value,
        grid_width_pct:DEFAULT_GRID_WIDTH_PCT,
      }),
    }),payload=await response.json();
    if(!response.ok)throw new Error(payload.message||payload.error||'添加失败');
    watchForm.reset();
    formError.className='success';
    formError.textContent=`已添加 ${submittedSymbol}，等待下一轮行情刷新`;
    await poll();
    await loadBacktests();
  }catch(error){
    formError.className='error';
    formError.textContent=error.message;
  }finally{
    watchSubmit.disabled=false;
  }
});
```

- [ ] **Step 4: Run the page test and verify GREEN**

Run the same command from Step 2.

Expected: PASS.

- [ ] **Step 5: Run the complete monitor web test class**

Run:

```powershell
python -m unittest tests.test_t_monitor.MonitorWebTests -v
```

Expected: all `MonitorWebTests` pass.

- [ ] **Step 6: Commit the page feedback fix**

```powershell
git add -- src/etf_rotation/t_page.py tests/test_t_monitor.py
git commit -m "fix: show watchlist add results"
```

### Task 3: Roll back the invalid runtime item and verify the live application

**Files:**
- Restore: `data/monitor/watchlist.json` (remove only `515180` added at 2026-09-01 14:24)
- Verify: `scripts/run-tests.ps1`, `scripts/stop-monitor.ps1`, `scripts/start-monitor.ps1`

- [ ] **Step 1: Remove only the invalid runtime entry**

Delete this exact object from `data/monitor/watchlist.json`, leaving the six existing entries unchanged:

```json
{
  "symbol": "515180",
  "name": "中证红利",
  "grid_width_pct": 0.002,
  "enabled": true
}
```

- [ ] **Step 2: Confirm the cleanup does not create a source diff**

Run:

```powershell
git diff -- data/monitor/watchlist.json
git status --short
```

Expected: no diff for `watchlist.json`; only intended implementation files or commits are present.

- [ ] **Step 3: Run the complete automated suite**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1
```

Expected: exit code 0 with all project tests passing.

- [ ] **Step 4: Restart the local monitor from the updated code**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/stop-monitor.ps1
powershell -ExecutionPolicy Bypass -File scripts/start-monitor.ps1
```

Expected: the service listens on `127.0.0.1:8765` and starts without a metadata outage.

- [ ] **Step 5: Verify the live API and page behavior**

Check `/health` and `/api/snapshot`; then reload the existing local page. Submit `515180 / 中证红利` once and verify:

- HTTP response is 422 with `missing_metadata`.
- The form retains both values and visibly shows `缺少交易元数据，无法添加: 515180`.
- The watchlist remains at 6 items.
- Existing health is not degraded by `515180`.

- [ ] **Step 6: Verify repository cleanliness and final diff**

Run:

```powershell
git status --short
git log -3 --oneline
```

Expected: no uncommitted source or runtime changes; the design, backend fix, and page feedback commits are visible.
