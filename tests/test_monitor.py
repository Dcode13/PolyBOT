import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from polybot.config import Config
from polybot.monitor import build_snapshot, main


NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
WALLET = "0x" + "1" * 40


def ledger(path, *, cash=11, positions=None, trades=None, scan=None):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE positions(token_id TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE trades(id TEXT PRIMARY KEY,time TEXT,value TEXT);
            CREATE TABLE intents(id TEXT PRIMARY KEY,status TEXT,value TEXT);
        """)
        meta = {"cash": cash, "initial_cash": 11, "last_scan": scan}
        db.executemany("INSERT INTO meta VALUES (?,?)", [(key, json.dumps(value)) for key, value in meta.items()])
        for index, row in enumerate(positions or []):
            db.execute("INSERT INTO positions VALUES (?,?)", (str(index), json.dumps(row)))
        for index, row in enumerate(trades or []):
            db.execute("INSERT INTO trades VALUES (?,?,?)", (str(index), NOW.isoformat(), json.dumps(row)))
    db.close()


class ReadOnlyExchange:
    def __init__(self, rows=None, failure=None):
        self.rows = rows or []
        self.failure = failure
        self.calls = []
        self.client = self

    def cash_balance(self):
        self.calls.append("cash_balance")
        return 0.31

    def list_positions(self, **kwargs):
        self.calls.append("positions_" + kwargs["status"])
        if self.failure:
            raise RuntimeError(self.failure)
        rows = [row for row in self.rows if (row.get("status") == "CLOSED") == (kwargs["status"] == "CLOSED")]
        return [SimpleNamespace(items=rows)]

    def open_orders(self):
        self.calls.append("open_orders")
        return []

    def close(self):
        self.calls.append("close")


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.cfg = replace(Config(), state_dir=str(self.root))
        self.env = patch.dict(os.environ, {"POLYMARKET_WALLET_ADDRESS": WALLET})
        self.env.start()
        self.loader = patch("polybot.monitor.load_env")
        self.loader.start()

    def tearDown(self):
        self.loader.stop()
        self.env.stop()
        self.directory.cleanup()

    def live(self, fake, **kwargs):
        return build_snapshot(self.cfg, "live", live_factory=lambda _: fake, now=kwargs.get("now", NOW))

    def test_missing_paper_ledger_does_not_initialize_a_trading_account(self):
        result = build_snapshot(self.cfg, "paper", now=NOW)
        self.assertIsNone(result["account"]["cash"])
        self.assertFalse(result["bot"]["ledgerAvailable"])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(result["errors"][0]["code"], "paper_ledger_missing")

    def test_paper_ledger_is_unchanged_and_history_uses_observed_scan_time(self):
        path = self.root / "paper.sqlite3"
        scan = {"time": (NOW - timedelta(minutes=2)).isoformat(), "cash": 10,
                "equity": 11.2, "scanned": 400, "analyzed": 20, "qualified": [],
                "rejections": {"crypto": 250}, "private_key": "must-not-be-exported"}
        ledger(path, cash=10, positions=[{"token_id": "12", "question": "Election?", "shares": 4, "cost": 1}], scan=scan)
        before = path.read_bytes()
        result = build_snapshot(self.cfg, "paper", now=NOW)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result["account"]["equity"], 11.2)
        self.assertIsNone(result["positions"][0]["currentPrice"])
        self.assertAlmostEqual(result["account"]["totalPnl"], .2)
        self.assertEqual(result["equityHistory"], [{"time": scan["time"], "equity": 11.2}])
        self.assertNotIn("must-not-be-exported", json.dumps(result))

    def test_paper_ledger_cash_changed_since_scan_does_not_invent_a_valuation(self):
        ledger(self.root / "paper.sqlite3", cash=9,
               positions=[{"token_id": "12", "shares": 2, "cost": 1}],
               scan={"time": NOW.isoformat(), "cash": 10, "equity": 11})
        result = build_snapshot(self.cfg, "paper", now=NOW)
        self.assertIsNone(result["account"]["equity"])
        self.assertEqual(result["equityHistory"], [])

    def test_paper_entry_kind_is_whitelisted_and_scan_counts_stay_separate(self):
        kinds = ["paper_exploratory", "forecast", "untrusted-private-diagnostic", None]
        rows = [{"entry_kind": kind, "token_id": str(index), "shares": 1, "cost": .2,
                 "question": "Public market", "cash": .2, "side": "BUY", "pnl": 0}
                for index, kind in enumerate(kinds)]
        ledger(self.root / "paper.sqlite3", positions=rows, trades=rows,
               scan={"time": NOW.isoformat(), "qualified": [
                   {"entry_kind": "paper_exploratory"}, {"entry_kind": "forecast"}]})
        result = build_snapshot(self.cfg, "paper", now=NOW)
        self.assertEqual([row["entryKind"] for row in result["positions"]],
                         ["paper_exploratory", "forecast", "unknown", "unknown"])
        self.assertEqual([row["entryKind"] for row in result["trades"]],
                         ["paper_exploratory", "forecast", "unknown", "unknown"])
        self.assertEqual(result["bot"]["qualifiedCount"], 1)
        self.assertEqual(result["bot"]["exploratoryCount"], 1)
        self.assertNotIn("untrusted-private-diagnostic", json.dumps(result))

    def test_live_never_inherits_paper_exploration_or_external_entry_kind(self):
        ledger(self.root / "live.sqlite3", trades=[
            {"entry_kind": "paper_exploratory", "shares": 1, "cash": 1},
            {"entry_kind": "forecast", "shares": 1, "cash": 1}],
            scan={"time": NOW.isoformat(), "qualified": [{"entry_kind": "paper_exploratory"}]})
        result = self.live(ReadOnlyExchange([
            {"token_id": "1", "entry_kind": "paper_exploratory", "current_value": 0},
            {"token_id": "2", "entry_kind": "forecast", "current_value": 0},
        ]))
        self.assertEqual([row["entryKind"] for row in result["positions"]], ["unknown", "unknown"])
        self.assertEqual([row["entryKind"] for row in result["trades"]], ["unknown", "forecast"])
        self.assertEqual(result["bot"]["qualifiedCount"], 0)
        self.assertEqual(result["bot"]["exploratoryCount"], 0)

    def test_live_includes_old_zero_value_crypto_and_closed_history(self):
        fake = ReadOnlyExchange([
            {"token_id": "1", "title": "Bitcoin old market", "size": "2", "current_value": "0", "current_price": "0",
             "avg_price": ".5", "total_cost_usdc": "1", "realized_pnl": "0", "unrealized_pnl": "-1",
             "total_pnl": "-1", "percent_pnl": "-100", "status": "REDEEMABLE", "redeemable": True},
            {"token_id": "2", "title": "Past election", "size": "0", "current_value": "0",
             "realized_pnl": "2", "unrealized_pnl": "0", "total_pnl": "2", "status": "CLOSED"},
        ])
        result = self.live(fake)
        self.assertEqual(fake.calls, ["cash_balance", "open_orders", "positions_OPEN", "positions_CLOSED", "close"])
        self.assertEqual(result["account"]["equity"], .31)
        self.assertEqual(result["account"]["realizedPnl"], 2)
        self.assertEqual(result["account"]["totalPnl"], 1)
        self.assertTrue(result["account"]["historyComplete"])
        self.assertEqual(result["account"]["positionsCount"], 2)
        self.assertEqual(result["positions"][0]["pnlPercent"], -100)
        self.assertEqual(result["positions"][0]["category"], "Unknown")
        self.assertFalse((self.root / "live.sqlite3").exists())

    def test_live_error_keeps_cash_does_not_publish_exception_or_fake_zero_equity(self):
        fake = ReadOnlyExchange(failure="PRIVATE_KEY=secret-value")
        result = self.live(fake)
        self.assertEqual(result["account"]["cash"], .31)
        self.assertIsNone(result["account"]["equity"])
        self.assertFalse(result["account"]["positionsComplete"])
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertEqual(result["equityHistory"], [])

    def test_history_survives_connection_failure_and_is_wallet_isolated(self):
        first = self.live(ReadOnlyExchange())
        broken = self.live(ReadOnlyExchange(failure="offline"), now=NOW + timedelta(minutes=1))
        self.assertEqual(first["equityHistory"], broken["equityHistory"])
        with patch.dict(os.environ, {"POLYMARKET_WALLET_ADDRESS": "0x" + "2" * 40}):
            other = self.live(ReadOnlyExchange(failure="offline"))
        self.assertEqual(other["equityHistory"], [])

    def test_history_deduplicates_and_returns_last_of_maximum_200_points(self):
        first = self.live(ReadOnlyExchange())
        second = self.live(ReadOnlyExchange(), now=NOW + timedelta(seconds=20))
        self.assertEqual(first["equityHistory"], second["equityHistory"])
        path = self.root / "monitor.sqlite3"
        with sqlite3.connect(path) as db:
            for index in range(1, 205):
                stamp = (NOW + timedelta(minutes=index)).isoformat()
                db.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)", ("live", WALLET.lower(), stamp, index, index, 0))
        db.close()
        result = self.live(ReadOnlyExchange(failure="offline"), now=NOW + timedelta(minutes=205))
        points = result["equityHistory"]
        self.assertEqual(len(points), 200)
        self.assertEqual(points[-1]["equity"], 204)
        self.assertEqual(points[0], first["equityHistory"][0])
        self.assertEqual(points, sorted(points, key=lambda row: row["time"]))

    def test_stop_status_never_claims_bot_is_running(self):
        (self.root / "STOP").write_text("paused")
        result = self.live(ReadOnlyExchange())
        self.assertEqual(result["bot"]["status"], "stopped")
        self.assertFalse(result["bot"]["ledgerAvailable"])

    def test_cli_is_valid_json_even_when_sdk_prints_or_raises_secrets(self):
        def broken(*args, **kwargs):
            print("PRIVATE_KEY=secret-value")
            raise RuntimeError("PRIVATE_KEY=secret-value")
        output = io.StringIO()
        with patch("polybot.monitor.build_snapshot", side_effect=broken), contextlib.redirect_stdout(output):
            code = main(["--mode", "live", "--output-json"])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertIsNone(payload["account"]["equity"])
        self.assertNotIn("secret-value", output.getvalue())
        self.assertEqual(payload["errors"][0]["code"], "monitor_unavailable")


if __name__ == "__main__":
    unittest.main()
