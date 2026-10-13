"""Conservative forecast inputs for a prediction-market *research* bot.

Historical prices contain no independent evidence about which outcome will win.
``QuantitativeForecaster`` only proposes a possible short-term price recovery. Its
non-overlapping, held-out touch observations are descriptive evidence, not a
calibrated probability, and its output is never eligible as a live forecast.

``ExternalForecasts`` reads explicit, timestamped forecasts from a local JSON file.
The producer, not this module, is responsible for independent research and honest
calibration. In particular, ``calibrated: true`` is a producer's assertion, not a
fact this module can establish. A resolution probability is not the probability
of reaching an intermediate sale price within the supplied trading horizon.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return result


def _datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 timestamp with a timezone")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} is not an ISO-8601 timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return result.astimezone(timezone.utc)


def _utc(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any, name: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be nonempty text (maximum {maximum} characters)")
    return value.strip()


def _market_ids(market: dict[str, Any]) -> tuple[str, str]:
    market_id = market.get("market_id", market.get("id"))
    token_id = market.get("token_id")
    if market_id is None or token_id is None:
        raise ValueError("market must supply an exact market_id (or id) and token_id")
    return _text(str(market_id), "market_id", 256), _text(str(token_id), "token_id", 256)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON property: {key}")
        result[key] = value
    return result


class ExternalForecasts:
    """Read a strict ``{\"forecasts\": [...]}`` local JSON feed.

    Every record needs market_id, token_id, probability, lower_probability,
    target_price, confidence, horizon_hours, observed_at, expires_at, source and
    rationale. IDs must be strings. ``probability_kind`` is either ``resolution``
    (the backward-compatible default) or ``target_touch``. The latter means the
    stated target will be touched during the horizon starting at observed_at.
    ``calibrated`` defaults to false and must be a literal boolean if supplied.

    A record expires at the earliest of its explicit expiry, its horizon, and the
    reader's maximum age. Expired/future-dated records are unavailable. Malformed
    feeds, duplicated JSON properties and duplicate token IDs are rejected in
    full; silently choosing one forecast would hide an upstream data problem.
    """

    MAX_FILE_BYTES = 1_048_576

    def __init__(self, path: str | Path, max_age_hours: float = 24) -> None:
        self.path = Path(path)
        self.max_age_hours = _number(max_age_hours, "max_age_hours", 0.01, 8760)

    def _read(self) -> dict[tuple[str, str], dict[str, Any]]:
        # A bounded read also prevents an accidentally enormous local export.
        with self.path.open("rb") as stream:
            raw = stream.read(self.MAX_FILE_BYTES + 1)
        if len(raw) > self.MAX_FILE_BYTES:
            raise ValueError("external forecast file exceeds 1 MiB")
        try:
            payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("external forecast file must contain valid UTF-8 JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {"forecasts"}:
            raise ValueError('external forecast file must contain only a "forecasts" list')
        records = payload["forecasts"]
        if not isinstance(records, list):
            raise ValueError("forecasts must be a list")
        result: dict[tuple[str, str], dict[str, Any]] = {}
        seen_tokens: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("every forecast must be an object")
            market_id = _text(record.get("market_id"), "market_id", 256)
            token_id = _text(record.get("token_id"), "token_id", 256)
            if token_id in seen_tokens:
                raise ValueError(f"duplicate forecast token_id: {token_id}")
            seen_tokens.add(token_id)
            probability = _number(record.get("probability"), "probability", 0, 1)
            lower = _number(record.get("lower_probability"), "lower_probability", 0, probability)
            target = _number(record.get("target_price"), "target_price", 0.000001, 0.999999)
            confidence = _number(record.get("confidence"), "confidence", 0, 1)
            horizon = _number(record.get("horizon_hours"), "horizon_hours", 0.01, 8760)
            observed = _datetime(record.get("observed_at"), "observed_at")
            expires = _datetime(record.get("expires_at"), "expires_at")
            if expires <= observed:
                raise ValueError("expires_at must be later than observed_at")
            calibrated = record.get("calibrated", False)
            if not isinstance(calibrated, bool):
                raise ValueError("calibrated must be a boolean")
            kind = record.get("probability_kind", "resolution")
            if kind not in ("resolution", "target_touch"):
                raise ValueError("probability_kind must be resolution or target_touch")
            result[(market_id, token_id)] = {
                "market_id": market_id,
                "token_id": token_id,
                "probability": probability,
                "lower_probability": lower,
                "probability_kind": kind,
                "target_price": target,
                "confidence": confidence,
                "horizon_hours": horizon,
                "observed_at": _iso(observed),
                "as_of": _iso(observed),
                "expires_at": _iso(expires),
                "source": _text(record.get("source"), "source"),
                "rationale": _text(record.get("rationale"), "rationale"),
                "calibrated": calibrated,
                "forecast_type": "external",
                "calibration_status": "producer_asserted" if calibrated else "uncalibrated",
            }
        return result

    def forecast(
        self,
        market: dict[str, Any],
        history: list[dict[str, Any]],
        current_price: float,
        now: datetime,
    ) -> dict[str, Any] | None:
        """Return the matching, fresh record; history is intentionally unused."""
        now = _utc(now)
        _number(current_price, "current_price", 0.000001, 0.999999)
        record = self._read().get(_market_ids(market))
        if record is None:
            return None
        observed = _datetime(record["observed_at"], "observed_at")
        age_hours = (now - observed).total_seconds() / 3600
        if (
            age_hours < 0
            or age_hours >= min(self.max_age_hours, record["horizon_hours"])
            or now >= _datetime(record["expires_at"], "expires_at")
        ):
            return None
        # Preserve the actual forecast horizon but report what remains for entry.
        record["remaining_horizon_hours"] = record["horizon_hours"] - age_hours
        if record["probability_kind"] == "target_touch":
            record["target_touch_probability"] = record["probability"]
            record["target_touch_lower_bound"] = record["lower_probability"]
        return record


def _wilson_lower(successes: int, count: int) -> float:
    """Descriptive 95% Wilson lower bound; observations need not be IID."""
    if not count:
        return 0.0
    z = 1.959963984540054
    rate = successes / count
    return (rate + z * z / (2 * count) - z * math.sqrt(
        rate * (1 - rate) / count + z * z / (4 * count * count)
    )) / (1 + z * z / count)


class QuantitativeForecaster:
    """Experimental price-recovery research with a chronological holdout.

    The first 60% of observations supplies an initial training period. A fixed
    rolling-median recovery rule is evaluated on subsequent observations, using
    only each observation's past to form its target. Evaluation windows never
    overlap and must have a full subsequent horizon. There is no hyperparameter
    search. At least eight held-out comparable dips are required by default.

    Even a successful holdout cannot establish fundamental value, independence,
    future stationarity, or execution at recorded prices. Accordingly every
    result has calibrated=False and forecast_type="experimental". Price touches
    ignore order-book depth, spread and fees; callers must check these separately.
    """

    def __init__(
        self,
        min_samples: int = 96,
        horizon_hours: float = 24,
        min_holdout_events: int = 8,
        min_target_return: float = 0.15,
        min_touch_lower_bound: float = 0.50,
    ) -> None:
        if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 48:
            raise ValueError("min_samples must be an integer of at least 48")
        if (
            isinstance(min_holdout_events, bool)
            or not isinstance(min_holdout_events, int)
            or min_holdout_events < 3
        ):
            raise ValueError("min_holdout_events must be an integer of at least 3")
        self.min_samples = min_samples
        self.horizon_hours = _number(horizon_hours, "horizon_hours", 0.25, 168)
        self.min_holdout_events = min_holdout_events
        self.min_target_return = _number(min_target_return, "min_target_return", 0.01, 10)
        self.min_touch_lower_bound = _number(min_touch_lower_bound, "min_touch_lower_bound", 0, 1)

    def forecast(
        self,
        market: dict[str, Any],
        history: list[dict[str, Any]],
        current_price: float,
        now: datetime,
    ) -> dict[str, Any] | None:
        now = _utc(now)
        try:
            current = _number(current_price, "current_price", 0.000001, 0.999999)
            market_id, token_id = _market_ids(market)
            points: list[tuple[float, float]] = []
            previous_t = -math.inf
            for point in history:
                if not isinstance(point, dict):
                    return None
                timestamp = _number(point.get("t"), "history t", 0, 32_503_680_000)
                price = _number(point.get("p"), "history p", 0.000001, 0.999999)
                # Fail closed on duplicates and disorder; future prices never enter statistics.
                if timestamp <= previous_t:
                    return None
                previous_t = timestamp
                if timestamp <= now.timestamp():
                    points.append((timestamp, price))
        except (ValueError, TypeError, OverflowError):
            return None
        if len(points) < self.min_samples:
            return None
        intervals = [points[i][0] - points[i - 1][0] for i in range(1, len(points))]
        cadence = statistics.median(intervals)
        if cadence < 60 or any(delta > cadence * 3 for delta in intervals):
            return None
        if now.timestamp() - points[-1][0] > max(2 * cadence, 3600):
            return None
        prices = [point[1] for point in points]
        train_end = int(len(points) * 0.60)
        lookback = min(48, max(12, int(24 * 3600 / cadence)))
        horizon_seconds = self.horizon_hours * 3600
        if train_end < lookback * 2 or points[-1][0] - points[train_end][0] < horizon_seconds:
            return None
        target = statistics.median(prices[-lookback:])
        if target / current - 1 < self.min_target_return:
            return None
        # A stale mark far from the executable ask does not describe this entry.
        if abs(prices[-1] - current) > max(0.03, current * 0.10):
            return None
        desired_drop = 1 - current / target

        def evaluate(start: int, end: int) -> tuple[int, int]:
            successes = 0
            count = 0
            index = max(lookback, start)
            while index < end:
                deadline = points[index][0] + horizon_seconds
                if deadline > points[end - 1][0]:
                    break
                past_target = statistics.median(prices[index - lookback:index])
                drop = 1 - prices[index] / past_target
                if (
                    past_target / prices[index] - 1 >= self.min_target_return
                    and desired_drop * 0.75 <= drop <= min(0.99, desired_drop * 1.25)
                ):
                    finish = index + 1
                    touched = False
                    while finish < end and points[finish][0] <= deadline:
                        touched = touched or prices[finish] >= past_target
                        finish += 1
                    # At least one later observation is necessary to evaluate a touch.
                    if finish > index + 1:
                        count += 1
                        successes += int(touched)
                    index = finish  # The next forecast starts after this horizon.
                else:
                    index += 1
            return successes, count

        train_successes, train_count = evaluate(lookback, train_end)
        holdout_successes, holdout_count = evaluate(train_end, len(points))
        if train_count < 3 or holdout_count < self.min_holdout_events:
            return None
        train_rate = train_successes / train_count
        holdout_rate = holdout_successes / holdout_count
        lower_bound = _wilson_lower(holdout_successes, holdout_count)
        if train_rate < 0.5 or lower_bound < self.min_touch_lower_bound:
            return None
        return {
            "market_id": market_id,
            "token_id": token_id,
            "target_price": target,
            "target_touch_probability": holdout_rate,
            "target_touch_lower_bound": lower_bound,
            "probability_kind": "target_touch",
            "confidence": lower_bound,
            "horizon_hours": self.horizon_hours,
            "remaining_horizon_hours": self.horizon_hours,
            "as_of": _iso(now),
            "observed_at": _iso(datetime.fromtimestamp(points[-1][0], timezone.utc)),
            "source": "experimental_historical_mean_reversion",
            "rationale": (
                f"Executable price {current:.4f} is below the trailing median {target:.4f}; "
                f"{holdout_successes}/{holdout_count} comparable non-overlapping held-out "
                f"dips touched their past-only target within {self.horizon_hours:g} hours. "
                "This is descriptive price-history research, not a calibrated win probability."
            ),
            "calibrated": False,
            "forecast_type": "experimental",
            "validation": {
                "method": "chronological_60_40_nonoverlapping_holdout",
                "samples": len(points),
                "training_samples": train_end,
                "holdout_samples": len(points) - train_end,
                "lookback_samples": lookback,
                "cadence_seconds": cadence,
                "training_events": train_count,
                "training_touch_rate": train_rate,
                "holdout_events": holdout_count,
                "holdout_successes": holdout_successes,
                "holdout_touch_rate": holdout_rate,
                "wilson_95_lower_bound": lower_bound,
                "limitations": "Uncalibrated; dependence and regime changes remain; touches exclude execution costs.",
            },
        }
