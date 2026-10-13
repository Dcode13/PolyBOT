from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .config import load_config
from .demo import run_demo
from .engine import Engine
from .exchange import ExchangeError, LiveExchange, PublicExchange
from .state import State, process_lock


def load_env(path: str = ".env"):
    """Small literal KEY=VALUE loader: no interpolation or code execution."""
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip().replace("_", "").isalnum():
            raise ValueError("Format .env tidak valid")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def emit(value):
    print(json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False), flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PolyBOT: pemindai non-crypto dan trading berbatas risiko. Default: paper.")
    parser.add_argument("--config", default="config.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="Demo sintetis offline tanpa akun")
    commands.add_parser("scan", help="Analisis pasar publik tanpa membuat order")
    run = commands.add_parser("run", help="Jalankan bot; default saldo simulasi $11")
    run.add_argument("--once", action="store_true")
    run.add_argument("--live", action="store_true", help="Kirim order uang asli dengan kredensial lokal")
    status = commands.add_parser("status", help="Lihat ledger lokal")
    status.add_argument("--live", action="store_true")
    doctor = commands.add_parser("doctor", help="Periksa koneksi dan status wilayah tanpa order")
    doctor.add_argument("--live", action="store_true", help="Periksa autentikasi/saldo; tanpa order atau approval token")
    reconcile = commands.add_parser("reconcile", help="Periksa order live pending dengan membaca data bursa")
    reconcile.add_argument("--live", action="store_true", required=True)
    commands.add_parser("stop", help="Aktifkan STOP untuk semua pengiriman order")
    commands.add_parser("resume", help="Hapus STOP; tidak menghapus batas rugi/unknown order")
    args = parser.parse_args(argv)
    live = None
    state = None
    try:
        cfg = load_config(args.config)
        root = Path(cfg.state_dir)
        if args.command == "demo":
            emit(run_demo())
            return 0
        if args.command in ("stop", "resume"):
            root.mkdir(parents=True, exist_ok=True)
            switch = root / "STOP"
            if args.command == "stop":
                switch.write_text("Order submissions paused by operator\n", encoding="utf-8")
            else:
                switch.unlink(missing_ok=True)
            emit({"paused": switch.exists(), "note": "Posisi yang sudah terbuka tetap memiliki risiko pasar."})
            return 0
        mode = "live" if getattr(args, "live", False) else "paper"
        public = PublicExchange()
        if args.command == "doctor":
            region = public.geoblock()
            result = {"mode": mode, "geoblock": region, "orders_submitted": 0}
            if mode == "live":
                if region.get("blocked") is not False:
                    raise RuntimeError("Status wilayah tidak mengizinkan live trading")
                load_env()
                live = LiveExchange.from_env(public)
                result["account"] = live.preflight()
            emit(result)
            return 0
        with process_lock(root / (mode + ".lock")):
            state = State(root / (mode + ".sqlite3"), cfg.starting_cash)
            if args.command == "status":
                emit({"mode": mode, **state.summary()})
                return 0
            if mode == "live":
                if public.geoblock().get("blocked") is not False:
                    raise RuntimeError("Status wilayah tidak mengizinkan live trading")
                load_env()
                live = LiveExchange.from_env(public)
            engine = Engine(cfg, public, state, live)
            if args.command == "reconcile":
                emit(engine.reconcile())
                return 0
            if live:
                engine.initialize_live()
            while True:
                try:
                    result = engine.cycle(trade=args.command == "run")
                    emit(result)
                    if result.get("halted") == "unresolved_order":
                        return 2
                except Exception as exc:
                    # SDK exception strings may contain HTTP authentication material.
                    emit({"error": type(exc).__name__, "message": "Siklus dihentikan. Periksa koneksi/ledger; order ambigu tidak diulang."})
                    if live or args.command == "scan" or getattr(args, "once", False):
                        return 2
                if args.command != "run" or args.once:
                    return 0
                time.sleep(cfg.poll_seconds)
    except KeyboardInterrupt:
        emit({"stopped": True, "note": "Order/posisi yang sudah ada tidak otomatis dibatalkan/ditutup."})
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        message = str(exc) if isinstance(exc, (ValueError, ExchangeError)) or type(exc) is RuntimeError else type(exc).__name__
        emit({"error": type(exc).__name__, "message": message})
        return 2
    finally:
        if state:
            state.close()
        if live:
            live.close()


if __name__ == "__main__":
    sys.exit(main())
