"""Two slow regime indicators for allocation: equity risk premium and style ratio.

* **ERP** (股债性价比) = 100 / CSI300 PE-TTM − 10-year CGB yield, in percentage
  points.  Its trailing percentile sets the equity allocation tier.  Because a
  falling yield inflates the ERP without stocks getting cheaper, the tier is
  downgraded one step when the ERP percentile runs far ahead of the PE
  percentile ("rate correction").
* **Style ratio** (成长/红利) = growth total-return proxy / dividend
  total-return proxy, both taken from the verified adjusted ETF history so no
  price-index drift creeps in.  Its trailing percentile sets growth versus
  dividend DCA multipliers; the high side only lowers additions and requires
  a trend break before any switch.

Everything here is read-only guidance.  Nothing trades, nothing changes the
formal strategy, and a missing or stale source leaves the indicator marked
UNAVAILABLE instead of being estimated.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import threading
from urllib.parse import urlencode
from urllib.request import Request
from zoneinfo import ZoneInfo

from .eastmoney_client import _default_transport
from .swing_data import DailyBar


SHANGHAI = ZoneInfo("Asia/Shanghai")
Transport = Callable[[Request, float], bytes]

TREASURY_YIELD_ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
TREASURY_REPORT = "RPTA_WEB_TREASURYYIELD"
# Eastmoney column for the China 10-year CGB yield to maturity (percent).
TREASURY_10Y_FIELD = "EMM00166466"
CSINDEX_PERF_ENDPOINT = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
CSI300_CODE = "000300"

SERIES_CN10Y = "CN10Y"
SERIES_CSI300_PE = "CSI300_PE_TTM"

GROWTH_PROXY_SYMBOL = "159915"    # 创业板ETF
DIVIDEND_PROXY_SYMBOL = "515180"  # 中证红利ETF

ERP_WINDOW_DAYS = 2500        # ~10 trading years
ERP_MINIMUM_DAYS = 1700       # ~7 years; must span 2018 and 2021 extremes
RATE_CORRECTION_GAP = 15.0    # ERP pct − PE cheapness pct beyond this is rate-driven
STYLE_WINDOW_DAYS = 750       # ~3 trading years
STYLE_MINIMUM_DAYS = 500
STYLE_TREND_DAYS = 20

# ERP tiers: (lower percentile bound, key, label, equity target %, DCA multiplier, action)
ERP_TIERS: tuple[tuple[float, str, str, int, float, str], ...] = (
    (90.0, "VERY_CHEAP", "极度便宜", 85, 2.0, "把债券/现金分批转权益，3 个月内到位"),
    (70.0, "CHEAP", "偏便宜", 70, 1.5, "维持或缓慢加仓，不减任何权益"),
    (30.0, "NEUTRAL", "中性", 55, 1.0, "按计划定投，不调仓"),
    (10.0, "RICH", "偏贵", 35, 0.5, "停止新增权益，分红和到期资金转债券"),
    (0.0, "VERY_RICH", "极贵", 20, 0.0, "分批减权益到目标，6 个月内完成"),
)

# Style tiers: (lower bound, key, label, growth mult, dividend mult, switch rule)
STYLE_TIERS: tuple[tuple[float, str, str, float, float, str], ...] = (
    (90.0, "GROWTH_HOT", "成长过热", 0.5, 1.2, "仅当比值跌破 20 日均线且均线拐头向下时，成长存量 20% 分 2 次换红利"),
    (70.0, "GROWTH_WARM", "成长偏热", 0.8, 1.1, "不动，不新增成长超配"),
    (30.0, "NEUTRAL", "中性", 1.0, 1.0, "不动"),
    (10.0, "GROWTH_FAVORED", "偏向成长", 1.2, 0.9, "不动"),
    (0.0, "GROWTH_WEAK", "成长极弱", 1.5, 0.8, "允许：红利存量 20% 分 2 次换成长；低位持续短，不等确认"),
)


class MacroRegimeError(ValueError):
    """Raised for malformed records or payloads."""


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SeriesPoint:
    series: str
    observed_date: date
    value: float
    source: str
    observed_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "series": self.series,
            "date": self.observed_date.isoformat(),
            "value": self.value,
            "source": self.source,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "SeriesPoint":
        try:
            series = payload["series"]
            observed = date.fromisoformat(str(payload["date"]))
            value = payload["value"]
            source = payload["source"]
            observed_at = payload["observed_at"]
        except (KeyError, TypeError, ValueError) as error:
            raise MacroRegimeError("macro series record is malformed") from error
        if type(series) is not str or series not in (SERIES_CN10Y, SERIES_CSI300_PE):
            raise MacroRegimeError("unknown macro series")
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise MacroRegimeError("macro series value must be a finite positive number")
        if type(source) is not str or not source or type(observed_at) is not str:
            raise MacroRegimeError("macro series source/observed_at must be strings")
        return cls(series, observed, float(value), source, observed_at)


class MacroSeriesStore:
    """Append-only JSONL store of (series, date) -> value, replaced atomically."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, dict[date, SeriesPoint]]:
        result: dict[str, dict[date, SeriesPoint]] = {SERIES_CN10Y: {}, SERIES_CSI300_PE: {}}
        if not self.path.exists():
            return result
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                point = SeriesPoint.from_mapping(json.loads(line))
            except (ValueError, TypeError) as error:
                raise MacroRegimeError(f"macro series store is corrupt: {error}") from error
            result[point.series][point.observed_date] = point
        return result

    def upsert(self, points: Sequence[SeriesPoint]) -> dict[str, dict[date, SeriesPoint]]:
        with self._lock:
            current = self.load()
            changed = False
            for point in points:
                if type(point) is not SeriesPoint:
                    raise MacroRegimeError("points must be SeriesPoint records")
                existing = current[point.series].get(point.observed_date)
                if existing is None or existing.value != point.value:
                    current[point.series][point.observed_date] = point
                    changed = True
            if changed:
                self._write(current)
            return current

    def _write(self, current: Mapping[str, Mapping[date, SeriesPoint]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(point.to_dict(), ensure_ascii=False, separators=(",", ":"))
            for series in sorted(current)
            for _, point in sorted(current[series].items())
        ]
        text = "\n".join(lines) + ("\n" if lines else "")
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(self.path)
        finally:
            if temporary.exists():
                temporary.unlink()


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------


class MacroCollector:
    """Fetch the two external series.  Never derives a missing value."""

    def __init__(
        self,
        timeout: float = 8.0,
        transport: Transport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.timeout = float(timeout)
        self.transport = transport or _default_transport
        self.now = now or (lambda: datetime.now(SHANGHAI))

    def _observed_at(self) -> str:
        return self.now().astimezone(SHANGHAI).isoformat(timespec="seconds")

    def collect_treasury_yield(self, *, pages: int = 6, page_size: int = 500) -> tuple[SeriesPoint, ...]:
        """Return the China 10-year yield, newest pages first, nulls skipped."""
        if type(pages) is not int or not 1 <= pages <= 40:
            raise MacroRegimeError("pages must be an integer from 1 to 40")
        observed_at = self._observed_at()
        points: dict[date, SeriesPoint] = {}
        for page in range(1, pages + 1):
            query = urlencode({
                "reportName": TREASURY_REPORT, "columns": "ALL",
                "sortColumns": "SOLAR_DATE", "sortTypes": -1,
                "pageSize": page_size, "pageNumber": page,
            })
            request = Request(f"{TREASURY_YIELD_ENDPOINT}?{query}", headers={
                "Accept": "application/json", "User-Agent": "Mozilla/5.0",
                "Referer": "https://data.eastmoney.com/",
            })
            payload = self._json(self.transport(request, self.timeout), "treasury yield")
            result = payload.get("result") if isinstance(payload, Mapping) else None
            rows = result.get("data") if isinstance(result, Mapping) else None
            if not isinstance(rows, list):
                raise MacroRegimeError("treasury yield payload has no data rows")
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                value = row.get(TREASURY_10Y_FIELD)
                raw_date = row.get("SOLAR_DATE")
                if value is None or not isinstance(raw_date, str):
                    continue
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise MacroRegimeError("treasury yield value is not a finite positive number")
                observed = date.fromisoformat(raw_date[:10])
                points.setdefault(observed, SeriesPoint(
                    SERIES_CN10Y, observed, float(value),
                    "东方财富 datacenter RPTA_WEB_TREASURYYIELD EMM00166466 (中国10年期国债到期收益率, %)",
                    observed_at,
                ))
            if len(rows) < page_size:
                break
        if not points:
            raise MacroRegimeError("treasury yield payload yielded no usable rows")
        return tuple(points[key] for key in sorted(points))

    def collect_csi300_pe(self, *, start: date, end: date) -> tuple[SeriesPoint, ...]:
        """Return CSI300 rolling PE from the index provider's daily performance feed."""
        if type(start) is not date or type(end) is not date or start > end:
            raise MacroRegimeError("start/end must be dates with start <= end")
        observed_at = self._observed_at()
        # A decade in one reply is ~1 MB and slower than the per-request
        # timeout; one calendar year per request keeps each call small.
        rows: list[object] = []
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, date(chunk_start.year, 12, 31))
            query = urlencode({
                "indexCode": CSI300_CODE,
                "startDate": chunk_start.strftime("%Y%m%d"),
                "endDate": chunk_end.strftime("%Y%m%d"),
            })
            request = Request(f"{CSINDEX_PERF_ENDPOINT}?{query}", headers={
                "Accept": "application/json", "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.csindex.com.cn/",
            })
            payload = self._json(self.transport(request, self.timeout), "csindex perf")
            chunk = payload.get("data") if isinstance(payload, Mapping) else None
            if str(payload.get("code")) != "200" or not isinstance(chunk, list):
                raise MacroRegimeError("csindex perf payload rejected")
            rows.extend(chunk)
            chunk_start = chunk_end + timedelta(days=1)
        points: list[SeriesPoint] = []
        for row in rows:
            if not isinstance(row, Mapping) or row.get("indexCode") != CSI300_CODE:
                raise MacroRegimeError("csindex perf row is not CSI300")
            value = row.get("peg")
            raw_date = row.get("tradeDate")
            if value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise MacroRegimeError("csindex PE is not a finite positive number")
            if not isinstance(raw_date, str) or len(raw_date) != 8:
                raise MacroRegimeError("csindex tradeDate is malformed")
            observed = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:]))
            points.append(SeriesPoint(
                SERIES_CSI300_PE, observed, float(value),
                "中证指数 index-perf peg (沪深300 滚动市盈率)", observed_at,
            ))
        if not points:
            raise MacroRegimeError("csindex perf payload yielded no PE rows")
        return tuple(sorted(points, key=lambda point: point.observed_date))

    @staticmethod
    def _json(raw: bytes, label: str) -> Mapping[str, object]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise MacroRegimeError(f"{label} payload is not JSON") from error
        if not isinstance(payload, Mapping):
            raise MacroRegimeError(f"{label} payload is not an object")
        return payload


# --------------------------------------------------------------------------
# Indicator maths
# --------------------------------------------------------------------------


def percentile_rank(values: Sequence[float], current: float) -> float:
    """Share of ``values`` strictly below ``current`` (0..100)."""
    if not values:
        raise MacroRegimeError("percentile needs at least one value")
    below = sum(1 for value in values if value < current)
    return below / len(values) * 100.0


def _tier(
    table: Sequence[tuple[float, str, str, float | int, float | int, str]], percentile: float,
) -> tuple[float, str, str, float | int, float | int, str]:
    for row in table:
        if percentile >= row[0]:
            return row
    return table[-1]


def _downgrade(table: Sequence[tuple], key: str) -> tuple:
    keys = [row[1] for row in table]
    index = keys.index(key)
    return table[min(index + 1, len(table) - 1)]


def compute_erp(
    pe_points: Mapping[date, SeriesPoint],
    yield_points: Mapping[date, SeriesPoint],
    *,
    window_days: int = ERP_WINDOW_DAYS,
    minimum_days: int = ERP_MINIMUM_DAYS,
) -> dict[str, object]:
    """Equity risk premium with a rate-driven downgrade and allocation tier."""
    pe_dates = sorted(pe_points)
    yield_dates = sorted(yield_points)
    if not pe_dates or not yield_dates:
        return {"status": "UNAVAILABLE", "reason": "MISSING_SERIES", "read_only": True}
    # Yields are published on calendar days; use the latest on or before each PE date.
    rows: list[tuple[date, float, float, float]] = []
    cursor = 0
    latest_yield: float | None = None
    for trading_day in pe_dates:
        while cursor < len(yield_dates) and yield_dates[cursor] <= trading_day:
            latest_yield = yield_points[yield_dates[cursor]].value
            cursor += 1
        if latest_yield is None:
            continue
        pe = pe_points[trading_day].value
        rows.append((trading_day, pe, latest_yield, 100.0 / pe - latest_yield))
    rows = rows[-window_days:]
    if len(rows) < minimum_days:
        return {
            "status": "UNAVAILABLE", "reason": "INSUFFICIENT_HISTORY",
            "sample_days": len(rows), "minimum_days": minimum_days, "read_only": True,
        }
    as_of, pe, yield_value, erp = rows[-1]
    erp_pct = percentile_rank([row[3] for row in rows], erp)
    pe_pct = percentile_rank([row[1] for row in rows], pe)
    pe_cheapness = 100.0 - pe_pct
    gap = erp_pct - pe_cheapness
    raw_tier = _tier(ERP_TIERS, erp_pct)
    rate_driven = gap > RATE_CORRECTION_GAP
    tier = _downgrade(ERP_TIERS, raw_tier[1]) if rate_driven else raw_tier
    pe_date = as_of
    yield_date = max(day for day in yield_dates if day <= as_of)
    return {
        "status": "OK",
        "as_of": pe_date.isoformat(),
        "pe_ttm": round(pe, 4),
        "pe_as_of": pe_date.isoformat(),
        "earnings_yield_pct": round(100.0 / pe, 4),
        "cn10y_yield_pct": round(yield_value, 4),
        "cn10y_as_of": yield_date.isoformat(),
        "erp_pct": round(erp, 4),
        "erp_percentile": round(erp_pct, 1),
        "pe_percentile": round(pe_pct, 1),
        "pe_cheapness_percentile": round(pe_cheapness, 1),
        "rate_correction_gap": round(gap, 1),
        "rate_driven": rate_driven,
        "raw_tier": raw_tier[1],
        "tier": tier[1],
        "tier_label": tier[2],
        "equity_target_pct": tier[3],
        "dca_multiplier": tier[4],
        "action": tier[5],
        "window_days": window_days,
        "sample_days": len(rows),
        "sample_start": rows[0][0].isoformat(),
        "read_only": True,
    }


def compute_style_ratio(
    growth_bars: Sequence[DailyBar],
    dividend_bars: Sequence[DailyBar],
    *,
    window_days: int = STYLE_WINDOW_DAYS,
    minimum_days: int = STYLE_MINIMUM_DAYS,
    trend_days: int = STYLE_TREND_DAYS,
) -> dict[str, object]:
    """Growth / dividend total-return ratio percentile with asymmetric guidance."""
    growth = {bar.trading_date: float(bar.adjusted_close) for bar in growth_bars}
    dividend = {bar.trading_date: float(bar.adjusted_close) for bar in dividend_bars}
    dates = sorted(set(growth) & set(dividend))
    if not dates:
        return {"status": "UNAVAILABLE", "reason": "MISSING_SERIES", "read_only": True}
    ratios = [growth[day] / dividend[day] for day in dates]
    # Normalize to the first session so the level is readable; percentiles are scale-free.
    base = ratios[0]
    normalized = [value / base for value in ratios]
    window_dates = dates[-window_days:]
    window = normalized[-window_days:]
    if len(window) < minimum_days:
        return {
            "status": "UNAVAILABLE", "reason": "INSUFFICIENT_HISTORY",
            "sample_days": len(window), "minimum_days": minimum_days, "read_only": True,
        }
    current = window[-1]
    pct = percentile_rank(window, current)
    tier = _tier(STYLE_TIERS, pct)
    trend = window[-trend_days:]
    moving_average = sum(trend) / len(trend)
    previous_trend = window[-trend_days - 1:-1]
    previous_average = sum(previous_trend) / len(previous_trend) if len(previous_trend) == trend_days else None
    above_average = current > moving_average
    average_falling = previous_average is not None and moving_average < previous_average
    switch_confirmed = (
        tier[1] == "GROWTH_HOT" and not above_average and average_falling
    )
    peak_index = max(range(len(window)), key=lambda index: window[index])
    trough_index = min(range(len(window)), key=lambda index: window[index])
    return {
        "status": "OK",
        "as_of": window_dates[-1].isoformat(),
        "growth_symbol": growth_bars[0].symbol if growth_bars else None,
        "dividend_symbol": dividend_bars[0].symbol if dividend_bars else None,
        "ratio_normalized": round(current, 4),
        "ratio_percentile": round(pct, 1),
        "ratio_ma20": round(moving_average, 4),
        "above_ma20": above_average,
        "ma20_falling": average_falling,
        "window_high": round(window[peak_index], 4),
        "window_high_date": window_dates[peak_index].isoformat(),
        "window_low": round(window[trough_index], 4),
        "window_low_date": window_dates[trough_index].isoformat(),
        "from_high_pct": round((current / window[peak_index] - 1.0) * 100.0, 1),
        "tier": tier[1],
        "tier_label": tier[2],
        "growth_multiplier": tier[3],
        "dividend_multiplier": tier[4],
        "switch_rule": tier[5],
        "switch_confirmed": switch_confirmed,
        "window_days": window_days,
        "sample_days": len(window),
        "sample_start": window_dates[0].isoformat(),
        "read_only": True,
    }


def combine_guidance(erp: Mapping[str, object], style: Mapping[str, object]) -> dict[str, object]:
    """Three-by-three matrix: ERP sets the total, the style ratio splits it."""
    if erp.get("status") != "OK" or style.get("status") != "OK":
        return {"status": "UNAVAILABLE", "read_only": True}
    erp_tier = str(erp["tier"])
    style_tier = str(style["tier"])
    total = "ADD" if erp_tier in ("VERY_CHEAP", "CHEAP") else "REDUCE" if erp_tier in ("RICH", "VERY_RICH") else "HOLD"
    lean = "GROWTH" if style_tier in ("GROWTH_FAVORED", "GROWTH_WEAK") else "DIVIDEND" if style_tier in ("GROWTH_WARM", "GROWTH_HOT") else "BALANCED"
    text = {
        ("ADD", "GROWTH"): "加仓，新增资金主投成长",
        ("ADD", "BALANCED"): "加仓，按原比例",
        ("ADD", "DIVIDEND"): "加仓，新增资金主投红利",
        ("HOLD", "GROWTH"): "按计划，成长线上调、红利线下调",
        ("HOLD", "BALANCED"): "按计划",
        ("HOLD", "DIVIDEND"): "按计划，成长线下调、红利线上调",
        ("REDUCE", "GROWTH"): "减仓，先减红利留成长",
        ("REDUCE", "BALANCED"): "减仓，按比例",
        ("REDUCE", "DIVIDEND"): "减仓，先减成长留红利",
    }[(total, lean)]
    return {
        "status": "OK",
        "total_action": total,
        "style_lean": lean,
        "summary": text,
        "dca_total_multiplier": erp["dca_multiplier"],
        "growth_multiplier": style["growth_multiplier"],
        "dividend_multiplier": style["dividend_multiplier"],
        "read_only": True,
    }


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------


def load_proxy_bars(history_path: Path, symbols: Sequence[str]) -> dict[str, tuple[DailyBar, ...]]:
    """Read only the proxy symbols from the canonical daily history file."""
    wanted = set(symbols)
    grouped: dict[str, list[DailyBar]] = {symbol: [] for symbol in symbols}
    path = Path(history_path)
    if not path.exists():
        return {symbol: () for symbol in symbols}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, Mapping) or payload.get("symbol") not in wanted:
            continue
        grouped[str(payload["symbol"])].append(DailyBar.from_mapping(payload))
    return {
        symbol: tuple(sorted(bars, key=lambda bar: bar.trading_date))
        for symbol, bars in grouped.items()
    }


class MacroRegimeService:
    """Lazily refreshed, read-only snapshot of both indicators.

    External series are refreshed at most once per ``refresh_interval`` and
    only when the stored data is older than the previous calendar day; a
    failed refresh is recorded in ``errors`` and the snapshot falls back to
    whatever is stored.  Without a collector the service is read-only.
    """

    def __init__(
        self,
        store_path: Path,
        history_path: Path,
        collector: MacroCollector | None,
        *,
        clock: Callable[[], datetime] | None = None,
        refresh_interval: float = 3600.0,
        growth_symbol: str = GROWTH_PROXY_SYMBOL,
        dividend_symbol: str = DIVIDEND_PROXY_SYMBOL,
    ) -> None:
        self.store = MacroSeriesStore(store_path)
        self.history_path = Path(history_path)
        self.collector = collector
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self.refresh_interval = float(refresh_interval)
        self.growth_symbol = growth_symbol
        self.dividend_symbol = dividend_symbol
        self._lock = threading.Lock()
        self._last_attempt: datetime | None = None
        self.errors: dict[str, str] = {}

    def _needs_refresh(self, series: Mapping[str, Mapping[date, SeriesPoint]], now: datetime) -> bool:
        if self.collector is None:
            return False
        if self._last_attempt is not None and (now - self._last_attempt).total_seconds() < self.refresh_interval:
            return False
        stale_before = now.astimezone(SHANGHAI).date() - timedelta(days=1)
        for key in (SERIES_CN10Y, SERIES_CSI300_PE):
            points = series.get(key) or {}
            if not points or max(points) < stale_before:
                return True
        return False

    def refresh(self, now: datetime | None = None) -> dict[str, str]:
        """Fetch both series and upsert them; errors are recorded per series."""
        if self.collector is None:
            return dict(self.errors)
        current = now or self.clock()
        with self._lock:
            self._last_attempt = current
            incoming: list[SeriesPoint] = []
            today = current.astimezone(SHANGHAI).date()
            # Keep ~11 years so the 10-year window is always full; once the
            # store covers that depth only the recent tail is re-fetched.
            backfill_start = today - timedelta(days=11 * 365)
            try:
                stored_yield = self.store.load()[SERIES_CN10Y]
                deep = bool(stored_yield) and min(stored_yield) <= backfill_start + timedelta(days=45)
                incoming.extend(self.collector.collect_treasury_yield(pages=1 if deep else 6))
                self.errors.pop(SERIES_CN10Y, None)
            except Exception as error:  # noqa: BLE001 - recorded, never raised
                self.errors[SERIES_CN10Y] = type(error).__name__
            try:
                stored = self.store.load()[SERIES_CSI300_PE]
                deep = bool(stored) and min(stored) <= backfill_start + timedelta(days=45)
                start = max(stored) - timedelta(days=14) if deep else backfill_start
                incoming.extend(self.collector.collect_csi300_pe(start=start, end=today))
                self.errors.pop(SERIES_CSI300_PE, None)
            except Exception as error:  # noqa: BLE001
                self.errors[SERIES_CSI300_PE] = type(error).__name__
            if incoming:
                try:
                    self.store.upsert(incoming)
                    self.errors.pop("store", None)
                except Exception as error:  # noqa: BLE001
                    self.errors["store"] = type(error).__name__
        return dict(self.errors)

    def snapshot(self) -> dict[str, object]:
        now = self.clock()
        try:
            series = self.store.load()
        except MacroRegimeError as error:
            self.errors["store"] = type(error).__name__
            series = {SERIES_CN10Y: {}, SERIES_CSI300_PE: {}}
        if self._needs_refresh(series, now):
            self.refresh(now)
            try:
                series = self.store.load()
            except MacroRegimeError:
                pass
        try:
            proxies = load_proxy_bars(self.history_path, (self.growth_symbol, self.dividend_symbol))
            self.errors.pop("history", None)
        except Exception as error:  # noqa: BLE001
            self.errors["history"] = type(error).__name__
            proxies = {self.growth_symbol: (), self.dividend_symbol: ()}
        erp = compute_erp(series[SERIES_CSI300_PE], series[SERIES_CN10Y])
        style = compute_style_ratio(proxies[self.growth_symbol], proxies[self.dividend_symbol])
        return {
            "generated_at": now.astimezone(SHANGHAI).isoformat(timespec="seconds"),
            "read_only": True,
            "mode": "GUIDANCE_ONLY",
            "erp": erp,
            "style_ratio": style,
            "combined": combine_guidance(erp, style),
            "series_coverage": {
                key: {
                    "count": len(points),
                    "start": min(points).isoformat() if points else None,
                    "end": max(points).isoformat() if points else None,
                }
                for key, points in series.items()
            },
            "erp_tiers": [
                {"min_percentile": row[0], "tier": row[1], "label": row[2],
                 "equity_target_pct": row[3], "dca_multiplier": row[4], "action": row[5]}
                for row in ERP_TIERS
            ],
            "style_tiers": [
                {"min_percentile": row[0], "tier": row[1], "label": row[2],
                 "growth_multiplier": row[3], "dividend_multiplier": row[4], "switch_rule": row[5]}
                for row in STYLE_TIERS
            ],
            "errors": dict(self.errors),
        }
