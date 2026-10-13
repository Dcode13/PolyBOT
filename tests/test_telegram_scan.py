from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from polybot.telegram import TelegramController, read_fresh_scan
from tests.test_telegram import BOT, OWNER, FakeAPI, FakeWorkers, snapshot, update


def scan_report(mode="paper"):
    return {"mode": mode, "scan_only": True, "capital_basis": "configured_starting_cash",
            "analysis_cash": 11, "analysis_entry": 1, "orders": [], "qualified": [],
            "scanned": 20, "analyzed": 5, "rejections": {"crypto_excluded": 2}, "errors": [],
            "time": "2026-10-12T00:00:00Z"}


class TelegramFreshScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.api, self.workers = FakeAPI(), FakeWorkers()
        self.scan = Mock(side_effect=scan_report)
        self.reader = Mock(side_effect=snapshot)
        self.controller = TelegramController(
            self.api, OWNER, self.workers, self.root, self.root / "config.toml",
            self.root / "data", BOT, snapshot_reader=self.reader, scan_reader=self.scan,
        )

    def tearDown(self):
        self.controller.close()
        self.controller.executor.shutdown(wait=True, cancel_futures=True)
        self.temp.cleanup()

    def finish(self):
        self.controller.pending[0].result(timeout=2)
        self.controller.tick()

    def test_scan_is_fresh_and_does_not_start_trading_or_read_account(self):
        self.controller.dispatch(update("/scan"))
        self.assertIn("Memindai", self.api.messages[0][1])
        self.finish()
        self.scan.assert_called_once_with("paper")
        self.reader.assert_not_called()
        self.assertIn("20", self.api.messages[-1][1])
        self.assertEqual(self.workers.started, [])
        self.assertTrue(all(row[0] == OWNER for row in self.api.messages))

    def test_real_alias_uses_live_analysis_without_trading(self):
        self.controller.dispatch(update("/scan real"))
        self.finish()
        self.scan.assert_called_once_with("live")
        self.assertEqual(self.workers.started, [])
        self.reader.assert_not_called()

    def test_cached_report_keeps_separate_command(self):
        self.controller.dispatch(update("/scan_last live"))
        self.finish()
        self.reader.assert_called_once_with("live")
        self.scan.assert_not_called()

    def test_invalid_and_unauthorized_scan_commands_do_no_work(self):
        for i, command in enumerate(("/scan extra", "/scan paper more", "/scan ../.env"), 1):
            self.controller.dispatch(update(command, identifier=i))
        self.controller.dispatch(update("/scan", identifier=4, owner=OWNER + 1))
        self.controller.dispatch(update("/scan", identifier=5, chat={"id": -1, "type": "group"}))
        self.controller.dispatch(update("/scan", identifier=6, forward_origin={"type": "user"}))
        self.scan.assert_not_called()
        self.reader.assert_not_called()
        self.assertEqual(len(self.api.messages), 3)

    def test_slow_scan_allows_stop_and_status_but_not_duplicate_scan(self):
        started, release = threading.Event(), threading.Event()

        def slow(mode):
            started.set()
            if not release.wait(3):
                raise RuntimeError("Test scan not released")
            return scan_report(mode)

        self.scan.side_effect = slow
        try:
            self.controller.dispatch(update("/scan", identifier=1))
            self.assertTrue(started.wait(1))
            self.controller.dispatch(update("/status", identifier=2))
            self.assertIn("Scan pasar: sedang berjalan", self.api.messages[-1][1])
            self.controller.dispatch(update("/live_off", identifier=3))
            self.assertEqual(self.workers.stopped, ["live"])
            self.controller.dispatch(update("/scan live", identifier=4))
            self.assertIn("Data sedang diambil", self.api.messages[-1][1])
            self.scan.assert_called_once_with("paper")
        finally:
            release.set()
        self.finish()

    def test_scan_cooldown_and_notifications_off_still_deliver_result(self):
        self.controller.notifications = False
        with patch("polybot.telegram.time.monotonic", return_value=100):
            self.controller.dispatch(update("/scan", identifier=1))
        self.finish()
        self.assertEqual(len(self.api.messages), 2)
        with patch("polybot.telegram.time.monotonic", return_value=105):
            self.controller.dispatch(update("/scan live", identifier=2))
        self.scan.assert_called_once_with("paper")
        self.assertIn("30 detik", self.api.messages[-1][1])
        with patch("polybot.telegram.time.monotonic", return_value=131):
            self.controller.dispatch(update("/scan live", identifier=3))
        self.finish()
        self.assertEqual(self.scan.call_count, 2)

    def test_scan_failure_and_timeout_are_sanitized(self):
        for error in (RuntimeError("PRIVATE_KEY_SECRET"),
                      subprocess.TimeoutExpired(["SECRET_TOKEN"], 180)):
            with self.subTest(error=type(error).__name__):
                self.scan.side_effect = error
                self.controller.last_request = 0
                self.controller.last_scan_request = float("-inf")
                self.controller.dispatch(update("/scan", identifier=self.controller.offset + 1))
                with self.assertRaises(type(error)):
                    self.controller.pending[0].result(timeout=2)
                self.controller.tick()
                self.assertIsNone(self.controller.pending)
                self.assertNotIn("SECRET", repr(self.api.messages))
                self.assertIn("Scan", self.api.messages[-1][1])


class ScanSubprocessTests(unittest.TestCase):
    def test_fixed_scan_module_no_shell_and_bounded_runtime(self):
        root = Path("test root")
        config = root / "config.toml"
        with patch("polybot.telegram.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(scan_report("live")))) as run:
            self.assertEqual(read_fresh_scan(root, config, "live")["scanned"], 20)
        args, kwargs = run.call_args
        self.assertEqual(args[0][1:], ["-m", "polybot.scan", "--mode", "live", "--config", str(config)])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 180)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)

    def test_malformed_failed_or_order_bearing_reports_are_rejected(self):
        bad = ["[]", "oops", json.dumps({**scan_report(), "orders": [{}]}),
               json.dumps({**scan_report(), "scan_only": False}),
               json.dumps({**scan_report(), "mode": "live"}),
               json.dumps({**scan_report(), "capital_basis": "actual_account"})]
        for output in bad:
            with self.subTest(output=output), patch("polybot.telegram.subprocess.run", return_value=SimpleNamespace(
                    returncode=0, stdout=output)), self.assertRaises((ValueError, RuntimeError)):
                read_fresh_scan(Path("."), Path("config.toml"), "paper")
        with patch("polybot.telegram.subprocess.run", return_value=SimpleNamespace(
                returncode=2, stdout="SECRET")), self.assertRaises(RuntimeError):
            read_fresh_scan(Path("."), Path("config.toml"), "paper")
        with patch("polybot.telegram.subprocess.run") as run, self.assertRaises(ValueError):
            read_fresh_scan(Path("."), Path("config.toml"), "live;command")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
