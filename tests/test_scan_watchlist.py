from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from polybot.config import Config
from polybot.engine import Engine
from polybot.state import State


class ScanWatchlistTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.cfg = replace(Config(), state_dir=str(root), forecast_path=str(root / "absent.json"))
        self.now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
        clock = patch("polybot.engine.utcnow", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.market = {
            "market_id": "market-1", "token_id": "token-1", "event_id": "event-1",
            "question": "Will the home team win tonight?", "outcome": "Yes",
            "category": "Sports", "active": True, "closed": False,
            "accepting_orders": True,
            "end_date": (self.now + timedelta(hours=12)).isoformat(),
            "liquidity": 2000, "volume_24h": 1000,
        }
        self.book = {
            "timestamp": self.now.isoformat(), "min_order_size": 1, "tick_size": .01,
            "asks": [{"price": .40, "size": 100}],
            "bids": [{"price": .39, "size": 100}],
        }
        self.public = Mock()
        self.public.iter_markets.return_value = [self.market]
        self.public.get_book.return_value = self.book
        self.public.get_history.return_value = []
        self.public.get_fee.return_value = {"max_fee_fraction": 0}

    def cycle(self, analysis_mode="paper"):
        state = State(Path(":memory:"), self.cfg.starting_cash)
        try:
            engine = Engine(self.cfg, self.public, state, analysis_mode=analysis_mode)
            with patch.object(engine, "_execute", side_effect=AssertionError("Scan cannot execute")):
                return engine.cycle(trade=False)
        finally:
            state.close()

    def test_missing_forecast_is_observation_only_without_an_invented_probability(self):
        for mode in ("paper", "live"):
            with self.subTest(mode=mode):
                result = self.cycle(mode)
                self.assertEqual([], result["qualified"])
                self.assertEqual([], result["orders"])
                self.assertEqual({"no_forecast": 1}, result["rejections"])
                row, = result["watchlist"]
                self.assertEqual("no_forecast", row["reason"])
                self.assertEqual(.40, row["ask"])
                self.assertAlmostEqual(.01, row["spread"])
                self.assertEqual(1, row["min_order_shares"])
                self.assertEqual(.40, row["minimum_notional"])
                self.assertFalse(any("probability" in key for key in row))

    def test_exchange_minimum_is_visible_but_never_qualified_or_funded(self):
        self.public.get_book.return_value = {**self.book, "min_order_size": 5}
        result = self.cycle()
        row, = result["watchlist"]
        self.assertEqual("entry_below_exchange_minimum", row["reason"])
        self.assertEqual(2, row["minimum_notional"])
        self.assertEqual([], result["qualified"])
        self.assertEqual([], result["orders"])
        self.assertEqual(11, result["cash"])
        self.public.get_history.assert_not_called()
        self.public.get_fee.assert_not_called()

    def test_crypto_and_stale_or_invalid_books_never_become_watchlist_rows(self):
        for book in (
            {**self.book, "timestamp": (self.now - timedelta(seconds=61)).isoformat()},
            {**self.book, "timestamp": (self.now + timedelta(seconds=6)).isoformat()},
            {**self.book, "asks": []},
            {**self.book, "bids": [{"price": .30, "size": 100}]},
        ):
            with self.subTest(book=book):
                self.public.iter_markets.return_value = [
                    self.market,
                    {**self.market, "token_id": "crypto", "question": "Will Bitcoin rise today?"},
                ]
                self.public.get_book.return_value = book
                self.public.get_book.reset_mock()
                result = self.cycle()
                self.assertEqual([], result["watchlist"])
                self.assertEqual(1, result["rejections"]["crypto_excluded"])
                self.public.get_book.assert_called_once_with("token-1")

    def test_watchlist_is_bounded_and_prioritizes_affordability_then_volume(self):
        markets = [
            {**self.market, "token_id": str(i), "volume_24h": 1000 + i}
            for i in range(15)
        ]
        self.public.iter_markets.return_value = markets
        self.public.get_book.side_effect = lambda token: {
            **self.book, "min_order_size": 1 if int(token) < 5 else 10,
        }
        result = self.cycle()
        self.assertEqual(10, len(result["watchlist"]))
        self.assertEqual(["4", "3", "2", "1", "0"],
                         [row["token_id"] for row in result["watchlist"][:5]])
        self.assertEqual(["14", "13", "12", "11", "10"],
                         [row["token_id"] for row in result["watchlist"][5:]])
        self.assertEqual({"no_forecast": 5, "entry_below_exchange_minimum": 10}, result["rejections"])
        self.assertEqual([], result["qualified"])

    def test_book_expiring_during_forecast_request_is_not_observed(self):
        clock = [self.now]

        def delayed_history(_token):
            clock[0] += timedelta(seconds=self.cfg.max_book_age_seconds + 1)
            return []

        self.public.get_history.side_effect = delayed_history
        with patch("polybot.engine.utcnow", side_effect=lambda: clock[0]):
            result = self.cycle()
        self.assertEqual({"stale_book": 1}, result["rejections"])
        self.assertEqual([], result["watchlist"])
        self.assertEqual([], result["qualified"])

    def test_market_expiring_during_fee_request_is_not_observed(self):
        clock = [self.now]
        self.public.iter_markets.return_value = [{
            **self.market, "end_date": (self.now + timedelta(seconds=1)).isoformat(),
        }]

        def delayed_fee(_token):
            clock[0] += timedelta(seconds=2)
            return {"max_fee_fraction": 0}

        self.public.get_fee.side_effect = delayed_fee
        with patch("polybot.engine.utcnow", side_effect=lambda: clock[0]):
            result = self.cycle()
        self.assertEqual({"outside_time_window": 1}, result["rejections"])
        self.assertEqual([], result["watchlist"])
        self.assertEqual([], result["qualified"])

    def test_forecast_rejection_is_preserved_and_qualified_rows_are_separate(self):
        forecast = {
            "probability": .90, "lower_probability": .85, "target_price": .50,
            "confidence": .95, "horizon_hours": 12, "remaining_horizon_hours": 12,
            "probability_kind": "target_touch", "calibrated": True,
        }
        with patch.object(Engine, "_external_forecast", return_value=forecast):
            result = self.cycle()
        self.assertEqual("profit_target_not_supported", result["watchlist"][0]["reason"])
        self.assertEqual([], result["qualified"])
        with patch.object(Engine, "_external_forecast", return_value={**forecast, "target_price": .70}):
            result = self.cycle()
        self.assertEqual(1, len(result["qualified"]))
        self.assertEqual([], result["watchlist"])
        self.assertEqual([], result["orders"])

    def test_normal_engine_does_not_create_a_watchlist_or_change_decisions(self):
        result = self.cycle(analysis_mode=None)
        self.assertNotIn("watchlist", result)
        self.assertEqual({"no_forecast": 1}, result["rejections"])
        self.assertEqual([], result["qualified"])

    def test_manual_scan_reaches_later_markets_when_nearest_books_are_unusable(self):
        self.cfg = replace(self.cfg, max_candidates=6)
        self.public.iter_markets.return_value = [
            {**self.market, "token_id": str(i),
             "end_date": (self.now + timedelta(hours=i + 1)).isoformat()}
            for i in range(20)
        ]
        self.public.get_book.side_effect = lambda token: (
            {**self.book, "asks": []} if int(token) < 10 else self.book
        )
        report = self.cycle()
        tokens = [args[0][0] for args in self.public.get_book.call_args_list]
        self.assertEqual(6, len(set(tokens)))
        self.assertEqual(["0", "1"], tokens[:2])
        self.assertIn("19", tokens)
        self.assertEqual(6, report["analyzed"])
        self.assertIn("19", [row["token_id"] for row in report["watchlist"]])
        self.assertEqual([], report["qualified"])
        self.assertEqual([], report["orders"])
        self.public.get_book.reset_mock()
        normal = self.cycle(analysis_mode=None)
        self.assertEqual([str(i) for i in range(6)],
                         [args[0][0] for args in self.public.get_book.call_args_list])
        self.assertNotIn("watchlist", normal)


if __name__ == "__main__":
    unittest.main()
