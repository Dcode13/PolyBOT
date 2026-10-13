from __future__ import annotations

import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from polybot.config import Config
from polybot.state import process_lock
from polybot.telegram_worker import TradingWorkers, _OwnedWorker, _safe_event, run_worker, safe_report


class SafeReportTests(unittest.TestCase):
    def test_only_approved_fields_and_codes_cross_process_boundary(self):
        report = safe_report({
            "cash": 11, "equity": float("nan"), "scanned": True,
            "analyzed": 10 ** 1000, "private_key": "secret",
            "orders": [{"side": "BUY", "status": "filled", "reason": "take_profit",
                        "order_id": "secret", "token_id": "secret"},
                       {"side": "secret", "status": "secret", "reason": ["secret"]}],
            "errors": [{"exception": "secret"}], "qualified": [{"secret": "secret"}],
            "rejections": {"stale_book": 3, "secret": 1, "no_forecast": float("inf")},
            "halted": {"secret": "secret"},
        })
        self.assertNotIn("secret", json.dumps(report))
        self.assertNotIn("equity", report)
        self.assertNotIn("scanned", report)
        self.assertNotIn("analyzed", report)
        self.assertEqual({"stale_book": 3}, report["rejections"])
        self.assertEqual("unknown", report["halted"])
        self.assertEqual(2, report["orders_count"])

    def test_repeated_sanitization_preserves_total_count_without_large_payload(self):
        original = {"qualified": [{}] * 180, "errors": [{}] * 150, "orders": []}
        report = safe_report(safe_report(original))
        self.assertEqual(180, report["qualified_count"])
        self.assertEqual(150, report["errors_count"])
        self.assertEqual(100, len(report["errors"]))

    def test_exploration_label_and_count_survive_parent_and_child_sanitization(self):
        report = {"qualified": [{"entry_kind": "paper_exploratory", "probability_lower": None,
                                  "forecast": {"secret": "secret"}}] * 120,
                  "orders": [{"side": "BUY", "status": "filled", "entry_kind": "paper_exploratory",
                               "reason": "paper_exploratory", "private_key": "secret"}]}
        safe = safe_report(safe_report(report))
        self.assertEqual(120, safe["exploratory_count"])
        self.assertEqual(120, safe["qualified_count"])
        self.assertEqual("paper_exploratory", safe["orders"][0]["entry_kind"])
        self.assertNotIn("secret", json.dumps(safe))

    def test_no_entry_and_exit_keep_outcome_through_both_process_boundaries(self):
        report = {"orders": [
            {"side": "BUY", "outcome": " no\n\u202e", "status": "filled", "token_id": "hidden-token"},
            {"side": "SELL", "outcome": "No", "status": "filled", "raw": {"key": "hidden-key"}},
            {"side": "BUY", "outcome": "Yes", "status": "pending"},
        ]}
        safe = safe_report(safe_report(report))
        self.assertEqual([("BUY", "No"), ("SELL", "No"), ("BUY", "Yes")],
                         [(row["side"], row["outcome"]) for row in safe["orders"]])
        self.assertNotIn("hidden", json.dumps(safe))
        self.assertNotIn("\u202e", str(safe))

    def test_outcomes_are_bounded_public_labels_with_no_structured_values(self):
        report = safe_report({"orders": [
            {"outcome": {"private_key": "secret"}}, {"outcome": ["secret"]},
            {"outcome": "\x00\u202e"}, {"outcome": "Team\nB"}, {"outcome": "\U0001f600" * 1000},
        ]})
        outcomes = [row["outcome"] for row in report["orders"]]
        self.assertEqual(["", "", "", "Team B"], outcomes[:4])
        self.assertEqual(40, len(outcomes[-1]))
        self.assertNotIn("secret", json.dumps(report))

    def test_event_mode_is_owned_by_parent_and_exception_is_never_forwarded(self):
        event = _safe_event({"type": "error", "mode": "live", "code": {"key": "secret"},
                             "message": "secret"}, "paper")
        self.assertEqual("paper", event["mode"])
        self.assertEqual("worker_error", event["code"])
        self.assertNotIn("secret", json.dumps(event))
        self.assertIsNone(_safe_event({"type": "command", "command": "start"}, "paper"))


class FakeProcess:
    def __init__(self, *, code=None, output=""):
        self.returncode = code
        self.stdout = io.StringIO(output)
        self.stdin = io.StringIO()
        self.wait = Mock(side_effect=self._wait)
        self.kill = Mock()
        self.terminate = Mock()

    def poll(self):
        return self.returncode

    def _wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("owned-worker", timeout)
        return self.returncode


class TradingWorkersTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.manager = TradingWorkers(Path("config.toml"), self.root)
        self.processes = []
        self.calls = []

        def spawn(*args, **kwargs):
            process = FakeProcess()
            self.processes.append(process)
            self.calls.append((args, kwargs))
            return process

        self.popen = patch("polybot.telegram_worker.subprocess.Popen", side_effect=spawn).start()
        self.thread = patch("polybot.telegram_worker.threading.Thread").start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.manager.close)

    def test_paper_and_live_use_separate_owned_processes_without_shell_or_secret_arguments(self):
        for mode in ("paper", "live"):
            self.manager.start(mode, once=mode == "paper")
        self.assertEqual(2, len(self.calls))
        paths = []
        for mode, (args, kwargs) in zip(("paper", "live"), self.calls):
            argv = args[0]
            self.assertEqual(mode, argv[argv.index("--mode") + 1])
            self.assertEqual(str(self.root / "config.toml"), argv[argv.index("--config") + 1])
            self.assertIn("--parent-watch", argv)
            self.assertEqual(mode == "paper", "--once" in argv)
            self.assertFalse(kwargs["shell"])
            self.assertEqual(str(self.root), kwargs["cwd"])
            self.assertEqual(subprocess.DEVNULL, kwargs["stderr"])
            paths.append(self.manager._workers[mode].stop_path)
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(self.manager.statuses()["paper"]["once"])
        self.assertFalse(self.manager.statuses()["live"]["once"])

    def test_duplicate_or_stopping_worker_cannot_be_restarted(self):
        self.manager.start("paper")
        self.assertIn("sudah berjalan", self.manager.start("paper"))
        self.manager.stop("paper")
        self.assertIn("masih berhenti", self.manager.start("paper"))
        self.assertEqual(1, self.popen.call_count)
        self.assertTrue(self.manager.statuses()["paper"]["stopping"])

    def test_stop_closes_only_owned_pipe_and_preserves_global_and_other_mode_state(self):
        self.manager.start("paper")
        self.manager.start("live")
        global_stop = self.root / "data" / "STOP"
        global_stop.write_text("global-stop", encoding="utf-8")
        self.manager.stop("paper")
        self.assertTrue(self.processes[0].stdin.closed)
        self.assertFalse(self.processes[1].stdin.closed)
        self.assertTrue(self.manager._workers["paper"].stop_path.exists())
        self.assertFalse(self.manager._workers["live"].stop_path.exists())
        self.assertEqual("global-stop", global_stop.read_text(encoding="utf-8"))
        for process in self.processes:
            process.kill.assert_not_called()
            process.terminate.assert_not_called()

    def test_controller_instances_have_unique_stop_files(self):
        other = TradingWorkers(Path("config.toml"), self.root)
        self.addCleanup(other.close)
        self.manager.start("paper")
        other.start("paper")
        self.assertNotEqual(self.manager._workers["paper"].stop_path,
                            other._workers["paper"].stop_path)
        self.manager.stop("paper")
        self.assertFalse(other._workers["paper"].stop_path.exists())

    def test_held_ledger_lock_prevents_child_and_global_stop_is_not_cleared(self):
        state_dir = self.root / "data"
        with process_lock(state_dir / "live.lock"):
            global_stop = state_dir / "STOP"
            global_stop.touch()
            message = self.manager.start("live")
        self.assertIn("Ledger sedang dipakai", message)
        self.popen.assert_not_called()
        self.assertTrue(global_stop.exists())

    def test_close_never_kills_inflight_workers_and_refuses_future_start(self):
        self.manager.start("live")
        self.manager.close()
        process = self.processes[0]
        process.kill.assert_not_called()
        process.terminate.assert_not_called()
        self.assertTrue(process.stdin.closed)
        self.assertLessEqual(process.wait.call_args.kwargs["timeout"], 8)
        self.assertIn("Pengendali sedang berhenti", self.manager.start("paper"))

    def test_invalid_mode_and_spawn_failures_do_not_expose_raw_errors(self):
        with self.assertRaises(ValueError):
            self.manager.start("live --untrusted")
        self.popen.assert_not_called()
        self.popen.side_effect = RuntimeError("private_key=secret")
        self.assertNotIn("secret", self.manager.start("paper"))
        self.assertFalse(self.manager.statuses()["paper"]["running"])

    def test_reader_rejects_noise_oversized_lines_and_sanitizes_approved_events(self):
        lines = ["not-json\n", "x" * 70_000 + "\n",
                 json.dumps({"type": "cycle", "mode": "live", "report": {
                     "cash": 11, "private_key": "secret", "orders": []}}) + "\n"]
        process = FakeProcess(code=2, output="".join(lines))
        worker = _OwnedWorker(process, self.root / "owned.stop", False)
        worker.stop_path.touch()
        self.manager._read("paper", worker)
        events = self.manager.drain_events()
        self.assertEqual(["cycle", "error", "stopped"], [event["type"] for event in events])
        self.assertTrue(all(event["mode"] == "paper" for event in events))
        self.assertNotIn("secret", json.dumps(events))
        self.assertEqual([], self.manager.drain_events())
        self.assertFalse(worker.stop_path.exists())
        self.assertTrue(process.stdin.closed)


class WorkerExecutionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.cfg = replace(Config(), state_dir=str(self.root), forecast_path=str(self.root / "forecast.json"))
        patch("polybot.telegram_worker._resolved_config", return_value=self.cfg).start()
        self.public = patch("polybot.telegram_worker.PublicExchange").start().return_value
        self.public.geoblock.return_value = {"blocked": False}
        self.live_factory = patch("polybot.telegram_worker.LiveExchange.from_env").start()
        self.env_loader = patch("polybot.telegram_worker.load_env").start()
        self.engine_factory = patch("polybot.telegram_worker.Engine").start()
        self.engine = self.engine_factory.return_value
        self.engine.cycle.return_value = {"cash": 11, "orders": [], "errors": [], "qualified": []}
        self.addCleanup(patch.stopall)

    def run_once(self, mode="paper", **kwargs):
        output = io.StringIO()
        result = run_worker(self.root / "config.toml", mode, once=True, output=output, **kwargs)
        return result, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_paper_uses_paper_ledger_and_never_loads_signing_client(self):
        result, events = self.run_once()
        self.assertEqual(0, result)
        self.engine.cycle.assert_called_once_with(trade=True)
        self.assertTrue((self.root / "paper.sqlite3").exists())
        self.assertFalse((self.root / "live.sqlite3").exists())
        self.live_factory.assert_not_called()
        self.env_loader.assert_not_called()
        self.engine.initialize_live.assert_not_called()
        self.assertEqual(["started", "cycle", "stopped"], [event["type"] for event in events])
        # The worker releases the same operating-system lock used by the CLI.
        with process_lock(self.root / "paper.lock"):
            pass

    def test_live_region_block_prevents_signing_client_and_engine(self):
        self.public.geoblock.return_value = {"blocked": True}
        result, events = self.run_once("live")
        self.assertEqual(2, result)
        self.live_factory.assert_not_called()
        self.engine_factory.assert_not_called()
        self.assertEqual("region_blocked", events[0]["code"])

    def test_live_initialization_runs_before_cycle_and_closes_client(self):
        calls = []
        self.engine.initialize_live.side_effect = lambda: calls.append("initialize")
        self.engine.cycle.side_effect = lambda **kwargs: (calls.append("cycle") or {"orders": []})
        result, events = self.run_once("live")
        self.assertEqual(0, result)
        self.assertEqual(["initialize", "cycle"], calls)
        self.live_factory.return_value.close.assert_called_once()
        self.assertTrue((self.root / "live.sqlite3").exists())
        self.assertFalse((self.root / "paper.sqlite3").exists())

    def test_stop_arriving_during_scan_prevents_a_new_submission(self):
        original_execute = self.engine._execute

        def cycle(**kwargs):
            (self.root / "telegram-paper.stop").touch()
            execution = self.engine._execute({"outcome": "No"}, "BUY", {}, 0)
            self.assertEqual("telegram_stop", execution["reason"])
            return {"orders": [execution]}

        self.engine.cycle.side_effect = cycle
        result, events = self.run_once()
        self.assertEqual(0, result)
        original_execute.assert_not_called()
        self.assertEqual("rejected", events[1]["report"]["orders"][0]["status"])
        self.assertEqual("No", events[1]["report"]["orders"][0]["outcome"])

    def test_unresolved_order_stops_worker_without_retrying_cycle(self):
        self.engine.cycle.return_value = {"halted": "unresolved_order", "orders": []}
        output = io.StringIO()
        result = run_worker(self.root / "config.toml", "live", output=output)
        self.assertEqual(2, result)
        self.engine.cycle.assert_called_once()

    def test_sdk_prints_and_exception_details_never_reach_output_or_console(self):
        def failing_cycle(**kwargs):
            import sys
            print("private_key=secret")
            print("api_secret=secret", file=sys.stderr)
            raise RuntimeError("private_key=secret")

        self.engine.cycle.side_effect = failing_cycle
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result, events = self.run_once("live")
        self.assertEqual(2, result)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())
        self.assertNotIn("secret", json.dumps(events))
        self.assertEqual("cycle_error", events[1]["code"])

    def test_external_inventory_stops_before_cycle_with_approved_error_only(self):
        self.engine.initialize_live.side_effect = RuntimeError(
            "Ada posisi akun di luar ledger bot; rekonsiliasi diperlukan")
        result, events = self.run_once("live")
        self.assertEqual(2, result)
        self.engine.cycle.assert_not_called()
        self.assertEqual("external_positions", events[0]["code"])

    def test_parent_pipe_eof_prevents_new_cycles(self):
        # A closed ownership pipe is also how an unexpected controller crash
        # reaches its child. No textual commands are consumed from that pipe.
        fake_stdin = Mock()
        fake_stdin.fileno.return_value = 0
        with patch("polybot.telegram_worker.sys.stdin", fake_stdin), \
                patch("polybot.telegram_worker.os.read", return_value=b""):
            result, events = self.run_once(parent_watch=True)
        self.assertEqual(0, result)
        self.engine.cycle.assert_not_called()
        self.assertEqual("stopped", events[-1]["type"])

    def test_worker_specific_stop_does_not_stop_a_different_owner(self):
        (self.root / "telegram-paper.stop").touch()
        result, events = self.run_once(control_id="a" * 32)
        self.assertEqual(0, result)
        self.engine.cycle.assert_called_once()
        self.assertEqual("started", events[0]["type"])


class WorkerProcessTests(unittest.TestCase):
    def test_real_once_child_exits_cleanly_with_parent_pipe_still_open(self):
        # Global STOP makes the real engine return before scanning or submitting
        # anything. Use a disposable ledger; the account .env is never loaded.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "STOP").touch()
            config = root / "config.toml"
            config.write_text("state_dir = " + json.dumps(str(root)) + "\n", encoding="utf-8")
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "polybot.telegram_worker", "--config", str(config),
                 "--mode", "paper", "--once", "--parent-watch"],
                cwd=str(Path(__file__).resolve().parents[1]),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                # communicate() would close stdin and hide the daemon-reader
                # shutdown bug this smoke test guards against.
                code = process.wait(timeout=10)
                output = process.stdout.read()
                errors = process.stderr.read()
                self.assertEqual(0, code, errors)
                self.assertEqual("", errors)
                events = [json.loads(line) for line in output.splitlines()]
                self.assertEqual(["started", "cycle", "stopped"], [event["type"] for event in events])
                self.assertEqual("kill_switch", events[1]["report"]["halted"])
                self.assertEqual(0, events[1]["report"]["orders_count"])
            finally:
                process.stdin.close()
                process.wait(timeout=10)
                process.stdout.close()
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
