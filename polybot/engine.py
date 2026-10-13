from __future__ import annotations

from collections import Counter
from datetime import timedelta
import math
from pathlib import Path
import time

from .config import Config
from .exchange import ExchangeError
from .forecast import ExternalForecasts, QuantitativeForecaster
from .selection import next_scan_cursor, select_markets
from .state import State
from .strategy import (book_rejection, evaluate, exit_signal, levels, market_rejection,
                       number, timestamp, utcnow, walk_book)


class Engine:
    def __init__(self, cfg: Config, public, state: State, live=None, *, analysis_mode: str | None = None):
        if analysis_mode not in (None, "paper", "live"):
            raise ValueError("Mode analisis tidak valid")
        if analysis_mode is not None and live is not None:
            raise ValueError("Analisis publik tidak boleh memakai koneksi trading live")
        self.cfg, self.public, self.state, self.live = cfg, public, state, live
        self.analysis_mode = analysis_mode
        self.history_model = QuantitativeForecaster(min_target_return=cfg.take_profit_roi)
        self.external = ExternalForecasts(cfg.forecast_path, cfg.max_forecast_age_hours)

    def _external_forecast(self, market, price):
        try:
            return self.external.forecast(market, [], price, utcnow())
        except FileNotFoundError:
            return None

    def _pending_reason(self):
        intents = self.state.intents()
        if intents and all(i.get("baseline") and i.get("execution", {}).get("order_id") for i in intents):
            return "awaiting_settlement"
        return "unresolved_order"

    def _fee(self, token: str) -> float:
        fee = self.public.get_fee(token)
        value = number(fee["max_fee_fraction"])
        if not 0 <= value < .5:
            raise ValueError("Unknown or invalid fee")
        return value

    def _observe_rejected(self, report: dict, market: dict, book: dict, reason: str) -> None:
        """Keep public observations separate from qualified trading candidates.

        The caller must first validate the book. These rows explain scan failures,
        not entry signals, and never change the strategy or any order decision.
        """
        if self.analysis_mode is None:
            return
        now = utcnow()
        # Forecast and fee requests can outlast the initial quote validation.
        # Do not turn a quote or market that expired meanwhile into an observation.
        if market_rejection(market, self.cfg, now) or book_rejection(book, self.cfg, now):
            return
        ask = levels(book, "asks")[0][0]
        bid = levels(book, "bids")[0][0]
        minimum_shares = number(book["min_order_size"])
        minimum_notional = ask * minimum_shares
        if not math.isfinite(minimum_notional):
            return
        rows = report["watchlist"]
        rows.append({
            "token_id": market["token_id"], "question": market.get("question", ""),
            "outcome": market.get("outcome", ""), "ask": ask, "spread": ask - bid,
            "min_order_shares": minimum_shares,
            # A lower bound only: fees, slippage and depth can raise the cost.
            "minimum_notional": minimum_notional, "reason": reason,
            "end_date": market.get("end_date"),
            "volume_24h": number(market.get("volume_24h")),
        })
        rows.sort(key=lambda row: (
            row["minimum_notional"] > self.cfg.entry_dollars + 1e-8,
            -row["volume_24h"], row["minimum_notional"], row["token_id"],
        ))
        del rows[10:]

    def initialize_live(self):
        if self.live is None:
            return
        self._region_check()
        if self.state.intents():
            # Existing durable intents are reconciled by cycle before any new orders.
            return
        if self.live.open_orders():
            raise RuntimeError("Ada open order di akun; gunakan akun khusus bot dan rekonsiliasi dahulu")
        inventory = self.live.positions()
        known = {p["token_id"] for p in self.state.positions()}
        for position in inventory:
            token = str(position.get("token_id", position.get("asset", position.get("asset_id", ""))))
            size = number(position.get("size", position.get("shares", 0)))
            if size > 0.0001 and token not in known:
                raise RuntimeError("Ada posisi akun di luar ledger bot; rekonsiliasi diperlukan")
        actual = number(self.live.cash_balance())
        if not self.state.get("live_initialized"):
            if self.state.positions():
                raise RuntimeError("Ledger awal live tidak kosong")
            with self.state.db:
                for key in ("cash", "initial_cash", "peak_equity", "day_start_equity"):
                    self.state.set(key, actual)
                self.state.set("live_initialized", True)
        self._check_balances()

    def _region_check(self):
        result = self.public.geoblock()
        if result.get("blocked") is not False:
            raise RuntimeError("Live trading tidak tersedia atau status wilayah tidak diketahui")

    def _check_balances(self):
        if self.live is None:
            return
        if abs(number(self.live.cash_balance()) - self.state.get("cash")) > 0.005:
            raise RuntimeError("Saldo bursa berbeda dari ledger; hentikan dan rekonsiliasi")
        for p in self.state.positions():
            if abs(number(self.live.token_balance(p["token_id"])) - p["shares"]) > 0.0001:
                raise RuntimeError("Saldo token berbeda dari ledger; hentikan dan rekonsiliasi")

    def reconcile(self) -> list[dict]:
        results = []
        for intent in self.state.intents():
            execution = intent.get("execution", {})
            baseline = intent.get("baseline")
            order_id = execution.get("order_id")
            if not order_id or not baseline:
                results.append({"intent": intent["id"], "status": "unknown", "reason": "manual_exchange_reconciliation_required_no_order_id"})
                continue
            result = self.live.reconcile_order(order_id, **baseline)
            self.state.record_execution(intent["id"], result)
            results.append({"intent": intent["id"], "status": result["status"]})
        return results

    def _execute(self, plan: dict, side: str, book: dict, fee: float) -> dict:
        order = {"side": side, "token_id": plan.get("token_id"),
                 "outcome": plan.get("outcome", ""),
                 "entry_kind": plan.get("entry_kind", "forecast")}
        if self.live is not None and plan.get("entry_kind") == "paper_exploratory":
            return {**order, "status": "rejected", "reason": "paper_exploration_live_disabled"}
        if Path(self.cfg.state_dir, "STOP").exists():
            return {**order, "status": "rejected", "reason": "kill_switch"}
        if self.state.intents():
            raise RuntimeError("Unresolved order: no further submissions")
        before_shares = next((p["shares"] for p in self.state.positions() if p["token_id"] == plan["token_id"]), 0.0)
        identifier = self.state.journal(plan, side, self.state.get("cash"), before_shares)
        if self.live is None:
            fill = walk_book(book, side, plan["shares"], plan["limit_price"])
            if fill is None:
                result = {"status": "rejected", "confirmed": False, "reason": "paper_fok_no_fill"}
            else:
                gross, _ = fill
                result = {"status": "filled", "confirmed": True, "order_id": "paper-" + identifier,
                          "shares": plan["shares"], "cash": gross * (1 + fee if side == "BUY" else 1 - fee)}
        else:
            prepared_called = False
            try:
                self._region_check()
                self._check_balances()
                def prepared(baseline):
                    nonlocal prepared_called
                    if abs(number(baseline["before_cash"]) - self.state.get("cash")) > .005 or abs(number(baseline["before_shares"]) - before_shares) > .0001:
                        raise ExchangeError("Account changed before submission")
                    self.state.prepared(identifier, baseline)
                    prepared_called = True
                result = self.live.place_fok(plan["token_id"], side, plan["shares"], plan["limit_price"],
                                             max_spend=plan.get("budget") if side == "BUY" else None,
                                             on_prepared=prepared)
            except Exception as exc:
                # Never retry a submission after an exception: it might have reached the exchange.
                known_pre_submit = isinstance(exc, ExchangeError) and not prepared_called
                result = {"status": "rejected" if known_pre_submit else "unknown", "confirmed": known_pre_submit,
                          "reason": type(exc).__name__}
        self.state.record_execution(identifier, result)
        return {**order, "reason": plan.get("reason"),
                "status": result.get("status", "unknown"), "order_id": result.get("order_id")}

    def _mark_and_exit(self, trade: bool) -> tuple[float, bool, list[dict]]:
        marks, complete, executions = {}, True, []
        for position in self.state.positions():
            token = position["token_id"]
            try:
                book, fee = self.public.get_book(token), self._fee(token)
                now = utcnow()
                age = (now - timestamp(book["timestamp"])).total_seconds()
                fill = walk_book(book, "SELL", position["shares"])
                if not -5 <= age <= self.cfg.max_book_age_seconds or fill is None:
                    raise ValueError("Position cannot be priced with fresh executable bids")
                marks[token] = fill[0] * (1 - fee)
                plan = exit_signal(position, book, fee, self.cfg, now)
                if plan and trade:
                    plan["question"] = position["question"]
                    plan["entry_kind"] = position.get("entry_kind", "forecast")
                    result = self._execute(plan, "SELL", book, fee)
                    executions.append(result)
                    if self.state.intents():
                        return self.state.get("cash"), False, executions
                    remaining = next((p for p in self.state.positions() if p["token_id"] == token), None)
                    if remaining:
                        residual = walk_book(book, "SELL", remaining["shares"])
                        if residual is None:
                            complete = False
                        else:
                            marks[token] = residual[0] * (1 - fee)
            except Exception as exc:
                complete = False
                self.state.decision({"token_id": token, "reason": "position_data_unavailable", "error": type(exc).__name__})
        equity = self.state.get("cash") + sum(marks.get(p["token_id"], 0) for p in self.state.positions())
        return equity, complete, executions

    def cycle(self, trade: bool = True) -> dict:
        if self.analysis_mode is not None and trade:
            raise ValueError("Engine analisis tidak boleh mengirim order")
        mode = self.analysis_mode or ("live" if self.live else "paper")
        if Path(self.cfg.state_dir, "STOP").exists():
            return {"mode": mode, "halted": "kill_switch", "orders": []}
        if self.state.intents():
            if self.live:
                self.reconcile()
            if self.state.intents():
                return {"mode": mode, "halted": self._pending_reason(), "orders": []}
        if self.live:
            self._check_balances()
        equity, priced, orders = self._mark_and_exit(trade)
        report = {"mode": mode, "time": utcnow().isoformat(), "equity": round(equity, 6),
                  "cash": self.state.get("cash"), "scanned": 0, "analyzed": 0,
                  "qualified": [], "rejections": {}, "errors": [], "orders": orders}
        if self.analysis_mode is not None:
            report["watchlist"] = []
        # Missing quotes block new risk without permanently latching a fictitious zero mark.
        halt = "position_data_unavailable" if not priced else self.state.risk_gate(equity, self.cfg, utcnow())
        if self.state.intents():
            halt = self._pending_reason()
        if halt:
            report["halted"] = halt
            return self._save_report(report)
        rejections, eligible = Counter(), []
        seen = set()
        for market in self.public.iter_markets(max_pages=self.cfg.scan_pages, page_size=100):
            token = market.get("token_id")
            if token in seen:
                continue
            seen.add(token)
            report["scanned"] += 1
            try:
                reason = market_rejection(market, self.cfg, utcnow())
            except (ValueError, TypeError, KeyError, OverflowError):
                reason = "invalid_market"
            if reason:
                rejections[reason] += 1
            else:
                eligible.append(market)
        # Analyze both outcome tokens together within the same request budget.
        # Manual scans start near expiry; workers rotate their analysis window.
        eligible.sort(key=lambda m: timestamp(m["end_date"]))
        cursor = self.state.get("scan_cursor", 0) % len(eligible) if eligible else 0
        selected = select_markets(
            eligible, 0 if self.analysis_mode is not None else cursor, self.cfg.max_candidates,
            spread=self.analysis_mode is not None or (self.live is None and self.cfg.paper_entry_mode == "explore"),
        )
        candidates = []
        analysis_started = time.monotonic()
        for market in selected:
            if time.monotonic() - analysis_started >= self.cfg.max_scan_seconds:
                report["analysis_budget_reached"] = True
                break
            report["analyzed"] += 1
            try:
                book = self.public.get_book(market["token_id"])
                if reason := book_rejection(book, self.cfg, utcnow()):
                    rejections[reason] += 1
                    continue
                ask = levels(book, "asks")[0][0]
                maximum_stake = self.cfg.growth_entry_dollars if equity >= self.cfg.growth_balance else self.cfg.entry_dollars
                if ask * number(book["min_order_size"]) > maximum_stake + 1e-8:
                    rejections["entry_below_exchange_minimum"] += 1
                    self._observe_rejected(report, market, book, "entry_below_exchange_minimum")
                    continue
                forecast = self._external_forecast(market, ask)
                if forecast is None and mode == "paper":
                    history = self.public.get_history(market["token_id"])
                    forecast = self.history_model.forecast(market, history, ask, utcnow())
                fee = self._fee(market["token_id"])
                plan = evaluate(market, book, forecast, fee, self.state.get("cash"), equity,
                                self.state.positions(), self.cfg, utcnow(), live=mode == "live")
                self.state.decision(plan)
                if plan["accepted"]:
                    candidates.append((plan, market))
                else:
                    rejections[plan["reason"]] += 1
                    self._observe_rejected(report, market, book, plan["reason"])
            except Exception as exc:
                rejections["data_error"] += 1
                report["errors"].append({"token_id": market.get("token_id"), "error": type(exc).__name__})
        if eligible:
            with self.state.db:
                self.state.set("scan_cursor", next_scan_cursor(eligible, cursor, selected[:report["analyzed"]]))
        candidates.sort(key=lambda item: (item[0].get("entry_kind") != "paper_exploratory", item[0]["score"]), reverse=True)
        exploration_attempted = False
        for original, market in candidates:
            report["qualified"].append({**{k: original[k] for k in ("token_id", "question", "outcome", "budget", "target_price", "probability_lower", "long_term")},
                                        "entry_kind": original.get("entry_kind", "forecast")})
            if not trade:
                continue
            if exploration_attempted and original.get("entry_kind") == "paper_exploratory":
                rejections["paper_exploration_cycle_limit"] += 1
                continue
            try:
                # Refresh book/fees and re-evaluate with the post-trade portfolio just before each order.
                book, fee = self.public.get_book(market["token_id"]), self._fee(market["token_id"])
                forecast = self._external_forecast(market, levels(book, "asks")[0][0])
                if forecast is None and mode == "paper" and original["forecast"].get("forecast_type") == "experimental":
                    forecast = original["forecast"]
                plan = evaluate(market, book, forecast, fee, self.state.get("cash"), equity,
                                self.state.positions(), self.cfg, utcnow(), live=mode == "live")
                if not plan["accepted"]:
                    rejections[plan["reason"]] += 1
                    continue
                if exploration_attempted and plan.get("entry_kind") == "paper_exploratory":
                    rejections["paper_exploration_cycle_limit"] += 1
                    continue
                forecast = plan["forecast"]
                # A forecast expires independently of the market; never extend its initial horizon.
                deadline = min(utcnow() + timedelta(hours=plan["horizon_hours"]),
                               timestamp(market["end_date"]) - timedelta(seconds=max(300, self.cfg.poll_seconds * 2)))
                if forecast.get("observed_at"):
                    deadline = min(deadline, timestamp(forecast["observed_at"]) + timedelta(hours=forecast["horizon_hours"]))
                if forecast.get("expires_at"):
                    deadline = min(deadline, timestamp(forecast["expires_at"]))
                plan["deadline"] = deadline.isoformat()
                plan["horizon_hours"] = max(0, (deadline - utcnow()).total_seconds() / 3600)
                if plan["horizon_hours"] <= 0:
                    rejections["expired_forecast"] += 1
                    continue
                if plan.get("entry_kind") == "paper_exploratory":
                    exploration_attempted = True
                orders.append(self._execute(plan, "BUY", book, fee))
                if self.state.intents():
                    report["halted"] = self._pending_reason()
                    break
                # Update equity at executable bids so spread/fees count towards risk immediately.
                equity, priced, _ = self._mark_and_exit(False)
                if not priced or self.state.risk_gate(equity, self.cfg, utcnow()):
                    report["halted"] = "risk_after_entry"
                    break
            except Exception as exc:
                report["errors"].append({"token_id": market.get("token_id"), "error": type(exc).__name__})
                if self.state.intents():
                    report["halted"] = self._pending_reason()
                    break
        report.update(rejections=dict(rejections), cash=self.state.get("cash"), equity=round(equity, 6))
        return self._save_report(report)

    def _save_report(self, report):
        with self.state.db:
            self.state.set("last_scan", report)
        return report
