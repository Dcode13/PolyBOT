"""Isolated, explicitly started trading workers for the Telegram controller.

Only this subprocess touches an Engine. The controller owns no signing client,
and shutdown requests never kill an in-flight order or clear the global STOP.
"""
from __future__ import annotations

import argparse
from collections import deque
import contextlib
from dataclasses import dataclass, replace
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import TextIO
import unicodedata
import uuid

from .__main__ import load_env
from .config import load_config
from .engine import Engine
from .exchange import LiveExchange, PublicExchange
from .state import State, process_lock


MODES = ("paper", "live")
MESSAGES = {
    "started": "Worker mulai berjalan.",
    "stopped": "Worker berhenti; posisi dan order yang sudah ada tidak ditutup otomatis.",
    "worker_error": "Worker dihentikan karena pemeriksaan gagal. Periksa konfigurasi dan ledger lokal; order ambigu tidak diulang.",
    "cycle_error": "Siklus gagal. Periksa koneksi dan ledger lokal; order ambigu tidak diulang.",
    "ledger_busy": "Ledger sedang dipakai proses lain. Worker kedua tidak dijalankan.",
    "region_blocked": "Trading live tidak tersedia atau status wilayah belum dapat dipastikan.",
    "external_positions": "Akun memiliki posisi di luar ledger bot. Rekonsiliasi diperlukan sebelum live dapat dijalankan.",
    "external_orders": "Akun memiliki order terbuka. Rekonsiliasi diperlukan sebelum live dapat dijalankan.",
    "balance_mismatch": "Saldo akun berbeda dari ledger. Rekonsiliasi diperlukan sebelum melanjutkan.",
}
REASONS = frozenset("""
    crypto_excluded market_not_tradable missing_market_identity missing_end_date
    outside_time_window low_liquidity low_volume stale_book empty_book
    wide_or_crossed_spread invalid_order_rules invalid_book insufficient_capital
    invalid_inventory no_forecast position_limit event_already_held unknown_fee
    live_requires_external_calibrated_forecast insufficient_evidence
    forecast_horizon_invalid intraday_requires_target_touch_evidence
    long_position_limit position_risk_limit exposure_limit cash_reserve
    long_capital_limit invalid_tick entry_below_exchange_minimum
    insufficient_ask_depth insufficient_bid_depth profit_target_not_supported
    edge_too_small qualified invalid_market expired_forecast data_error
    kill_switch unresolved_order awaiting_settlement position_data_unavailable
    drawdown_limit daily_loss_limit risk_after_entry take_profit stop_loss
    deadline paper_fok_no_fill telegram_stop unknown
    paper_exploratory paper_exploration_live_disabled paper_exploration_cycle_limit
    paper_exploration_horizon_invalid paper_exploration_spread_too_costly
""".split())


def _mode(value: str) -> str:
    if value not in MODES:
        raise ValueError("Mode harus paper atau live")
    return value


def _number(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return value
    except OverflowError:
        pass
    return None


def _reason(value):
    return value if isinstance(value, str) and value in REASONS else "unknown"


def _entry_kind(value):
    return value if value in ("forecast", "paper_exploratory") else "unknown"


def _outcome(value):
    """Keep only a bounded public outcome label, never a structured SDK value."""
    if not isinstance(value, str):
        return ""
    clean = " ".join("".join(" " if unicodedata.category(char).startswith("C") else char
                             for char in value[:256]).split())
    return {"yes": "Yes", "no": "No"}.get(clean.casefold(), clean[:40])


def safe_report(report: dict) -> dict:
    """Export counts, approved codes and public outcome labels; exclude SDK data."""
    result = {}
    for key in ("cash", "equity", "scanned", "analyzed"):
        value = _number(report.get(key))
        if value is not None:
            result[key] = value
    for key in ("orders", "qualified", "errors"):
        rows = report.get(key)
        rows = rows if isinstance(rows, list) else []
        # A report is sanitized both in the child and at the parent boundary.
        # Keep aggregate counts after the exported lists have been truncated.
        count = report.get(key + "_count")
        result[key + "_count"] = count if isinstance(count, int) and not isinstance(count, bool) and len(rows) <= count <= 1_000_000_000 else len(rows)
        if key == "orders":
            result[key] = [{"side": row.get("side") if row.get("side") in ("BUY", "SELL") else "unknown",
                            "outcome": _outcome(row.get("outcome")),
                            "status": row.get("status") if row.get("status") in ("filled", "pending", "rejected", "unknown") else "unknown",
                            "entry_kind": _entry_kind(row.get("entry_kind")),
                            "reason": _reason(row.get("reason"))}
                           for row in rows[:100] if isinstance(row, dict)]
        elif key == "qualified":
            result[key] = [{"entry_kind": _entry_kind(row.get("entry_kind"))}
                           for row in rows[:100] if isinstance(row, dict)]
        else:
            # Preserve list-based counters used by the regular engine report UI.
            result[key] = [{} for _ in rows[:100]]
    explored = sum(row.get("entry_kind") == "paper_exploratory" for row in result.get("qualified", []))
    count = report.get("exploratory_count")
    if type(count) is int and explored <= count <= result["qualified_count"]:
        result["exploratory_count"] = count
    else:
        rows = report.get("qualified")
        result["exploratory_count"] = sum(isinstance(row, dict) and row.get("entry_kind") == "paper_exploratory"
                                           for row in rows) if isinstance(rows, list) else 0
    result["rejections"] = {key: value for key, value in (report.get("rejections") or {}).items()
                            if key in REASONS and _number(value) is not None} if isinstance(report.get("rejections"), dict) else {}
    if "halted" in report:
        result["halted"] = _reason(report["halted"])
    if isinstance(report.get("analysis_budget_reached"), bool):
        result["analysis_budget_reached"] = report["analysis_budget_reached"]
    return result


def _safe_event(value, mode: str) -> dict | None:
    if not isinstance(value, dict) or value.get("type") not in ("cycle", "error", "started", "stopped"):
        return None
    kind = value["type"]
    result = {"mode": mode, "type": kind}
    if kind == "cycle":
        if not isinstance(value.get("report"), dict):
            return None
        result["report"] = safe_report(value["report"])
    else:
        code = value.get("code")
        code = code if isinstance(code, str) and code in MESSAGES else (kind if kind in MESSAGES else "worker_error")
        result.update(code=code, message=MESSAGES[code])
    return result


def _resolved_config(config_path: Path, root: Path):
    cfg = load_config(str(config_path))
    return replace(cfg, state_dir=str((root / cfg.state_dir).resolve()),
                   forecast_path=str((root / cfg.forecast_path).resolve()))


def _error_code(exc: Exception) -> str:
    # Compare to our own exact messages only. Never publish arbitrary exceptions.
    return {
        "Bot lain sedang menggunakan ledger ini": "ledger_busy",
        "Status wilayah tidak mengizinkan live trading": "region_blocked",
        "Live trading tidak tersedia atau status wilayah tidak diketahui": "region_blocked",
        "Ada posisi akun di luar ledger bot; rekonsiliasi diperlukan": "external_positions",
        "Ada open order di akun; gunakan akun khusus bot dan rekonsiliasi dahulu": "external_orders",
        "Saldo bursa berbeda dari ledger; hentikan dan rekonsiliasi": "balance_mismatch",
        "Saldo token berbeda dari ledger; hentikan dan rekonsiliasi": "balance_mismatch",
    }.get(str(exc), "worker_error")


def run_worker(config_path: Path, mode: str, *, once: bool = False,
               parent_watch: bool = False, output: TextIO | None = None,
               control_id: str | None = None) -> int:
    """Run with the normal ledger lock, initialization, and pending-order rules."""
    _mode(mode)
    if control_id is not None and re.fullmatch(r"[a-f0-9]{32}", control_id) is None:
        raise ValueError("Identitas worker tidak valid")
    output = output or sys.stdout
    disconnected = threading.Event()
    stop_path = None

    def stopped():
        return disconnected.is_set() or (stop_path is not None and stop_path.exists())

    def emit(kind, **values):
        event = _safe_event({"type": kind, **values}, mode)
        if event is not None:
            try:
                output.write(json.dumps(event, ensure_ascii=True, allow_nan=False) + "\n")
                output.flush()
            except (OSError, ValueError):
                disconnected.set()

    if parent_watch:
        # Parent exit closes this private pipe, including an unexpected app crash.
        # No commands or user-provided text are interpreted through stdin.
        def watch_parent():
            try:
                # A daemon blocked on sys.stdin.buffer.read() can hold Python's
                # buffered-I/O lock during interpreter shutdown and crash a
                # --once child. The OS pipe needs no buffered wrapper.
                descriptor = sys.stdin.fileno()
                while os.read(descriptor, 1):
                    pass
            except (OSError, ValueError, AttributeError, TypeError):
                pass
            disconnected.set()
        threading.Thread(target=watch_parent, daemon=True, name="telegram-parent-watch").start()

    state = live = None
    # The SDK must not pollute the JSON pipe or publish authentication material.
    with open(os.devnull, "w", encoding="utf-8") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        try:
            cfg = _resolved_config(config_path.resolve(), Path.cwd())
            root = Path(cfg.state_dir)
            suffix = "-" + control_id if control_id else ""
            stop_path = root / ("telegram-" + mode + suffix + ".stop")
            with process_lock(root / (mode + ".lock")):
                if stopped():
                    return 0
                state = State(root / (mode + ".sqlite3"), cfg.starting_cash)
                public = PublicExchange()
                if mode == "live":
                    if public.geoblock().get("blocked") is not False:
                        raise RuntimeError("Status wilayah tidak mengizinkan live trading")
                    load_env(str(Path.cwd() / ".env"))
                    live = LiveExchange.from_env(public)
                engine = Engine(cfg, public, state, live)
                original_execute = engine._execute

                def guarded_execute(plan, side, book, fee):
                    if stopped():
                        return {"side": side, "outcome": _outcome(plan.get("outcome")),
                                "status": "rejected", "reason": "telegram_stop"}
                    return original_execute(plan, side, book, fee)

                # Also prevent new submissions if stop arrives during scanning.
                # An order that has already entered _execute is allowed to settle.
                engine._execute = guarded_execute
                if live:
                    engine.initialize_live()
                if stopped():
                    return 0
                emit("started", code="started")
                while not stopped():
                    try:
                        report = engine.cycle(trade=True)
                        emit("cycle", report=report)
                        if report.get("halted") == "unresolved_order":
                            return 2
                    except Exception:
                        emit("error", code="cycle_error")
                        if mode == "live" or once:
                            return 2
                    if once:
                        return 0
                    deadline = time.monotonic() + cfg.poll_seconds
                    while not stopped() and time.monotonic() < deadline:
                        disconnected.wait(min(0.25, max(0, deadline - time.monotonic())))
                return 0
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            emit("error", code=_error_code(exc))
            return 2
        finally:
            for resource in (state, live):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
            emit("stopped", code="stopped")


@dataclass
class _OwnedWorker:
    process: subprocess.Popen
    stop_path: Path
    once: bool
    stopping: bool = False
    reader: threading.Thread | None = None


class TradingWorkers:
    """Own at most one paper and one live worker; never auto-start or kill one."""

    def __init__(self, config_path: Path, root: Path):
        self.root = Path(root).resolve()
        self.config_path = (self.root / config_path).resolve()
        self._workers: dict[str, _OwnedWorker] = {}
        self._events: deque[dict] = deque(maxlen=256)
        self._lock = threading.RLock()
        self._closed = False

    def _put(self, event):
        with self._lock:
            self._events.append(event)

    def _read(self, mode: str, worker: _OwnedWorker):
        saw_error = saw_stopped = False
        try:
            stream = worker.process.stdout
            if stream is not None:
                while line := stream.readline(65_537):
                    if len(line) > 65_536:
                        while line and not line.endswith("\n"):
                            line = stream.readline(65_537)
                        continue
                    try:
                        event = _safe_event(json.loads(line), mode)
                    except (ValueError, TypeError, OverflowError):
                        continue
                    if event:
                        saw_error |= event["type"] == "error"
                        saw_stopped |= event["type"] == "stopped"
                        self._put(event)
                stream.close()
            code = worker.process.wait()
            if code and not saw_error:
                self._put(_safe_event({"type": "error", "code": "worker_error"}, mode))
        except (OSError, ValueError):
            if not saw_error:
                self._put(_safe_event({"type": "error", "code": "worker_error"}, mode))
        finally:
            # This file belongs to exactly this child. Never delete a different
            # worker's stop request if a new worker has already been started.
            if worker.process.poll() is not None:
                try:
                    worker.stop_path.unlink(missing_ok=True)
                except OSError:
                    pass
                if worker.process.stdin is not None:
                    try:
                        worker.process.stdin.close()
                    except (OSError, ValueError):
                        pass
            if not saw_stopped:
                self._put(_safe_event({"type": "stopped"}, mode))

    def start(self, mode: str, once: bool = False) -> str:
        _mode(mode)
        label = mode.upper()
        with self._lock:
            if self._closed:
                return "Pengendali sedang berhenti; worker tidak dijalankan."
            prior = self._workers.get(mode)
            if prior is not None and prior.process.poll() is None:
                return f"Worker {label} masih berhenti secara aman." if prior.stopping else f"Worker {label} sudah berjalan."
            try:
                cfg = _resolved_config(self.config_path, self.root)
                state_dir = Path(cfg.state_dir)
                control_id = uuid.uuid4().hex
                stop_path = state_dir / ("telegram-" + mode + "-" + control_id + ".stop")
                # Early feedback; the child acquires the same lock for its lifetime.
                with process_lock(state_dir / (mode + ".lock")):
                    stop_path.unlink(missing_ok=True)
                argv = [sys.executable, "-u", "-m", "polybot.telegram_worker", "--config", str(self.config_path),
                        "--mode", mode, "--parent-watch", "--control-id", control_id]
                if once:
                    argv.append("--once")
                env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
                process = subprocess.Popen(argv, cwd=str(self.root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                           shell=False, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception as exc:
                return MESSAGES[_error_code(exc)]
            worker = _OwnedWorker(process, stop_path, bool(once))
            self._workers[mode] = worker
            worker.reader = threading.Thread(target=self._read, args=(mode, worker), daemon=True,
                                             name="telegram-" + mode + "-events")
            worker.reader.start()
            suffix = "satu siklus" if once else "berulang"
            return f"Worker {label} diminta mulai ({suffix}). Hasil pemeriksaan awal akan dikirim terpisah."

    def stop(self, mode: str) -> str:
        _mode(mode)
        with self._lock:
            worker = self._workers.get(mode)
            if worker is None or worker.process.poll() is not None:
                return f"Tidak ada worker {mode.upper()} milik Telegram yang sedang berjalan."
            try:
                worker.stop_path.write_text("Graceful stop requested by Telegram controller\n", encoding="utf-8")
            except OSError:
                # EOF on the ownership pipe is a second graceful stop mechanism.
                pass
            if worker.process.stdin is not None:
                try:
                    worker.process.stdin.close()
                except (OSError, ValueError):
                    pass
            worker.stopping = True
            return f"Worker {mode.upper()} diminta berhenti. Proses/order yang sudah berjalan diselesaikan dahulu; posisi tidak dijual otomatis."

    def statuses(self) -> dict:
        with self._lock:
            result = {}
            for mode in MODES:
                worker = self._workers.get(mode)
                running = worker is not None and worker.process.poll() is None
                result[mode] = {"running": running, "stopping": bool(running and worker.stopping),
                                "once": bool(running and worker.once)}
            return result

    def drain_events(self) -> list[dict]:
        with self._lock:
            result = list(self._events)
            self._events.clear()
            return result

    def close(self):
        with self._lock:
            self._closed = True
            for mode in MODES:
                self.stop(mode)
            owned = list(self._workers.values())
        # Bounded waiting, never TerminateProcess/kill: an active live order must
        # finish its journal update. The stop file and pipe remain effective.
        deadline = time.monotonic() + 8
        for worker in owned:
            try:
                worker.process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Internal isolated PolyBOT Telegram worker")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--parent-watch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--control-id", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.control_id is not None and re.fullmatch(r"[a-f0-9]{32}", args.control_id) is None:
        parser.error("Identitas worker tidak valid")
    return run_worker(args.config, args.mode, once=args.once, parent_watch=args.parent_watch,
                      control_id=args.control_id)


if __name__ == "__main__":
    sys.exit(main())
