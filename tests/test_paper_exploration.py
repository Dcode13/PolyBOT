from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from polybot.config import Config
from polybot.strategy import evaluate


class PaperExplorationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
        self.cfg = replace(Config(), paper_entry_mode="explore").validate()
        self.market = {
            "market_id": "market-1", "token_id": "token-yes", "event_id": "event-1",
            "question": "Will the home team win tonight?", "outcome": "Yes",
            "category": "Sports", "active": True, "closed": False,
            "accepting_orders": True,
            "end_date": (self.now + timedelta(hours=12)).isoformat(),
            "liquidity": 2000, "volume_24h": 1000,
        }
        self.book = {
            "timestamp": self.now.isoformat(), "min_order_size": "1", "tick_size": "0.01",
            "asks": [{"price": "0.40", "size": "100"}],
            "bids": [{"price": "0.39", "size": "100"}],
        }

    def evaluate(self, **overrides):
        args = dict(market=self.market, book=self.book, forecast=None,
                    fee_fraction=0.0, cash=11.0, equity=11.0, positions=[],
                    cfg=self.cfg, now=self.now)
        args.update(overrides)
        return evaluate(**args)

    def assert_rejected(self, reason, **overrides):
        result = self.evaluate(**overrides)
        self.assertFalse(result["accepted"], result)
        self.assertEqual(reason, result["reason"])

    def position(self, **overrides):
        result = {"token_id": "other-token", "event_id": "other-event",
                  "shares": 5.0, "cost": 1.0, "long_term": False}
        result.update(overrides)
        return result

    def test_missing_forecast_can_only_be_explicit_paper_exploration(self):
        result = self.evaluate()
        self.assertTrue(result["accepted"], result)
        self.assertEqual("paper_exploratory", result["entry_kind"])
        self.assertEqual("paper_exploratory", result["reason"])
        self.assertIsNone(result["probability_lower"])
        self.assertIsNone(result["confidence"])
        self.assertIsNone(result["edge"])
        self.assertEqual({}, result["forecast"])
        self.assertAlmostEqual(-0.025, result["score"])
        self.assert_rejected("no_forecast", live=True)
        self.assert_rejected("no_forecast", cfg=Config().validate())

    def test_exploration_never_increases_stake_after_balance_growth(self):
        result = self.evaluate(cash=100, equity=100)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(1.0, result["budget"])
        self.assertLessEqual(result["estimated_cost"], 1.0)

    def test_mechanical_profit_target_accounts_for_fees_without_forecast_support(self):
        fee = 0.02
        result = self.evaluate(fee_fraction=fee)
        self.assertTrue(result["accepted"], result)
        self.assertLessEqual(result["estimated_cost"], 1.0)
        net_profit = result["shares"] * result["target_price"] * (1 - fee) - result["estimated_cost"]
        self.assertGreaterEqual(net_profit + 1e-9, 0.5)
        self.assertGreaterEqual(net_profit + 1e-9, result["estimated_cost"] * 0.5)

    def test_mechanical_profit_target_must_be_below_one(self):
        self.book.update(asks=[{"price": "0.90", "size": "100"}],
                         bids=[{"price": "0.89", "size": "100"}])
        self.assert_rejected("profit_target_not_supported")

    def test_horizon_is_bounded_by_setting_and_market_closing_buffer(self):
        result = self.evaluate()
        self.assertEqual(6.0, result["horizon_hours"])
        self.assertFalse(result["long_term"])
        self.market["end_date"] = (self.now + timedelta(hours=1)).isoformat()
        result = self.evaluate()
        self.assertTrue(result["accepted"], result)
        self.assertAlmostEqual(55 / 60, result["horizon_hours"])
        self.market["end_date"] = (self.now + timedelta(minutes=5)).isoformat()
        self.assert_rejected("paper_exploration_horizon_invalid")

    def test_invalid_or_insufficient_forecast_does_not_fall_back_to_exploration(self):
        self.assert_rejected("insufficient_evidence", forecast={})
        weak = {"probability": 0.5, "lower_probability": 0.4, "confidence": 0.5,
                "target_price": 0.70, "horizon_hours": 6, "calibrated": True,
                "probability_kind": "target_touch"}
        self.assert_rejected("insufficient_evidence", forecast=weak)

    def test_valid_forecast_keeps_forecast_based_rules_and_label(self):
        forecast = {"probability": 0.9, "lower_probability": 0.85, "confidence": 0.95,
                    "target_price": 0.70, "horizon_hours": 12, "calibrated": True,
                    "probability_kind": "target_touch"}
        result = self.evaluate(forecast=forecast)
        self.assertTrue(result["accepted"], result)
        self.assertEqual("forecast", result["entry_kind"])
        self.assertEqual(0.85, result["probability_lower"])
        self.assertEqual(12, result["horizon_hours"])
        self.assertGreater(result["edge"], 0)

    def test_exchange_minimum_remains_enforced(self):
        self.book["min_order_size"] = "5"
        self.assert_rejected("entry_below_exchange_minimum")
        self.book.update(asks=[{"price": ".19", "size": "100"}],
                         bids=[{"price": ".18", "size": "100"}])
        result = self.evaluate()
        self.assertTrue(result["accepted"], result)
        self.assertGreaterEqual(result["shares"], 5)
        self.assertLessEqual(result["estimated_cost"], 1)

    def test_entry_depth_exit_depth_and_freshness_remain_enforced(self):
        self.assert_rejected("insufficient_ask_depth", book={**self.book,
                             "asks": [{"price": ".40", "size": "2"}]})
        self.assert_rejected("insufficient_bid_depth", book={**self.book,
                             "bids": [{"price": ".39", "size": "2"}]})
        self.assert_rejected("stale_book", book={**self.book,
                             "timestamp": (self.now - timedelta(seconds=61)).isoformat()})

    def test_market_filters_and_crypto_exclusion_remain_enforced(self):
        self.assert_rejected("crypto_excluded", market={**self.market, "category": "Crypto"})
        self.assert_rejected("market_not_tradable", market={**self.market, "closed": True})
        self.assert_rejected("low_liquidity", market={**self.market, "liquidity": 1})
        self.assert_rejected("low_volume", market={**self.market, "volume_24h": 1})

    def test_reserves_risk_and_inventory_limits_remain_enforced(self):
        self.assert_rejected("position_risk_limit", equity=5, cash=5)
        self.assert_rejected("cash_reserve", cash=2.99)
        self.assert_rejected("exposure_limit", positions=[self.position(cost=4)])
        self.assert_rejected("event_already_held", positions=[self.position(event_id="event-1")])
        self.assert_rejected("position_limit", positions=[self.position(event_id=str(i)) for i in range(3)])
        self.assert_rejected("invalid_inventory", positions=[self.position(shares=0)])

    def test_unknown_fee_remains_rejected(self):
        for fee in (-0.01, 0.5, float("nan")):
            with self.subTest(fee=fee):
                self.assert_rejected("unknown_fee", fee_fraction=fee)

    def test_round_trip_cost_cap_counts_relative_spread_and_fees(self):
        self.book.update(asks=[{"price": ".19", "size": "100"}],
                         bids=[{"price": ".17", "size": "100"}])
        self.assert_rejected("paper_exploration_spread_too_costly")
        self.book["bids"] = [{"price": ".18", "size": "100"}]
        self.assertTrue(self.evaluate()["accepted"])
        self.assert_rejected("paper_exploration_spread_too_costly", fee_fraction=0.03)

    def test_round_trip_cap_uses_half_stop_loss_and_never_exceeds_ten_percent(self):
        self.book["bids"] = [{"price": ".37", "size": "100"}]
        self.assertTrue(self.evaluate()["accepted"])
        self.assert_rejected("paper_exploration_spread_too_costly",
                             cfg=replace(self.cfg, stop_loss_fraction=0.10))
        self.book.update(asks=[{"price": ".19", "size": "100"}],
                         bids=[{"price": ".17", "size": "100"}])
        self.assert_rejected("paper_exploration_spread_too_costly",
                             cfg=replace(self.cfg, stop_loss_fraction=0.50))


if __name__ == "__main__":
    unittest.main()
