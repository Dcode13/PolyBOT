from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from polybot.telegram import (
    TelegramController, discard_backlog, main, pair, private_sender, save_owner,
)
from polybot.telegram_api import TelegramError
from polybot.telegram_worker import safe_report


OWNER = 123456789
BOT = "Polybot_Test_bot"
NONCE = "unit-test-pairing-code"


def update(text="/status", identifier=1, owner=OWNER, **message_fields):
    return {
        "update_id": identifier,
        "message": {
            "from": {"id": owner, "is_bot": False},
            "chat": {"id": owner, "type": "private"},
            "date": int(time.time()), "text": text, **message_fields,
        },
    }


def snapshot(mode):
    return {
        "mode": mode, "generatedAt": "2026-10-11T00:00:00Z",
        "account": {"connected": True, "cash": 11, "equity": 11,
                    "positionsComplete": True, "historyComplete": True},
        "bot": {"ledgerAvailable": True}, "positions": [], "trades": [],
        "errors": [], "risk": {}, "equityHistory": [],
    }


class FakeAPI:
    def __init__(self, batches=None):
        self.batches = list(batches or [])
        self.messages = []
        self.photos = []
        self.polls = []
        self.send_error = None
        self.photo_error = None
        self.commands_set = 0

    def send_message(self, chat_id, text, keyboard=False):
        self.messages.append((chat_id, text, keyboard))
        if self.send_error:
            raise self.send_error
        return {"message_id": len(self.messages)}

    def send_photo(self, chat_id, png, caption=""):
        self.photos.append((chat_id, png, caption))
        if self.photo_error:
            raise self.photo_error
        return {"message_id": len(self.photos)}

    def get_updates(self, offset=None, timeout=25):
        self.polls.append((offset, timeout))
        if not self.batches:
            raise AssertionError("Unexpected additional polling")
        return self.batches.pop(0)

    def set_commands(self):
        self.commands_set += 1
        return True


class FakeWorkers:
    def __init__(self):
        self.started = []
        self.stopped = []
        self.events = []
        self.closed = False
        self.running = {"paper": False, "live": False}

    def statuses(self):
        return {mode: {"running": running} for mode, running in self.running.items()}

    def start(self, mode, once=False):
        self.started.append((mode, once))
        self.running[mode] = True
        return f"Worker {mode} started"

    def stop(self, mode):
        self.stopped.append(mode)
        self.running[mode] = False
        return f"Worker {mode} stopping"

    def drain_events(self):
        result, self.events = self.events, []
        return result

    def close(self):
        self.closed = True


class TelegramControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.api = FakeAPI()
        self.workers = FakeWorkers()
        self.reader = Mock(side_effect=snapshot)
        self.controller = TelegramController(
            self.api, OWNER, self.workers, self.root, self.root / "config.toml",
            self.root / "data", BOT, snapshot_reader=self.reader,
        )

    def tearDown(self):
        self.controller.close()
        # Tests must leave no monitor tasks alive even though service shutdown
        # deliberately does not block on an in-flight snapshot subprocess.
        self.controller.executor.shutdown(wait=True, cancel_futures=True)
        self.temp.cleanup()

    def finish_view(self):
        self.assertIsNotNone(self.controller.pending)
        self.controller.pending[0].result(timeout=2)
        self.controller.tick()
        self.assertIsNone(self.controller.pending)

    def test_owner_only_requires_matching_private_sender_and_chat(self):
        rejected = [
            update("/live_on", owner=OWNER + 1),
            update("/live_on", chat={"id": OWNER + 1, "type": "private"}),
            update("/live_on", chat={"id": -100123, "type": "supergroup"}),
            update("/live_on", chat={"id": OWNER, "type": "group"}),
            update("/live_on", **{"from": {"id": OWNER, "is_bot": True}}),
            update("/live_on", **{"from": {"id": OWNER}}),
            update("/live_on", sender_chat={"id": OWNER}),
            update("/live_on", forward_origin={"type": "user"}),
            update("/live_on", forward_from={"id": OWNER}),
            update("/live_on", forward_date=int(time.time())),
            {"edited_message": update("/live_on")["message"]},
            {"channel_post": update("/live_on")["message"]},
        ]
        for identifier, item in enumerate(rejected, 1):
            item["update_id"] = identifier
            self.controller.dispatch(item)
        self.assertEqual(self.workers.started, [])
        self.assertEqual(self.api.messages, [])
        self.reader.assert_not_called()
        self.controller.dispatch(update("/status", identifier=100))
        self.assertEqual(len(self.api.messages), 1)
        self.assertEqual(self.api.messages[0][0], OWNER)

    def test_service_stop_during_poll_cannot_start_returned_live_command(self):
        stop_path = self.root / "service.stop"

        def poll_after_stop(**kwargs):
            stop_path.write_text("stop", encoding="utf-8")
            return [update("/live_on")]

        self.api.get_updates = poll_after_stop
        with patch("polybot.telegram.discard_backlog", return_value=0):
            self.controller.run(stop_path)
        self.assertEqual(self.workers.started, [])
        self.assertTrue(self.workers.closed)

    def test_private_sender_rejects_invalid_identifiers_and_shape(self):
        for item in ({}, {"message": []}, update(**{"from": []}),
                     update(chat=[]), update(owner=0), update(owner=-1), update(owner=True)):
            with self.subTest(item=item):
                self.assertIsNone(private_sender(item))
        self.assertEqual(private_sender(update())[1], OWNER)

    def test_replayed_updates_and_wrong_bot_commands_are_ignored(self):
        self.controller.dispatch(update("/paper_once@Another_bot", identifier=1))
        self.controller.dispatch(update("/paper_once@" + BOT.upper(), identifier=2))
        self.controller.dispatch(update("/live_on", identifier=2))
        self.controller.dispatch(update("/live_on", identifier=1))
        self.controller.dispatch(update("/live_on", identifier=True))
        self.assertEqual(self.workers.started, [("paper", True)])
        self.assertEqual(self.controller.offset, 3)

    def test_stale_missing_and_far_future_start_dates_cannot_start_workers(self):
        for identifier, date in enumerate((int(time.time()) - 180, 0, None, True, int(time.time()) + 120), 1):
            self.controller.dispatch(update("/live_on", identifier=identifier, date=date))
        self.assertEqual(self.workers.started, [])
        self.assertEqual(len(self.api.messages), 5)
        self.assertTrue(all("terlalu lama" in message[1] for message in self.api.messages))

    def test_only_explicit_exact_controls_start_and_stop_matching_mode(self):
        commands = ["/paper_on", "/paper_off", "/paper_once", "/live_on", "/live_off"]
        for identifier, command in enumerate(commands, 1):
            self.controller.dispatch(update(command, identifier=identifier))
        self.assertEqual(self.workers.started, [("paper", False), ("paper", True), ("live", False)])
        self.assertEqual(self.workers.stopped, ["paper", "live"])
        self.reader.assert_not_called()
        for identifier, text in enumerate(("live_on", "/paper_on live", "/live_on yes", "/live_off extra"), 10):
            self.controller.dispatch(update(text, identifier=identifier))
        self.assertEqual(len(self.workers.started), 3)
        self.assertEqual(len(self.workers.stopped), 2)

    def test_dashboard_defaults_paper_and_does_not_start_trading(self):
        self.controller.dispatch(update("/ringkasan"))
        self.finish_view()
        self.reader.assert_called_once_with("paper")
        self.assertIn("PAPER", self.api.messages[-1][1])
        self.assertIn("Ekuitas: $11.00", self.api.messages[-1][1])
        self.assertEqual(self.workers.started, [])

    def test_real_monitoring_alias_and_paging_uses_full_snapshot(self):
        data = snapshot("live")
        data["positions"] = [{"title": f"Market {index}"} for index in range(42)]
        self.reader.side_effect = None
        self.reader.return_value = data
        self.controller.dispatch(update("/posisi real 2"))
        self.finish_view()
        self.reader.assert_called_once_with("live")
        text = self.api.messages[-1][1]
        self.assertIn("Halaman 2/5", text)
        self.assertIn("11. Market 10", text)
        self.assertIn("20. Market 19", text)
        self.assertIn("/posisi live 3", text)
        self.assertNotIn("Market 20", text)
        self.assertEqual(self.workers.started, [])

    def test_unknown_mode_and_extra_arguments_never_reach_snapshot_reader(self):
        for identifier, command in enumerate((
            "/saldo live;whoami", "/ringkasan ../../.env", "/scan live extra",
            "/posisi live 0", "/posisi live -1", "/aktivitas live 10000",
            "/posisi live 2 extra", "/risiko $(whoami)",
        ), 1):
            self.controller.dispatch(update(command, identifier=identifier))
        self.reader.assert_not_called()
        self.assertIsNone(self.controller.pending)
        self.assertEqual(self.workers.started, [])
        self.assertTrue(all("Gunakan /" in message[1] for message in self.api.messages))

    def test_slow_snapshot_does_not_block_stop_or_launch_duplicate_snapshot(self):
        started, release = threading.Event(), threading.Event()

        def slow_reader(mode):
            started.set()
            if not release.wait(timeout=3):
                raise RuntimeError("Test did not release snapshot thread")
            return snapshot(mode)

        self.controller.snapshot_reader = slow_reader
        try:
            self.controller.dispatch(update("/ringkasan live", identifier=1))
            self.assertTrue(started.wait(timeout=1))
            self.controller.dispatch(update("/live_off", identifier=2))
            self.assertEqual(self.workers.stopped, ["live"])
            self.assertFalse(self.controller.pending[0].done())
            self.controller.dispatch(update("/posisi paper", identifier=3))
            self.assertIn("Data sedang diambil", self.api.messages[-1][1])
        finally:
            release.set()
        self.finish_view()

    def test_snapshot_failures_are_sanitized(self):
        self.reader.side_effect = RuntimeError("SECRET_TOKEN private-key env-data")
        self.controller.dispatch(update("/ringkasan live"))
        future = self.controller.pending[0]
        with self.assertRaises(RuntimeError):
            future.result(timeout=2)
        self.controller.tick()
        self.assertIsNone(self.controller.pending)
        self.assertIn("Data belum tersedia", self.api.messages[-1][1])
        self.assertNotIn("SECRET_TOKEN", repr(self.api.messages))

    def test_send_errors_are_sanitized_and_never_retried(self):
        self.api.send_error = TelegramError("SECRET_TOKEN", code=429, retry_after=3)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.controller.dispatch(update("/status"))
            self.controller.tick()
        self.assertEqual(len(self.api.messages), 1)
        self.assertEqual(json.loads(output.getvalue()), {"telegram_send_failed": 429})
        self.assertNotIn("SECRET_TOKEN", output.getvalue())

    def test_graph_photo_uses_owner_and_sanitizes_send_failure(self):
        self.api.photo_error = TelegramError("SECRET_PHOTO_TOKEN", code=500)
        output = io.StringIO()
        with patch("polybot.telegram.chart_png", return_value=b"PNG-test-fixture"), contextlib.redirect_stdout(output):
            self.controller.dispatch(update("/grafik live"))
            self.finish_view()
            self.controller.tick()
        self.assertEqual(len(self.api.photos), 1)
        self.assertEqual(self.api.photos[0][0], OWNER)
        self.assertEqual(self.api.photos[0][1], b"PNG-test-fixture")
        self.assertIn("AKUN REAL", self.api.photos[0][2])
        self.assertNotIn("SECRET_PHOTO_TOKEN", output.getvalue())
        self.assertEqual(json.loads(output.getvalue()), {"telegram_send_failed": 500})

    def test_notification_preference_persists_only_boolean(self):
        self.controller.dispatch(update("/notifikasi_off"))
        self.assertFalse(self.controller.notifications)
        self.assertEqual(json.loads(self.controller.preferences.read_text()), {"notifications": False})
        self.controller.dispatch(update("/notifikasi_on", identifier=2))
        self.assertTrue(self.controller.notifications)
        self.assertEqual(json.loads(self.controller.preferences.read_text()), {"notifications": True})

    def test_single_cycle_report_is_sent_with_notifications_disabled(self):
        self.controller.notifications = False
        self.controller.dispatch(update("/paper_once"))
        self.workers.events = [{"mode": "paper", "type": "cycle", "report": {
            "scanned": 100, "analyzed": 5, "qualified_count": 0, "orders_count": 0,
        }}]
        self.controller.tick()
        self.assertIn("hasil siklus", self.api.messages[-1][1])
        self.assertIn("Dipindai: 100", self.api.messages[-1][1])
        self.assertEqual(self.controller.once_modes, set())

    def test_order_notifications_distinguish_no_entry_and_no_exit_in_both_modes(self):
        for mode in ("paper", "live"):
            with self.subTest(mode=mode):
                report = safe_report(safe_report({"orders": [
                    {"side": "BUY", "outcome": "No", "status": "filled", "entry_kind": "forecast"},
                    {"side": "SELL", "outcome": "No", "status": "filled", "entry_kind": "forecast"},
                    {"side": "BUY", "outcome": "Yes", "status": "pending", "entry_kind": "forecast"},
                ]}))
                self.workers.events = [{"mode": mode, "type": "cycle", "report": report}]
                self.controller.tick()
                recipient, message, _ = self.api.messages[-1]
                self.assertEqual(OWNER, recipient)
                self.assertIn("ENTRY NO (BUY No) · filled", message)
                self.assertIn("EXIT NO (SELL No) · filled", message)
                self.assertIn("ENTRY YES (BUY Yes) · pending", message)
                self.assertNotIn("ENTRY NO (SELL", message)
                self.assertLessEqual(len(message.encode("utf-16-le")), 7600)
        self.assertEqual([], self.workers.started)

    def test_help_explains_negative_outcome_without_describing_sell_as_short_entry(self):
        self.controller.dispatch(update("/help"))
        message = self.api.messages[-1][1]
        self.assertIn("ENTRY NO = BUY No (peristiwa tidak terjadi)", message)
        self.assertIn("SELL menutup saham Yes/No yang sudah dimiliki", message)
        self.assertLessEqual(len(message.encode("utf-16-le")), 7600)

    def test_service_restart_discards_backlog_without_running_old_live_command(self):
        self.api.batches = [[update("/live_on", identifier=90)]]
        stop_path = self.root / "stop"
        stop_path.touch()
        self.controller.run(stop_path)
        self.assertEqual(self.api.polls, [(-1, 0)])
        self.assertEqual(self.controller.offset, 91)
        self.assertEqual(self.api.commands_set, 1)
        self.assertEqual(self.workers.started, [])
        self.assertTrue(self.workers.closed)


class TelegramPairingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp.name) / ".env"

    def tearDown(self):
        self.temp.cleanup()

    def test_backlog_offset_only_accepts_integer_update_ids(self):
        api = FakeAPI([[{"update_id": 3}, {"update_id": "90"}, {"update_id": True}, {"update_id": 7}]])
        self.assertEqual(discard_backlog(api), 8)
        self.assertEqual(api.polls, [(-1, 0)])
        self.assertEqual(discard_backlog(FakeAPI([[]])), 0)

    def test_pair_ignores_wrong_nonce_and_unicode_then_saves_only_matching_owner(self):
        original = "# preserve comment\nTELEGRAM_BOT_TOKEN=unit-test-token\nOTHER_KEY=preserve-this\n"
        self.env_path.write_text(original, encoding="utf-8")
        batch = [
            update("/pair wrong-code", identifier=11, owner=OWNER + 1),
            update("halo \U0001f44b", identifier=12, owner=OWNER + 1),
            update(f"/pair {NONCE}", identifier=13, owner=OWNER + 1,
                   chat={"id": -1001, "type": "group"}),
            update(f"/pair {NONCE}", identifier=14, forward_origin={"type": "user"}),
            update(f"/start pair_{NONCE}", identifier=15),
        ]
        api = FakeAPI([[{"update_id": 10}], batch, []])
        output = io.StringIO()
        with patch("polybot.telegram.secrets.token_urlsafe", return_value=NONCE), contextlib.redirect_stdout(output):
            owner = pair(api, self.env_path, BOT, timeout=1)
        self.assertEqual(owner, OWNER)
        self.assertEqual(self.env_path.read_text(), original + f"TELEGRAM_ALLOWED_USER_ID={OWNER}\n")
        self.assertEqual(api.polls, [(-1, 0), (11, 5), (16, 0)])
        self.assertEqual(len(api.messages), 1)
        self.assertEqual(api.messages[0][0], OWNER)
        self.assertNotIn("unit-test-token", output.getvalue())
        self.assertFalse(self.env_path.with_name(".env.telegram.tmp").exists())

    def test_pair_accepts_direct_nonce_command(self):
        api = FakeAPI([[], [update(f"/pair {NONCE}")], []])
        with patch("polybot.telegram.secrets.token_urlsafe", return_value=NONCE), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pair(api, self.env_path, BOT, timeout=1), OWNER)
        self.assertEqual(self.env_path.read_text(), f"TELEGRAM_ALLOWED_USER_ID={OWNER}\n")

    def test_unpaired_start_help_and_pair_explain_setup_without_binding_or_leaking_secrets(self):
        original = ("TELEGRAM_BOT_TOKEN=unit-test-secret-token\n"
                    "POLYMARKET_PRIVATE_KEY=unit-test-secret-key\n"
                    "POLYMARKET_WALLET_ADDRESS=unit-test-account-address\n")
        for command in ("/start", "/help", "/pair", f"/start@{BOT.upper()}"):
            with self.subTest(command=command):
                self.env_path.write_text(original, encoding="utf-8")
                api = FakeAPI([[], [update(command)],
                               [update(f"/pair {NONCE}", identifier=2)], []])
                get_updates = api.get_updates

                def check_before_next_message(offset=None, timeout=25):
                    if offset == 2:
                        self.assertEqual(self.env_path.read_text(), original)
                        self.assertEqual(len(api.messages), 1)
                        recipient, text, keyboard = api.messages[0]
                        self.assertEqual(recipient, OWNER)
                        self.assertIn("pair", text.lower())
                        self.assertIn("/pair", text)
                        self.assertFalse(keyboard)
                        for secret in (NONCE, "unit-test-secret-token", "unit-test-secret-key",
                                       "unit-test-account-address"):
                            self.assertNotIn(secret, text)
                    return get_updates(offset=offset, timeout=timeout)

                api.get_updates = check_before_next_message
                with patch("polybot.telegram.secrets.token_urlsafe", return_value=NONCE), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(pair(api, self.env_path, BOT, timeout=1), OWNER)
                self.assertEqual(len(api.messages), 2)
                self.assertIn("Pairing berhasil", api.messages[-1][1])
                self.assertEqual(self.env_path.read_text(),
                                 original + f"TELEGRAM_ALLOWED_USER_ID={OWNER}\n")

    def test_unpaired_onboarding_alone_never_creates_owner(self):
        stop_path = self.env_path.parent / "service.stop"
        api = FakeAPI([[], [update("/start")], []])
        get_updates = api.get_updates

        def stop_after_onboarding(offset=None, timeout=25):
            if offset == 2:
                stop_path.touch()
            return get_updates(offset=offset, timeout=timeout)

        api.get_updates = stop_after_onboarding
        with patch("polybot.telegram.secrets.token_urlsafe", return_value=NONCE), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, "dihentikan"):
            pair(api, self.env_path, BOT, timeout=1, stop_path=stop_path)
        self.assertFalse(self.env_path.exists())
        self.assertEqual(len(api.messages), 1)
        self.assertNotIn(NONCE, api.messages[0][1])

    def test_unpaired_repeated_commands_send_one_prompt_per_private_sender(self):
        messages = [update(command, identifier=index)
                    for index, command in enumerate(("/start", "/start", "/help", "/pair"), 1)]
        messages.extend([
            update("/start", identifier=5, owner=OWNER + 1),
            update("/help", identifier=6, owner=OWNER + 1),
            update(f"/pair {NONCE}", identifier=7),
        ])
        api = FakeAPI([[], messages, []])
        with patch("polybot.telegram.secrets.token_urlsafe", return_value=NONCE), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pair(api, self.env_path, BOT, timeout=1), OWNER)
        self.assertEqual([message[0] for message in api.messages], [OWNER, OWNER + 1, OWNER])
        self.assertEqual(api.messages[0][1], api.messages[1][1])
        self.assertNotIn(NONCE, api.messages[0][1])
        self.assertIn("Pairing berhasil", api.messages[-1][1])

    def test_unpaired_onboarding_ignores_groups_forwarded_and_wrong_bot_commands(self):
        rejected = [
            update("/start", chat={"id": -1001, "type": "group"}),
            update("/start", chat={"id": -1001, "type": "supergroup"}),
            update("/start", forward_origin={"type": "user"}),
            update("/help", forward_from={"id": OWNER}),
            update("/pair", forward_date=int(time.time())),
            update("/start", sender_chat={"id": OWNER}),
            update("/start", **{"from": {"id": OWNER, "is_bot": True}}),
            update("/start@Another_bot"),
            update("/help@Another_bot"),
            update("/pair@Another_bot"),
        ]
        for index, row in enumerate(rejected, 1):
            row["update_id"] = index
        rejected.append(update(f"/pair {NONCE}", identifier=100))
        api = FakeAPI([[], rejected, []])
        with patch("polybot.telegram.secrets.token_urlsafe", return_value=NONCE), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pair(api, self.env_path, BOT, timeout=1), OWNER)
        self.assertEqual(len(api.messages), 1)
        self.assertIn("Pairing berhasil", api.messages[0][1])

    def test_unpaired_onboarding_prompt_limit_does_not_prevent_valid_pairing(self):
        messages = [update("/start", identifier=index, owner=OWNER + index)
                    for index in range(1, 102)]
        messages.append(update(f"/pair {NONCE}", identifier=102, owner=OWNER + 101))
        api = FakeAPI([[], messages, []])
        with patch("polybot.telegram.secrets.token_urlsafe", return_value=NONCE), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pair(api, self.env_path, BOT, timeout=1), OWNER + 101)
        self.assertEqual(len(api.messages), 101)
        self.assertEqual([message[0] for message in api.messages[:-1]],
                         [OWNER + index for index in range(1, 101)])
        self.assertEqual(api.messages[-1][0], OWNER + 101)
        self.assertIn("Pairing berhasil", api.messages[-1][1])

    def test_pair_restart_reuses_challenge_and_keeps_queued_matching_message(self):
        nonce = "abcdefghijklmnopqrstuvwx"
        challenge_path = self.env_path.parent / "state" / "pairing.json"
        stop_path = self.env_path.parent / "service.stop"
        first_api = FakeAPI([[], []])
        first_poll = first_api.get_updates

        def stop_after_first_poll(offset=None, timeout=25):
            rows = first_poll(offset=offset, timeout=timeout)
            if offset != -1:
                stop_path.touch()
            return rows

        first_api.get_updates = stop_after_first_poll
        with patch("polybot.telegram.secrets.token_urlsafe", return_value=nonce), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, "dihentikan"):
            pair(first_api, self.env_path, BOT, timeout=300,
                 challenge_path=challenge_path, stop_path=stop_path)
        persisted = json.loads(challenge_path.read_text())
        self.assertEqual(persisted["nonce"], nonce)
        self.assertEqual(persisted["username"], BOT)
        self.assertFalse(self.env_path.exists())
        stop_path.unlink()

        resumed_api = FakeAPI([[update(f"/pair {nonce}", identifier=71)], []])
        output = io.StringIO()
        with patch("polybot.telegram.secrets.token_urlsafe", return_value="ABCDEFGHIJKLMNOPQRSTUVWX"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(pair(resumed_api, self.env_path, BOT, timeout=300,
                                  challenge_path=challenge_path, stop_path=stop_path), OWNER)
        self.assertEqual(resumed_api.polls, [(0, 5), (72, 0)])
        self.assertFalse(challenge_path.exists())
        self.assertFalse(challenge_path.with_suffix(".tmp").exists())
        self.assertEqual(self.env_path.read_text(), f"TELEGRAM_ALLOWED_USER_ID={OWNER}\n")
        self.assertEqual(len(resumed_api.messages), 1)
        self.assertIn(f"/pair {nonce}", output.getvalue())
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWX", output.getvalue())

    def test_pair_replaces_expired_wrong_bot_and_invalid_nonce_challenges(self):
        now = 1_800_000_000.0
        fresh_nonce = "ABCDEFGHIJKLMNOPQRSTUVWX"
        valid = {"username": BOT, "nonce": "abcdefghijklmnopqrstuvwx", "expires_at": now + 300}
        invalid = [
            {**valid, "username": "Another_Bot"},
            {**valid, "expires_at": now - 1},
            {**valid, "expires_at": now + 86401},
            {**valid, "expires_at": True},
            {**valid, "nonce": "too-short"},
            {**valid, "nonce": "abcdefghijklmnopqrstuvw!"},
        ]
        for index, saved in enumerate(invalid):
            with self.subTest(saved=saved):
                case_root = self.env_path.parent / str(index)
                case_root.mkdir()
                challenge_path = case_root / "pairing.json"
                challenge_path.write_text(json.dumps(saved), encoding="utf-8")
                stop_path = case_root / "service.stop"
                api = FakeAPI([[], []])
                get_updates = api.get_updates

                def stop_poll(offset=None, timeout=25):
                    result = get_updates(offset=offset, timeout=timeout)
                    if offset != -1:
                        stop_path.touch()
                    return result

                api.get_updates = stop_poll
                with patch("polybot.telegram.time.time", return_value=now), \
                        patch("polybot.telegram.secrets.token_urlsafe", return_value=fresh_nonce), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        self.assertRaisesRegex(RuntimeError, "dihentikan"):
                    pair(api, self.env_path, BOT, timeout=300,
                         challenge_path=challenge_path, stop_path=stop_path)
                replacement = json.loads(challenge_path.read_text())
                self.assertEqual(replacement, {
                    "username": BOT, "nonce": fresh_nonce, "expires_at": now + 300,
                })
                self.assertEqual(api.polls, [(-1, 0), (0, 5)])
                self.assertFalse(self.env_path.exists())
                self.assertEqual(api.messages, [])

    def test_pair_accepts_whitespace_bot_mention_and_sentence_period(self):
        commands = [
            f"  /pair   {NONCE}  \n",
            f"/pair@{BOT.upper()}\t{NONCE}",
            f"/pair {NONCE}.",
            f"/pair {NONCE} .",
            f"\n/start@{BOT}  pair_{NONCE}. \n",
        ]
        for command in commands:
            with self.subTest(command=command):
                api = FakeAPI([[], [update(command)], []])
                with patch("polybot.telegram.secrets.token_urlsafe", return_value=NONCE), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(pair(api, self.env_path, BOT, timeout=1), OWNER)
                self.assertEqual(len(api.messages), 1)
                self.assertEqual(api.messages[0][0], OWNER)

    def test_pair_format_tolerance_does_not_accept_wrong_code_bot_or_extra_arguments(self):
        rejected = [
            f"/pair@Another_Bot {NONCE}",
            f"/pair {NONCE}..",
            f"/pair {NONCE} extra",
            f"/pair {NONCE}-wrong.",
            f"/unknown {NONCE}",
            f"/start {NONCE}",
            f"/pair pair_{NONCE}",
        ]
        batch = [update(command, identifier=index, owner=OWNER + 1)
                 for index, command in enumerate(rejected, 1)]
        batch.append(update(f"/pair {NONCE}", identifier=100))
        api = FakeAPI([[], batch, []])
        with patch("polybot.telegram.secrets.token_urlsafe", return_value=NONCE), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pair(api, self.env_path, BOT, timeout=1), OWNER)
        self.assertEqual(self.env_path.read_text(), f"TELEGRAM_ALLOWED_USER_ID={OWNER}\n")
        self.assertEqual(len(api.messages), 1)
        self.assertEqual(api.messages[0][0], OWNER)

    def test_pair_heartbeat_counts_updates_without_logging_received_message(self):
        sensitive_text = "PRIVATE_MESSAGE_CONTENT_must_not_appear_in_logs"
        batches = [[], [update(sensitive_text, identifier=1)],
                   [update(f"/pair {NONCE}", identifier=2)], []]
        api = FakeAPI(batches)
        poll = api.get_updates
        clock = [100.0]

        def elapsed_poll(offset=None, timeout=25):
            clock[0] += 31
            return poll(offset=offset, timeout=timeout)

        api.get_updates = elapsed_poll
        output = io.StringIO()
        with patch("polybot.telegram.secrets.token_urlsafe", return_value=NONCE), \
                patch("polybot.telegram.time.monotonic", side_effect=lambda: clock[0]), \
                contextlib.redirect_stdout(output):
            self.assertEqual(pair(api, self.env_path, BOT, timeout=300), OWNER)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([event for event in events if "pairing_poll" in event], [
            {"pairing_poll": 1, "updates_received": 0},
            {"pairing_poll": 2, "updates_received": 1},
            {"pairing_poll": 3, "updates_received": 2},
        ])
        self.assertNotIn(sensitive_text, output.getvalue())

    def test_pair_recovers_transient_backlog_and_poll_errors_without_losing_offset(self):
        api = FakeAPI()
        api.get_updates = Mock(side_effect=[
            TelegramError("SECRET_NETWORK_DETAIL"),
            [{"update_id": 40}],
            TelegramError("SECRET_UPSTREAM_DETAIL", code=500),
            [update(f"/start pair_{NONCE}", identifier=41)],
            [],
        ])
        clock = [0.0]
        output = io.StringIO()

        def advance(seconds):
            clock[0] += seconds

        with patch("polybot.telegram.secrets.token_urlsafe", return_value=NONCE), \
                patch("polybot.telegram.time.monotonic", side_effect=lambda: clock[0]), \
                patch("polybot.telegram.time.sleep", side_effect=advance), \
                contextlib.redirect_stdout(output):
            self.assertEqual(pair(api, self.env_path, BOT, timeout=30), OWNER)
        self.assertEqual(api.get_updates.call_args_list, [
            call(offset=-1, timeout=0), call(offset=-1, timeout=0),
            call(offset=41, timeout=5), call(offset=41, timeout=5),
            call(offset=42, timeout=0),
        ])
        self.assertEqual(self.env_path.read_text(), f"TELEGRAM_ALLOWED_USER_ID={OWNER}\n")
        self.assertEqual(len(api.messages), 1)
        self.assertGreater(clock[0], 0)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row["pairing_network_retry"] for row in events if "pairing_network_retry" in row], [None, 500])
        self.assertNotIn("SECRET_", output.getvalue())

    def test_pair_stop_during_poll_does_not_bind_returned_matching_owner(self):
        original = "KEEP=unchanged\n"
        self.env_path.write_text(original, encoding="utf-8")
        stop_path = self.env_path.parent / "service.stop"
        api = FakeAPI()

        def polling(offset=None, timeout=25):
            if offset == -1:
                return []
            stop_path.write_text("stop", encoding="utf-8")
            return [update(f"/start pair_{NONCE}")]

        api.get_updates = Mock(side_effect=polling)
        with patch("polybot.telegram.secrets.token_urlsafe", return_value=NONCE), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, "dihentikan"):
            pair(api, self.env_path, BOT, timeout=30, stop_path=stop_path)
        self.assertEqual(self.env_path.read_text(), original)
        self.assertEqual(api.messages, [])
        self.assertEqual(api.get_updates.call_count, 2)

    def test_setup_runs_controller_with_newly_paired_owner_without_starting_trading(self):
        root = self.env_path.parent
        config_path = root / "config.toml"
        state_dir = root / "data"
        stop_path = state_dir / "telegram-service.stop"
        api = Mock()
        api.get_me.return_value = {"username": BOT}
        api.call.return_value = {"url": ""}
        workers = FakeWorkers()
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), \
                patch("polybot.telegram.Path.cwd", return_value=root), \
                patch("polybot.telegram.load_env"), \
                patch("polybot.telegram.load_config", return_value=SimpleNamespace(state_dir="data")), \
                patch("polybot.telegram.TelegramAPI", return_value=api), \
                patch("polybot.telegram.pair", return_value=OWNER) as pairing, \
                patch("polybot.telegram.TradingWorkers", return_value=workers) as worker_factory, \
                patch("polybot.telegram.TelegramController") as controller_factory, \
                contextlib.redirect_stdout(output):
            result = main(["--setup", "--pair-timeout", "120"])
        self.assertEqual(result, 0)
        pairing.assert_called_once_with(api, self.env_path, BOT, timeout=120, stop_path=stop_path,
                                        challenge_path=stop_path.parent / "telegram-pairing.json")
        worker_factory.assert_called_once_with(config_path, root)
        controller_factory.assert_called_once_with(api, OWNER, workers, root, config_path, state_dir, BOT)
        controller_factory.return_value.run.assert_called_once_with(stop_path)
        self.assertEqual(workers.started, [])
        self.assertEqual(json.loads(output.getvalue()), {
            "telegram": "running", "bot": "@" + BOT, "trading_started": False,
        })

    def test_setup_stop_marker_after_pair_prevents_controller_and_worker_creation(self):
        root = self.env_path.parent
        stop_path = root / "data" / "telegram-service.stop"
        api = Mock()
        api.get_me.return_value = {"username": BOT}
        api.call.return_value = {"url": ""}

        def paired_then_stopped(*args, **kwargs):
            stop_path.parent.mkdir(parents=True, exist_ok=True)
            stop_path.write_text("stop", encoding="utf-8")
            return OWNER

        with patch.dict("os.environ", {}, clear=True), \
                patch("polybot.telegram.Path.cwd", return_value=root), \
                patch("polybot.telegram.load_env"), \
                patch("polybot.telegram.load_config", return_value=SimpleNamespace(state_dir="data")), \
                patch("polybot.telegram.TelegramAPI", return_value=api), \
                patch("polybot.telegram.pair", side_effect=paired_then_stopped), \
                patch("polybot.telegram.TradingWorkers") as worker_factory, \
                patch("polybot.telegram.TelegramController") as controller_factory, \
                contextlib.redirect_stdout(io.StringIO()):
            result = main(["--setup"])
        self.assertEqual(result, 0)
        worker_factory.assert_not_called()
        controller_factory.assert_not_called()
        self.assertTrue(stop_path.exists())

    def test_save_owner_preserves_other_keys_and_replaces_duplicate_owner_settings(self):
        self.env_path.write_text("KEEP=unchanged\nTELEGRAM_ALLOWED_USER_ID=12\n"
                                 "# another comment\nTELEGRAM_ALLOWED_USER_ID=34\n", encoding="utf-8")
        save_owner(self.env_path, OWNER)
        saved = self.env_path.read_text()
        self.assertEqual(saved.count("TELEGRAM_ALLOWED_USER_ID="), 1)
        self.assertIn(f"TELEGRAM_ALLOWED_USER_ID={OWNER}", saved)
        self.assertIn("KEEP=unchanged", saved)
        self.assertIn("# another comment", saved)
        for bad in (0, -1, True, "invalid"):
            with self.assertRaises(ValueError):
                save_owner(self.env_path, bad)
        self.assertEqual(self.env_path.read_text(), saved)

    def test_pair_timeout_does_not_create_owner_or_send_message(self):
        api = FakeAPI([[]])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "kedaluwarsa"):
            pair(api, self.env_path, BOT, timeout=0)
        self.assertFalse(self.env_path.exists())
        self.assertEqual(api.messages, [])

    def test_missing_token_check_never_accesses_env_file_or_network(self):
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), \
                patch("polybot.telegram.load_env") as env_loader, \
                patch("polybot.telegram.load_config", return_value=SimpleNamespace(state_dir="data")), \
                patch("polybot.telegram_api.build_opener") as network, \
                contextlib.redirect_stdout(output):
            result = main(["--check"])
        self.assertEqual(result, 2)
        env_loader.assert_called_once()  # Stubbed: no real .env is opened.
        network.assert_not_called()
        self.assertIn("TELEGRAM_BOT_TOKEN", json.loads(output.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
