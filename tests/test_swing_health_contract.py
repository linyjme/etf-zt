from __future__ import annotations

import json
import unittest
from dataclasses import replace

from tests import test_swing_service as service_fixtures
from tests.test_swing_page import run_swing_helpers


class SwingHealthContractTests(unittest.TestCase):
    """Exercise the page with actual service output, not invented health fields."""

    def setUp(self) -> None:
        self.fixture = service_fixtures.SwingServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def page_state(self, *snapshots: dict[str, object]) -> dict[str, object]:
        return run_swing_helpers(
            "const snapshots=" + json.dumps(snapshots) + ";"
            "const state=createPageState();"
            "for(const payload of snapshots)applySnapshotPayload(state,payload,true);"
            "console.log(JSON.stringify({"
            "ready:payloadSafetyReady(state.snapshot),"
            "paused:state.safetyPaused,executable:state.selectedExecutable,"
            "copy:executionStatusCopy(state)}));"
        )

    def use_uninitialized_account(self) -> None:
        paths = self.fixture.paths
        self.fixture.paths = replace(
            paths,
            trades=paths.trades.with_name("uninitialized-trades.jsonl"),
            portfolio_snapshot=paths.portfolio_snapshot.with_name(
                "uninitialized-portfolio.json",
            ),
        )

    def test_actual_healthy_snapshot_without_strategy_health_allows_candidate(self) -> None:
        service = self.fixture.make_service()
        service.refresh_intraday()
        snapshot = service.snapshot()
        self.assertNotIn("strategy", snapshot["health"])
        self.assertEqual(snapshot["health"]["portfolio"], "OK")
        self.assertEqual(snapshot["health"]["intraday"], "REALTIME")
        self.assertEqual(snapshot["errors"], {})
        self.assertEqual(snapshot["items"][0]["execution_status"], "READY_TO_EXECUTE")

        page = self.page_state(snapshot)
        self.assertTrue(page["ready"])
        self.assertFalse(page["paused"])
        self.assertTrue(page["executable"])
        self.assertIn("手工确认", page["copy"])

    def test_actual_uninitialized_account_explains_pause_despite_realtime_data(self) -> None:
        self.use_uninitialized_account()
        service = self.fixture.make_service()
        service.refresh_intraday()
        snapshot = service.snapshot()
        self.assertEqual(snapshot["health"]["portfolio"], "UNINITIALIZED")
        self.assertEqual(snapshot["health"]["intraday"], "REALTIME")
        self.assertIsNone(snapshot["portfolio"])

        page = self.page_state(snapshot)
        self.assertFalse(page["ready"])
        self.assertTrue(page["paused"])
        self.assertFalse(page["executable"])
        self.assertIn("账户未初始化", page["copy"])
        self.assertNotIn("行情异常", page["copy"])

    def test_initializing_only_temporary_account_lifts_page_safety_pause(self) -> None:
        self.use_uninitialized_account()
        service = self.fixture.make_service()
        service.refresh_intraday()
        before = service.snapshot()
        service.initialize_portfolio("isolated-contract-test", 100_000.0, "init")
        service.refresh_intraday()
        after = service.snapshot()
        self.assertEqual(after["health"]["portfolio"], "OK")
        self.assertGreater(after["revision"], before["revision"])
        self.assertEqual(after["errors"], {})

        page = self.page_state(before, after)
        self.assertTrue(page["ready"])
        self.assertFalse(page["paused"])
        self.assertNotIn("账户未初始化", page["copy"])

    def test_actual_stale_quote_still_blocks_initialized_account(self) -> None:
        service = self.fixture.make_service(intraday_provider=lambda: {
            "generated_at": "2026-09-01T14:00:00+08:00",
            "items": [{
                "symbol": "510300", "price": 106.0,
                "timestamp": "2026-09-01T13:55:00+08:00",
                "health_status": "REALTIME",
            }],
        })
        service.refresh_intraday()
        snapshot = service.snapshot()
        self.assertEqual(snapshot["health"]["portfolio"], "OK")
        self.assertEqual(snapshot["health"]["intraday"], "STALE")

        page = self.page_state(snapshot)
        self.assertFalse(page["ready"])
        self.assertTrue(page["paused"])
        self.assertFalse(page["executable"])
        self.assertIn("过期", page["copy"])
        self.assertNotIn("账户未初始化", page["copy"])


if __name__ == "__main__":
    unittest.main()
