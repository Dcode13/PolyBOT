from __future__ import annotations

import math
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Config:
    starting_cash: float = 11.0
    entry_dollars: float = 1.0
    minimum_profit: float = 0.50
    take_profit_roi: float = 0.50
    growth_balance: float = 30.0
    growth_entry_dollars: float = 5.0
    growth_min_probability: float = 0.90
    minimum_probability: float = 0.75
    minimum_confidence: float = 0.80
    minimum_edge: float = 0.08
    max_position_fraction: float = 0.18
    max_exposure_fraction: float = 0.40
    reserve_cash: float = 2.0
    max_open_positions: int = 3
    max_long_positions: int = 1
    max_long_dollars: float = 2.0
    max_horizon_hours: float = 720.0
    daily_loss_limit: float = 1.0
    max_drawdown_fraction: float = 0.25
    stop_loss_fraction: float = 0.20
    max_spread: float = 0.04
    max_slippage_fraction: float = 0.02
    min_liquidity: float = 500.0
    min_volume_24h: float = 100.0
    max_book_age_seconds: float = 60.0
    poll_seconds: int = 60
    scan_pages: int = 5
    max_candidates: int = 40
    max_scan_seconds: float = 60.0
    paper_entry_mode: str = "forecast_required"
    paper_exploration_horizon_hours: float = 6.0
    forecast_path: str = "forecasts.json"
    max_forecast_age_hours: float = 24.0
    state_dir: str = "data"

    def validate(self) -> Config:
        defaults = asdict(Config())
        for key, value in asdict(self).items():
            expected = defaults[key]
            if isinstance(expected, (int, float)) and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ValueError(f"{key} harus angka")
            if isinstance(expected, str) and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{key} harus teks non-kosong")
            if isinstance(value, (int, float)) and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"{key} harus angka positif dan finite")
        for key in ("take_profit_roi", "growth_min_probability", "minimum_probability",
                    "minimum_confidence", "minimum_edge", "max_position_fraction",
                    "max_exposure_fraction", "max_drawdown_fraction", "stop_loss_fraction",
                    "max_spread", "max_slippage_fraction"):
            if getattr(self, key) >= 1:
                raise ValueError(f"{key} harus kurang dari 1")
        for key in ("max_open_positions", "max_long_positions", "poll_seconds", "scan_pages", "max_candidates"):
            if not isinstance(getattr(self, key), int):
                raise ValueError(f"{key} harus integer")
        if self.growth_entry_dollars < self.entry_dollars:
            raise ValueError("growth_entry_dollars harus >= entry_dollars")
        if self.paper_entry_mode not in ("forecast_required", "explore"):
            raise ValueError("paper_entry_mode harus forecast_required atau explore")
        if self.paper_exploration_horizon_hours > 24:
            raise ValueError("paper_exploration_horizon_hours maksimum 24 jam")
        return self


def load_config(path: str = "config.toml") -> Config:
    source = Path(path)
    if not source.exists():
        return Config().validate()
    with source.open("rb") as stream:
        raw = tomllib.load(stream)
    unknown = set(raw) - {f.name for f in fields(Config)}
    if unknown:
        raise ValueError(f"Konfigurasi tidak dikenal: {sorted(unknown)}")
    return Config(**raw).validate()
