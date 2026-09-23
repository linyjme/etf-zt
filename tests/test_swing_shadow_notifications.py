from __future__ import annotations

import unittest

from etf_rotation.notification_rules import build_alerts


class SwingShadowNotificationTests(unittest.TestCase):
    def test_shadow_candidate_is_not_a_formal_trade_alert(self) -> None:
        alerts = build_alerts(
            formal_state="PULLBACK_WATCH",
            shadow_state="TECHNICAL_CANDIDATE",
        )
        self.assertEqual(alerts.formal, [])
        self.assertEqual(len(alerts.shadow), 1)
        self.assertEqual(alerts.shadow[0].kind, "SHADOW_RESEARCH")
        self.assertEqual(alerts.shadow[0].strategy_version, "SWING_V2_SHADOW")
        self.assertFalse(alerts.shadow[0].executable)

    def test_unknown_data_suppresses_shadow_notification(self) -> None:
        alerts = build_alerts(
            data_status="UNKNOWN",
            shadow_state="TECHNICAL_CANDIDATE",
        )
        self.assertEqual(alerts.shadow, [])

    def test_shadow_notification_requires_all_research_gates(self) -> None:
        for kwargs in (
            {"data_status": "STALE"},
            {"data_status": "INSUFFICIENT"},
            {"snapshot_only": True},
            {"account_known": False},
            {"data_healthy": False},
            {"cost_ok": False},
            {"risk_ok": False},
        ):
            with self.subTest(kwargs=kwargs):
                alerts = build_alerts(
                    shadow_state="TECHNICAL_CANDIDATE", **kwargs,
                )
                self.assertEqual(alerts.shadow, [])

    def test_shadow_notification_keeps_audit_fields(self) -> None:
        alerts = build_alerts(
            symbol="512170",
            opportunity_id="op-1",
            shadow_state="TECHNICAL_CANDIDATE",
            blocked_reasons=(),
        )
        event = alerts.shadow[0]
        self.assertEqual(event.symbol, "512170")
        self.assertEqual(event.opportunity_id, "op-1")
        self.assertEqual(event.blocked_reasons, ())


if __name__ == "__main__":
    unittest.main()
