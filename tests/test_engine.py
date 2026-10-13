from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from polybot.config import Config
from polybot.engine import Engine
from polybot.state import State
from polybot.strategy import timestamp


class FakePublic:
    def __init__(self, markets, books, fee=0):
        self.markets = markets
        self.books = books
        self.fee = fee
        self.scans = 0

    def iter_markets(self, **kwargs):
        self.scans += 1
        yield from copy.deepcopy(self.markets)

    def get_book(self, token):
        return copy.deepcopy(self.books[token])

    def get_fee(self, token):
        return {"max_fee_fraction": self.fee}

    def get_history(self, token):
        return []

    def geoblock(self):
        return {"blocked": False}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        for module in ("polybot.engine", "polybot.state"):
            clock = patch(module + ".utcnow", return_value=self.now)
            clock.start()
            self.addCleanup(clock.stop)
        self.cfg = replace(Config(), state_dir=self.folder.name,
                           forecast_path=str(Path(self.folder.name) / "forecasts.json"))
        self.path = Path(self.folder.name) / "state.sqlite3"
        self.state = State(self.path, 11)
        self.addCleanup(lambda: self.state.close())
        self.market = {
            "market_id": "market-1", "token_id": "token-1", "event_id": "event-1",
            "question": "Will the home team win tonight?", "outcome": "Yes",
            "category": "Sports", "active": True, "closed": False,
            "accepting_orders": True,
            "end_date": (self.now + timedelta(hours=12)).isoformat(),
            "liquidity": 2000, "volume_24h": 1000,
        }
        self.book = {
            "timestamp": self.now.isoformat(), "min_order_size": "1", "tick_size": "0.01",
            "asks": [{"price": ".40", "size": "100"}],
            "bids": [{"price": ".39", "size": "100"}],
        }
        self.forecast = {
            "probability": .90, "lower_probability": .85, "target_price": .70,
            "confidence": .95, "horizon_hours": 12, "remaining_horizon_hours": 12,
            "probability_kind": "target_touch", "calibrated": True,
            "observed_at": self.now.isoformat(),
            "expires_at": (self.now + timedelta(hours=12)).isoformat(),
        }
        self.public = FakePublic([self.market], {"token-1": self.book})
        self.engine = Engine(self.cfg, self.public, self.state)
        self.engine.external = Mock()
        self.engine.external.forecast.side_effect = lambda *args: copy.deepcopy(self.forecast)
        self.engine.history_model = Mock()
        self.engine.history_model.forecast.return_value = None

    def test_paper_cycle_buys_and_records_actual_fee_adjusted_cash_and_shares(self):
        self.public.fee = .02
        report = self.engine.cycle()
        self.assertEqual("paper", report["mode"])
        self.assertEqual(1, len(report["orders"]))
        self.assertEqual("filled", report["orders"][0]["status"])
        self.assertEqual("BUY", report["orders"][0]["side"])
        position = self.state.positions()[0]
        expected_cost = position["shares"] * .40 * 1.02
        self.assertAlmostEqual(expected_cost, position["cost"])
        self.assertAlmostEqual(11 - expected_cost, report["cash"])
        self.assertLessEqual(position["cost"], 1)
        self.assertAlmostEqual(report["cash"] + position["shares"] * .39 * .98, report["equity"], places=5)
        self.assertEqual([], self.state.intents())

    def test_exploratory_paper_fill_is_capped_and_labelled_in_ledger_and_exit(self):
        self.engine.cfg = replace(self.cfg, paper_entry_mode="explore")
        self.engine.external.forecast.side_effect = None
        self.engine.external.forecast.return_value = None
        self.public.markets.append({**self.market, "token_id": "token-2", "event_id": "event-2"})
        self.public.books["token-2"] = copy.deepcopy(self.book)
        report = self.engine.cycle()
        self.assertEqual(2, len(report["qualified"]))
        self.assertTrue(all(row["probability_lower"] is None for row in report["qualified"]))
        self.assertEqual(1, len(report["orders"]))
        self.assertEqual("filled", report["orders"][0]["status"])
        self.assertEqual("paper_exploratory", report["orders"][0]["entry_kind"])
        position, = self.state.positions()
        self.assertEqual("paper_exploratory", position["entry_kind"])
        self.assertLessEqual(position["cost"], 1)
        self.assertEqual("paper_exploratory", self.state.summary()["recent_trades"][0]["entry_kind"])
        self.public.markets = []
        self.public.books[position["token_id"]]["bids"] = [{"price": .65, "size": 100}]
        self.engine.cycle()
        trade = next(row for row in self.state.summary()["recent_trades"] if row["side"] == "SELL")
        self.assertEqual("paper_exploratory", trade["entry_kind"])

    def test_live_execution_cannot_accept_exploration_even_if_given_a_plan(self):
        self.engine.live = Mock()
        result = self.engine._execute({"entry_kind": "paper_exploratory"}, "BUY", self.book, 0)
        self.assertEqual("paper_exploration_live_disabled", result["reason"])
        self.assertEqual([], self.state.intents())
        self.engine.live.place_fok.assert_not_called()

    def test_refresh_cannot_turn_two_forecast_entries_into_two_exploratory_fills(self):
        self.engine.cfg = replace(self.cfg, paper_entry_mode="explore")
        self.public.markets.append({**self.market, "token_id": "token-2", "event_id": "event-2"})
        self.public.books["token-2"] = copy.deepcopy(self.book)
        self.engine.external.forecast.side_effect = [copy.deepcopy(self.forecast), copy.deepcopy(self.forecast), None, None]
        report = self.engine.cycle()
        self.assertEqual(1, len(report["orders"]))
        self.assertEqual("paper_exploratory", report["orders"][0]["entry_kind"])
        self.assertEqual(1, report["rejections"]["paper_exploration_cycle_limit"])

    def test_scan_exploration_still_has_no_order_or_ledger_trade(self):
        self.engine.cfg = replace(self.cfg, paper_entry_mode="explore")
        self.engine.external.forecast.side_effect = None
        self.engine.external.forecast.return_value = None
        report = self.engine.cycle(trade=False)
        self.assertEqual("paper_exploratory", report["qualified"][0]["entry_kind"])
        self.assertEqual([], report["orders"])
        self.assertEqual([], self.state.summary()["recent_trades"])

    def test_scan_only_reports_qualified_entry_without_orders_or_cash_changes(self):
        report = self.engine.cycle(trade=False)
        self.assertEqual(1, len(report["qualified"]))
        self.assertEqual([], report["orders"])
        self.assertEqual([], self.state.positions())
        self.assertEqual(11, self.state.get("cash"))

    def test_analysis_time_budget_keeps_unprocessed_candidates_in_rotation(self):
        self.engine.cfg = replace(self.cfg, max_scan_seconds=1)
        with patch("polybot.engine.time.monotonic", side_effect=[0, 2]):
            report = self.engine.cycle(trade=False)
        self.assertTrue(report["analysis_budget_reached"])
        self.assertEqual(0, report["analyzed"])
        self.assertEqual(0, self.state.get("scan_cursor"))

    def test_changed_pre_submit_balance_rejects_without_creating_unknown_order(self):
        live = Mock()
        live.cash_balance.return_value = 11
        def prepare(token, side, shares, price, **kwargs):
            kwargs["on_prepared"]({"token_id": token, "side": side,
                                    "before_cash": 10.5, "before_shares": 0})
            self.fail("A changed account baseline must never reach submission")
        live.place_fok.side_effect = prepare
        self.engine.live = live
        report = self.engine.cycle()
        self.assertEqual("rejected", report["orders"][0]["status"])
        self.assertEqual([], self.state.intents())
        self.assertEqual([], self.state.positions())
        self.assertEqual(11, self.state.get("cash"))

    def test_take_profit_sells_using_actual_net_bid_proceeds(self):
        self.public.fee = .02
        self.engine.cycle()
        position = self.state.positions()[0]
        self.public.markets = []
        self.book["bids"] = [{"price": ".65", "size": "100"}]
        report = self.engine.cycle()
        self.assertEqual(1, len(report["orders"]))
        self.assertEqual("SELL", report["orders"][0]["side"])
        self.assertEqual("take_profit", report["orders"][0]["reason"])
        self.assertEqual([], self.state.positions())
        proceeds = position["shares"] * .65 * .98
        self.assertAlmostEqual(11 - position["cost"] + proceeds, self.state.get("cash"))
        sell = next(t for t in self.state.summary()["recent_trades"] if t["side"] == "SELL")
        self.assertAlmostEqual(proceeds - position["cost"], sell["pnl"])

    def test_stop_switch_prevents_scanning_and_execution(self):
        Path(self.cfg.state_dir, "STOP").touch()
        report = self.engine.cycle()
        self.assertEqual("kill_switch", report["halted"])
        self.assertEqual([], report["orders"])
        self.assertEqual(0, self.public.scans)
        self.assertEqual(11, self.state.get("cash"))

    def test_pending_journal_survives_restart_and_halts_without_resubmission(self):
        self.state.journal({"token_id": "token-1"}, "BUY", 11, 0)
        self.state.close()
        self.state = State(self.path, 500)
        self.engine.state = self.state
        report = self.engine.cycle()
        self.assertEqual("unresolved_order", report["halted"])
        self.assertEqual([], report["orders"])
        self.assertEqual(0, self.public.scans)
        self.assertEqual(11, self.state.get("cash"))

    def test_missing_position_book_blocks_new_entries_without_latching_false_loss(self):
        self.engine.cycle()
        del self.public.books["token-1"]
        scans = self.public.scans
        report = self.engine.cycle()
        self.assertEqual("position_data_unavailable", report["halted"])
        self.assertEqual([], report["orders"])
        self.assertEqual(scans, self.public.scans)
        self.assertFalse(self.state.get("day_halted"))
        self.assertFalse(self.state.get("drawdown_halted"))

    def test_invalid_forecast_or_fee_cannot_create_order_intents(self):
        self.engine.external.forecast.side_effect = ValueError("invalid forecast feed")
        report = self.engine.cycle()
        self.assertEqual([], report["orders"])
        self.assertEqual(1, len(report["errors"]))
        self.assertEqual([], self.state.intents())
        self.engine.external.forecast.side_effect = lambda *args: copy.deepcopy(self.forecast)
        self.public.fee = float("nan")
        report = self.engine.cycle()
        self.assertEqual([], report["orders"])
        self.assertEqual(1, len(report["errors"]))
        self.assertEqual(11, self.state.get("cash"))

    def test_missing_forecast_is_skipped_without_buying_from_price_alone(self):
        self.engine.external.forecast.return_value = None
        self.engine.external.forecast.side_effect = None
        report = self.engine.cycle()
        self.assertEqual({"no_forecast": 1}, report["rejections"])
        self.assertEqual([], report["orders"])

    def test_duplicate_market_rows_and_event_concentration_do_not_duplicate_orders(self):
        other = {**self.market, "token_id": "token-2"}
        self.public.markets = [self.market, self.market, other]
        self.public.books["token-2"] = copy.deepcopy(self.book)
        report = self.engine.cycle()
        self.assertEqual(2, report["scanned"])
        self.assertEqual(1, len(report["orders"]))
        self.assertEqual(1, len(self.state.positions()))
        self.assertGreaterEqual(report["rejections"].get("event_already_held", 0), 1)

    def test_refresh_rechecks_market_depth_before_submitting(self):
        original = self.public.get_book
        count = 0

        def changing_book(token):
            nonlocal count
            count += 1
            book = original(token)
            if count >= 2:
                book["asks"] = [{"price": ".40", "size": "1"}]
            return book

        self.public.get_book = changing_book
        report = self.engine.cycle()
        self.assertEqual([], report["orders"])
        self.assertEqual(1, report["rejections"]["insufficient_ask_depth"])
        self.assertEqual(11, self.state.get("cash"))

    def test_forecast_deadline_is_not_extended_when_forecast_is_already_aged(self):
        self.forecast.update(observed_at=(self.now - timedelta(hours=10)).isoformat(),
                             remaining_horizon_hours=2,
                             expires_at=(self.now + timedelta(hours=10)).isoformat())
        report = self.engine.cycle()
        self.assertEqual(1, len(report["orders"]))
        self.assertLessEqual(timestamp(self.state.positions()[0]["deadline"]), self.now + timedelta(hours=2))

    def test_existing_risk_halt_prevents_new_entries(self):
        self.state.risk_gate(10, self.cfg, self.now)
        report = self.engine.cycle()
        self.assertEqual("daily_loss_limit", report["halted"])
        self.assertEqual([], report["orders"])
        self.assertEqual(0, self.public.scans)

    def test_live_submission_exception_stays_unknown_and_is_not_retried(self):
        live = Mock()
        live.cash_balance.return_value = 11
        live.place_fok.side_effect = TimeoutError("outcome uncertain")
        self.engine.live = live
        report = self.engine.cycle()
        self.assertEqual("unresolved_order", report["halted"])
        self.assertEqual(1, live.place_fok.call_count)
        self.assertEqual("unknown", self.state.intents()[0]["status"])
        self.assertEqual(11, self.state.get("cash"))
        again = self.engine.cycle()
        self.assertEqual("unresolved_order", again["halted"])
        self.assertEqual(1, live.place_fok.call_count)

    def test_known_pending_order_reconciles_once_without_duplicate_submission(self):
        live = Mock()
        live.cash_balance.return_value = 11

        def pending_order(token, side, shares, price, **kwargs):
            kwargs["on_prepared"]({"token_id": token, "side": side, "before_cash": 11, "before_shares": 0})
            return {"status": "pending", "confirmed": False, "order_id": "pending-123"}

        live.place_fok.side_effect = pending_order
        self.engine.live = live
        report = self.engine.cycle()
        self.assertEqual("awaiting_settlement", report["halted"])
        self.assertEqual(11, self.state.get("cash"))
        live.cash_balance.return_value = 10
        live.token_balance.return_value = 2.5
        live.reconcile_order.return_value = {
            "status": "filled", "confirmed": True, "order_id": "pending-123", "cash": 1, "shares": 2.5,
        }
        self.public.markets = []
        settled = self.engine.cycle()
        self.assertNotIn("halted", settled)
        self.assertEqual(10, self.state.get("cash"))
        self.assertEqual(1, len(self.state.positions()))
        self.assertEqual([], self.state.intents())
        self.assertEqual(1, live.place_fok.call_count)
        self.assertEqual(1, live.reconcile_order.call_count)

    def test_stop_switch_created_during_analysis_is_checked_before_submission(self):
        original = self.public.get_book
        count = 0

        def book_and_stop(token):
            nonlocal count
            count += 1
            if count >= 2:
                Path(self.cfg.state_dir, "STOP").touch()
            return original(token)

        self.public.get_book = book_and_stop
        report = self.engine.cycle()
        self.assertTrue(all(order["status"] == "rejected" for order in report["orders"]))
        self.assertEqual([], self.state.intents())
        self.assertEqual([], self.state.positions())
        self.assertEqual(11, self.state.get("cash"))


if __name__ == "__main__":
    unittest.main()
