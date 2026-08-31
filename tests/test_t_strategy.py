import unittest
from dataclasses import replace

from etf_rotation.market_data import MarketHealth
from etf_rotation.t_strategy import CandidateContext, TStrategy
from tests.regime_fixtures import confirmed_range_quote


class TStrategyTests(unittest.TestCase):
    def test_uncertain_delayed_and_widening_deviation_are_observation_only(self) -> None:
        quote = confirmed_range_quote(previous_deviation=-0.007, current_deviation=-0.006)
        strategy = TStrategy()
        uncertain = strategy.evaluate(CandidateContext(quote, "UNCERTAIN", MarketHealth("REALTIME", 10, "行情实时"), 0.002))
        delayed = strategy.evaluate(CandidateContext(quote, "RANGE", MarketHealth("DELAYED", 100, "行情延迟"), 0.002))
        widening_quote = confirmed_range_quote(previous_deviation=-0.005, current_deviation=-0.006)
        widening = strategy.evaluate(CandidateContext(widening_quote, "RANGE", MarketHealth("REALTIME", 10, "行情实时"), 0.002))
        self.assertEqual(uncertain.action, "DEVIATION_OBSERVE")
        self.assertEqual(delayed.action, "DEVIATION_OBSERVE")
        self.assertEqual(widening.action, "DEVIATION_OBSERVE")
        self.assertIn("REGIME_NOT_RANGE", uncertain.blocked_reasons)
        self.assertIn("MARKET_NOT_REALTIME", delayed.blocked_reasons)
        self.assertIn("DEVIATION_NOT_NARROWING", widening.blocked_reasons)

    def test_confirmed_range_narrowing_and_cost_coverage_produces_candidate(self) -> None:
        quote = confirmed_range_quote(previous_deviation=-0.008, current_deviation=-0.007)
        decision = TStrategy().evaluate(CandidateContext(quote, "RANGE", MarketHealth("REALTIME", 10, "行情实时"), 0.002))
        self.assertEqual(decision.action, "BUY_CANDIDATE")
        self.assertGreater(decision.expected_net_edge_pct, 0)
        self.assertEqual(decision.blocked_reasons, ())

    def test_edge_below_round_trip_cost_is_observation_only(self) -> None:
        quote = confirmed_range_quote(previous_deviation=-0.0023, current_deviation=-0.0022, previous_close_distance=0.02)
        decision = TStrategy().evaluate(CandidateContext(quote, "RANGE", MarketHealth("REALTIME", 10, "行情实时"), 0.0007))
        self.assertEqual(decision.action, "DEVIATION_OBSERVE")
        self.assertIn("COST_NOT_COVERED", decision.blocked_reasons)

    def test_empty_points_zero_grid_and_invalid_prices_wait_safely(self) -> None:
        quote = confirmed_range_quote(previous_deviation=-0.008, current_deviation=-0.007)
        health = MarketHealth("REALTIME", 10, "行情实时")
        strategy = TStrategy()

        empty = strategy.evaluate(CandidateContext(replace(quote, points=()), "RANGE", health, 0.002))
        zero_grid = strategy.evaluate(CandidateContext(quote, "RANGE", health, 0.0))
        invalid_point = replace(quote.points[-1], price=float("nan"))
        invalid = strategy.evaluate(CandidateContext(replace(quote, points=(quote.points[-2], invalid_point)), "RANGE", health, 0.002))

        self.assertEqual(empty.action, "WAIT")
        self.assertIn("INSUFFICIENT_FINALIZED_POINTS", empty.blocked_reasons)
        self.assertEqual(zero_grid.action, "WAIT")
        self.assertIn("INVALID_GRID_WIDTH", zero_grid.blocked_reasons)
        self.assertEqual(invalid.action, "WAIT")
        self.assertIn("INVALID_PRICE_DATA", invalid.blocked_reasons)
