from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from .config import Config


CRYPTO = re.compile(
    r"\b(crypto(?:currency|currencies)?|bitcoin|btc|ethereum|ether|eth|solana|sol|"
    r"dogecoin|doge|xrp|ripple|defi|nfts?|blockchain|altcoins?|memecoins?|"
    r"stablecoins?|usdt|usdc|tether|binance|coinbase|airdrops?|tokenization|"
    r"microstrategy|bitmine|chainlink|litecoin|cardano|polkadot|avax|hyperliquid|"
    r"decentralized finance|digital assets?|web3|kripto|kriptokurensi|koin digital)\b",
    re.IGNORECASE,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) or (isinstance(value, str) and value.replace(".", "", 1).isdigit()):
        number = float(value)
        dt = datetime.fromtimestamp(number / 1000 if number > 1e11 else number, timezone.utc)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamp harus memiliki zona waktu")
    return dt.astimezone(timezone.utc)


def number(value, default=0.0) -> float:
    result = float(default if value is None else value)
    if not math.isfinite(result):
        raise ValueError("Angka tidak finite")
    return result


def crypto_market(market: dict) -> bool:
    parts = [market.get(key, "") for key in ("question", "description", "slug", "category", "tags", "event_title", "event_slug")]
    return bool(CRYPTO.search(" ".join(str(part) for part in parts)))


def market_rejection(market: dict, cfg: Config, now: datetime) -> str | None:
    if crypto_market(market):
        return "crypto_excluded"
    if not market.get("active") or market.get("closed") or not market.get("accepting_orders"):
        return "market_not_tradable"
    if not market.get("token_id") or not market.get("market_id") or not market.get("event_id"):
        return "missing_market_identity"
    try:
        hours = (timestamp(market.get("end_date")) - now).total_seconds() / 3600
    except (ValueError, TypeError, OverflowError):
        return "missing_end_date"
    if hours <= 0 or hours > cfg.max_horizon_hours:
        return "outside_time_window"
    if number(market.get("liquidity")) < cfg.min_liquidity:
        return "low_liquidity"
    if number(market.get("volume_24h")) < cfg.min_volume_24h:
        return "low_volume"
    return None


def levels(book: dict, side: str) -> list[tuple[float, float]]:
    result = []
    for row in book.get(side, []):
        price, size = number(row["price"]), number(row["size"])
        if not 0 < price < 1 or size < 0:
            raise ValueError("Orderbook invalid")
        if size:
            result.append((price, size))
    return sorted(result, reverse=side == "bids")


def book_rejection(book: dict, cfg: Config, now: datetime) -> str | None:
    try:
        age = (now - timestamp(book["timestamp"])).total_seconds()
        if age < -5 or age > cfg.max_book_age_seconds:
            return "stale_book"
        asks, bids = levels(book, "asks"), levels(book, "bids")
        if not asks or not bids:
            return "empty_book"
        spread = asks[0][0] - bids[0][0]
        if spread < 0 or spread > cfg.max_spread:
            return "wide_or_crossed_spread"
        if number(book["min_order_size"]) <= 0 or number(book["tick_size"]) <= 0:
            return "invalid_order_rules"
    except (KeyError, ValueError, TypeError, OverflowError):
        return "invalid_book"
    return None


def walk_book(book: dict, side: str, shares: float, limit: float | None = None) -> tuple[float, float] | None:
    """Gross cash and worst execution price for a fully executable quantity."""
    remaining, cash, worst = shares, 0.0, 0.0
    for price, size in levels(book, "asks" if side == "BUY" else "bids"):
        if limit is not None and ((side == "BUY" and price > limit + 1e-9) or (side == "SELL" and price < limit - 1e-9)):
            break
        amount = min(size, remaining)
        cash += amount * price
        remaining -= amount
        worst = price
        if remaining <= 1e-8:
            return cash, worst
    return None


def tick_round(price: float, tick: float, up: bool = False) -> float:
    step = Decimal(str(tick))
    return float((Decimal(str(price)) / step).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * step)


def evaluate(market: dict, book: dict, forecast: dict | None, fee_fraction: float,
             cash: float, equity: float, positions: list[dict], cfg: Config,
             now: datetime, live: bool = False) -> dict:
    def reject(reason: str):
        return {"accepted": False, "reason": reason, "token_id": market.get("token_id"),
                "question": market.get("question"), "outcome": market.get("outcome", "")}
    cash, equity = number(cash), number(equity)
    if cash < 0 or equity <= 0:
        return reject("insufficient_capital")
    for position in positions:
        if number(position["cost"]) <= 0 or number(position["shares"]) <= 0:
            return reject("invalid_inventory")
    reason = market_rejection(market, cfg, now) or book_rejection(book, cfg, now)
    if reason:
        return reject(reason)
    exploratory = forecast is None and cfg.paper_entry_mode == "explore" and not live
    if forecast is None and not exploratory:
        return reject("no_forecast")
    if len(positions) >= cfg.max_open_positions:
        return reject("position_limit")
    if any(p["event_id"] == market["event_id"] for p in positions):
        return reject("event_already_held")
    if not math.isfinite(fee_fraction) or not 0 <= fee_fraction < 0.5:
        return reject("unknown_fee")
    # Exploration is explicitly a simulation exercise, not a probability model.
    forecast = {} if exploratory else forecast
    is_external = forecast.get("probability") is not None
    if live and (not is_external or not forecast.get("calibrated")):
        return reject("live_requires_external_calibrated_forecast")
    if exploratory:
        probability, confidence = None, None
        hours_until_close = (timestamp(market["end_date"]) - now).total_seconds() / 3600
        horizon = min(cfg.paper_exploration_horizon_hours, hours_until_close - 300 / 3600)
        if not 0 < horizon <= cfg.max_horizon_hours:
            return reject("paper_exploration_horizon_invalid")
    else:
        probability = number(forecast.get("lower_probability") if is_external else forecast.get("target_touch_lower_bound"))
        confidence = number(forecast.get("confidence"))
        if not cfg.minimum_probability <= probability <= 1 or not cfg.minimum_confidence <= confidence <= 1:
            return reject("insufficient_evidence")
        horizon = number(forecast.get("remaining_horizon_hours", forecast.get("horizon_hours")))
        if not 0 < horizon <= cfg.max_horizon_hours:
            return reject("forecast_horizon_invalid")
    long_term = horizon > 24
    if is_external and not long_term and forecast.get("probability_kind", "resolution") != "target_touch":
        return reject("intraday_requires_target_touch_evidence")
    if long_term and (sum(p["long_term"] for p in positions) >= cfg.max_long_positions):
        return reject("long_position_limit")
    high_quality = is_external and forecast.get("calibrated") and probability >= cfg.growth_min_probability and confidence >= cfg.growth_min_probability
    stake = cfg.growth_entry_dollars if equity >= cfg.growth_balance and high_quality else cfg.entry_dollars
    if stake > equity * cfg.max_position_fraction + 1e-8:
        return reject("position_risk_limit")
    exposure = sum(p["cost"] for p in positions)
    if exposure + stake > equity * cfg.max_exposure_fraction + 1e-8:
        return reject("exposure_limit")
    if cash - stake < cfg.reserve_cash:
        return reject("cash_reserve")
    if long_term and sum(p["cost"] for p in positions if p["long_term"]) + stake > cfg.max_long_dollars:
        return reject("long_capital_limit")
    ask = levels(book, "asks")[0][0]
    tick = number(book["tick_size"])
    limit = tick_round(min(1 - tick, ask * (1 + cfg.max_slippage_fraction)), tick)
    if limit < ask - 1e-9:
        return reject("invalid_tick")
    # Budget uses worst limit price plus a conservative fee reserve; never round stake up.
    shares = float((Decimal(str(stake)) / (Decimal(str(limit)) * (1 + Decimal(str(fee_fraction))))).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
    if shares < number(book["min_order_size"]):
        return reject("entry_below_exchange_minimum")
    fill = walk_book(book, "BUY", shares, limit)
    if fill is None:
        return reject("insufficient_ask_depth")
    gross, worst = fill
    cost = shares * limit * (1 + fee_fraction)
    # Require enough current exit depth too; future liquidity is not guaranteed.
    bid_fill = walk_book(book, "SELL", shares)
    if bid_fill is None:
        return reject("insufficient_bid_depth")
    target_profit = max(cfg.minimum_profit, cost * cfg.take_profit_roi)
    target = tick_round((cost + target_profit) / (shares * (1 - fee_fraction)), tick, up=True)
    if target >= 1 or (not exploratory and target > number(forecast.get("target_price")) + 1e-9):
        return reject("profit_target_not_supported")
    if exploratory:
        # This is an executable round-trip cost score, never a forecast or edge.
        score = bid_fill[0] * (1 - fee_fraction) / cost - 1
        if -score > min(cfg.stop_loss_fraction / 2, 0.10) + 1e-9:
            return reject("paper_exploration_spread_too_costly")
        edge = None
    elif is_external and forecast.get("probability_kind", "resolution") == "resolution":
        # Conservative expected settlement value; still no promise of an intraday exit.
        edge = probability / (cost / shares) - 1
    else:
        # Target touch models are scored using a full-loss downside, never a guaranteed stop.
        expected = probability * shares * target * (1 - fee_fraction) - cost
        edge = expected / cost
    if not exploratory and edge < cfg.minimum_edge:
        return reject("edge_too_small")
    if not exploratory:
        score = edge / max(1, horizon / 24)
    return {"accepted": True, "reason": "paper_exploratory" if exploratory else "qualified",
            "entry_kind": "paper_exploratory" if exploratory else "forecast",
            "token_id": market["token_id"],
            "market_id": market["market_id"], "event_id": market["event_id"],
            "question": market["question"], "outcome": market.get("outcome", ""),
            "shares": shares, "limit_price": limit, "estimated_cost": cost,
            "budget": stake, "target_price": target, "target_profit": target_profit,
            "long_term": long_term, "horizon_hours": horizon, "edge": edge,
            "probability_lower": probability, "confidence": confidence, "forecast": forecast,
            "fee_fraction": fee_fraction, "score": score}


def exit_signal(position: dict, book: dict, fee_fraction: float, cfg: Config, now: datetime) -> dict | None:
    if not math.isfinite(fee_fraction) or not 0 <= fee_fraction < 0.5:
        return None
    # Exits may have a wide spread: depth and executable bids, not midpoints, determine proceeds.
    age = (now - timestamp(book["timestamp"])).total_seconds()
    if not -5 <= age <= cfg.max_book_age_seconds:
        return None
    if position["shares"] < number(book["min_order_size"]):
        return None
    fill = walk_book(book, "SELL", position["shares"])
    if fill is None:
        return None
    gross, worst = fill
    net = gross * (1 - fee_fraction)
    pnl = net - position["cost"]
    reason = None
    if pnl + 1e-8 >= max(cfg.minimum_profit, position["cost"] * cfg.take_profit_roi):
        reason = "take_profit"
    elif pnl <= -position["cost"] * cfg.stop_loss_fraction:
        reason = "stop_loss"
    elif now >= timestamp(position["deadline"]):
        reason = "time_exit"
    if reason:
        return {"side": "SELL", "token_id": position["token_id"], "shares": position["shares"],
                "outcome": position.get("outcome", ""),
                "limit_price": tick_round(worst, number(book["tick_size"])),
                "estimated_proceeds": net, "reason": reason}
    return None
