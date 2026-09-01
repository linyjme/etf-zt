from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from http.client import HTTPConnection
import json
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from etf_rotation.swing_alerts import AlertInput, SwingAlertStore
from etf_rotation.swing_service import SwingPaths
from etf_rotation.t_web import create_server

from tests.swing_helpers import metadata_fixture


ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = timezone(timedelta(hours=8))


class SwingWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.quotes_path = root / "monitor" / "quotes.json"
        self.watchlist_path = root / "monitor" / "watchlist.json"
        self.metadata_path = root / "metadata.json"
        self.calendar_path = root / "calendar.json"
        self.quotes_path.parent.mkdir(parents=True)
        self.quotes_path.write_text(json.dumps({"quotes": []}), encoding="utf-8")
        self.watchlist_path.write_text(
            json.dumps({"watchlist": [{
                "symbol": "510300", "name": "沪深300",
                "grid_width_pct": 0.002, "enabled": True,
            }]}), encoding="utf-8",
        )
        self.metadata_path.write_text(
            json.dumps(metadata_fixture(("510300", "510500"))), encoding="utf-8",
        )
        self.calendar_path.write_text(
            json.dumps({"schema_version": 1, "closed_dates": []}), encoding="utf-8",
        )
        swing_root = root / "swing-runtime"
        swing_root.mkdir()
        swing_watchlist = swing_root / "watchlist.json"
        swing_watchlist.write_text(json.dumps({
            "schema_version": 1,
            "items": [
                {"symbol": "510300", "enabled": True},
                {"symbol": "510500", "enabled": False},
            ],
        }), encoding="utf-8")
        swing_strategy = swing_root / "strategy.json"
        shutil.copyfile(ROOT / "data" / "swing" / "strategy.json", swing_strategy)
        self.swing_paths = SwingPaths(
            watchlist=swing_watchlist,
            strategy=swing_strategy,
            daily_history=swing_root / "daily_quotes.jsonl",
            portfolio_snapshot=swing_root / "portfolio.json",
            trades=swing_root / "trades.jsonl",
            alerts=swing_root / "alerts.jsonl",
            metadata=self.metadata_path,
            calendar=self.calendar_path,
            backtests=swing_root / "backtests",
        )
        self.now = datetime(2026, 9, 1, 14, 0, tzinfo=SHANGHAI)
        self.alert = SwingAlertStore(
            self.swing_paths.alerts, clock=lambda: self.now,
        ).publish_formal(AlertInput(
            trading_date=date(2026, 8, 31), symbol="510300",
            state="TRIAL_ENTRY_CANDIDATE", strategy_version="SWING_V1",
            level="YELLOW", label="试仓候选", evidence={"close": 4.0},
        ))
        self.server = create_server(
            "127.0.0.1", 0,
            quotes_path=self.quotes_path,
            watchlist_path=self.watchlist_path,
            metadata_path=self.metadata_path,
            calendar_path=self.calendar_path,
            collector=None,
            swing_paths=self.swing_paths,
            swing_collector=None,
            clock=lambda: self.now,
            swing_clock=lambda: self.now,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def _stop_server(self) -> None:
        if getattr(self, "server", None) is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server = None

    def _get(self, path: str) -> tuple[int, object, str]:
        with urlopen(self.base + path, timeout=2) as response:
            content_type = response.headers.get("Content-Type", "")
            body = response.read()
            payload = (
                json.loads(body.decode("utf-8"))
                if content_type.startswith("application/json") else body.decode("utf-8")
            )
            return response.status, payload, content_type

    def _post(
        self, path: str, payload: object = None, *, raw: bytes | None = None,
        content_type: str | None = "application/json", key: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        body = raw if raw is not None else json.dumps(
            {} if payload is None else payload,
        ).encode("utf-8")
        headers: dict[str, str] = {}
        if content_type is not None:
            headers["Content-Type"] = content_type
        if key is not None:
            headers["Idempotency-Key"] = key
        request = Request(self.base + path, data=body, headers=headers, method="POST")
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            finally:
                error.close()
        with response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _raw_status(self, request: bytes) -> int:
        host, port = self.server.server_address
        with socket.create_connection((host, port), timeout=2) as client:
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            response = client.recv(4096)
        return int(response.split(b" ", 2)[1])

    def test_swing_page_and_snapshot_are_independent_from_t_snapshot(self) -> None:
        status, page, content_type = self._get("/swing")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("指数ETF波段监控", page)
        _, swing, _ = self._get("/api/swing/snapshot")
        _, intraday, _ = self._get("/api/snapshot")
        self.assertEqual(swing["mode"], "MONITOR_ONLY")
        self.assertIn("as_of_trading_date", swing)
        self.assertNotIn("as_of_trading_date", intraday)

    def test_read_routes_and_daily_cursor_validation(self) -> None:
        for path in (
            "/api/swing/watchlist", "/api/swing/portfolio", "/api/swing/alerts",
        ):
            status, payload, content_type = self._get(path)
            self.assertEqual(status, 200)
            self.assertIsInstance(payload, dict)
            self.assertTrue(content_type.startswith("application/json"))
        status, daily, _ = self._get(
            "/api/swing/daily-quotes?symbol=510300&since=0&limit=120",
        )
        self.assertEqual(status, 200)
        self.assertEqual(daily["symbol"], "510300")
        self.assertTrue(daily["reset"])
        for query in (
            "", "?symbol=510300", "?symbol=%EF%BC%95%EF%BC%91%EF%BC%90%EF%BC%93%EF%BC%90%EF%BC%90&since=0",
            "?symbol=510300&since=-1", "?symbol=510300&since=0&extra=1",
            "?symbol=510300&since=0&since=1", "?symbol=510300&since=0&limit=0",
        ):
            with self.subTest(query=query):
                with self.assertRaises(HTTPError) as captured:
                    urlopen(self.base + "/api/swing/daily-quotes" + query, timeout=2)
                self.assertEqual(captured.exception.code, 400)
                captured.exception.close()

    def test_watchlist_only_toggles_metadata_verified_symbols(self) -> None:
        status, payload = self._post(
            "/api/swing/watchlist", {"symbol": "510500", "enabled": True},
        )
        self.assertEqual(status, 200)
        self.assertTrue(any(
            item["symbol"] == "510500" and item["enabled"]
            for item in payload["items"]
        ))
        status, _ = self._post(
            "/api/swing/watchlist", {"symbol": "588000", "enabled": True},
        )
        self.assertEqual(status, 422)
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual([item["symbol"] for item in metadata["items"]], ["510300", "510500"])

    def test_portfolio_trade_idempotency_reversal_and_conflict(self) -> None:
        status, initialized = self._post(
            "/api/swing/portfolio/initialize",
            {"name": "波段账户", "cash": 100000.0, "initial_positions": {}},
            key="account-1",
        )
        self.assertEqual(status, 201)
        self.assertEqual(initialized["event_type"], "ACCOUNT_INITIALIZED")
        trade = {
            "symbol": "510300", "side": "BUY", "shares": 100,
            "price": 10.0, "fee": 1.0,
            "executed_at": "2026-09-01T13:30:00+08:00",
        }
        status, created = self._post("/api/swing/trades", trade, key="trade-1")
        self.assertEqual(status, 201)
        status, retried = self._post("/api/swing/trades", trade, key="trade-1")
        self.assertEqual(status, 201)
        self.assertEqual(retried["event_id"], created["event_id"])
        status, conflict = self._post(
            "/api/swing/trades", dict(trade, price=10.1), key="trade-1",
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "conflict")
        status, reversed_event = self._post(
            f"/api/swing/trades/{created['event_id']}/reverse", {}, key="reverse-1",
        )
        self.assertEqual(status, 201)
        self.assertEqual(reversed_event["event_type"], "TRADE_REVERSED")
        self.assertFalse(hasattr(self.server, "broker"))

    def test_all_non_watchlist_writes_require_idempotency_key(self) -> None:
        cases = (
            ("/api/swing/portfolio/initialize", {"name": "x", "cash": 1}),
            ("/api/swing/trades", {}),
            ("/api/swing/trades/00000000-0000-4000-8000-000000000000/reverse", {}),
            (f"/api/swing/alerts/{self.alert.alert_id}/acknowledge", {}),
            (f"/api/swing/alerts/{self.alert.alert_id}/ignore", {}),
        )
        for path, payload in cases:
            with self.subTest(path=path):
                status, error = self._post(path, payload)
                self.assertEqual(status, 400)
                self.assertEqual(error["error"], "invalid_request")

    def test_alert_acknowledge_and_ignore_transitions(self) -> None:
        path = f"/api/swing/alerts/{self.alert.alert_id}/acknowledge"
        status, result = self._post(path, {}, key="ack-1")
        self.assertEqual(status, 200)
        self.assertTrue(result["acknowledged"])
        status, repeated = self._post(path, {}, key="ack-1")
        self.assertEqual(status, 200)
        self.assertEqual(repeated["alert_id"], self.alert.alert_id)
        status, ignored = self._post(
            f"/api/swing/alerts/{self.alert.alert_id}/ignore", {}, key="ignore-1",
        )
        self.assertEqual(status, 200)
        self.assertTrue(ignored["ignored"])

    def test_post_method_allowlist_and_dynamic_identifier_canonicality(self) -> None:
        for path in (
            "/swing", "/api/swing/snapshot", "/api/swing/daily-quotes",
            "/api/swing/events", "/api/swing/portfolio", "/api/swing/alerts",
        ):
            with self.subTest(path=path):
                status, error = self._post(path, {})
                self.assertEqual(status, 405)
                self.assertEqual(error["error"], "method_not_allowed")
        for path in (
            "/api/swing/nope", "/api/swing/backtest",
            "/api/swing/trades/00000000-0000-1000-8000-000000000000/reverse",
            f"/api/swing/alerts/{self.alert.alert_id.upper()}/acknowledge",
            "/api/swing/alerts/123/ignore",
        ):
            with self.subTest(path=path):
                status, error = self._post(path, {}, key="key")
                self.assertEqual(status, 404)
                self.assertEqual(error["error"], "not_found")
        with self.assertRaises(HTTPError) as captured:
            urlopen(self.base + "/api/swing/backtest?symbol=510300", timeout=2)
        self.assertEqual(captured.exception.code, 404)
        captured.exception.close()

    def test_json_body_rejects_media_type_utf8_shape_duplicates_and_nonfinite(self) -> None:
        cases = (
            ({"raw": b"{}", "content_type": None}, "media"),
            ({"raw": b"{}", "content_type": "text/plain"}, "media"),
            ({"raw": b"[]"}, "shape"),
            ({"raw": b'{"symbol":"510300","symbol":"510500","enabled":true}'}, "duplicate"),
            ({"raw": b'{"symbol":"510300","enabled":NaN}'}, "nonfinite"),
            ({"raw": b"\xff"}, "utf8"),
        )
        for kwargs, label in cases:
            with self.subTest(label=label):
                status, error = self._post("/api/swing/watchlist", **kwargs)
                self.assertEqual(status, 400)
                self.assertEqual(error["error"], "invalid_request")
        status, _ = self._post(
            "/api/swing/watchlist", {"symbol": "510500", "enabled": True},
            content_type="application/json; charset=utf-8",
        )
        self.assertEqual(status, 200)
        status, _ = self._post(
            "/api/swing/watchlist", {"symbol": "510500", "enabled": False},
            content_type="application/json; charset=utf-8; charset=utf-8",
        )
        self.assertEqual(status, 400)
        status, _ = self._post(
            "/api/swing/watchlist", raw=b"{" + b'"pad":"' + b"x" * 16384 + b'"}',
        )
        self.assertEqual(status, 400)

    def test_content_length_is_required_canonical_bounded_and_not_short(self) -> None:
        prefix = b"POST /api/swing/watchlist HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nConnection: close\r\n"
        requests = (
            prefix + b"\r\n{}",
            prefix + b"Content-Length: -1\r\n\r\n{}",
            prefix + b"Content-Length: nope\r\n\r\n{}",
            prefix + b"Content-Length: 16385\r\n\r\n{}",
            prefix + b"Content-Length: 10\r\n\r\n{}",
            prefix + b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        )
        for request in requests:
            with self.subTest(request=request[:80]):
                self.assertEqual(self._raw_status(request), 400)

    def test_alert_query_and_events_headers_are_strict(self) -> None:
        _, history, _ = self._get("/api/swing/alerts?include_retracted=true")
        self.assertIn("items", history)
        for path in (
            "/api/swing/alerts?include_retracted=1",
            "/api/swing/alerts?unknown=x",
            "/api/swing/events?since=0",
        ):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as captured:
                    urlopen(self.base + path, timeout=2)
                self.assertEqual(captured.exception.code, 400)
                captured.exception.close()
        connection = HTTPConnection(*self.server.server_address, timeout=2)
        connection.request("GET", "/api/swing/events", headers={"Last-Event-ID": "bad"})
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        connection.close()
        connection = HTTPConnection(*self.server.server_address, timeout=2)
        connection.request("GET", "/api/swing/events")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type", "").startswith("text/event-stream"))
        connection.close()

    def test_default_swing_paths_are_isolated_and_close_stops_both_services(self) -> None:
        other = create_server(
            "127.0.0.1", 0,
            quotes_path=self.quotes_path,
            watchlist_path=self.watchlist_path,
            metadata_path=self.metadata_path,
            calendar_path=self.calendar_path,
            collector=None,
            clock=lambda: self.now,
        )
        try:
            expected = self.quotes_path.parent / "swing"
            self.assertEqual(other.swing_application.paths.daily_history.parent, expected)
            self.assertNotEqual(expected, ROOT / "var" / "swing")
        finally:
            other.server_close()
        self.assertTrue(other.application.is_stopping())
        self.assertTrue(other.swing_application._stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
