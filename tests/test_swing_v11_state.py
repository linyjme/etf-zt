from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from etf_rotation.swing_v11_state import (
    V11StateStore,
    advance_environment_state,
    normalize_environment_state,
    normalize_position_state,
)


class V11StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "v11_positions.json"

    def test_missing_file_loads_empty_state(self) -> None:
        state = V11StateStore(self.path).load()
        self.assertEqual(state["positions"], {})
        self.assertIsNone(state["environment"]["state"])

    def test_round_trip_normalizes_positions_and_environment(self) -> None:
        store = V11StateStore(self.path)
        store.save(
            positions={
                "510300": {
                    "entry_trading_date": "2026-09-01",
                    "stop_price_raw": 3.9,
                    "tracking_started": True,
                    "setup": "A_PULLBACK",
                    "unknown_key": "ignored",
                },
                "broken": {"stop_price_raw": 1.0},
            },
            environment={"state": "NEUTRAL", "as_of_trading_date": "2026-09-01"},
        )
        loaded = store.load()
        self.assertEqual(set(loaded["positions"]), {"510300"})
        position = loaded["positions"]["510300"]
        self.assertEqual(position["entry_trading_date"], "2026-09-01")
        self.assertEqual(position["stop_price_raw"], 3.9)
        self.assertTrue(position["tracking_started"])
        self.assertFalse(position["topup_done"])
        self.assertIsNone(position["tracking_price_raw"])
        self.assertNotIn("unknown_key", position)
        self.assertEqual(loaded["environment"]["state"], "NEUTRAL")
        self.assertIsNone(loaded["environment"]["defense_recovery_started"])
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)

    def test_invalid_position_states_are_dropped(self) -> None:
        self.assertIsNone(normalize_position_state({"entry_trading_date": "bad"}))
        self.assertIsNone(normalize_position_state("510300"))
        state = normalize_position_state({
            "entry_trading_date": "2026-09-01", "stop_price_raw": -1.0,
            "tracking_started": "yes",
        })
        self.assertIsNone(state["stop_price_raw"])
        self.assertFalse(state["tracking_started"])

    def test_unsupported_schema_fails_closed(self) -> None:
        self.path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
        with self.assertRaises(ValueError):
            V11StateStore(self.path).load()


class EnvironmentTransitionTests(unittest.TestCase):
    def test_defense_to_neutral_starts_the_recovery_window(self) -> None:
        defense = advance_environment_state(
            None, state="DEFENSE", as_of_trading_date="2026-09-01",
        )
        self.assertEqual(defense["state"], "DEFENSE")
        self.assertIsNone(defense["defense_recovery_started"])
        neutral = advance_environment_state(
            defense, state="NEUTRAL", as_of_trading_date="2026-09-02",
        )
        self.assertEqual(neutral["previous_state"], "DEFENSE")
        self.assertEqual(neutral["defense_recovery_started"], "2026-09-02")
        still_neutral = advance_environment_state(
            neutral, state="NEUTRAL", as_of_trading_date="2026-09-03",
        )
        self.assertEqual(still_neutral["defense_recovery_started"], "2026-09-02")
        self.assertEqual(still_neutral["previous_state"], "DEFENSE")
        attack = advance_environment_state(
            still_neutral, state="ATTACK", as_of_trading_date="2026-09-04",
        )
        self.assertIsNone(attack["defense_recovery_started"])
        self.assertEqual(attack["previous_state"], "NEUTRAL")

    def test_attack_to_neutral_does_not_open_a_recovery_window(self) -> None:
        attack = advance_environment_state(
            None, state="ATTACK", as_of_trading_date="2026-09-01",
        )
        neutral = advance_environment_state(
            attack, state="NEUTRAL", as_of_trading_date="2026-09-02",
        )
        self.assertIsNone(neutral["defense_recovery_started"])
        self.assertEqual(neutral["previous_state"], "ATTACK")

    def test_unknown_state_or_same_session_keeps_history(self) -> None:
        recorded = advance_environment_state(
            None, state="DEFENSE", as_of_trading_date="2026-09-01",
        )
        self.assertEqual(
            advance_environment_state(recorded, state="UNKNOWN", as_of_trading_date="2026-09-02"),
            recorded,
        )
        self.assertEqual(
            advance_environment_state(recorded, state="DEFENSE", as_of_trading_date=None),
            recorded,
        )
        self.assertEqual(
            advance_environment_state(recorded, state="DEFENSE", as_of_trading_date="2026-09-01"),
            recorded,
        )
        self.assertEqual(normalize_environment_state("garbage")["state"], None)


if __name__ == "__main__":
    unittest.main()
