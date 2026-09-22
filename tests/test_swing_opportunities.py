from __future__ import annotations

from datetime import date
import unittest

from etf_rotation.swing_opportunities import (
    OpportunityObservation,
    OpportunityStatus,
    update_opportunity,
)


def observation(
    trading_date: str,
    *,
    pullback: bool = False,
    recovery: bool = False,
) -> OpportunityObservation:
    return OpportunityObservation(
        symbol="510300",
        trading_date=date.fromisoformat(trading_date),
        pullback=pullback,
        recovery=recovery,
        conditions={"trend_ok": True, "close_ma20_atr_distance": 0.4},
        data_version="sha256:test-history",
    )


class SwingOpportunityTests(unittest.TestCase):
    def test_pullback_event_recovers_once_and_is_idempotent(self):
        first = update_opportunity(
            previous=None,
            observation=observation("2026-09-10", pullback=True),
        )
        self.assertIsNotNone(first)
        second = update_opportunity(
            previous=first,
            observation=observation("2026-09-12", recovery=True),
        )
        repeat = update_opportunity(
            previous=second,
            observation=observation("2026-09-12", recovery=True),
        )
        self.assertEqual(second.opportunity_id, repeat.opportunity_id)
        self.assertIs(second.status, OpportunityStatus.TECHNICAL_CANDIDATE)
        self.assertEqual(second.recovery_date, date(2026, 9, 12))

    def test_event_expires_after_five_completed_sessions(self):
        event = update_opportunity(
            previous=None,
            observation=observation("2026-09-10", pullback=True),
        )
        expired = update_opportunity(
            previous=event,
            observation=observation("2026-09-18"),
        )
        self.assertIs(expired.status, OpportunityStatus.EXPIRED)
        self.assertEqual(expired.cancel_reason, "RECOVERY_WINDOW_EXPIRED")

    def test_event_does_not_read_future_bars(self):
        event = update_opportunity(
            previous=None,
            observation=observation("2026-09-10", pullback=True),
        )
        self.assertIsNone(event.recovery_date)

    def test_unfinished_observation_is_rejected(self):
        unfinished = observation("2026-09-10", pullback=True)
        object.__setattr__(unfinished, "is_final", False)
        with self.assertRaises(ValueError):
            update_opportunity(previous=None, observation=unfinished)

    def test_expiry_counts_completed_trading_sessions_not_weekdays(self):
        event = update_opportunity(
            previous=None,
            observation=observation("2026-09-10", pullback=True),
            recovery_window_sessions=2,
            closed_dates={date(2026, 9, 11)},
        )
        self.assertEqual(event.expiry_date, date(2026, 9, 15))


if __name__ == "__main__":
    unittest.main()
