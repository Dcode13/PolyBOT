"""Fresh public analysis using simulated capital and an in-memory ledger only."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import tomllib

from .config import Config, load_config
from .engine import Engine
from .exchange import PublicExchange
from .state import State
from .strategy import utcnow


SCAN_OVERRIDES = frozenset({"min_liquidity", "min_volume_24h", "minimum_probability",
                            "minimum_confidence", "minimum_edge", "max_candidates",
                            "max_scan_seconds"})


def scan_config(cfg: Config, path: Path) -> tuple[Config, dict]:
    """Load discovery-only overrides; position sizes and loss controls cannot change."""
    if not path.is_file():
        return cfg, {}
    with path.open("rb") as stream:
        overrides = tomllib.load(stream)
    if set(overrides) - SCAN_OVERRIDES:
        raise ValueError("Profil scan hanya menerima pengaturan penyaringan pasar")
    return replace(cfg, **overrides).validate(), overrides


def run_scan(cfg: Config, mode: str = "paper", *, public=None) -> dict:
    """Apply paper/live forecast requirements without connecting a trading account.

    Scan capital always starts from configuration, independent of account holdings.
    The normal strategy filters and STOP switch still apply. All engine writes go
    to a private SQLite memory database, so active trading ledgers are untouched.
    """
    if mode not in ("paper", "live"):
        raise ValueError("Mode analisis tidak valid")
    cfg.validate()
    owned_public = public is None
    if owned_public:
        public = PublicExchange(timeout=8, retries=0)
    state = None
    try:
        state = State(Path(":memory:"), cfg.starting_cash)
        report = Engine(cfg, public, state, analysis_mode=mode).cycle(trade=False)
        normalized = {
            "mode": mode, "time": utcnow().isoformat(),
            "cash": cfg.starting_cash, "equity": cfg.starting_cash,
            "scanned": 0, "analyzed": 0, "qualified": [],
            "rejections": {}, "errors": [], "orders": [],
            **report,
            "scan_only": True,
            "capital_basis": "configured_starting_cash",
            "analysis_cash": cfg.starting_cash,
            "analysis_entry": cfg.entry_dollars,
        }
        return normalized
    finally:
        if state is not None:
            state.close()
        if owned_public and callable(getattr(public, "close", None)):
            public.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Scan pasar publik tanpa order atau perubahan ledger.")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--profile", default=None,
                        help="Profil scan; default scanner.toml di sebelah config.toml")
    parser.add_argument("--mode", choices=("paper", "live"), default="paper")
    args = parser.parse_args(argv)
    try:
        profile_path = Path(args.profile) if args.profile else Path(args.config).resolve().with_name("scanner.toml")
        cfg, overrides = scan_config(load_config(args.config), profile_path)
        result = run_scan(cfg, args.mode)
        result["scan_overrides"] = overrides
        print(json.dumps(result, ensure_ascii=True, allow_nan=False), flush=True)
        return 0
    except Exception as exc:
        # Neither credentials nor raw HTTP/SDK errors belong in Telegram output.
        print(json.dumps({"error": type(exc).__name__,
                          "message": "Scan belum tersedia. Periksa koneksi dan konfigurasi."}), flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
