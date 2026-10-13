from __future__ import annotations

import json
import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from .strategy import timestamp, utcnow


class State:
    def __init__(self, path: Path, starting_cash: float):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions (token_id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, status TEXT NOT NULL, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, time TEXT NOT NULL, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY, time TEXT NOT NULL, value TEXT NOT NULL);
        """)
        if self.get("cash") is None:
            with self.db:
                self.set("cash", starting_cash)
                self.set("initial_cash", starting_cash)
                self.set("peak_equity", starting_cash)
                self.set("day", utcnow().date().isoformat())
                self.set("day_start_equity", starting_cash)

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value, allow_nan=False)))

    def positions(self) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute("SELECT value FROM positions")]

    def intents(self) -> list[dict]:
        return [dict(json.loads(row[0]), id=row[1], status=row[2]) for row in self.db.execute("SELECT value,id,status FROM intents WHERE status IN ('pending','unknown')")]

    def journal(self, plan: dict, side: str, before_cash: float, before_shares: float) -> str:
        identifier = uuid.uuid4().hex
        value = {"plan": plan, "side": side, "before_cash": before_cash,
                 "before_shares": before_shares, "created_at": utcnow().isoformat()}
        with self.db:
            self.db.execute("INSERT INTO intents VALUES (?,?,?)", (identifier, "pending", json.dumps(value, allow_nan=False)))
        return identifier

    def record_execution(self, identifier: str, execution: dict):
        row = self.db.execute("SELECT value,status FROM intents WHERE id=?", (identifier,)).fetchone()
        if not row or row[1] in ("filled", "rejected"):
            return
        value = json.loads(row[0])
        value["execution"] = execution
        status = execution.get("status", "unknown")
        if status not in ("filled", "rejected", "pending", "unknown"):
            status = "unknown"
        if status == "filled" and execution.get("confirmed") is not True:
            status = "pending"
        with self.db:
            if status == "filled":
                self._apply_fill(identifier, value)
            self.db.execute("UPDATE intents SET status=?,value=? WHERE id=?", (status, json.dumps(value, allow_nan=False), identifier))

    def prepared(self, identifier: str, baseline: dict):
        row = self.db.execute("SELECT value FROM intents WHERE id=?", (identifier,)).fetchone()
        value = json.loads(row[0])
        value["baseline"] = baseline
        with self.db:
            self.db.execute("UPDATE intents SET value=? WHERE id=?", (json.dumps(value, allow_nan=False), identifier))

    def _apply_fill(self, identifier: str, value: dict):
        plan, execution, side = value["plan"], value["execution"], value["side"]
        quantity, amount = float(execution["shares"]), float(execution["cash"])
        if side not in ("BUY", "SELL") or not math.isfinite(quantity) or not math.isfinite(amount) or quantity <= 0 or amount <= 0:
            raise ValueError("Invalid confirmed fill")
        cash = self.get("cash")
        pnl = 0.0
        if side == "BUY":
            if amount > plan["budget"] + 0.005 or amount > cash + 0.005:
                raise ValueError("Actual fill exceeds reserved budget; manual reconciliation required")
            if self.db.execute("SELECT 1 FROM positions WHERE token_id=?", (plan["token_id"],)).fetchone():
                raise ValueError("Duplicate position; manual reconciliation required")
            opened = timestamp(value["created_at"])
            position = {key: plan[key] for key in ("token_id", "market_id", "event_id", "question", "outcome", "long_term", "target_price", "fee_fraction")}
            position["entry_kind"] = "paper_exploratory" if plan.get("entry_kind") == "paper_exploratory" else "forecast"
            position.update(shares=quantity, cost=amount, opened_at=opened.isoformat(),
                            deadline=plan.get("deadline", (opened + timedelta(hours=plan["horizon_hours"])).isoformat()))
            self.db.execute("INSERT INTO positions VALUES (?,?)", (plan["token_id"], json.dumps(position, allow_nan=False)))
            self.set("cash", cash - amount)
        else:
            row = self.db.execute("SELECT value FROM positions WHERE token_id=?", (plan["token_id"],)).fetchone()
            if not row:
                raise ValueError("Position missing; manual reconciliation required")
            position = json.loads(row[0])
            if quantity > position["shares"] + 0.0001:
                raise ValueError("Sold quantity exceeds inventory")
            cost = position["cost"] * min(1.0, quantity / position["shares"])
            pnl = amount - cost
            remaining = position["shares"] - quantity
            if remaining < 0.0001:
                self.db.execute("DELETE FROM positions WHERE token_id=?", (plan["token_id"],))
            else:
                position.update(shares=remaining, cost=position["cost"] - cost)
                self.db.execute("UPDATE positions SET value=? WHERE token_id=?", (json.dumps(position), plan["token_id"]))
            self.set("cash", cash + amount)
        trade = {"side": side, "token_id": plan["token_id"], "question": position.get("question", ""),
                 "outcome": position.get("outcome", ""),
                 "entry_kind": position.get("entry_kind", "forecast"),
                 "shares": quantity, "cash": amount, "pnl": pnl, "reason": plan.get("reason"),
                 "order_id": execution.get("order_id")}
        self.db.execute("INSERT INTO trades VALUES (?,?,?)", (identifier, utcnow().isoformat(), json.dumps(trade)))

    def risk_gate(self, equity: float, cfg, now) -> str | None:
        with self.db:
            peak = max(equity, self.get("peak_equity"))
            self.set("peak_equity", peak)
            if self.get("day") != now.date().isoformat():
                self.set("day", now.date().isoformat())
                self.set("day_start_equity", equity)
            if self.get("day_start_equity") - equity >= cfg.daily_loss_limit:
                self.set("day_halted", now.date().isoformat())
            if peak > 0 and (peak - equity) / peak >= cfg.max_drawdown_fraction:
                self.set("drawdown_halted", True)
        if self.get("drawdown_halted"):
            return "drawdown_limit"
        if self.get("day_halted") == now.date().isoformat():
            return "daily_loss_limit"
        return None

    def decision(self, value: dict):
        with self.db:
            self.db.execute("INSERT INTO decisions(time,value) VALUES (?,?)", (utcnow().isoformat(), json.dumps(value, allow_nan=False)))
            # Bound disk growth of rejected scans.
            self.db.execute("DELETE FROM decisions WHERE id <= (SELECT COALESCE(MAX(id),0)-10000 FROM decisions)")

    def summary(self) -> dict:
        trades = [dict(json.loads(row[0]), time=row[1]) for row in self.db.execute("SELECT value,time FROM trades ORDER BY time DESC LIMIT 20")]
        return {"cash": self.get("cash"), "positions": self.positions(), "pending_intents": self.intents(),
                "peak_equity": self.get("peak_equity"), "drawdown_halted": self.get("drawdown_halted", False),
                "recent_trades": trades, "last_scan": self.get("last_scan")}


@contextmanager
def process_lock(path: Path):
    """OS-held lock is released automatically on process death (Windows and Unix)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Bot lain sedang menggunakan ledger ini") from exc
        yield
    finally:
        stream.close()
