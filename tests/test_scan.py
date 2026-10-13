from __future__ import annotations

from contextlib import redirect_stdout
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from polybot.config import Config
from polybot.engine import Engine
from polybot.scan import main, run_scan
from polybot.state import State, process_lock


class ScanTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = replace(Config(), state_dir=str(self.root),
                           forecast_path=str(self.root / "forecasts.json"))
        self.now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
        for module in ("engine", "state", "scan"):
            clock = patch(f"polybot.{module}.utcnow", return_value=self.now)
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
            "timestamp": self.now.isoformat(), "min_order_size": "1", "tick_size": "0.01",
            "asks": [{"price": ".40", "size": "100"}],
            "bids": [{"price": ".39", "size": "100"}],
        }
        self.forecast = {
            "probability": .90, "lower_probability": .85, "target_price": .70,
            "confidence": .95, "horizon_hours": 12, "remaining_horizon_hours": 12,
            "probability_kind": "target_touch", "calibrated": True,
        }
        self.public = Mock()
        self.public.iter_markets.return_value = [self.market]
        self.public.get_book.return_value = self.book
        self.public.get_fee.return_value = {"max_fee_fraction": 0}
        self.public.get_history.return_value = []

    def test_qualified_analysis_never_submits_an_order_or_reads_live_credentials(self):
        for mode in ("paper", "live"):
            with self.subTest(mode=mode), patch.object(
                    Engine, "_external_forecast", return_value=self.forecast), patch.object(
                    Engine, "_execute", side_effect=AssertionError("Submission forbidden")) as execute, patch(
                    "polybot.exchange.LiveExchange.from_env", side_effect=AssertionError("No auth")) as auth:
                report = run_scan(self.cfg, mode, public=self.public)
                self.assertEqual(mode, report["mode"])
                self.assertEqual(1, len(report["qualified"]))
                self.assertEqual([], report["orders"])
                self.assertTrue(report["scan_only"])
                self.assertEqual("configured_starting_cash", report["capital_basis"])
                self.assertEqual(11, report["analysis_cash"])
                self.assertEqual(1, report["analysis_entry"])
                self.assertEqual(11, report["cash"])
                execute.assert_not_called()
                auth.assert_not_called()
        self.assertEqual([], list(self.root.iterdir()))

    def test_scan_does_not_modify_existing_ledger_while_trading_lock_is_held(self):
        ledger = State(self.root / "paper.sqlite3", self.cfg.starting_cash)
        with ledger.db:
            ledger.set("cash", 7)
            ledger.set("scan_cursor", 17)
            ledger.set("last_scan", {"marker": "existing worker report"})
        ledger.close()
        with process_lock(self.root / "paper.lock"):
            before = {path.name: path.read_bytes() if path.suffix != ".lock" else None
                      for path in self.root.iterdir()}
            with patch.object(Engine, "_external_forecast", return_value=self.forecast):
                report = run_scan(self.cfg, "paper", public=self.public)
            after = {path.name: path.read_bytes() if path.suffix != ".lock" else None
                     for path in self.root.iterdir()}
            self.assertEqual(before, after)
        self.assertEqual(11, report["cash"])

    def test_live_analysis_requires_forecast_and_never_falls_back_to_price_history(self):
        report = run_scan(self.cfg, "live", public=self.public)
        self.assertEqual([], report["qualified"])
        self.assertEqual({"no_forecast": 1}, report["rejections"])
        self.public.get_history.assert_not_called()
        with patch.object(Engine, "_external_forecast", return_value={**self.forecast, "calibrated": False}):
            report = run_scan(self.cfg, "live", public=self.public)
        self.assertEqual({"live_requires_external_calibrated_forecast": 1}, report["rejections"])
        self.public.get_history.assert_not_called()

    def test_paper_analysis_keeps_experimental_history_fallback(self):
        run_scan(self.cfg, "paper", public=self.public)
        self.public.get_history.assert_called_once_with("token-1")

    def test_crypto_is_excluded_before_order_book_or_forecast_requests(self):
        self.public.iter_markets.return_value = [{**self.market, "question": "Will Bitcoin rise today?"}]
        report = run_scan(self.cfg, "paper", public=self.public)
        self.assertEqual(1, report["scanned"])
        self.assertEqual(0, report["analyzed"])
        self.assertEqual({"crypto_excluded": 1}, report["rejections"])
        self.public.get_book.assert_not_called()

    def test_stop_switch_returns_normalized_halt_without_network_or_ledger_writes(self):
        (self.root / "STOP").write_text("paused", encoding="utf-8")
        report = run_scan(self.cfg, "live", public=self.public)
        self.assertEqual("kill_switch", report["halted"])
        self.assertEqual("live", report["mode"])
        self.assertEqual(0, report["scanned"])
        self.assertEqual(0, report["analyzed"])
        self.assertEqual([], report["orders"])
        self.assertEqual(self.now.isoformat(), report["time"])
        self.public.iter_markets.assert_not_called()
        self.assertEqual(["STOP"], [path.name for path in self.root.iterdir()])

    def test_analysis_mode_rejects_trade_before_any_network_or_state_action(self):
        state = Mock()
        engine = Engine(self.cfg, self.public, state, analysis_mode="live")
        with self.assertRaises(ValueError):
            engine.cycle()
        self.assertEqual([], state.mock_calls)
        self.assertEqual([], self.public.mock_calls)

    def test_invalid_mode_or_live_adapter_fails_before_analysis(self):
        with self.assertRaises(ValueError):
            run_scan(self.cfg, "invalid", public=self.public)
        with self.assertRaises(ValueError):
            Engine(self.cfg, self.public, Mock(), analysis_mode="invalid")
        with self.assertRaises(ValueError):
            Engine(self.cfg, self.public, Mock(), live=Mock(), analysis_mode="paper")
        self.assertEqual([], self.public.mock_calls)

    def test_default_engine_still_trades_paper(self):
        state = State(Path(":memory:"), 11)
        self.addCleanup(state.close)
        engine = Engine(self.cfg, self.public, state)
        with patch.object(engine, "_external_forecast", return_value=copy.deepcopy(self.forecast)):
            report = engine.cycle()
        self.assertEqual("paper", report["mode"])
        self.assertEqual(1, len(report["orders"]))
        self.assertEqual("filled", report["orders"][0]["status"])

    def test_cli_emits_safe_json_and_suppresses_raw_failure_details(self):
        output = io.StringIO()
        with patch("polybot.scan.run_scan", side_effect=RuntimeError("secret-url-and-token")), redirect_stdout(output):
            code = main(["--config", str(self.root / "config.toml"), "--mode", "live"])
        self.assertEqual(2, code)
        result = json.loads(output.getvalue())
        self.assertEqual("RuntimeError", result["error"])
        self.assertNotIn("secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
