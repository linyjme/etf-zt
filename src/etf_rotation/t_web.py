from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from . import constants
from .etf_metadata import EtfMetadataStore
from .market_data import MarketHealthClassifier, MinuteHistoryStore, finalized_points, load_closed_dates
from .t_monitor import AlertHistoryStore, JsonQuoteAdapter, QuoteHistoryStore, TMonitorEngine, load_watchlist, snapshot_to_dict


_DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "monitor"
_DEFAULT_METADATA_PATH = _DATA_ROOT / "etf_metadata.json"
_DEFAULT_CALENDAR_PATH = _DATA_ROOT / "market_calendar.json"


@dataclass
class MonitorApplication:
    quotes_path: Path
    watchlist_path: Path
    history_path: Path | None = None
    collector: Any | None = None
    refresh_interval: float = 5.0
    alert_history_path: Path | None = None
    metadata_path: Path | None = None
    calendar_path: Path | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now().astimezone(), compare=False)
    watchlist_lock: threading.Lock = field(default_factory=threading.Lock, compare=False)
    refresh_lock: threading.Lock = field(default_factory=threading.Lock, compare=False)
    producer_lock: threading.Lock = field(default_factory=threading.Lock, compare=False)
    refresh_error: str | None = None
    last_refresh_at: str | None = None
    _stop_event: threading.Event = field(default_factory=threading.Event, compare=False)
    _refresh_thread: threading.Thread | None = field(default=None, compare=False)
    _published: dict[str, Any] = field(default_factory=dict, init=False, compare=False)
    _revision: int = field(default=0, init=False, compare=False)
    metadata_store: EtfMetadataStore = field(init=False, compare=False)
    history_store: MinuteHistoryStore | None = field(init=False, compare=False)
    alert_store: AlertHistoryStore | None = field(init=False, compare=False)
    health_classifier: MarketHealthClassifier = field(init=False, compare=False)
    engine: TMonitorEngine = field(init=False, compare=False)

    def __post_init__(self) -> None:
        self.quotes_path = Path(self.quotes_path)
        self.watchlist_path = Path(self.watchlist_path)
        self.history_path = Path(self.history_path) if self.history_path is not None else None
        self.alert_history_path = (
            Path(self.alert_history_path) if self.alert_history_path is not None else None
        )
        self.metadata_path = Path(self.metadata_path or _DEFAULT_METADATA_PATH)
        self.calendar_path = Path(self.calendar_path or _DEFAULT_CALENDAR_PATH)
        self.metadata_store = EtfMetadataStore(self.metadata_path)
        self.history_store = (
            MinuteHistoryStore(self.history_path) if self.history_path is not None else None
        )
        self.alert_store = (
            AlertHistoryStore(self.alert_history_path)
            if self.alert_history_path is not None else None
        )
        self.health_classifier = MarketHealthClassifier(
            load_closed_dates(self.calendar_path),
        )
        self.engine = TMonitorEngine(self.health_classifier)
        self._published = self._empty_snapshot()
        self._bootstrap(increment_revision=self.collector is None)

    def snapshot(self) -> dict[str, Any]:
        with self.refresh_lock:
            return copy.deepcopy(self._published)

    def start_refresh(self) -> None:
        with self.refresh_lock:
            if (
                self.collector is None
                or self._refresh_thread is not None
                and self._refresh_thread.is_alive()
            ):
                return
            self._stop_event.clear()
            thread = threading.Thread(target=self._refresh_loop, daemon=True)
            self._refresh_thread = thread
            thread.start()

    def stop_refresh(self) -> None:
        self._stop_event.set()
        with self.refresh_lock:
            thread = self._refresh_thread
        if thread is not None:
            thread.join(timeout=max(self.refresh_interval, 1.0) + 1.0)
            with self.refresh_lock:
                if self._refresh_thread is thread:
                    self._refresh_thread = None

    def refresh_once(self) -> bool:
        with self.producer_lock:
            if self.collector is None:
                return False
            try:
                watchlist = load_watchlist(self.watchlist_path)
                payload = self.collector.collect_to_file(watchlist, self.quotes_path)
                quotes = JsonQuoteAdapter().parse(payload)
                metadata = self.metadata_store.load()
                if self.history_store is not None:
                    self.history_store.upsert(quotes, metadata)
                now = self.clock()
                completed_at = [
                    point.timestamp
                    for quote in quotes.values()
                    for point in finalized_points(quote.points, quote.observed_at)
                ]
                health = self.health_classifier.classify(
                    now, max(completed_at) if completed_at else None, None,
                )
                published = snapshot_to_dict(self.engine.evaluate(
                    watchlist, quotes, generated_at=now, health=health,
                ))
                if self.alert_store is not None:
                    self.alert_store.append_candidates(published)
            except Exception as error:
                self._publish_outage(str(error))
                return False
            self._publish(published, payload)
            return True

    def _refresh_loop(self) -> None:
        while not self._stop_event.is_set():
            self.refresh_once()
            self._stop_event.wait(self.refresh_interval)

    def _bootstrap(self, *, increment_revision: bool) -> None:
        watchlist = load_watchlist(self.watchlist_path)
        error: str | None = None
        try:
            raw = json.loads(self.quotes_path.read_text(encoding="utf-8"))
            quotes = JsonQuoteAdapter().parse(raw)
            payload = raw if isinstance(raw, dict) else {}
        except (ValueError, OSError) as failure:
            quotes = {}
            payload = {}
            error = str(failure)
        now = self.clock()
        completed_at = [
            point.timestamp
            for quote in quotes.values()
            for point in finalized_points(quote.points, quote.observed_at)
        ]
        health = self.health_classifier.classify(
            now, max(completed_at) if completed_at else None, error,
        )
        published = snapshot_to_dict(self.engine.evaluate(
            watchlist, quotes, generated_at=now, health=health,
        ))
        if error is not None:
            published["errors"] = [error]
        self._publish(
            published, payload, error=error, increment_revision=increment_revision,
        )

    def _empty_snapshot(self) -> dict[str, Any]:
        return {
            "generated_at": self.clock().isoformat(),
            "mode": "MONITOR_ONLY",
            "auto_trade": False,
            "errors": [],
            "items": [],
            "revision": 0,
            "source": None,
            "refresh_error": None,
            "last_refresh_at": None,
        }

    def _publish(
        self,
        published: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        error: str | None = None,
        increment_revision: bool = True,
    ) -> None:
        with self.refresh_lock:
            if increment_revision:
                self._revision += 1
            result = copy.deepcopy(dict(published))
            result["revision"] = self._revision
            result["source"] = copy.deepcopy(payload.get("source"))
            result["refresh_error"] = error
            result["last_refresh_at"] = payload.get("collected_at")
            self.refresh_error = error
            self.last_refresh_at = result["last_refresh_at"]
            self._published = copy.deepcopy(result)

    def _publish_outage(self, message: str) -> None:
        with self.refresh_lock:
            self._revision += 1
            result = copy.deepcopy(self._published)
            result["revision"] = self._revision
            result["generated_at"] = self.clock().isoformat()
            result["errors"] = [message]
            result["refresh_error"] = message
            for item in result.get("items", []):
                if item.get("action") in {"BUY_CANDIDATE", "SELL_CANDIDATE"}:
                    item["action"] = "DEVIATION_OBSERVE"
                    item["label"] = "偏离观察"
                item["health_status"] = "OUTAGE"
                item["health_reason"] = message
                reasons = list(item.get("blocked_reasons") or [])
                if "MARKET_NOT_REALTIME" not in reasons:
                    reasons.append("MARKET_NOT_REALTIME")
                item["blocked_reasons"] = reasons
            self.refresh_error = message
            self._published = copy.deepcopy(result)

    def backtest(self) -> dict[str, Any]:
        quotes = JsonQuoteAdapter().load(self.quotes_path)
        if self.history_path is not None:
            store = QuoteHistoryStore(self.history_path)
            quotes = store.merge(quotes)
        watchlist = load_watchlist(self.watchlist_path)
        items = []
        for item in watchlist:
            if not item.enabled:
                continue
            quote = quotes.get(item.symbol)
            if quote is None:
                items.append({
                    "symbol": item.symbol, "status": "MISSING_QUOTE",
                    "initial_capital_cny": None, "ending_value_cny": None,
                    "cumulative_return": None, "maximum_drawdown": None,
                    "trade_count": None, "trades": [],
                })
                continue
            initial = constants.DEFAULT_BASE_NOTIONAL_CNY
            cash = initial
            position: tuple[int, float] | None = None
            trades: list[dict[str, Any]] = []
            curve: list[float] = [initial]
            for index in range(1, len(quote.points)):
                point = quote.points[index]
                decision_quote = type(quote)(
                    quote.symbol, quote.name, quote.points[index - 1].price,
                    quote.points[index - 1].average_price, quote.previous_close,
                    quote.points[index - 1].timestamp, quote.points[:index],
                    quote.observed_at, quote.source,
                )
                signal = TMonitorEngine().evaluate(
                    (item,), {quote.symbol: decision_quote},
                    generated_at=point.timestamp,
                ).signals[0]
                if signal.action == "BUY_CANDIDATE" and position is None:
                    shares = int(cash / (point.price * (1 + constants.SLIPPAGE_RATE) * (1 + constants.BUY_COMMISSION_RATE)) / 100) * 100
                    if shares:
                        fill = point.price * (1 + constants.SLIPPAGE_RATE)
                        fee = max(shares * fill * constants.BUY_COMMISSION_RATE, constants.MINIMUM_COMMISSION_CNY)
                        cash -= shares * fill + fee
                        position = (shares, shares * fill + fee)
                        trades.append({"timestamp": point.timestamp.isoformat(), "action": "BUY", "symbol": quote.symbol, "shares": shares, "price": fill, "fee": fee})
                elif signal.action == "SELL_CANDIDATE" and position is not None:
                    shares, basis = position
                    fill = point.price * (1 - constants.SLIPPAGE_RATE)
                    fee = max(shares * fill * constants.SELL_COMMISSION_RATE, constants.MINIMUM_COMMISSION_CNY)
                    proceeds = shares * fill - fee
                    cash += proceeds
                    position = None
                    trades.append({"timestamp": point.timestamp.isoformat(), "action": "SELL", "symbol": quote.symbol, "shares": shares, "price": fill, "fee": fee, "pnl": proceeds - basis})
                curve.append(cash + (position[0] * point.price if position else 0.0))
            peak = initial
            maximum_drawdown = 0.0
            for value in curve:
                peak = max(peak, value)
                maximum_drawdown = max(maximum_drawdown, 1 - value / peak)
            ending = curve[-1]
            first_price = quote.points[0].price
            hold_ending = initial * quote.points[-1].price / first_price
            realized = [trade["pnl"] for trade in trades if "pnl" in trade]
            wins = sum(1 for pnl in realized if pnl > 0)
            items.append({
                "symbol": item.symbol, "status": "OK",
                "strategy_version": "T_V1_BASELINE",
                "initial_capital_cny": initial, "ending_value_cny": ending,
                "cumulative_return": ending / initial - 1,
                "maximum_drawdown": maximum_drawdown,
                "trade_count": len(trades), "trades": trades,
                "buy_and_hold_ending_value_cny": hold_ending,
                "buy_and_hold_return": hold_ending / initial - 1,
                "excess_return_vs_hold": ending / initial - hold_ending / initial,
                "winning_trade_count": wins,
                "win_rate": wins / len(realized) if realized else None,
                "realized_trade_count": len(realized),
            })
        return {
            "items": items,
            "commission_rate": constants.BUY_COMMISSION_RATE,
            "buy_commission_rate": constants.BUY_COMMISSION_RATE,
            "sell_commission_rate": constants.SELL_COMMISSION_RATE,
            "minimum_commission_cny": constants.MINIMUM_COMMISSION_CNY,
            "commission_minimum_waived": True,
            "slippage_rate": constants.SLIPPAGE_RATE,
            "execution": "NEXT_POINT",
            "read_only": True,
        }

    def add_watch_item(self, symbol: object, name: object) -> dict[str, Any]:
        if not isinstance(symbol, str) or re.fullmatch(r"\d{6}", symbol.strip()) is None:
            raise ValueError("代码必须是6位数字")
        if name is not None and not isinstance(name, str):
            raise ValueError("名称必须是字符串")
        normalized_symbol = symbol.strip()
        normalized_name = name.strip() if isinstance(name, str) else ""
        if len(normalized_name) > 50:
            raise ValueError("名称不能超过50个字符")
        item = {
            "symbol": normalized_symbol,
            "name": normalized_name or normalized_symbol,
            "grid_width_pct": constants.DEFAULT_GRID_WIDTH_PCT,
            "enabled": True,
        }
        with self.watchlist_lock:
            watchlist = load_watchlist(self.watchlist_path)
            if any(current.symbol == normalized_symbol for current in watchlist):
                raise FileExistsError(f"代码已在监控列表中: {normalized_symbol}")
            self._atomic_write_watchlist([*watchlist, item])
        return item

    def _atomic_write_watchlist(self, records: list[Any]) -> None:
        payload = [
            {
                "symbol": item.symbol,
                "name": item.name,
                "grid_width_pct": item.grid_width_pct,
                "enabled": item.enabled,
            } if not isinstance(item, dict) else item
            for item in records
        ]
        path = Path(self.watchlist_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="\n", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump({"watchlist": payload}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], application: MonitorApplication):
        super().__init__(address, MonitorRequestHandler)
        self.application = application

    def server_close(self) -> None:
        self.application.stop_refresh()
        super().server_close()


class MonitorRequestHandler(BaseHTTPRequestHandler):
    server: MonitorServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._send(HTTPStatus.OK, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/snapshot":
            self._snapshot()
        elif path == "/api/events":
            self._events()
        elif path == "/api/backtest":
            self._backtest()
        elif path == "/api/alerts":
            self._alerts()
        elif path == "/api/history/dates":
            self._history_dates()
        elif path == "/api/history/quotes":
            self._history_quotes()
        elif path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok", "mode": "MONITOR_ONLY"})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/watchlist":
            self._add_watch_item()
        else:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {
                "error": "read_only",
                "message": "本服务仅允许写入监控列表，不提供交易接口",
            })

    def log_message(self, format: str, *args: object) -> None:
        return

    def _add_watch_item(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("请求体大小无效")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是对象")
            item = self.server.application.add_watch_item(
                payload.get("symbol"), payload.get("name"),
            )
            self._json(HTTPStatus.CREATED, {"item": item})
        except FileExistsError as error:
            self._json(HTTPStatus.CONFLICT, {"error": "duplicate", "message": str(error)})
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "message": str(error)})
        except OSError as error:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})

    def _snapshot(self) -> None:
        try:
            self._json(HTTPStatus.OK, self.server.application.snapshot())
        except (ValueError, OSError) as error:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})

    def _backtest(self) -> None:
        try:
            self._json(HTTPStatus.OK, self.server.application.backtest())
        except (ValueError, OSError) as error:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})

    def _history_dates(self) -> None:
        path = self.server.application.history_path
        dates = QuoteHistoryStore(path).available_dates() if path is not None else []
        self._json(HTTPStatus.OK, {"dates": dates, "read_only": True})

    def _history_quotes(self) -> None:
        path = self.server.application.history_path
        query = parse_qs(urlsplit(self.path).query)
        trading_date = query.get("date", [None])[0]
        symbol = query.get("symbol", [None])[0]
        if path is None or trading_date is None or re.fullmatch(r"\d{4}-\d{2}-\d{2}", trading_date) is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_date"})
            return
        records = QuoteHistoryStore(path).query(trading_date, symbol)
        self._json(HTTPStatus.OK, {"date": trading_date, "symbol": symbol, "records": records, "read_only": True})

    def _alerts(self) -> None:
        if self.server.application.alert_history_path is None:
            self._json(HTTPStatus.OK, {"items": []})
            return
        query = parse_qs(urlsplit(self.path).query)
        try:
            limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_limit"})
            return
        items = AlertHistoryStore(self.server.application.alert_history_path).query(
            query.get("date", [None])[0], query.get("symbol", [None])[0],
            query.get("action", [None])[0], limit,
        )
        self._json(HTTPStatus.OK, {"items": items, "read_only": True})

    def _events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for _ in range(60):
                try:
                    payload = self.server.application.snapshot()
                    event = "snapshot"
                except (ValueError, OSError) as error:
                    payload = {"error": str(error)}
                    event = "monitor-error"
                content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                self.wfile.write(f"event: {event}\ndata: {content}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(5)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)


def create_server(
    host: str,
    port: int,
    quotes_path: Path,
    watchlist_path: Path,
    history_path: Path | None = None,
    collector: Any | None = None,
    refresh_interval: float = 5.0,
    alert_history_path: Path | None = None,
    metadata_path: Path | None = None,
    calendar_path: Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> MonitorServer:
    application = MonitorApplication(
        quotes_path=quotes_path,
        watchlist_path=watchlist_path,
        history_path=history_path,
        collector=collector,
        refresh_interval=refresh_interval,
        alert_history_path=alert_history_path,
        metadata_path=metadata_path,
        calendar_path=calendar_path,
        **({"clock": clock} if clock is not None else {}),
    )
    server = MonitorServer((host, port), application)
    application.start_refresh()
    return server


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>本地做T监控</title>
<style>
:root{color-scheme:dark;--bg:#08111f;--panel:#111d2e;--line:#26364e;--text:#e6edf7;--muted:#8fa1b8;--up:#ff5964;--down:#32d296;--wait:#f4bf4f;--stale:#ff3b30}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#12233b,var(--bg) 44%);color:var(--text);font:14px system-ui,"Microsoft YaHei",sans-serif}.shell{max-width:1380px;margin:auto;padding:28px 18px}header{display:flex;justify-content:space-between;gap:16px;align-items:end;margin-bottom:18px}h1{font-size:26px;margin:0 0 6px}.sub,.time,.hint{color:var(--muted)}.badge{display:inline-block;border:1px solid #365271;border-radius:99px;padding:5px 10px;color:#9ac8ff}.status{display:flex;align-items:center;gap:8px}.dot{width:9px;height:9px;border-radius:50%;background:var(--wait)}.dot.live{background:var(--down);box-shadow:0 0 12px var(--down)}.status.stale{color:var(--stale);font-weight:700}.dot.stale{background:var(--stale);box-shadow:0 0 14px var(--stale)}#errors,#form-error{color:#ff9b9b;margin:8px 0}.layout{display:grid;grid-template-columns:280px minmax(0,1fr);gap:18px;align-items:start}.card,.sidebar{background:linear-gradient(145deg,#132238,#0d1727);border:1px solid var(--line);border-radius:14px;padding:17px;box-shadow:0 12px 40px #0004}.sidebar{position:sticky;top:18px;padding:14px}.sidebar-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}.sidebar h2{font-size:17px;margin:0}.watch-count{color:var(--muted);font-size:12px}.watch-list{display:grid;gap:6px;max-height:52vh;overflow:auto;margin-bottom:14px}.watch-item{width:100%;display:flex;align-items:center;justify-content:space-between;gap:8px;border:1px solid transparent;border-radius:9px;background:#081321;color:var(--text);padding:9px 10px;text-align:left;cursor:pointer}.watch-item:hover{border-color:#365271}.watch-item.active{border-color:#5c8fc5;background:#162b45}.watch-item-main{min-width:0}.watch-item-name{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:700}.watch-item-symbol{display:block;color:var(--muted);font-size:11px;margin-top:2px}.watch-item-state{flex:none;width:8px;height:8px;border-radius:50%;background:var(--down)}.watch-item-state.missing{background:var(--wait)}.sidebar-controls{display:flex;align-items:center;justify-content:space-between;gap:8px;border-top:1px solid var(--line);padding-top:12px}.missing-toggle{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px}.compact-form{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) auto;gap:6px;margin-top:10px}.compact-form input{min-width:0;width:100%;border:1px solid var(--line);border-radius:7px;background:#081321;color:var(--text);padding:8px;font:inherit;outline:none}.compact-form input:focus{border-color:#5c8fc5}.compact-form button{border:0;border-radius:7px;background:#2f7ed8;color:white;padding:8px 10px;font:700 13px inherit;cursor:pointer}.compact-form button:disabled{cursor:wait;opacity:.6}.history-picker{display:grid;gap:5px;margin-top:12px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}.history-picker select{width:100%;border:1px solid var(--line);border-radius:7px;background:#081321;color:var(--text);padding:8px}.detail{min-width:0;overflow-anchor:none}.top{display:flex;justify-content:space-between;gap:12px}.symbol{color:var(--muted);font-size:12px}.name{font-size:18px;font-weight:700;margin-top:2px}.price{text-align:right;font:700 24px ui-monospace,monospace}.pct{font:13px ui-monospace,monospace}.up{color:var(--up)}.down{color:var(--down)}.wait{color:var(--wait)}.signal{margin:14px 0 8px;font-weight:700}.golden-alert{display:flex;align-items:center;gap:10px;margin:10px 0 12px;padding:11px 13px;border:1px solid #f4bf4f;border-radius:9px;background:#3b2b0c;color:#ffd76a;font-weight:700;box-shadow:0 0 18px #f4bf4f26}.golden-alert strong{color:#fff}.regime-alert{margin:10px 0;padding:10px 12px;border:1px solid #365271;border-radius:9px;background:#0b1d31;font-weight:700}.regime-alert.uptrend{border-color:#ff5964;color:#ff9da5}.regime-alert.downtrend{border-color:#32d296;color:#74f0c4}.regime-alert.range{border-color:#f4bf4f;color:#ffd76a}.regime-alert.uncertain{color:#b8c5d8}.golden-alert.buy{border-color:var(--down);background:#0b302b;color:#74f0c4}.golden-alert.sell{border-color:var(--up);background:#3a1820;color:#ff9da5}.watch-item.opportunity{border-color:#f4bf4f;box-shadow:inset 3px 0 #f4bf4f}.watch-item-state.opportunity{background:#f4bf4f;box-shadow:0 0 10px #f4bf4f}.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px}.meta div{background:#081321;border-radius:8px;padding:8px}.meta span{display:block;color:var(--muted);font-size:11px;margin-bottom:3px}.market-time{margin-top:16px}.market-time.stale{color:var(--stale);font-weight:700}.stale-banner{display:none;color:var(--stale);font-weight:700;margin:10px 0}.stale-banner.visible{display:block}.backtest,.alerts{border-top:1px solid var(--line);margin-top:16px;padding-top:14px}.backtest h3,.alerts h3{font-size:15px;margin:0 0 8px}.alert-list{display:grid;gap:6px;max-height:240px;overflow:auto}.alert-row{background:#081321;border-radius:8px;padding:8px;font-size:12px}.alert-row strong{color:#ffd76a}.alert-row span{color:var(--muted);display:block;margin-top:3px}svg{width:100%;height:190px;display:block}.price-line{fill:none;stroke:#fff;stroke-width:2;vector-effect:non-scaling-stroke}.average-line{fill:none;stroke:#f4d03f;stroke-width:2;vector-effect:non-scaling-stroke}.zero-line{stroke:#6f8098;stroke-dasharray:4 4}.grid-line{stroke:#365271;stroke-dasharray:2 5}.empty{padding:40px;text-align:center;color:var(--muted)}@media(max-width:850px){.layout{grid-template-columns:1fr}.sidebar{position:static}.watch-list{max-height:260px}}@media(max-width:600px){header{align-items:start;flex-direction:column}.meta{grid-template-columns:1fr 1fr}.compact-form{grid-template-columns:1fr auto}.compact-form #watch-name{grid-column:1/-1;grid-row:2}}
</style>
</head>
<body><main class="shell"><header><div><h1>本地做T监控</h1><div class="sub">阈值提醒 · JSON 行情 · 只读交易</div></div><div><span class="badge">仅监控，不自动交易</span><div id="connection-status" class="status"><i id="dot" class="dot"></i><span id="status">正在连接</span></div></div></header><div id="errors"></div><div id="stale-banner" class="stale-banner" role="alert">当前行情数据已过期，请勿按对应价格操作</div><div class="layout"><aside class="sidebar"><div class="sidebar-head"><h2>已监控</h2><span id="watch-count" class="watch-count">0 项</span></div><nav id="watch-list" class="watch-list" aria-label="已监控标的"><div class="empty">正在载入</div></nav><div class="sidebar-controls"><strong>添加标的</strong><label class="missing-toggle"><input id="show-missing" type="checkbox" checked>显示无行情</label></div><form id="watch-form" class="compact-form"><input id="watch-symbol" name="symbol" inputmode="numeric" maxlength="6" pattern="[0-9]{6}" placeholder="代码" aria-label="代码" required><input id="watch-name" name="name" maxlength="50" placeholder="名称（可选）" aria-label="名称"><button id="watch-submit" type="submit">添加</button></form><div id="form-error" role="alert"></div><div class="hint">仅保存监控标的，不会提交任何交易。</div><label class="history-picker">历史行情日期<select id="history-date"><option value="">实时行情</option></select></label></aside><section id="detail" class="detail"><div class="card empty">正在载入行情</div></section></div><div id="refresh-time" class="time">页面刷新时间：尚未刷新</div></main>
<script>
const detail=document.querySelector('#detail'),watchList=document.querySelector('#watch-list'),watchCount=document.querySelector('#watch-count'),showMissing=document.querySelector('#show-missing'),connectionStatus=document.querySelector('#connection-status'),statusNode=document.querySelector('#status'),dot=document.querySelector('#dot'),errors=document.querySelector('#errors'),refreshTimeNode=document.querySelector('#refresh-time'),staleBanner=document.querySelector('#stale-banner'),watchForm=document.querySelector('#watch-form'),watchSymbol=document.querySelector('#watch-symbol'),watchName=document.querySelector('#watch-name'),watchSubmit=document.querySelector('#watch-submit'),formError=document.querySelector('#form-error'),historyDate=document.querySelector('#history-date');
const STALE_AFTER_MS=60000;
const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=v=>v==null?'—':Number(v).toFixed(3),pct=v=>v==null?'—':(Number(v)*100).toFixed(2)+'%';
let latestData=null,backtests=new Map(),selectedSymbol=null,alertHistoryCache=new Map(),dailyHistoryCache=new Map(),alertRequestSequence=0,dailyRequestSequence=0;
function chart(item){const points=item.points,w=720,h=190,p=10,values=points.flatMap(x=>[Number(x.price),Number(x.average_price)]).concat([Number(item.previous_close),Number(item.upper_grid_price),Number(item.lower_grid_price)]),lo=Math.min(...values),hi=Math.max(...values),span=hi-lo||1,x=i=>p+i*(w-p*2)/Math.max(points.length-1,1),y=v=>h-p-(v-lo)*(h-p*2)/span,path=key=>points.map((q,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(q[key]).toFixed(1)).join(' '),line=(cls,value)=>`<line class="${cls}" x1="${p}" y1="${y(value)}" x2="${w-p}" y2="${y(value)}"/>`,markers=(item.trade_markers||[]).map(marker=>{const index=points.findIndex(point=>point.timestamp===marker.timestamp);if(index<0)return '';const color=marker.type==='B'?'#32d296':'#ff5964',offset=marker.type==='B'?14:-4;return `<g class="trade-marker ${marker.type==='B'?'buy':'sell'}"><circle cx="${x(index).toFixed(1)}" cy="${y(marker.price).toFixed(1)}" r="4" fill="${color}"/><text x="${x(index).toFixed(1)}" y="${(y(marker.price)+offset).toFixed(1)}" text-anchor="middle" fill="${color}" font-size="12" font-weight="700">${marker.type}</text></g>`}).join('');return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-label="分时图，白线实时价，黄线均价，B为买入观察点，S为卖出观察点">${line('grid-line',item.upper_grid_price)}${line('zero-line',item.previous_close)}${line('grid-line',item.lower_grid_price)}<path class="average-line" d="${path('average_price')}"/><path class="price-line" d="${path('price')}"/>${markers}</svg>`}
function backtestSummary(symbol){const data=backtests.get(symbol);if(!data)return '<section class="backtest"><h3>独立回测</h3><div class="meta"><div><span>状态</span>正在计算</div></div></section>';if(data.status==='MISSING_QUOTE')return '<section class="backtest"><h3>独立回测</h3><div class="meta"><div><span>状态</span>MISSING_QUOTE</div><div><span>交易次数</span>—</div><div><span>累计收益</span>—</div><div><span>最大回撤</span>—</div></div></section>';return `<section class="backtest"><h3>独立回测</h3><div class="meta"><div><span>执行方式</span>NEXT_POINT</div><div><span>交易次数</span>${data.trade_count}</div><div><span>累计收益</span>${pct(data.cumulative_return)}</div><div><span>最大回撤</span>${pct(data.maximum_drawdown)}</div><div><span>初始资金</span>${num(data.initial_capital_cny)}</div><div><span>期末权益</span>${num(data.ending_value_cny)}</div></div></section>`}
function renderNav(items){watchCount.textContent=items.length+' 项';watchList.innerHTML=items.map(item=>{const marketAt=item.timestamp?new Date(item.timestamp):null,fresh=item.status==='OK'&&Number.isFinite(marketAt&&marketAt.getTime())&&Date.now()-marketAt.getTime()<=STALE_AFTER_MS,opportunity=fresh&&(item.action==='BUY_CANDIDATE'||item.action==='SELL_CANDIDATE');return `<button type="button" class="watch-item${item.symbol===selectedSymbol?' active':''}${opportunity?' opportunity':''}" data-symbol="${esc(item.symbol)}" aria-current="${item.symbol===selectedSymbol?'true':'false'}"><span class="watch-item-main"><span class="watch-item-name">${esc(item.name)}</span><span class="watch-item-symbol">${esc(item.symbol)}</span></span><i class="watch-item-state${item.status==='MISSING_QUOTE'?' missing':''}${opportunity?' opportunity':''}"></i></button>`}).join('')||'<div class="empty">监控列表为空</div>'}
function render(data){latestData=data;const refreshedAt=new Date(),allItems=data.items||[],items=showMissing.checked?allItems:allItems.filter(item=>item.status!=='MISSING_QUOTE');errors.textContent=(data.errors||[]).join(' · ');if(!items.some(item=>item.symbol===selectedSymbol))selectedSymbol=items.length?items[0].symbol:null;renderNav(items);const item=items.find(current=>current.symbol===selectedSymbol);if(!item){detail.innerHTML=`<div class="card empty">${allItems.length?'无可显示行情，请开启“显示无行情”':'监控列表为空'}</div>`;staleBanner.classList.remove('visible');connectionStatus.classList.remove('stale');statusNode.textContent='实时监控中';dot.classList.add('live');dot.classList.remove('stale');refreshTimeNode.textContent='页面刷新时间：'+refreshedAt.toLocaleString();return}const marketAt=item.timestamp?new Date(item.timestamp):null,stale=item.status==='OK'&&(!Number.isFinite(marketAt.getTime())||refreshedAt.getTime()-marketAt.getTime()>STALE_AFTER_MS),candidate=!stale&&(item.action==='BUY_CANDIDATE'||item.action==='SELL_CANDIDATE'),displayLabel=candidate?'做T候选':item.action==='DEVIATION_OBSERVE'?'偏离观察':item.label,cls=item.action==='SELL_CANDIDATE'?'up':item.action==='BUY_CANDIDATE'?'down':'wait',move=item.change_pct!=null&&item.change_pct>=0?'up':'down',missing=item.status==='MISSING_QUOTE',candidateAlert=candidate?`<div class="golden-alert ${item.action==='BUY_CANDIDATE'?'buy':'sell'}" role="alert"><strong>做T候选</strong></div>`:'';const regimeClass=item.regime_state==='UPTREND'?'uptrend':item.regime_state==='DOWNTREND'?'downtrend':item.regime_state==='RANGE'?'range':'uncertain';const regimeAlert=`<div class="regime-alert ${regimeClass}" role="status"><strong>${esc(item.regime_state==='UPTREND'?'上涨趋势日':item.regime_state==='DOWNTREND'?'下跌趋势日':item.regime_state==='RANGE'?'震荡日':'状态未知')}</strong><span> · ${esc(item.regime_label||'状态未确认，暂停做T')}</span></div>`;detail.innerHTML=`<article class="card" data-symbol="${esc(item.symbol)}"><div class="top"><div><div class="symbol">${esc(item.symbol)}</div><div class="name">${esc(item.name)}</div></div><div><div class="price">${num(item.price)}</div><div class="pct ${move}">${pct(item.change_pct)}</div></div></div><div class="signal ${cls}">${esc(item.status)} · ${esc(displayLabel)}</div>${regimeAlert}${candidateAlert}${missing?'<div class="empty">行情缺失，指标不可用</div>':chart(item)}<div class="meta"><div><span>黄线均价</span>${num(item.average_price)}</div><div><span>昨收零轴</span>${num(item.previous_close)}</div><div><span>白黄偏离</span>${item.white_yellow_deviation_grids==null?'—':num(item.white_yellow_deviation_grids)+'格'}</div><div><span>离昨收</span>${item.previous_close_distance_grids==null?'—':num(item.previous_close_distance_grids)+'格'}</div><div><span>快速上冲</span>${item.fast_rise_grids==null?'—':num(item.fast_rise_grids)+'格'}</div><div><span>格宽</span>${pct(item.grid_width_pct)}</div><div><span>上格</span>${num(item.upper_grid_price)}</div><div><span>下格</span>${num(item.lower_grid_price)}</div></div><section class="alerts"><h3>按日历史行情</h3><div id="daily-history" class="alert-list">${dailyHistoryCache.get(`${historyDate.value}|${item.symbol}`)||'<div class="hint">选择日期后查看当日分钟行情</div>'}</div></section><section class="alerts"><h3>提示追溯</h3><div id="alert-list" class="alert-list">${alertHistoryCache.get(item.symbol)||'<div class="hint">正在载入提示历史</div>'}</div></section><div class="time market-time${stale?' stale':''}">行情数据时间：${marketAt&&Number.isFinite(marketAt.getTime())?marketAt.toLocaleString():'—'}</div>${backtestSummary(item.symbol)}</article>`;refreshTimeNode.textContent='页面刷新时间：'+refreshedAt.toLocaleString();staleBanner.classList.toggle('visible',stale);connectionStatus.classList.toggle('stale',stale);statusNode.textContent=stale?'当前行情已过期':'实时监控中';dot.classList.toggle('live',!stale);dot.classList.toggle('stale',stale);loadAlerts();loadDailyHistory()}
function failed(message){statusNode.textContent=message;dot.classList.remove('live','stale');connectionStatus.classList.remove('stale')}
async function loadAlerts(){const symbol=selectedSymbol;if(!symbol)return;const sequence=++alertRequestSequence;try{const response=await fetch(`/api/alerts?symbol=${encodeURIComponent(symbol)}&limit=30`,{cache:'no-store'});if(!response.ok)throw new Error('提示历史不可用');const data=await response.json(),html=(data.items||[]).map(item=>`<div class="alert-row"><strong>${esc(item.action)} · ${esc(item.symbol)}</strong><span>${esc(item.timestamp||item.recorded_at||'—')} · ${esc(item.label||'')}</span><span>策略 ${esc(item.strategy_version||'T_V1')} · 偏离 ${item.deviation_pct==null?'—':pct(item.deviation_pct)} · 状态 ${esc(item.regime_state||item.trend_state||'—')}</span></div>`).join('')||'<div class="hint">当前标的暂无历史提示</div>';if(sequence!==alertRequestSequence||symbol!==selectedSymbol)return;alertHistoryCache.set(symbol,html);const node=document.querySelector('#alert-list');if(node)node.innerHTML=html}catch(error){if(sequence!==alertRequestSequence||symbol!==selectedSymbol)return;const html=`<div class="hint">${esc(error.message)}</div>`;alertHistoryCache.set(symbol,html);const node=document.querySelector('#alert-list');if(node)node.innerHTML=html}}
async function loadHistoryDates(){try{const selected=historyDate.value,response=await fetch('/api/history/dates',{cache:'no-store'}),data=await response.json();historyDate.innerHTML='<option value="">实时行情</option>'+data.dates.map(date=>`<option value="${esc(date)}">${esc(date)}</option>`).join('');if((data.dates||[]).includes(selected))historyDate.value=selected;loadDailyHistory()}catch(error){failed(error.message)}}
async function loadDailyHistory(){const node=document.querySelector('#daily-history');if(!node||!historyDate.value||!selectedSymbol){if(node)node.innerHTML='<div class="hint">选择日期后查看当日分钟行情</div>';return}try{const response=await fetch(`/api/history/quotes?date=${encodeURIComponent(historyDate.value)}&symbol=${encodeURIComponent(selectedSymbol)}`,{cache:'no-store'}),data=await response.json();if(!response.ok)throw new Error(data.error||'历史行情不可用');const records=data.records||[],first=records[0],last=records[records.length-1];node.innerHTML=records.length?`<div class="alert-row"><strong>${esc(data.date)} · ${esc(selectedSymbol)}</strong><span>共 ${records.length} 个分钟点</span><span>${esc(first.timestamp)} → ${esc(last.timestamp)}</span><span>开 ${num(first.open||first.price)} · 收 ${num(last.price)} · 最高 ${num(Math.max(...records.map(x=>Number(x.high||x.price))))} · 最低 ${num(Math.min(...records.map(x=>Number(x.low||x.price))))}</span></div>`:'<div class="hint">该日期没有此标的行情</div>'}catch(error){node.innerHTML=`<div class="hint">${esc(error.message)}</div>`}}
  async function poll(){try{const response=await fetch('/api/snapshot',{cache:'no-store'});if(!response.ok)throw new Error('行情不可用');render(await response.json())}catch(error){failed(error.message)}}
async function loadBacktest(){try{const response=await fetch('/api/backtest',{cache:'no-store'});if(!response.ok)throw new Error('回测不可用');const data=await response.json();backtests=new Map((data.items||[]).map(item=>[item.symbol,item]));if(latestData)render(latestData)}catch(error){failed(error.message)}}
watchList.addEventListener('click',event=>{const button=event.target.closest('[data-symbol]');if(!button)return;selectedSymbol=button.dataset.symbol;render(latestData)});
historyDate.addEventListener('change',loadDailyHistory);
showMissing.addEventListener('change',()=>{if(latestData)render(latestData)});
watchForm.addEventListener('submit',async event=>{event.preventDefault();formError.textContent='';watchSubmit.disabled=true;try{const response=await fetch('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:watchSymbol.value,name:watchName.value})}),payload=await response.json();if(!response.ok)throw new Error(payload.message||payload.error||'添加失败');watchForm.reset();await poll();await loadBacktest()}catch(error){formError.textContent=error.message}finally{watchSubmit.disabled=false}});
let timer;function fallback(){if(timer)return;failed('轮询模式');poll();timer=setInterval(poll,5000)}
loadHistoryDates();loadBacktest();if(window.EventSource){const source=new EventSource('/api/events');source.addEventListener('snapshot',event=>render(JSON.parse(event.data)));source.addEventListener('monitor-error',event=>failed(JSON.parse(event.data).error));source.onerror=fallback}else fallback();
</script></body></html>"""