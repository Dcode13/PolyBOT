"""Outcome selection and inventory accounting with synthetic, offline markets."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from polybot.config import Config
from polybot.engine import Engine
from polybot.exchange import normalize_event
from polybot.monitor import build_snapshot
from polybot.state import State
from polybot.telegram_views import format_snapshot, format_trade_action
from polybot.telegram_worker import safe_report
from tests.test_engine import FakePublic


class OutcomeTradingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
        for module in ("polybot.engine", "polybot.state"):
            clock = patch(module + ".utcnow", return_value=self.now)
            clock.start()
            self.addCleanup(clock.stop)
        self.cfg = replace(Config(), state_dir=str(self.root),
                           forecast_path=str(self.root / "forecasts.json"))
        self.state = State(self.root / "paper.sqlite3", 11)
        self.addCleanup(self.state.close)
        self.markets = list(normalize_event({
            "id": "event-1", "category": "Sports", "markets": [{
                "id": "market-1", "question": "Will the home team win?",
                "clobTokenIds": ["101", "102"], "outcomes": ["Yes", "No"],
                "endDate": (self.now + timedelta(days=3)).isoformat(),
                "active": True, "closed": False, "acceptingOrders": True,
                "enableOrderBook": True, "liquidityNum": 2000, "volume24hr": 1000,
            }],
        }))
        self.books = {"101": self.book(.61, .60), "102": self.book(.40, .39)}
        self.public = FakePublic(self.markets, self.books)
        self.engine = Engine(self.cfg, self.public, self.state)
        self.write_forecasts([
            self.forecast("101", probability=.15, lower_probability=.10),
            self.forecast("102"),
        ])

    def book(self, ask, bid):
        return {"timestamp": self.now.isoformat(), "min_order_size": 1, "tick_size": .01,
                "asks": [{"price": ask, "size": 100}], "bids": [{"price": bid, "size": 100}]}

    def forecast(self, token, **overrides):
        return {
            "market_id": "market-1", "token_id": token,
            "probability": .90, "lower_probability": .85, "confidence": .95,
            "target_price": .70, "horizon_hours": 48, "probability_kind": "resolution",
            "observed_at": self.now.isoformat(),
            "expires_at": (self.now + timedelta(hours=48)).isoformat(),
            "source": "OFFLINE TEST FIXTURE", "rationale": "Synthetic test, not market advice.",
            "calibrated": True, **overrides,
        }

    def write_forecasts(self, rows):
        Path(self.cfg.forecast_path).write_text(json.dumps({"forecasts": rows}), encoding="utf-8")

    def test_negative_event_forecast_buys_no_and_take_profit_sells_same_token(self):
        report = self.engine.cycle()
        order, = report["orders"]
        self.assertEqual(("BUY", "102", "No", "filled"),
                         (order["side"], order["token_id"], order["outcome"], order["status"]))
        position, = self.state.positions()
        self.assertEqual("No", position["outcome"])
        self.assertEqual(2.5, position["shares"])
        self.assertEqual(10, self.state.get("cash"))
        self.assertEqual(1, report["rejections"]["insufficient_evidence"])
        exported = safe_report(safe_report(report))
        self.assertEqual("ENTRY NO (BUY No)", format_trade_action(
            exported["orders"][0]["side"], exported["orders"][0]["outcome"]))
        self.public.markets = []
        self.books["102"] = self.book(.66, .65)
        exit_report = self.engine.cycle()
        sell, = exit_report["orders"]
        self.assertEqual(("SELL", "102", "No", "take_profit"),
                         (sell["side"], sell["token_id"], sell["outcome"], sell["reason"]))
        self.assertEqual([], self.state.positions())
        self.assertAlmostEqual(11.625, self.state.get("cash"))
        trades = self.state.summary()["recent_trades"]
        self.assertEqual({"No"}, {t["outcome"] for t in trades})
        self.assertEqual({"102"}, {t["token_id"] for t in trades})
        self.assertAlmostEqual(.625, sum(t["pnl"] for t in trades))
        snapshot = build_snapshot(self.cfg, "paper", now=self.now)
        self.assertEqual({"No"}, {t["outcome"] for t in snapshot["trades"]})
        activity = format_snapshot(snapshot, section="activity")
        self.assertIn("ENTRY NO (BUY No)", activity)
        self.assertIn("EXIT NO (SELL No)", activity)

    def test_both_qualify_but_better_no_score_wins_without_buying_opposite_side(self):
        self.write_forecasts([
            self.forecast("101", probability=.85, lower_probability=.80,
                          target_price=.98, probability_kind="target_touch", horizon_hours=12),
            self.forecast("102", probability=.95, lower_probability=.90,
                          probability_kind="target_touch", horizon_hours=12),
        ])
        report = self.engine.cycle()
        self.assertEqual(["No", "Yes"], [row["outcome"] for row in report["qualified"]])
        order, = report["orders"]
        self.assertEqual("102", order["token_id"])
        self.assertEqual(1, report["rejections"]["event_already_held"])
        self.assertEqual(1, len(self.state.positions()))

    def test_yes_still_wins_when_its_own_forecast_is_better(self):
        self.write_forecasts([
            self.forecast("101", target_price=.98),
            self.forecast("102", probability=.10, lower_probability=.05),
        ])
        order, = self.engine.cycle()["orders"]
        self.assertEqual(("BUY", "Yes", "101"), (order["side"], order["outcome"], order["token_id"]))

    def test_one_outcome_budget_resumes_no_on_next_cycle(self):
        self.engine.cfg = replace(self.cfg, max_candidates=1)
        first = self.engine.cycle(trade=False)
        self.assertEqual([], first["qualified"])
        self.assertEqual(1, self.state.get("scan_cursor"))
        second = self.engine.cycle(trade=False)
        self.assertEqual(["No"], [r["outcome"] for r in second["qualified"]])
        self.assertEqual(2, self.state.get("scan_cursor"))
        self.assertEqual([], second["orders"])

    def test_live_adapter_receives_buy_no_never_a_sell_to_open(self):
        live = Mock()
        live.cash_balance.side_effect = lambda: self.state.get("cash")
        live.token_balance.side_effect = lambda token: next(
            (p["shares"] for p in self.state.positions() if p["token_id"] == token), 0)

        def fill(token, side, shares, price, **kwargs):
            self.assertEqual(("102", "BUY"), (token, side))
            kwargs["on_prepared"]({"token_id": token, "side": side,
                                    "before_cash": 11, "before_shares": 0})
            return {"status": "filled", "confirmed": True, "order_id": "fake-no-order",
                    "shares": shares, "cash": shares * .40}

        live.place_fok.side_effect = fill
        self.engine.live = live
        report = self.engine.cycle()
        self.assertEqual("live", report["mode"])
        self.assertEqual("No", report["orders"][0]["outcome"])
        self.assertEqual(1, live.place_fok.call_count)
        self.assertEqual([], self.state.intents())

    def test_paper_exploration_can_buy_no_without_claiming_an_event_probability(self):
        self.write_forecasts([])
        self.engine.cfg = replace(self.cfg, paper_entry_mode="explore")
        self.books.update({"101": self.book(.78, .77), "102": self.book(.24, .23)})
        report = self.engine.cycle()
        order, = report["orders"]
        self.assertEqual(("BUY", "No", "paper_exploratory"),
                         (order["side"], order["outcome"], order["entry_kind"]))
        self.assertIsNone(report["qualified"][0]["probability_lower"])
        self.assertLessEqual(self.state.positions()[0]["cost"], 1)

    def test_expensive_no_still_requires_affordable_minimum_and_supported_profit(self):
        self.books["102"] = self.book(.80, .79)
        report = self.engine.cycle()
        self.assertEqual([], report["orders"])
        self.assertEqual(1, report["rejections"]["profit_target_not_supported"])
        self.books["102"] = {**self.book(.40, .39), "min_order_size": 5}
        report = self.engine.cycle()
        self.assertEqual([], report["orders"])
        self.assertEqual(1, report["rejections"]["entry_below_exchange_minimum"])
        self.assertEqual(11, self.state.get("cash"))

    def test_no_stop_loss_closes_no_inventory(self):
        self.engine.cycle()
        self.public.markets = []
        self.books["102"] = self.book(.31, .30)
        order, = self.engine.cycle()["orders"]
        self.assertEqual(("SELL", "No", "stop_loss"),
                         (order["side"], order["outcome"], order["reason"]))
        self.assertAlmostEqual(10.75, self.state.get("cash"))
        self.assertEqual([], self.state.positions())


if __name__ == "__main__":
    unittest.main()
