import json
from pathlib import Path
import tempfile
import unittest

from etf_rotation.constants import DEFAULT_GRID_WIDTH_PCT
from etf_rotation.t_monitor import load_watchlist
from etf_rotation.t_web import MonitorApplication


class DefaultConfigurationTests(unittest.TestCase):
    def test_grid_width_default_is_shared_by_loading_and_addition(self) -> None:
        self.assertEqual(DEFAULT_GRID_WIDTH_PCT, 0.002)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watchlist_path = root / "watchlist.json"
            watchlist_path.write_text(
                json.dumps([{"symbol": "510300", "name": "沪深300ETF"}]),
                encoding="utf-8",
            )

            loaded = load_watchlist(watchlist_path)
            self.assertEqual(loaded[0].grid_width_pct, 0.002)

            application = MonitorApplication(root / "quotes.json", watchlist_path)
            added = application.add_watch_item("159915", "创业板ETF")
            self.assertEqual(added["grid_width_pct"], 0.002)

            persisted = json.loads(watchlist_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["watchlist"][1]["grid_width_pct"], 0.002)


if __name__ == "__main__":
    unittest.main()
