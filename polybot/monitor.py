"""Account monitoring only; never starts an engine or mutates a trading ledger.

The only writes are independent equity observations in monitor.sqlite3. Values
from the exchange and local paper ledger are deliberately never mixed.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .__main__ import load_env
from .config import Config, load_config
from .exchange import LiveExchange, PublicExchange, _page_items, _record


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _text(value: Any, limit: int = 500) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _sum(values: list[float | None]) -> float | None:
    return sum(values) if all(value is not None for value in values) else None


def _error(result: dict, code: str, message: str) -> None:
    result["errors"].append({"code": code, "message": message})


def _empty(mode: str, now: datetime) -> dict:
    return {
        "generatedAt": now.isoformat(), "mode": mode,
        "account": {
            "wallet": None, "connected": False, "cash": None,
            "positionsValue": None, "equity": None, "realizedPnl": None,
            "unrealizedPnl": None, "totalPnl": None, "openOrdersCount": None,
            "positionsCount": 0, "activePositionsCount": 0,
            "redeemablePositionsCount": 0, "positionsComplete": False,
            "historyComplete": False,
            "pnlScope": "unavailable", "equityAsOf": None,
        },
        "positions": [], "trades": [], "tradesSource": "bot_ledger",
        "equityHistory": [], "historySource": "monitor_snapshots",
        "bot": {
            "status": "unknown", "lastScan": None, "lastScanAt": None,
            "stopped": False, "pendingCount": 0, "ledgerAvailable": False,
            "ledgerCash": None, "ledgerEquity": None, "rejections": [],
            "scanned": 0, "analyzed": 0, "qualifiedCount": 0, "exploratoryCount": 0,
        },
        "risk": {}, "errors": [],
    }


def _ledger(path: Path) -> dict | None:
    if not path.is_file():
        return None
    # URI mode=ro also prevents accidentally creating an empty bot ledger.
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        meta = {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM meta")}
        positions = [json.loads(row[0]) for row in connection.execute("SELECT value FROM positions")]
        rows = list(connection.execute("SELECT id,time,value FROM trades ORDER BY time DESC"))
        trades = [{"id": row[0], "time": row[1], **json.loads(row[2])} for row in rows]
        pending = connection.execute("SELECT COUNT(*) FROM intents WHERE status IN ('pending','unknown')").fetchone()[0]
        return {"meta": meta, "positions": positions, "trades": trades, "pending": pending}
    finally:
        connection.close()


def _safe_scan(raw: Any, mode: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    rejections = raw.get("rejections", {})
    if not isinstance(rejections, dict):
        rejections = {}
    qualified = [row for row in raw.get("qualified", []) if isinstance(row, dict)] if isinstance(raw.get("qualified"), list) else []
    return {
        "time": _text(raw.get("time")), "cash": _number(raw.get("cash")),
        "equity": _number(raw.get("equity")), "scanned": _number(raw.get("scanned")) or 0,
        "analyzed": _number(raw.get("analyzed")) or 0,
        "qualifiedCount": sum(row.get("entry_kind") != "paper_exploratory" for row in qualified),
        "exploratoryCount": (sum(row.get("entry_kind") == "paper_exploratory" for row in qualified)
                             if mode == "paper" else 0),
        "halted": _text(raw.get("halted")) or None,
        "rejections": {str(reason)[:100]: int(count) for reason, value in rejections.items()
                       if (count := _number(value)) is not None and count >= 0},
    }


def _entry_kind(row: dict, mode: str, source: str = "ledger") -> str:
    if source != "ledger":
        return "unknown"
    kind = row.get("entry_kind")
    if kind == "paper_exploratory" and mode == "paper":
        return "paper_exploratory"
    return "forecast" if kind == "forecast" else "unknown"


def _position(row: dict, source: str, *, mode: str = "live") -> dict:
    size = _number(row.get("size", row.get("current_size", row.get("shares"))))
    cost = _number(row.get("total_cost_usdc" if source == "polymarket" else "cost"))
    average = _number(row.get("avg_price"))
    if source == "ledger" and size and cost is not None:
        average = cost / size
    price = _number(row.get("current_price"))
    value = _number(row.get("current_value"))
    if value is None and price is not None and size is not None:
        value = price * size
    pnl = _number(row.get("total_pnl"))
    return {
        "id": _text(row.get("token_id", row.get("asset_id")), 200),
        "title": _text(row.get("title", row.get("question"))) or "Pasar tanpa judul",
        "slug": _text(row.get("slug")), "eventSlug": _text(row.get("event_slug")),
        "outcome": _text(row.get("outcome")) or "—", "size": size,
        "avgPrice": average, "currentPrice": price, "value": value, "cost": cost,
        "pnl": pnl, "pnlPercent": _number(row.get("percent_pnl")),
        "realizedPnl": _number(row.get("realized_pnl")),
        "unrealizedPnl": _number(row.get("unrealized_pnl")),
        "status": _text(row.get("status")) or "OPEN",
        "redeemable": row.get("redeemable") is True,
        "category": _text(row.get("category")) or "Unknown", "source": source,
        "entryKind": _entry_kind(row, mode, source),
    }


def _trades(rows: list[dict], mode: str) -> list[dict]:
    result = []
    for row in rows[:50]:
        size, cash = _number(row.get("shares")), _number(row.get("cash"))
        result.append({
            "id": _text(row.get("id")), "time": _text(row.get("time")),
            "side": _text(row.get("side")), "title": _text(row.get("question")),
            "outcome": _text(row.get("outcome")), "size": size, "cash": cash,
            "price": cash / size if size and cash is not None else None,
            "pnl": _number(row.get("pnl")), "status": "filled", "source": "ledger",
            "entryKind": _entry_kind(row, mode),
        })
    return result


def _read_bot(result: dict, ledger: dict | None, cfg: Config, now: datetime) -> None:
    bot = result["bot"]
    bot["stopped"] = (Path(cfg.state_dir) / "STOP").exists()
    if ledger:
        meta = ledger["meta"]
        scan = _safe_scan(meta.get("last_scan"), result["mode"])
        bot.update(ledgerAvailable=True, lastScan=scan, pendingCount=ledger["pending"],
                   ledgerCash=_number(meta.get("cash")),
                   ledgerEquity=scan["equity"] if scan else None,
                   lastScanAt=scan["time"] if scan else None)
        if scan:
            bot.update(scanned=scan["scanned"], analyzed=scan["analyzed"], qualifiedCount=scan["qualifiedCount"],
                       exploratoryCount=scan["exploratoryCount"],
                       rejections=[{"reason": key, "count": value} for key, value in
                                   sorted(scan["rejections"].items(), key=lambda item: item[1], reverse=True)])
            try:
                age = (now - datetime.fromisoformat(scan["time"].replace("Z", "+00:00"))).total_seconds()
                if 0 <= age <= max(cfg.poll_seconds * 3, 180):
                    bot["status"] = "recent_scan"
            except (ValueError, TypeError):
                pass
        if (meta.get("drawdown_halted") or meta.get("day_halted") == now.date().isoformat()
                or ledger["pending"] or (scan and scan["halted"])):
            bot["status"] = "halted"
        result["trades"] = _trades(ledger["trades"], result["mode"])
    if bot["stopped"]:
        bot["status"] = "stopped"


def _paper_account(result: dict, ledger: dict | None) -> None:
    if not ledger:
        _error(result, "paper_ledger_missing", "Ledger simulasi belum tersedia. Jalankan bot paper untuk membuatnya.")
        return
    account, meta = result["account"], ledger["meta"]
    positions = [_position(row, "ledger", mode="paper") for row in ledger["positions"]]
    cash = _number(meta.get("cash"))
    scan = result["bot"]["lastScan"]
    equity = cash if not positions else (scan["equity"] if scan and scan["cash"] == cash else None)
    initial = _number(meta.get("initial_cash"))
    realized = _sum([_number(row.get("pnl")) for row in ledger["trades"]])
    total = equity - initial if equity is not None and initial is not None else None
    account.update(connected=cash is not None, cash=cash, equity=equity,
                   positionsValue=equity - cash if equity is not None and cash is not None else None,
                   realizedPnl=realized, totalPnl=total,
                   unrealizedPnl=total - realized if total is not None and realized is not None else None,
                   openOrdersCount=ledger["pending"], positionsComplete=True,
                   historyComplete=True,
                   pnlScope="paper_ledger_since_initialization",
                   equityAsOf=scan["time"] if positions and scan else result["generatedAt"])
    result["positions"] = positions


def _live_positions(result: dict, live) -> None:
    rows: dict[str, dict] = {}
    complete = []
    # full_history removes time bounds; it does not include CLOSED positions.
    # OPEN is a superset containing settled-but-unredeemed holdings too.
    for status, extras, code, message in (
        ("OPEN", {"include_archived": True}, "positions_unavailable", "Posisi akun belum dapat dibaca secara lengkap."),
        ("CLOSED", {}, "closed_history_unavailable", "Riwayat posisi tertutup belum tersedia; PnL terlapor belum mencakup seluruh posisi."),
    ):
        try:
            pages = live.client.list_positions(status=status, full_history=True, **extras)
            normalized = [_position(_record(row), "polymarket") for row in _page_items(pages)]
            for row in normalized:
                # If the account changes between requests, keep the first quote.
                rows.setdefault(row["id"], row)
            complete.append(True)
        except Exception:
            complete.append(False)
            _error(result, code, message)
    result["positions"] = list(rows.values())
    result["account"].update(positionsComplete=complete[0], historyComplete=all(complete))


def _live_account(result: dict, live_factory) -> None:
    live = None
    try:
        load_env()
        wallet = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
        if re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet):
            result["account"]["wallet"] = wallet
        live = live_factory(PublicExchange(timeout=10, retries=0))
        account = result["account"]
        for method, field, code, message in (
            ("cash_balance", "cash", "balance_unavailable", "Saldo akun belum dapat dibaca."),
            ("open_orders", "openOrdersCount", "orders_unavailable", "Order terbuka belum dapat dibaca."),
        ):
            try:
                value = getattr(live, method)()
                if field == "openOrdersCount":
                    account[field] = len(value)
                else:
                    account[field] = _number(value)
                    if account[field] is None:
                        raise ValueError("Invalid balance")
            except Exception:
                # SDK/HTTP errors may embed credentials. Never serialize their text.
                _error(result, code, message)
        _live_positions(result, live)
        account["connected"] = account["cash"] is not None
        if account["positionsComplete"]:
            positions = result["positions"]
            account.update(
                positionsValue=_sum([row["value"] for row in positions]),
                realizedPnl=_sum([row["realizedPnl"] for row in positions]),
                unrealizedPnl=_sum([row["unrealizedPnl"] for row in positions]),
                totalPnl=_sum([row["pnl"] for row in positions]),
                pnlScope=("polymarket_open_and_closed_positions_not_wallet_cashflows"
                          if account["historyComplete"] else "polymarket_reported_positions_partial_history"),
            )
            if account["cash"] is not None and account["positionsValue"] is not None:
                account["equity"] = account["cash"] + account["positionsValue"]
                account["equityAsOf"] = result["generatedAt"]
    except Exception:
        _error(result, "connection_unavailable", "Koneksi akun belum tersedia. Periksa kredensial lokal, dependensi SDK, dan koneksi jaringan.")
    finally:
        if live:
            try:
                live.close()
            except Exception:
                pass


def _history(path: Path, mode: str, identity: str, account: dict, now: datetime) -> list[dict]:
    valid = account["connected"] and all(account[key] is not None for key in ("equity", "cash", "positionsValue"))
    if not path.exists() and not valid:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=2)
    try:
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS observations (mode TEXT NOT NULL, identity TEXT NOT NULL, time TEXT NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL, positions_value REAL NOT NULL, PRIMARY KEY(mode,identity,time))")
            last = connection.execute("SELECT time FROM observations WHERE mode=? AND identity=? ORDER BY time DESC LIMIT 1", (mode, identity)).fetchone()
            # Paper valuations come from the last scan and must not appear newly quoted.
            observed_at = account.get("equityAsOf")
            try:
                stamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00")) if observed_at else now
                valid = valid and timedelta(0) <= now - stamp <= timedelta(days=30)
                age = (stamp - datetime.fromisoformat(last[0])).total_seconds() if last else 31
            except (TypeError, ValueError):
                valid, age, stamp = False, 0, now
            if valid and age >= 30:
                connection.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)", (mode, identity, stamp.isoformat(), account["equity"], account["cash"], account["positionsValue"]))
            cutoff = (now - timedelta(days=30)).isoformat()
            connection.execute("DELETE FROM observations WHERE time < ?", (cutoff,))
            connection.execute("DELETE FROM observations WHERE mode=? AND identity=? AND time NOT IN (SELECT time FROM observations WHERE mode=? AND identity=? ORDER BY time DESC LIMIT 10000)", (mode, identity, mode, identity))
        rows = list(connection.execute("SELECT time,equity FROM observations WHERE mode=? AND identity=? ORDER BY time", (mode, identity)))
        if len(rows) > 200:
            rows = [rows[round(index * (len(rows) - 1) / 199)] for index in range(200)]
        return [{"time": row[0], "equity": row[1]} for row in rows]
    finally:
        connection.close()


def build_snapshot(cfg: Config, mode: str, *, live_factory=None, now: datetime | None = None) -> dict:
    if mode not in ("live", "paper"):
        raise ValueError("Invalid monitor mode")
    now = now or datetime.now(timezone.utc)
    result = _empty(mode, now)
    names = {
        "entryDollars": "entry_dollars", "minimumProfit": "minimum_profit",
        "takeProfitRoi": "take_profit_roi", "stopLossFraction": "stop_loss_fraction",
        "growthBalance": "growth_balance", "growthEntryDollars": "growth_entry_dollars",
        "growthMinProbability": "growth_min_probability", "minimumProbability": "minimum_probability",
        "maxExposureFraction": "max_exposure_fraction", "maxPositionFraction": "max_position_fraction",
        "reserveCash": "reserve_cash", "maxOpenPositions": "max_open_positions",
        "dailyLossLimit": "daily_loss_limit", "maxDrawdownFraction": "max_drawdown_fraction",
        "pollSeconds": "poll_seconds", "maxHorizonHours": "max_horizon_hours",
    }
    result["risk"] = {key: getattr(cfg, value) for key, value in names.items()}
    ledger = None
    try:
        ledger = _ledger(Path(cfg.state_dir) / (mode + ".sqlite3"))
        _read_bot(result, ledger, cfg, now)
    except Exception:
        _error(result, "ledger_unavailable", "Ledger bot belum dapat dibaca; data akun berasal dari bursa.")
    if mode == "live":
        _live_account(result, live_factory or LiveExchange.from_env)
    else:
        _paper_account(result, ledger)
    positions, account = result["positions"], result["account"]
    positions.sort(key=lambda row: row["value"] if row["value"] is not None else -1, reverse=True)
    account.update(positionsCount=len(positions),
                   activePositionsCount=sum(row["status"] == "OPEN" and (row["size"] or 0) > 0 for row in positions),
                   redeemablePositionsCount=sum(row["redeemable"] for row in positions))
    identity = account["wallet"].lower() if mode == "live" and account["wallet"] else ("paper" if mode == "paper" else "")
    if identity:
        try:
            result["equityHistory"] = _history(Path(cfg.state_dir) / "monitor.sqlite3", mode, identity, account, now)
        except Exception:
            _error(result, "history_unavailable", "Riwayat pengamatan saldo belum dapat dibaca atau disimpan.")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only PolyBOT account monitor; emits JSON to stdout.")
    parser.add_argument("--mode", choices=("live", "paper"), default="live")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--output-json", action="store_true", help="JSON is always the output format")
    args = parser.parse_args(argv)
    try:
        # Third-party SDK diagnostics must never corrupt JSON or disclose secrets.
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            result = build_snapshot(load_config(args.config), args.mode)
    except Exception:
        result = _empty(args.mode, datetime.now(timezone.utc))
        _error(result, "monitor_unavailable", "Pemantauan belum tersedia. Periksa konfigurasi dan dependensi lokal.")
    print(json.dumps(result, ensure_ascii=True, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
