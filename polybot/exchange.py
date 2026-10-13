"""Public market data and conservative execution through the official SDK.

The public adapter requires only the Python standard library. Live methods never
retry order submissions; a missing acknowledgement is an unknown outcome, not a
failed trade. Callers must durably reserve an intent before calling place_fok.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, is_dataclass
from decimal import Decimal, ROUND_DOWN
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

SDK_VERSION = "0.12.0"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


class ExchangeError(RuntimeError):
    """Safe, credential-free error suitable for a user-facing log."""


def _number(value: Any, name: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool):
        raise ExchangeError(f"Invalid numeric field: {name}")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ExchangeError(f"Missing or invalid numeric field: {name}") from None
    if not math.isfinite(result) or result < minimum:
        raise ExchangeError(f"Out-of-range numeric field: {name}")
    return result


def _list(value: Any) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    return value if isinstance(value, list) else []


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _record(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    return vars(value)


def _page_items(paginator: Any, max_pages: int = 100) -> list:
    result = []
    for count, page in enumerate(paginator):
        if count >= max_pages:
            raise ExchangeError("Account pagination limit reached; completeness is unknown")
        result.extend(_get(page, "items", ()))
    return result


def _tag_names(*sources: Any) -> list[str]:
    names = set()
    for source in sources:
        for tag in _list(source):
            if isinstance(tag, dict):
                names.update(str(tag[key]) for key in ("label", "slug") if tag.get(key))
            elif isinstance(tag, str):
                names.add(tag)
    return sorted(names)


def normalize_event(event: dict) -> Iterator[dict]:
    """Expand both outcomes; category exclusion belongs to the strategy layer."""
    for market in _list(event.get("markets")):
        if not isinstance(market, dict):
            continue
        protocol = market.get("version") or "v1"
        if protocol not in ("v1", "v2"):
            continue
        assets = _list(market.get("positionIds") if protocol == "v2" else market.get("clobTokenIds"))
        outcomes = _list(market.get("outcomes"))
        if len(assets) != 2 or len(outcomes) != 2 or not all(str(x).isdigit() for x in assets):
            continue
        prices = _list(market.get("outcomePrices"))
        tags = _tag_names(event.get("tags"), market.get("tags"))
        for index, (asset, outcome) in enumerate(zip(assets, outcomes)):
            yield {
                "market_id": str(market.get("id", "")),
                "condition_id": str(market.get("conditionId", "")),
                "event_id": str(event.get("id", "")),
                "event_title": str(event.get("title", "")),
                "event_slug": str(event.get("slug", "")),
                "slug": str(market.get("slug", "")),
                "question": str(market.get("question", "")),
                "description": str(market.get("description") or event.get("description") or ""),
                "category": str(market.get("category") or event.get("category") or ""),
                "tags": tags,
                "token_id": str(asset), "asset_id": str(asset),
                "outcome": str(outcome), "outcome_index": index,
                "end_date": market.get("endDate") or event.get("endDate"),
                "active": market.get("active") is True and event.get("active", True) is True,
                "closed": market.get("closed") is not False or event.get("closed", False) is True,
                "accepting_orders": market.get("acceptingOrders") is True,
                "enable_order_book": market.get("enableOrderBook") is True,
                "neg_risk": market.get("negRisk", False), "version": protocol,
                "seconds_delay": market.get("secondsDelay", 0),
                "volume": market.get("volumeNum", market.get("volume", 0)),
                "volume_24h": market.get("volume24hr", market.get("volume24hrClob", 0)),
                "liquidity": market.get("liquidityNum", market.get("liquidity", 0)),
                "last_price": prices[index] if len(prices) == 2 else None,
                "resolution_source": market.get("resolutionSource", ""),
            }


class PublicExchange:
    def __init__(self, *, timeout: float = 15, retries: int = 2,
                 transport: Callable[[str], Any] | None = None):
        self.timeout, self.retries, self.transport = timeout, retries, transport

    def _json(self, base: str, path: str, params: dict | None = None) -> Any:
        url = base + path + (("?" + urlencode(params)) if params else "")
        if self.transport is not None:
            return self.transport(url)
        for attempt in range(self.retries + 1):
            try:
                request = Request(url, headers={"Accept": "application/json", "User-Agent": "PolyBOT/0.1"})
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(20_000_001)
                if len(raw) > 20_000_000:
                    raise ExchangeError("Public API response exceeded size limit")
                return json.loads(raw)
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise ExchangeError(f"Public API HTTP {exc.code} at {path}") from None
            except (URLError, TimeoutError, OSError):
                if attempt == self.retries:
                    raise ExchangeError(f"Public API unavailable at {path}") from None
            except (ValueError, UnicodeError):
                raise ExchangeError(f"Invalid JSON from public API at {path}") from None
            time.sleep(min(2 ** attempt, 4))
        raise ExchangeError("Public API unavailable")

    def iter_markets(self, max_pages: int = 5, page_size: int = 100) -> Iterator[dict]:
        if not 1 <= page_size <= 100 or not 1 <= max_pages <= 1000:
            raise ExchangeError("Invalid discovery pagination limits")
        params: dict = {"active": "true", "closed": "false", "limit": page_size,
                        "order": "volume24hr", "ascending": "false"}
        seen_assets, seen_cursors = set(), set()
        for _ in range(max_pages):
            payload = self._json(GAMMA, "/events/keyset", params)
            if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
                raise ExchangeError("Unexpected Gamma events response")
            for event in payload["events"]:
                if not isinstance(event, dict):
                    continue
                for market in normalize_event(event):
                    if market["token_id"] not in seen_assets:
                        seen_assets.add(market["token_id"])
                        yield market
            cursor = payload.get("next_cursor")
            if not cursor:
                return
            if cursor in seen_cursors:
                raise ExchangeError("Gamma returned a repeated pagination cursor")
            seen_cursors.add(cursor)
            params["after_cursor"] = cursor

    def get_book(self, token_id: str) -> dict:
        raw = self._json(CLOB, "/book", {"token_id": token_id})
        if not isinstance(raw, dict) or str(raw.get("asset_id")) != str(token_id):
            raise ExchangeError("Order book asset mismatch")
        levels = {}
        for side in ("bids", "asks"):
            if not isinstance(raw.get(side), list):
                raise ExchangeError("Order book missing price levels")
            values = []
            for row in raw[side]:
                price = _number(row.get("price"), "book.price", minimum=0.0000001)
                size = _number(row.get("size"), "book.size", minimum=0.0000001)
                if price >= 1:
                    raise ExchangeError("Invalid prediction share price")
                values.append({"price": price, "size": size})
            levels[side] = sorted(values, key=lambda row: row["price"], reverse=side == "bids")
        stamp = _number(raw.get("timestamp"), "book.timestamp")
        if stamp > 100_000_000_000:
            stamp /= 1000
        tick = _number(raw.get("tick_size"), "tick_size", minimum=0.0000001)
        if tick not in (0.1, 0.01, 0.005, 0.0025, 0.001, 0.0001):
            raise ExchangeError("Unknown tick size")
        protocol = raw.get("version") or "v1"
        if protocol not in ("v1", "v2"):
            raise ExchangeError("Unsupported order book protocol")
        return {**levels, "token_id": str(token_id), "asset_id": str(token_id),
                "min_order_size": _number(raw.get("min_order_size"), "min_order_size", minimum=0.0000001),
                "tick_size": tick, "timestamp": stamp, "fetched_at": time.time(),
                "neg_risk": raw.get("neg_risk", False), "version": protocol,
                "market": raw.get("market", "")}

    def get_fee(self, token_id: str) -> dict:
        lookup = self._json(CLOB, "/markets-by-token/" + quote(str(token_id), safe=""))
        condition = lookup.get("condition_id") if isinstance(lookup, dict) else None
        if not isinstance(condition, str) or not condition:
            raise ExchangeError("Cannot determine fee market")
        raw = self._json(CLOB, "/clob-markets/" + quote(condition, safe=""))
        tokens = raw.get("t") if isinstance(raw, dict) else None
        if not isinstance(tokens, list) or not any(str(_get(t, "t")) == str(token_id) for t in tokens):
            raise ExchangeError("Fee metadata asset mismatch")
        fd = raw.get("fd")
        if fd is None:  # Official SDK explicitly defines absent/null fd as no fee.
            rate, exponent = 0.0, 0.0
        elif isinstance(fd, dict) and "r" in fd and "e" in fd:
            rate, exponent = _number(fd["r"], "fee.rate"), _number(fd["e"], "fee.exponent")
        else:
            raise ExchangeError("Unknown fee metadata")
        if rate > 0 and exponent < 1:
            raise ExchangeError("Fee curve cannot be safely bounded over all prices")
        bound = rate * (0.25 ** (exponent - 1)) if rate else 0.0
        if bound >= 1:
            raise ExchangeError("Fee rate is outside supported limits")
        return {"rate": rate, "exponent": exponent, "fraction": bound, "max_fee_fraction": bound,
                "buy_fraction": bound, "sell_fraction": bound,
                "known": True, "condition_id": condition, "fetched_at": time.time()}

    def get_history(self, token_id: str, interval: str = "max", fidelity: int = 60) -> list[dict]:
        if interval not in ("max", "all", "1m", "1w", "1d", "6h", "1h") or fidelity < 1:
            raise ExchangeError("Unsupported price history interval or fidelity")
        raw = self._json(CLOB, "/prices-history", {"market": token_id, "interval": interval, "fidelity": fidelity})
        if not isinstance(raw, dict) or not isinstance(raw.get("history"), list):
            raise ExchangeError("Unexpected price history response")
        result = []
        for row in raw["history"]:
            timestamp, price = _number(row.get("t"), "history.time"), _number(row.get("p"), "history.price")
            if price <= 1:
                result.append({"t": timestamp, "p": price})
        return sorted(result, key=lambda row: row["t"])[-20_000:]

    def geoblock(self) -> dict:
        raw = self._json("https://polymarket.com", "/api/geoblock")
        if not isinstance(raw, dict) or not isinstance(raw.get("blocked"), bool):
            raise ExchangeError("Geographic eligibility could not be verified")
        return {key: raw.get(key, "") for key in ("blocked", "country", "region")}


class LiveExchange:
    """Account-exclusive FOK execution. Constructing this class does not submit orders."""
    def __init__(self, client: Any, public: PublicExchange | None = None):
        self.client, self.public = client, public or PublicExchange()

    @classmethod
    def from_env(cls, public: PublicExchange | None = None) -> "LiveExchange":
        public = public or PublicExchange()
        if public.geoblock()["blocked"]:
            raise ExchangeError("Trading is blocked for the current connection")
        key, wallet = os.environ.get("POLYMARKET_PRIVATE_KEY"), os.environ.get("POLYMARKET_WALLET_ADDRESS")
        if not key or not wallet:
            raise ExchangeError("Set POLYMARKET_PRIVATE_KEY and POLYMARKET_WALLET_ADDRESS locally")
        try:
            installed = version("polymarket-client")
        except PackageNotFoundError:
            raise ExchangeError(f"Install optional dependency polymarket-client=={SDK_VERSION}") from None
        if installed != SDK_VERSION:
            raise ExchangeError(f"Live adapter requires polymarket-client=={SDK_VERSION}")
        try:
            from polymarket import SecureClient
            # Pinned SDK's public create() may deploy a missing Deposit Wallet.
            # _create authenticates only; never call _ensure_wallet_ready here.
            client = SecureClient._create(private_key=key, wallet=wallet)
        except Exception:
            raise ExchangeError("Polymarket authentication failed; check local wallet credentials") from None
        return cls(client, public)

    def close(self) -> None:
        self.client.close()

    def _balance(self, token_id: str | None = None) -> dict:
        args: dict = {"asset_type": "COLLATERAL"}
        if token_id is not None:
            protocol = self.public.get_book(token_id)["version"]
            args = {"asset_type": "CONDITIONAL-V2" if protocol == "v2" else "CONDITIONAL", "asset_id": token_id}
        try:
            raw = self.client.get_balance_allowance(**args)
            balance = _number(_get(raw, "balance"), "balance") / 1_000_000
            allowances = _get(raw, "allowances")
            if not isinstance(allowances, dict):
                raise ExchangeError("Allowance metadata missing")
            return {"balance": balance, "allowances": {str(k).lower(): _number(v, "allowance") / 1_000_000 for k, v in allowances.items()}}
        except ExchangeError:
            raise
        except Exception:
            raise ExchangeError("Could not read authenticated balance") from None

    def cash_balance(self) -> float:
        return self._balance()["balance"]

    def token_balance(self, token_id: str) -> float:
        return self._balance(token_id)["balance"]

    def open_orders(self) -> list[dict]:
        try:
            return [_record(row) for row in _page_items(self.client.list_open_orders())]
        except Exception:
            raise ExchangeError("Could not verify open account orders") from None

    def positions(self) -> list[dict]:
        try:
            result = []
            for row in _page_items(self.client.list_positions(full_history=True)):
                record = _record(row)
                record["token_id"] = str(_get(row, "asset_id"))
                record["size"] = _number(_get(row, "current_size"), "position.current_size")
                result.append(record)
            return result
        except Exception:
            raise ExchangeError("Could not verify account positions") from None

    def preflight(self) -> dict:
        """Read eligibility, balances, and complete account inventory without trading."""
        try:
            return {"geoblock": self.public.geoblock(), "cash": self.cash_balance(),
                    "open_orders": self.open_orders(), "positions": self.positions(),
                    "closed_only": bool(self.client.get_closed_only_mode())}
        except ExchangeError:
            raise
        except Exception:
            raise ExchangeError("Live account preflight failed") from None

    @staticmethod
    def _result(status: str, reason: str = "", **kwargs: Any) -> dict:
        return {"status": status, "confirmed": status in ("filled", "rejected"),
                "order_id": "", "shares": 0.0, "cash": 0.0, "price": 0.0,
                "fee": 0.0, "reason": reason, **kwargs}

    def place_fok(self, token_id: str, side: str, shares: float, limit_price: float,
                  max_spend: float | None = None,
                  on_prepared: Callable[[dict], None] | None = None) -> dict:
        if side not in ("BUY", "SELL"):
            raise ExchangeError("Order side must be BUY or SELL")
        size = Decimal(str(_number(shares, "shares", minimum=0.000001))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        price = Decimal(str(_number(limit_price, "limit_price", minimum=0.000001)))
        if price >= 1:
            raise ExchangeError("Limit price must be below 1")
        # Every failure before post_order is known not to have placed an order.
        geo = self.public.geoblock()
        if geo["blocked"]:
            raise ExchangeError("Trading is blocked for the current connection")
        try:
            closed_only = self.client.get_closed_only_mode()
        except Exception:
            raise ExchangeError("Could not verify account trading eligibility") from None
        if side == "BUY" and closed_only:
            raise ExchangeError("Account is in close-only mode")
        if self.open_orders():
            raise ExchangeError("Account has other open orders; reconcile before trading")
        book = self.public.get_book(token_id)
        if abs(time.time() - book["timestamp"]) > 60:
            raise ExchangeError("Order book is stale")
        if size < Decimal(str(book["min_order_size"])):
            raise ExchangeError("Budget cannot meet market minimum shares")
        if price % Decimal(str(book["tick_size"])) != 0:
            raise ExchangeError("Limit price does not follow the market tick size")
        fee = self.public.get_fee(token_id)
        level_side = "asks" if side == "BUY" else "bids"
        available = sum(level["size"] for level in book[level_side]
                        if (level["price"] <= float(price) if side == "BUY" else level["price"] >= float(price)))
        if available + 0.000001 < float(size):
            raise ExchangeError("Insufficient order book depth at the protected price")
        before_cash, before_shares = self.cash_balance(), self.token_balance(token_id)
        baseline = {"token_id": token_id, "side": side, "before_cash": before_cash,
                    "before_shares": before_shares, "limit_price": float(price),
                    "requested_shares": float(size), "submitted_at": time.time()}
        args: dict = {"asset_id": token_id, "side": side, "order_type": "FOK"}
        if side == "BUY":
            if max_spend is None:
                raise ExchangeError("A maximum all-in spend is required for every live BUY")
            budget = Decimal(str(_number(max_spend, "max_spend", minimum=0.000001)))
            notional = size * price
            if float(notional) * (1 + fee["fraction"]) + 0.00001 > float(budget) + 0.000001:
                raise ExchangeError("Share size plus worst-case fees exceeds the reserved budget")
            if float(budget) > before_cash + 0.000001:
                raise ExchangeError("Insufficient cash for reserved budget")
            args.update(amount=str(notional), max_spend=str(budget), max_price=str(price))
            baseline["max_spend"] = float(budget)
        else:
            if float(size) > before_shares + 0.000001:
                raise ExchangeError("Insufficient outcome shares")
            args.update(shares=str(size), min_price=str(price))
        try:
            signed = self.client.create_market_order(**args)
        except Exception:
            raise ExchangeError("Order preparation failed; no order was submitted") from None
        if _get(signed, "order_type") != "FOK":
            raise ExchangeError("SDK did not create the required FOK order")
        signed_shares = _number(_get(signed, "taker_amount" if side == "BUY" else "maker_amount"), "signed shares") / 1_000_000
        if signed_shares < book["min_order_size"]:
            raise ExchangeError("SDK rounding makes this order smaller than the market minimum")
        if on_prepared is not None:
            on_prepared(dict(baseline))  # Durable ledger write MUST complete before POST.
        try:
            response = self.client.post_order(signed)  # Exactly one submit; never retry here.
        except Exception:
            return self._result("unknown", "Submission outcome is unknown; halt and reconcile manually", **baseline)
        order_id = str(_get(response, "order_id", "") or "")
        if _get(response, "ok") is False:
            return self._result("rejected", "Exchange rejected the order", order_id=order_id, **baseline)
        if not order_id or _get(response, "ok") is not True:
            return self._result("unknown", "Unrecognized exchange acknowledgement; do not retry", **baseline)
        return self.reconcile_order(order_id, **baseline)

    def reconcile_order(self, order_id: str, *, token_id: str, side: str,
                        before_cash: float, before_shares: float,
                        limit_price: float | None = None, requested_shares: float | None = None,
                        max_spend: float | None = None, submitted_at: float | None = None) -> dict:
        baseline = {"order_id": order_id, "token_id": token_id, "side": side,
                    "before_cash": before_cash, "before_shares": before_shares,
                    "limit_price": limit_price, "requested_shares": requested_shares,
                    "max_spend": max_spend, "submitted_at": submitted_at}
        try:
            order = self.client.get_order(order_id=order_id)
            if str(_get(order, "id")) != order_id or str(_get(order, "asset_id")) != token_id or _get(order, "side") != side:
                return self._result("unknown", "Order identity mismatch", **baseline)
            matched = _number(_get(order, "size_matched"), "matched shares")
            status = str(_get(order, "status", "")).upper()
            ids = tuple(_get(order, "associate_trades", ()))
            if not ids and matched == 0:
                if status in ("CANCELED", "CANCELLED", "UNMATCHED", "EXPIRED"):
                    return self._result("rejected", "Order terminated without a fill", **baseline)
                return self._result("pending", "Order has no confirmed trade yet", **baseline)
            if not ids:
                return self._result("pending", "Matched order is awaiting trade records", **baseline)
            trades = []
            for trade_id in set(ids):
                rows = _page_items(self.client.list_account_trades(id=str(trade_id)))
                found = [row for row in rows if str(_get(row, "id")) == str(trade_id)]
                if len(found) != 1:
                    return self._result("pending", "Trade records are incomplete", **baseline)
                trade = found[0]
                if str(_get(trade, "taker_order_id")) != order_id or str(_get(trade, "asset_id")) != token_id or _get(trade, "side") != side:
                    return self._result("unknown", "Trade identity mismatch", **baseline)
                trades.append(trade)
            if all(_get(trade, "status") == "FAILED" for trade in trades):
                return self._result("rejected", "Every settlement failed", **baseline)
            if any(_get(trade, "status") != "CONFIRMED" for trade in trades):
                return self._result("pending", "Trades have not all reached CONFIRMED settlement", **baseline)
            size = sum(_number(_get(trade, "size"), "trade.size") for trade in trades)
            gross = sum(_number(_get(trade, "size"), "trade.size") * _number(_get(trade, "price"), "trade.price") for trade in trades)
            if size <= 0 or abs(size - matched) > 0.00001:
                return self._result("unknown", "Order and trade sizes disagree", **baseline)
            after_cash, after_shares = self.cash_balance(), self.token_balance(token_id)
            cash = before_cash - after_cash if side == "BUY" else after_cash - before_cash
            delta = after_shares - before_shares if side == "BUY" else before_shares - after_shares
            if abs(delta - size) > 0.00001 or cash <= 0:
                return self._result("pending", "Settled trades and wallet balances have not reconciled", **baseline)
            actual_fee = cash - gross if side == "BUY" else gross - cash
            if actual_fee < -0.00002 or actual_fee > gross * 0.5 + 0.00002:
                return self._result("unknown", "Cash movement does not match trade notional and fees", **baseline)
            if side == "BUY" and max_spend is not None and cash > max_spend + 0.00002:
                return self._result("unknown", "Actual debit exceeds reserved budget; halt", **baseline)
            if limit_price is not None and any(
                (_number(_get(t, "price"), "trade.price") > limit_price + 0.000001 if side == "BUY"
                 else _number(_get(t, "price"), "trade.price") < limit_price - 0.000001) for t in trades
            ):
                return self._result("unknown", "Fill price breached protected price", **baseline)
            return self._result("filled", shares=size, cash=cash, price=gross / size,
                                fee=max(actual_fee, 0), **baseline)
        except Exception:
            return self._result("pending", "Exchange records unavailable; do not submit another order", **baseline)
