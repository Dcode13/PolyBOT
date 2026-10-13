"""Deterministic synthetic example, never a performance backtest."""
from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from .config import Config
from .engine import Engine
from .state import State
from .strategy import utcnow


class DemoExchange:
    rising = False

    def iter_markets(self, **kwargs):
        yield {"market_id": "DEMO", "event_id": "DEMO-EVENT", "token_id": "DEMO-TOKEN",
               "question": "CONTOH SINTETIS: apakah tim A menang?", "outcome": "Yes",
               "description": "Data buatan untuk memeriksa alur, bukan peluang nyata.",
               "active": True, "closed": False, "accepting_orders": True,
               "end_date": (utcnow() + timedelta(hours=12)).isoformat(),
               "liquidity": 10000, "volume_24h": 5000, "tags": ["Sports"]}

    def get_book(self, token_id):
        return {"timestamp": utcnow().isoformat(), "min_order_size": 5, "tick_size": .01,
                "bids": [{"price": .29 if self.rising else .17, "size": 100}],
                "asks": [{"price": .30 if self.rising else .18, "size": 100}]}

    def get_fee(self, token_id):
        return {"max_fee_fraction": .01}

    def get_history(self, token_id):
        return []


def run_demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="polybot-demo-") as directory:
        root = Path(directory)
        forecast = {"market_id": "DEMO", "token_id": "DEMO-TOKEN", "probability": .97,
                    "lower_probability": .95, "probability_kind": "target_touch", "target_price": .35,
                    "confidence": .95, "horizon_hours": 12, "observed_at": utcnow().isoformat(),
                    "expires_at": (utcnow() + timedelta(hours=12)).isoformat(), "source": "SYNTHETIC",
                    "rationale": "Data buatan, bukan estimasi nyata", "calibrated": False}
        feed = root / "forecasts.json"
        feed.write_text(json.dumps({"forecasts": [forecast]}), encoding="utf-8")
        cfg = replace(Config(), forecast_path=str(feed), state_dir=str(root))
        state = State(root / "demo.db", cfg.starting_cash)
        try:
            public = DemoExchange()
            engine = Engine(cfg, public, state)
            first = engine.cycle()
            public.rising = True
            second = engine.cycle()
            return {"notice": "SIMULASI SINTETIS: bukan backtest atau bukti profit", "entry_cycle": first,
                    "exit_cycle": second, "portfolio": state.summary()}
        finally:
            state.close()
