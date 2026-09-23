from datetime import date, datetime, timedelta, timezone
import importlib
import math
import unittest


START = datetime.fromisoformat("2026-09-04T10:00:00+08:00")


def points_for(prices, start=START):
    return [
        {"timestamp": (start + timedelta(minutes=index)).isoformat(), "price": price}
        for index, price in enumerate(prices)
    ]


def item_for(points):
    return {
        "symbol": "510300", "name": "合成ETF", "eligible": True,
        "health_status": "REALTIME", "timestamp_basis": "MINUTE_START",
        "timestamp": points[-1]["timestamp"] if points else None,
        "points": points,
    }


class NotificationRulesTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec("etf_rotation.notification_rules"),
            "The pure notification rules module must exist",
        )
        self.rules = importlib.import_module("etf_rotation.notification_rules")
        self.points = points_for([100, 100.1, 100.2, 100.3, 100.4, 101])
        self.item = item_for(self.points)
        self.now = START + timedelta(minutes=6)

    def evaluate(self, item=None, now=None, **kwargs):
        return self.rules.evaluate_anomaly(
            self.item if item is None else item,
            self.now if now is None else now,
            **kwargs,
        )

    def test_six_completed_minutes_compute_one_percent_up(self):
        result = self.evaluate()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["change_pct"], 1.0)
        self.assertEqual(result["direction"], "UP")
        self.assertEqual(result["timestamp"], self.points[-1]["timestamp"])
        self.assertEqual(result["price"], 101)
        self.assertTrue(result["reason"])
        self.assertNotIn("action", result)
        self.assertNotIn("shares", result)

    def test_negative_threshold_equality_is_down(self):
        self.points[-1]["price"] = 99
        result = self.evaluate()
        self.assertEqual((result["status"], result["direction"]), ("READY", "DOWN"))
        self.assertEqual(result["change_pct"], -1.0)

    def test_decimal_price_threshold_equality_is_inclusive(self):
        self.points[0]["price"] = 99
        self.points[-1]["price"] = 99.99
        self.assertEqual(self.evaluate()["direction"], "UP")

    def test_below_threshold_is_ready_without_direction(self):
        self.points[-1]["price"] = 100.9999
        result = self.evaluate()
        self.assertEqual((result["status"], result["direction"]), ("READY", "NONE"))

    def test_threshold_uses_ui_percent_units(self):
        self.assertEqual(self.evaluate(threshold_pct=2.0)["direction"], "NONE")
        self.assertEqual(self.evaluate(threshold_pct=0.5)["direction"], "UP")

    def test_latest_six_minutes_are_used_in_long_history(self):
        points = points_for([50, 100, 100, 100, 100, 100, 101])
        result = self.evaluate(item_for(points), self.now + timedelta(minutes=1))
        self.assertEqual(result["change_pct"], 1.0)

    def test_fewer_than_six_rows_are_insufficient(self):
        for count in range(6):
            with self.subTest(count=count):
                result = self.evaluate(item_for(self.points[:count]), START + timedelta(minutes=count))
                self.assertEqual(result["status"], "INSUFFICIENT")
                self.assertIsNone(result["change_pct"])
                self.assertEqual(result["direction"], "NONE")

    def test_realtime_age_is_measured_from_minute_end_at_inclusive_75_seconds(self):
        self.assertEqual(
            self.evaluate(now=self.now + timedelta(seconds=75))["status"], "READY",
        )
        self.assertEqual(
            self.evaluate(now=self.now + timedelta(seconds=75, microseconds=1))["status"],
            "UNAVAILABLE",
        )

    def test_uncompleted_and_future_minutes_are_unavailable(self):
        for earlier in (timedelta(microseconds=1), timedelta(minutes=2)):
            with self.subTest(earlier=earlier):
                self.assertEqual(self.evaluate(now=self.now - earlier)["status"], "UNAVAILABLE")

    def test_only_explicit_true_eligibility_is_accepted(self):
        for eligible in (False, 1, "true", None):
            with self.subTest(eligible=eligible):
                self.item["eligible"] = eligible
                self.assertEqual(self.evaluate()["status"], "UNAVAILABLE")

    def test_only_realtime_health_is_accepted(self):
        for health in ("DELAYED", "OUTAGE", "DATA_ERROR", "UNKNOWN", None):
            with self.subTest(health=health):
                self.item["health_status"] = health
                self.assertEqual(self.evaluate()["status"], "UNAVAILABLE")

    def test_only_minute_start_basis_is_accepted(self):
        for basis in ("MINUTE_END", "UNKNOWN", None):
            with self.subTest(basis=basis):
                self.item["timestamp_basis"] = basis
                self.assertEqual(self.evaluate()["status"], "UNAVAILABLE")

    def test_latest_point_must_match_published_item_timestamp(self):
        for timestamp in (self.points[-2]["timestamp"], None, "not-a-time"):
            with self.subTest(timestamp=timestamp):
                self.item["timestamp"] = timestamp
                self.assertEqual(self.evaluate()["status"], "UNAVAILABLE")

    def test_prices_must_be_positive_finite_nonboolean_numbers(self):
        for price in (True, False, "100", None, 0, -1, math.inf, -math.inf, math.nan, 10**1000):
            for index in (0, 2, 5):
                with self.subTest(price=repr(price)[:30], index=index):
                    points = points_for([100] * 6)
                    points[index]["price"] = price
                    self.assertEqual(self.evaluate(item_for(points))["status"], "UNAVAILABLE")

    def test_duplicate_reordered_and_gapped_points_are_not_normalized(self):
        for timestamp in (self.points[1]["timestamp"], self.points[0]["timestamp"],
                          (START + timedelta(minutes=2, seconds=30)).isoformat()):
            with self.subTest(timestamp=timestamp):
                points = points_for([100] * 6)
                points[2]["timestamp"] = timestamp
                self.assertEqual(self.evaluate(item_for(points))["status"], "UNAVAILABLE")
        points = points_for([100] * 7)
        del points[2]
        self.assertEqual(
            self.evaluate(item_for(points), self.now + timedelta(minutes=1))["status"],
            "UNAVAILABLE",
        )

    def test_bad_row_is_not_dropped_to_invent_a_valid_window(self):
        points = points_for([100] * 6)
        points.insert(3, {"timestamp": "bad", "price": 100})
        self.assertEqual(self.evaluate(item_for(points))["status"], "UNAVAILABLE")

    def test_timestamps_must_be_aware_and_aligned_to_minute_start(self):
        for timestamp in ("bad", "2026-09-04T10:02:00", "2026-09-04", 3, None,
                          "2026-09-04T10:02:01+08:00", "2026-09-04T10:02:00.001+08:00"):
            with self.subTest(timestamp=timestamp):
                points = points_for([100] * 6)
                points[2]["timestamp"] = timestamp
                self.assertEqual(self.evaluate(item_for(points))["status"], "UNAVAILABLE")

    def test_equivalent_timezones_match_and_return_shanghai_timestamp(self):
        self.item["timestamp"] = (self.now - timedelta(minutes=1)).astimezone(timezone.utc).isoformat()
        for point in self.points:
            point["timestamp"] = datetime.fromisoformat(point["timestamp"]).astimezone(timezone.utc).isoformat()
        result = self.evaluate(now=self.now.astimezone(timezone.utc))
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["timestamp"], "2026-09-04T10:05:00+08:00")

    def test_invalid_now_and_threshold_fail_closed(self):
        for now in (self.now.replace(tzinfo=None), "2026-09-04T10:06:00+08:00", None):
            with self.subTest(now=now):
                result = self.rules.evaluate_anomaly(self.item, now)
                self.assertEqual(result["status"], "UNAVAILABLE")
        for threshold in (True, None, "1", 0, -1, math.inf, math.nan, 10**1000):
            with self.subTest(threshold=repr(threshold)[:30]):
                self.assertEqual(self.evaluate(threshold_pct=threshold)["status"], "UNAVAILABLE")

    def test_malformed_item_and_point_collections_fail_closed(self):
        for item in (None, [], "bad"):
            self.assertEqual(self.rules.evaluate_anomaly(item, self.now)["status"], "UNAVAILABLE")
        for points in (None, "bad", {}, [None] * 6, [{}] * 6):
            with self.subTest(points=points):
                self.item["points"] = points
                self.assertEqual(self.evaluate()["status"], "UNAVAILABLE")

    def test_nontrading_sessions_are_inactive(self):
        for stamp in ("2026-09-04T09:29:59+08:00", "2026-09-04T12:00:00+08:00",
                      "2026-09-04T15:00:01+08:00", "2026-09-05T10:06:00+08:00"):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.evaluate(now=datetime.fromisoformat(stamp))["status"], "INACTIVE")
        self.assertEqual(self.evaluate(closed_dates={date(2026, 9, 4)})["status"], "INACTIVE")

    def test_old_date_and_lunch_window_are_unavailable(self):
        self.assertEqual(self.evaluate(now=self.now + timedelta(days=3))["status"], "UNAVAILABLE")
        points = points_for([100] * 3, datetime.fromisoformat("2026-09-04T11:27:00+08:00"))
        points += points_for([102] * 3, datetime.fromisoformat("2026-09-04T13:00:00+08:00"))
        self.assertEqual(
            self.evaluate(item_for(points), datetime.fromisoformat("2026-09-04T13:03:00+08:00"))["status"],
            "UNAVAILABLE",
        )

    def test_session_final_completed_minute_remains_usable_at_close(self):
        for start in ("2026-09-04T11:24:00+08:00", "2026-09-04T14:54:00+08:00"):
            with self.subTest(start=start):
                beginning = datetime.fromisoformat(start)
                points = points_for([100] * 5 + [101], beginning)
                self.assertEqual(
                    self.evaluate(item_for(points), beginning + timedelta(minutes=6))["status"], "READY",
                )


class AnomalyReplayTests(unittest.TestCase):
    def setUp(self):
        self.rules = importlib.import_module("etf_rotation.notification_rules")
        self.assertTrue(
            callable(getattr(self.rules, "replay_anomalies", None)),
            "Read-only anomaly replay must be implemented",
        )

    def replay_changes(self, changes, **kwargs):
        # Five seed bars; each following price encodes its exact five-minute
        # change independently of earlier rolling-window changes.
        prices = [100.0] * 5
        for change in changes:
            prices.append(prices[-5] * (1.0 + change / 100.0))
        return self.rules.replay_anomalies(points_for(prices), **kwargs)

    def test_empty_replay_has_no_samples_or_events(self):
        result = self.rules.replay_anomalies([])
        self.assertEqual(
            {key: result[key] for key in ("raw_crossings", "merged_events", "invalid_samples", "sample_count")},
            {"raw_crossings": 0, "merged_events": 0, "invalid_samples": 0, "sample_count": 0},
        )

    def test_each_sample_clock_is_its_own_completed_minute_end(self):
        result = self.rules.replay_anomalies(points_for([100] * 5 + [101]))
        self.assertEqual(result["raw_crossings"], 1)
        self.assertEqual(result["merged_events"], 1)
        self.assertEqual(result["invalid_samples"], 5)
        self.assertEqual(result["sample_count"], 6)
        self.assertEqual(result["events"][0]["timestamp"], "2026-09-04T10:05:00+08:00")
        self.assertEqual(result["events"][0]["direction"], "UP")

    def test_persistent_crossing_does_not_repeat_after_cooldown(self):
        result = self.replay_changes([2] * 70)
        self.assertEqual(result["raw_crossings"], 70)
        self.assertEqual(result["merged_events"], 1)
        self.assertEqual(result["invalid_samples"], 5)

    def test_fall_below_hysteresis_rearms_but_cooldown_suppresses_reentry(self):
        result = self.replay_changes([2, 0, 2])
        self.assertEqual(result["raw_crossings"], 2)
        self.assertEqual(result["merged_events"], 1)

    def test_exact_80_percent_boundary_does_not_rearm(self):
        result = self.rules.replay_anomalies(
            points_for([100] * 5 + [102, 100.8, 102]), cooldown_minutes=0,
        )
        self.assertEqual(result["raw_crossings"], 2)
        self.assertEqual(result["merged_events"], 1)

    def test_below_80_percent_boundary_rearms(self):
        result = self.rules.replay_anomalies(
            points_for([100] * 5 + [102, 100.7999, 102]), cooldown_minutes=0,
        )
        self.assertEqual(result["merged_events"], 2)

    def test_downward_hysteresis_uses_signed_change(self):
        at_boundary = self.rules.replay_anomalies(
            points_for([100] * 5 + [98, 99.2, 98]), cooldown_minutes=0,
        )
        below_boundary = self.rules.replay_anomalies(
            points_for([100] * 5 + [98, 99.2001, 98]), cooldown_minutes=0,
        )
        self.assertEqual(at_boundary["merged_events"], 1)
        self.assertEqual(below_boundary["merged_events"], 2)

    def test_rearmed_crossing_at_exact_30_minute_boundary_is_new(self):
        result = self.replay_changes([2] + [0] * 29 + [2])
        self.assertEqual(result["raw_crossings"], 2)
        self.assertEqual(result["merged_events"], 2)

    def test_crossing_one_minute_before_cooldown_is_not_delayed_until_expiry(self):
        result = self.replay_changes([2] + [0] * 28 + [2, 2])
        self.assertEqual(result["raw_crossings"], 3)
        self.assertEqual(result["merged_events"], 1)

    def test_opposite_direction_is_independent_of_upward_cooldown(self):
        result = self.replay_changes([2, -2])
        self.assertEqual(result["merged_events"], 2)
        self.assertEqual([event["direction"] for event in result["events"]], ["UP", "DOWN"])

    def test_small_gap_restarts_window_but_preserves_cooldown(self):
        points = points_for([100] * 5 + [102])
        points += points_for([100] * 5 + [102], START + timedelta(minutes=7))
        result = self.rules.replay_anomalies(points)
        self.assertEqual(result["raw_crossings"], 2)
        self.assertEqual(result["merged_events"], 1)
        self.assertEqual(result["invalid_samples"], 10)

    def test_lunch_gap_never_bridges_the_computation(self):
        points = points_for([100] * 6, datetime.fromisoformat("2026-09-04T11:24:00+08:00"))
        points += points_for([200] * 6, datetime.fromisoformat("2026-09-04T13:00:00+08:00"))
        result = self.rules.replay_anomalies(points)
        self.assertEqual(result["raw_crossings"], 0)
        self.assertEqual(result["merged_events"], 0)
        self.assertEqual(result["invalid_samples"], 10)

    def test_day_change_never_bridges_the_computation(self):
        points = points_for([100] * 6)
        points += points_for([200] * 6, START + timedelta(days=3))
        result = self.rules.replay_anomalies(points)
        self.assertEqual(result["raw_crossings"], 0)
        self.assertEqual(result["invalid_samples"], 10)

    def test_duplicate_is_not_removed_to_invent_continuity(self):
        points = points_for([100] * 5 + [102])
        points.insert(4, dict(points[3]))
        result = self.rules.replay_anomalies(points)
        self.assertEqual(result["raw_crossings"], 0)
        self.assertEqual(result["invalid_samples"], 7)

    def test_bad_row_breaks_window_until_six_valid_rows_recover(self):
        points = points_for([100] * 5)
        points += [{"timestamp": "bad", "price": 100}]
        points += points_for([100] * 5 + [102], START + timedelta(minutes=5))
        result = self.rules.replay_anomalies(points)
        self.assertEqual(result["raw_crossings"], 1)
        self.assertEqual(result["merged_events"], 1)
        self.assertEqual(result["invalid_samples"], 11)
        self.assertEqual(result["sample_count"], 12)

    def test_reordered_row_and_bad_price_are_unavailable_samples(self):
        for bad in (None, {"timestamp": START.isoformat(), "price": 100},
                    {"timestamp": (START + timedelta(minutes=4)).isoformat(), "price": math.nan}):
            with self.subTest(bad=bad):
                points = points_for([100] * 5 + [102])
                points[4] = bad
                result = self.rules.replay_anomalies(points)
                self.assertEqual(result["raw_crossings"], 0)
                self.assertEqual(result["invalid_samples"], 6)

    def test_configured_holiday_prevents_historical_events(self):
        result = self.rules.replay_anomalies(
            points_for([100] * 5 + [102]), closed_dates={date(2026, 9, 4)},
        )
        self.assertEqual(result["raw_crossings"], 0)
        self.assertEqual(result["invalid_samples"], 6)

    def test_replay_does_not_mutate_input(self):
        points = points_for([100] * 5 + [102])
        expected = [dict(point) for point in points]
        self.rules.replay_anomalies(points)
        self.assertEqual(points, expected)

    def test_invalid_replay_options_are_rejected(self):
        for cooldown in (-1, True, "30", None, math.inf, math.nan, 10**1000):
            with self.subTest(cooldown=repr(cooldown)[:30]):
                with self.assertRaises(ValueError):
                    self.rules.replay_anomalies([], cooldown_minutes=cooldown)
        for threshold in (0, True, "1", math.nan):
            with self.subTest(threshold=threshold):
                with self.assertRaises(ValueError):
                    self.rules.replay_anomalies([], threshold_pct=threshold)
        for points in (None, {}, "bad"):
            with self.assertRaises(ValueError):
                self.rules.replay_anomalies(points)


if __name__ == "__main__":
    unittest.main()
