from __future__ import annotations

import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from polybot.config import Config
from polybot.state import State, process_lock


class StateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "ledger.sqlite3"
        self.now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        clock = patch("polybot.state.utcnow", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.state = State(self.path, 11)
        self.addCleanup(lambda: self.state.close())
        self.plan = {
            "token_id": "token-1", "market_id": "market-1", "event_id": "event-1",
            "question": "Does the home team win?", "outcome": "Yes", "long_term": False,
            "target_price": .6, "fee_fraction": 0, "shares": 2.5, "budget": 1,
            "horizon_hours": 12, "limit_price": .4,
        }

    def buy(self):
        intent = self.state.journal(self.plan, "BUY", self.state.get("cash"), 0)
        self.state.record_execution(intent, self.fill(2.5, 1))
        return intent

    @staticmethod
    def fill(shares, cash):
        return {"status": "filled", "confirmed": True, "shares": shares,
                "cash": cash, "order_id": "exchange-order-1"}

    def test_buy_fill_applies_once_even_if_confirmation_is_replayed(self):
        intent = self.buy()
        self.state.record_execution(intent, self.fill(2.5, 1))
        self.assertEqual(10, self.state.get("cash"))
        self.assertEqual(2.5, self.state.positions()[0]["shares"])
        self.assertEqual(1, len(self.state.summary()["recent_trades"]))
        self.assertEqual([], self.state.intents())

    def test_unconfirmed_filled_response_keeps_pending_without_mutating_cash(self):
        intent = self.state.journal(self.plan, "BUY", 11, 0)
        response = {**self.fill(2.5, 1), "confirmed": False}
        self.state.record_execution(intent, response)
        self.assertEqual(11, self.state.get("cash"))
        self.assertEqual([], self.state.positions())
        self.assertEqual("pending", self.state.intents()[0]["status"])
        self.state.record_execution(intent, self.fill(2.5, 1))
        self.assertEqual(10, self.state.get("cash"))

    def test_unknown_execution_and_prepared_baseline_survive_restart(self):
        intent = self.state.journal(self.plan, "BUY", 11, 0)
        baseline = {"before_cash": 11, "before_token": 0}
        self.state.prepared(intent, baseline)
        self.state.record_execution(intent, {"status": "unknown", "order_id": "order-uncertain"})
        self.state.close()
        self.state = State(self.path, 999)
        self.assertEqual(11, self.state.get("cash"))
        pending = self.state.intents()
        self.assertEqual(1, len(pending))
        self.assertEqual(intent, pending[0]["id"])
        self.assertEqual("unknown", pending[0]["status"])
        self.assertEqual(baseline, pending[0]["baseline"])
        self.assertEqual("order-uncertain", pending[0]["execution"]["order_id"])

    def test_confirmed_rejection_does_not_debit_or_allow_late_duplicate_processing(self):
        intent = self.state.journal(self.plan, "BUY", 11, 0)
        self.state.record_execution(intent, {"status": "rejected", "confirmed": False})
        self.state.record_execution(intent, self.fill(2.5, 1))
        self.assertEqual(11, self.state.get("cash"))
        self.assertEqual([], self.state.positions())
        self.assertEqual([], self.state.intents())
        self.assertEqual([], self.state.summary()["recent_trades"])

    def test_invalid_or_overbudget_fill_rolls_back_and_keeps_intent_pending(self):
        for shares, cash in ((2.5, 1.02), (-1, 1), (2.5, -1),
                             (math.nan, 1), (2.5, math.nan), (math.inf, 1)):
            with self.subTest(shares=shares, cash=cash):
                intent = self.state.journal(self.plan, "BUY", 11, 0)
                with self.assertRaises(ValueError):
                    self.state.record_execution(intent, self.fill(shares, cash))
                self.assertEqual(11, self.state.get("cash"))
                self.assertEqual([], self.state.positions())
                self.assertTrue(any(item["id"] == intent for item in self.state.intents()))

    def test_exception_after_position_insert_rolls_back_entire_transaction(self):
        intent = self.state.journal(self.plan, "BUY", 11, 0)
        original_set = self.state.set

        def fail_cash_update(key, value):
            if key == "cash":
                raise RuntimeError("simulated storage failure")
            return original_set(key, value)

        with patch.object(self.state, "set", side_effect=fail_cash_update):
            with self.assertRaises(RuntimeError):
                self.state.record_execution(intent, self.fill(2.5, 1))
        self.assertEqual(11, self.state.get("cash"))
        self.assertEqual([], self.state.positions())
        self.assertEqual([], self.state.summary()["recent_trades"])
        self.assertEqual("pending", self.state.intents()[0]["status"])
        self.state.record_execution(intent, self.fill(2.5, 1))
        self.assertEqual(10, self.state.get("cash"))

    def test_partial_and_final_sales_allocate_cost_and_realize_actual_net_profit(self):
        self.buy()
        sell_plan = {"token_id": "token-1", "question": "Does the home team win?", "reason": "take_profit"}
        partial = self.state.journal(sell_plan, "SELL", 10, 2.5)
        self.state.record_execution(partial, self.fill(1, .6))
        self.assertAlmostEqual(10.6, self.state.get("cash"))
        self.assertAlmostEqual(1.5, self.state.positions()[0]["shares"])
        self.assertAlmostEqual(.6, self.state.positions()[0]["cost"])
        final = self.state.journal(sell_plan, "SELL", 10.6, 1.5)
        self.state.record_execution(final, self.fill(1.5, .9))
        self.state.record_execution(final, self.fill(1.5, .9))
        self.assertAlmostEqual(11.5, self.state.get("cash"))
        self.assertEqual([], self.state.positions())
        trades = self.state.summary()["recent_trades"]
        self.assertEqual(3, len(trades))
        self.assertAlmostEqual(.5, sum(trade["pnl"] for trade in trades))

    def test_sell_cannot_exceed_inventory_and_failure_is_atomic(self):
        self.buy()
        intent = self.state.journal({"token_id": "token-1"}, "SELL", 10, 2.5)
        with self.assertRaises(ValueError):
            self.state.record_execution(intent, self.fill(3, 1.8))
        self.assertEqual(10, self.state.get("cash"))
        self.assertEqual(2.5, self.state.positions()[0]["shares"])
        self.assertEqual(1, len(self.state.summary()["recent_trades"]))

    def test_daily_loss_latches_until_next_day_even_if_equity_recovers(self):
        cfg = Config()
        self.assertEqual("daily_loss_limit", self.state.risk_gate(10, cfg, self.now))
        self.assertEqual("daily_loss_limit", self.state.risk_gate(11, cfg, self.now))
        self.assertIsNone(self.state.risk_gate(11, cfg, self.now + timedelta(days=1)))
        self.assertEqual(11, self.state.get("day_start_equity"))

    def test_drawdown_uses_high_water_mark_and_remains_latched_across_days(self):
        cfg = Config()
        self.assertIsNone(self.state.risk_gate(20, cfg, self.now))
        self.assertEqual(20, self.state.get("peak_equity"))
        self.assertEqual("drawdown_limit", self.state.risk_gate(15, cfg, self.now))
        self.assertEqual("drawdown_limit", self.state.risk_gate(21, cfg, self.now + timedelta(days=1)))

    def test_process_lock_prevents_concurrent_use_and_releases_after_exit(self):
        path = Path(self.folder.name) / "ledger.lock"
        with process_lock(path):
            with self.assertRaises(RuntimeError):
                with process_lock(path):
                    self.fail("second lock acquired")
        with process_lock(path):
            pass


if __name__ == "__main__":
    unittest.main()
