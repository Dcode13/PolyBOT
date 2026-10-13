from __future__ import annotations

import copy
import math
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polybot.config import Config, load_config
from polybot.strategy import crypto_market, evaluate, exit_signal, walk_book


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        self.cfg = Config().validate()
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
        self.forecast = {
            "probability": 0.90, "lower_probability": 0.85, "target_price": 0.70,
            "confidence": 0.95, "horizon_hours": 12, "calibrated": True,
            "probability_kind": "target_touch",
        }

    def evaluate(self, **overrides):
        args = dict(market=self.market, book=self.book, forecast=self.forecast,
                    fee_fraction=0.0, cash=11.0, equity=11.0, positions=[],
                    cfg=self.cfg, now=self.now)
        args.update(overrides)
        return evaluate(**args)

    def assert_rejected(self, reason, **overrides):
        result = self.evaluate(**overrides)
        self.assertFalse(result["accepted"], result)
        self.assertEqual(reason, result["reason"])

    def assert_fails_closed(self, **overrides):
        try:
            result = self.evaluate(**overrides)
        except ValueError:
            return
        self.assertFalse(result["accepted"], result)

    def position(self, **overrides):
        result = {
            "token_id": "token-yes", "event_id": "event-1", "shares": 2.5,
            "cost": 1.0, "long_term": False,
            "deadline": (self.now + timedelta(hours=12)).isoformat(),
        }
        result.update(overrides)
        return result

    def test_one_dollar_entry_stays_within_budget_and_has_net_profit_target(self):
        result = self.evaluate()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(1.0, result["budget"])
        self.assertLessEqual(result["estimated_cost"], 1.0)
        profit = result["shares"] * result["target_price"] - result["estimated_cost"]
        self.assertGreaterEqual(profit + 1e-9, 0.50)
        self.assertGreaterEqual(profit + 1e-9, result["estimated_cost"] * 0.50)

    def test_minimum_order_size_does_not_silently_increase_one_dollar_stake(self):
        self.book["min_order_size"] = "5"
        self.assert_rejected("entry_below_exchange_minimum")

    def test_low_price_can_meet_five_share_minimum_with_one_dollar(self):
        self.book.update(min_order_size="5", asks=[{"price": ".19", "size": "100"}],
                         bids=[{"price": ".18", "size": "100"}])
        self.forecast.update(probability=.995, lower_probability=.99)
        result = self.evaluate()
        self.assertTrue(result["accepted"], result)
        self.assertGreaterEqual(result["shares"], 5)
        self.assertLessEqual(result["estimated_cost"], 1)

    def test_profit_target_accounts_for_entry_and_exit_fees(self):
        fee = 0.02
        result = self.evaluate(fee_fraction=fee)
        self.assertTrue(result["accepted"], result)
        self.assertLessEqual(result["estimated_cost"], 1)
        net_profit = result["shares"] * result["target_price"] * (1 - fee) - result["estimated_cost"]
        self.assertGreaterEqual(net_profit + 1e-9, 0.50)
        self.assertGreaterEqual(net_profit + 1e-9, 0.50 * result["estimated_cost"])
        exit_book = copy.deepcopy(self.book)
        exit_book["bids"] = [{"price": result["target_price"], "size": "100"}]
        signal = exit_signal(self.position(shares=result["shares"], cost=result["estimated_cost"]),
                             exit_book, fee, self.cfg, self.now)
        self.assertEqual("take_profit", signal["reason"])

    def test_high_probability_price_cannot_meet_impossible_fifty_percent_return(self):
        self.book.update(asks=[{"price": ".90", "size": "100"}],
                         bids=[{"price": ".89", "size": "100"}])
        self.forecast.update(probability=.99, lower_probability=.98, target_price=.99)
        self.assert_rejected("profit_target_not_supported")

    def test_rejects_profit_target_above_forecast_support(self):
        self.forecast["target_price"] = .59
        self.assert_rejected("profit_target_not_supported")

    def test_crypto_excluded_from_titles_categories_tags_and_event_metadata(self):
        for key, value in (("question", "Will Bitcoin exceed its record?"),
                           ("category", "Crypto"), ("description", "Ethereum ETF approval"),
                           ("tags", [{"label": "blockchain"}]),
                           ("event_title", "Coinbase earnings"),
                           ("event_slug", "stablecoin-regulation"),
                           ("question", "Apakah pasar kripto naik?")):
            with self.subTest(key=key, value=value):
                self.assert_rejected("crypto_excluded", market={**self.market, key: value})

    def test_regular_noncrypto_categories_are_eligible_for_scanning(self):
        for category in ("Politics", "Sports", "Esports", "Finance", "Geopolitics",
                         "Technology", "Culture", "Weather", "Art"):
            with self.subTest(category=category):
                self.assertFalse(crypto_market({**self.market, "category": category}))

    def test_same_event_cannot_be_bought_again_via_another_token(self):
        self.assert_rejected("event_already_held", positions=[self.position(token_id="token-no")])

    def test_maximum_number_of_positions_is_enforced(self):
        positions = [self.position(event_id=f"other-{i}") for i in range(3)]
        self.assert_rejected("position_limit", positions=positions)

    def test_growth_requires_balance_and_lower_probability_not_point_estimate(self):
        self.forecast.update(probability=.99, lower_probability=.89)
        self.assertEqual(1, self.evaluate(cash=30, equity=30)["budget"])
        self.forecast["lower_probability"] = .90
        result = self.evaluate(cash=30, equity=30)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(5, result["budget"])
        self.assertLessEqual(result["estimated_cost"], 5)
        self.assertEqual(1, self.evaluate(cash=29, equity=29)["budget"])

    def test_growth_requires_confidence_and_calibration(self):
        self.forecast.update(probability=.99, lower_probability=.95, confidence=.89)
        self.assertEqual(1, self.evaluate(cash=30, equity=30)["budget"])
        self.forecast.update(confidence=.99, calibrated=False)
        self.assertEqual(1, self.evaluate(cash=30, equity=30)["budget"])

    def test_live_rejects_uncalibrated_and_historical_only_evidence(self):
        self.forecast["calibrated"] = False
        self.assert_rejected("live_requires_external_calibrated_forecast", live=True)
        experimental = {"target_touch_lower_bound": .99, "confidence": .99,
                        "target_price": .70, "horizon_hours": 12, "calibrated": True}
        self.assert_rejected("live_requires_external_calibrated_forecast", live=True, forecast=experimental)

    def test_target_touch_expected_value_uses_full_loss_downside(self):
        self.forecast.update(probability_kind="target_touch", lower_probability=.75)
        # 75% * $1.50 payout - $1 cost gives 12.5% expected return under full-loss downside.
        result = self.evaluate()
        self.assertTrue(result["accepted"], result)
        self.assertAlmostEqual(.125, result["edge"])
        self.assert_rejected("edge_too_small", cfg=replace(self.cfg, minimum_edge=.13))

    def test_intraday_requires_target_touch_evidence_not_resolution_probability(self):
        self.forecast["probability_kind"] = "resolution"
        self.assert_rejected("intraday_requires_target_touch_evidence")
        self.market["end_date"] = (self.now + timedelta(days=7)).isoformat()
        self.forecast["horizon_hours"] = 7 * 24
        result = self.evaluate()
        self.assertTrue(result["accepted"], result)
        self.assertTrue(result["long_term"])

    def test_total_exposure_position_risk_and_cash_reserve_caps(self):
        self.assert_rejected("position_risk_limit", equity=5, cash=5)
        self.assert_rejected("exposure_limit", positions=[self.position(event_id="other", cost=4)])
        self.assert_rejected("cash_reserve", cash=2.99)

    def test_long_horizon_is_allowed_but_capital_and_position_count_are_limited(self):
        self.market["end_date"] = (self.now + timedelta(days=29)).isoformat()
        self.forecast["horizon_hours"] = 24 * 28
        result = self.evaluate()
        self.assertTrue(result["accepted"], result)
        self.assertTrue(result["long_term"])
        self.assert_rejected("long_position_limit",
                             positions=[self.position(event_id="other", long_term=True)])
        self.forecast.update(probability=.99, lower_probability=.95)
        self.assert_rejected("long_capital_limit", cash=30, equity=30)

    def test_forecast_and_market_horizons_must_be_within_limits(self):
        for horizon in (0, -1, 721):
            with self.subTest(horizon=horizon):
                self.assert_rejected("forecast_horizon_invalid", forecast={**self.forecast, "horizon_hours": horizon})
        for hours in (-1, 721):
            with self.subTest(end_hours=hours):
                self.assert_rejected("outside_time_window", market={**self.market,
                    "end_date": (self.now + timedelta(hours=hours)).isoformat()})

    def test_empty_stale_future_crossed_and_nonfinite_books_fail_closed(self):
        cases = [({"asks": []}, "empty_book"),
                 ({"timestamp": (self.now - timedelta(seconds=61)).isoformat()}, "stale_book"),
                 ({"timestamp": (self.now + timedelta(seconds=6)).isoformat()}, "stale_book"),
                 ({"bids": [{"price": ".41", "size": "100"}]}, "wide_or_crossed_spread"),
                 ({"bids": [{"price": ".10", "size": "100"}]}, "wide_or_crossed_spread"),
                 ({"asks": [{"price": "nan", "size": "100"}]}, "invalid_book"),
                 ({"asks": [{"price": ".40", "size": "-1"}]}, "invalid_book"),
                 ({"min_order_size": 0}, "invalid_order_rules")]
        for changes, reason in cases:
            with self.subTest(changes=changes):
                self.assert_rejected(reason, book={**self.book, **changes})

    def test_asks_and_bids_must_fully_cover_the_entry_quantity(self):
        self.assert_rejected("insufficient_ask_depth", book={**self.book,
                            "asks": [{"price": ".40", "size": "2"}]})
        self.assert_rejected("insufficient_bid_depth", book={**self.book,
                            "bids": [{"price": ".39", "size": "2"}]})

    def test_book_walking_sorts_and_consumes_actual_depth(self):
        self.book["bids"] = [{"price": ".55", "size": "1.5"}, {"price": ".64", "size": "1"}]
        gross, worst = walk_book(self.book, "SELL", 2.5)
        self.assertAlmostEqual(1.465, gross)
        self.assertEqual(.55, worst)
        self.assertIsNone(walk_book(self.book, "SELL", 3))
        self.assertIsNone(walk_book(self.book, "SELL", 2.5, limit=.60))

    def test_stop_loss_uses_executable_bids_even_when_ask_suggests_profit(self):
        self.book.update(asks=[{"price": ".70", "size": "100"}],
                         bids=[{"price": ".30", "size": "100"}])
        signal = exit_signal(self.position(), self.book, 0, self.cfg, self.now)
        self.assertEqual("stop_loss", signal["reason"])
        self.assertAlmostEqual(.75, signal["estimated_proceeds"])
        self.assertEqual(.30, signal["limit_price"])

    def test_take_profit_uses_depth_weighted_bid_proceeds(self):
        self.book["bids"] = [{"price": ".64", "size": "1"}, {"price": ".55", "size": "1.5"}]
        self.assertIsNone(exit_signal(self.position(), self.book, 0, self.cfg, self.now))
        self.book["bids"] = [{"price": ".60", "size": "100"}]
        self.assertEqual("take_profit", exit_signal(self.position(long_term=True),
                         self.book, 0, self.cfg, self.now)["reason"])

    def test_deadline_exit_uses_actual_bid_and_obeys_book_freshness_and_depth(self):
        position = self.position(deadline=self.now.isoformat())
        self.assertEqual("time_exit", exit_signal(position, self.book, 0, self.cfg, self.now)["reason"])
        self.book["timestamp"] = (self.now - timedelta(seconds=61)).isoformat()
        self.assertIsNone(exit_signal(position, self.book, 0, self.cfg, self.now))
        self.book.update(timestamp=self.now.isoformat(), bids=[{"price": ".39", "size": "1"}])
        self.assertIsNone(exit_signal(position, self.book, 0, self.cfg, self.now))

    def test_nonfinite_portfolio_inputs_cannot_authorize_an_entry(self):
        for field in ("cash", "equity"):
            for value in (math.nan, math.inf, -math.inf):
                with self.subTest(field=field, value=value):
                    self.assert_fails_closed(**{field: value})
        for value in (math.nan, math.inf, -math.inf, -1):
            with self.subTest(position_cost=value):
                self.assert_fails_closed(positions=[self.position(event_id="other", cost=value)])

    def test_nonfinite_forecast_inputs_cannot_authorize_an_entry(self):
        for field in ("lower_probability", "confidence", "target_price", "horizon_hours"):
            for value in (math.nan, math.inf, -math.inf):
                with self.subTest(field=field, value=value):
                    self.assert_fails_closed(forecast={**self.forecast, field: value})

    def test_invalid_entry_and_exit_fee_cannot_authorize_an_order(self):
        for fee in (math.nan, math.inf, -math.inf, -.01, .5, 1):
            with self.subTest(fee=fee):
                self.assert_fails_closed(fee_fraction=fee)
                try:
                    result = exit_signal(self.position(deadline=self.now.isoformat()),
                                         self.book, fee, self.cfg, self.now)
                except ValueError:
                    continue
                self.assertIsNone(result)


    def test_relaxed_entry_config_accepts_more_evidence_without_raising_stake(self):
        cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.toml"))
        forecast = {**self.forecast, "lower_probability": .73, "confidence": .72}
        self.assert_rejected("insufficient_evidence", forecast=forecast)
        for live in (False, True):
            with self.subTest(live=live):
                plan = self.evaluate(cfg=cfg, forecast=forecast, live=live)
                self.assertTrue(plan["accepted"], plan)
                self.assertEqual(1, plan["budget"])
                self.assertLessEqual(plan["estimated_cost"], 1)
                self.assertGreaterEqual(plan["target_profit"], .5)
        self.assert_rejected("live_requires_external_calibrated_forecast", cfg=cfg, live=True,
                             forecast={**forecast, "calibrated": False})
        self.assert_rejected("entry_below_exchange_minimum", cfg=cfg, forecast=forecast,
                             book={**self.book, "min_order_size": 5})


class ConfigTests(unittest.TestCase):
    def test_default_configuration_matches_small_account_limits(self):
        cfg = Config().validate()
        self.assertEqual(11, cfg.starting_cash)
        self.assertEqual(1, cfg.entry_dollars)
        self.assertEqual(.5, cfg.minimum_profit)
        self.assertEqual(30, cfg.growth_balance)
        self.assertEqual(5, cfg.growth_entry_dollars)

    def test_all_numeric_configuration_fields_require_positive_finite_numbers(self):
        for field, default in asdict(Config()).items():
            if not isinstance(default, (int, float)):
                continue
            for value in (0, -1, math.nan, math.inf):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        replace(Config(), **{field: value}).validate()

    def test_numeric_configuration_rejects_strings_and_booleans(self):
        for field, default in asdict(Config()).items():
            if not isinstance(default, (int, float)):
                continue
            for value in (True, "1"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        replace(Config(), **{field: value}).validate()

    def test_counts_must_be_integer_and_growth_stake_cannot_shrink(self):
        for field in ("max_open_positions", "max_long_positions", "poll_seconds", "scan_pages", "max_candidates"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    replace(Config(), **{field: 2.5}).validate()
        with self.assertRaises(ValueError):
            replace(Config(), growth_entry_dollars=.5).validate()

    def test_unknown_config_keys_are_rejected_instead_of_silently_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text("entry_dolars = 100\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(str(path))

    def test_valid_config_file_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text('minimum_profit = 1.0\nforecast_path = "signals.json"\n', encoding="utf-8")
            cfg = load_config(str(path))
            self.assertEqual(1, cfg.minimum_profit)
            self.assertEqual("signals.json", cfg.forecast_path)
            self.assertEqual(1, cfg.entry_dollars)


if __name__ == "__main__":
    unittest.main()
