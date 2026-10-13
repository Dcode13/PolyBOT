import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polybot.forecast import ExternalForecasts, QuantitativeForecaster


NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
MARKET = {"id": "market-1", "token_id": "token-yes"}


def signal(**overrides):
    result = {
        "market_id": "market-1",
        "token_id": "token-yes",
        "probability": 0.85,
        "lower_probability": 0.75,
        "target_price": 0.60,
        "confidence": 0.80,
        "horizon_hours": 24,
        "observed_at": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": (NOW + timedelta(hours=23)).isoformat(),
        "source": "A documented external model",
        "rationale": "Independent evidence supplied by the forecast producer.",
        "calibrated": True,
    }
    result.update(overrides)
    return result


def repeated_dips(count=841):
    start = NOW - timedelta(hours=count - 1)
    return [
        {"t": (start + timedelta(hours=i)).timestamp(), "p": 0.30 if i % 12 < 2 else 0.60}
        for i in range(count)
    ]


class ExternalForecastTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "forecasts.json"

    def write(self, records):
        self.path.write_text(json.dumps({"forecasts": records}), encoding="utf-8")
        return ExternalForecasts(self.path)

    def test_exact_matching_and_explicit_semantics(self):
        provider = self.write([signal()])
        actual = provider.forecast(MARKET, [], 0.30, NOW)
        self.assertEqual(actual["probability_kind"], "resolution")
        self.assertEqual(actual["remaining_horizon_hours"], 23)
        self.assertEqual(actual["calibration_status"], "producer_asserted")
        self.assertNotIn("target_touch_probability", actual)
        self.assertIsNone(provider.forecast({"id": "different", "token_id": "token-yes"}, [], 0.30, NOW))
        self.assertIsNone(provider.forecast({"id": "market-1", "token_id": "token-no"}, [], 0.30, NOW))

    def test_target_touch_signals_are_labeled(self):
        provider = self.write([signal(probability_kind="target_touch")])
        actual = provider.forecast(MARKET, [], 0.30, NOW)
        self.assertEqual(actual["target_touch_probability"], 0.85)
        self.assertEqual(actual["target_touch_lower_bound"], 0.75)

    def test_no_forecast_uses_its_own_token_probability_and_target(self):
        no_market = {"market_id": "market-1", "token_id": "token-no", "outcome": "No"}
        for kind in ("resolution", "target_touch"):
            with self.subTest(kind=kind):
                provider = self.write([
                    signal(probability_kind=kind, probability=0.25, lower_probability=0.15),
                    signal(token_id="token-no", probability_kind=kind, probability=0.75,
                           lower_probability=0.60, target_price=0.80),
                ])
                actual = provider.forecast(no_market, [], 0.50, NOW)
                self.assertEqual(actual["token_id"], "token-no")
                self.assertEqual(actual["probability"], 0.75)
                self.assertEqual(actual["lower_probability"], 0.60)
                self.assertEqual(actual["target_price"], 0.80)
                yes = provider.forecast(MARKET, [], 0.30, NOW)
                self.assertEqual(yes["lower_probability"], 0.15)
                self.assertEqual(yes["target_price"], 0.60)

    def test_yes_only_feed_does_not_invent_no_from_bounds_or_label(self):
        no_market = {"market_id": "market-1", "token_id": "token-no", "outcome": "No"}
        for kind in ("resolution", "target_touch"):
            with self.subTest(kind=kind):
                # 1 - the Yes lower bound is not a conservative No lower bound.
                # An outcome label cannot override the identity of the forecast token.
                provider = self.write([signal(probability_kind=kind, probability=0.20,
                                              lower_probability=0.10, outcome="No")])
                self.assertIsNone(provider.forecast(no_market, [], 0.80, NOW))
                self.assertEqual(provider.forecast(MARKET, [], 0.20, NOW)["probability"], 0.20)

    def test_missing_calibration_defaults_to_false(self):
        record = signal()
        del record["calibrated"]
        self.assertFalse(self.write([record]).forecast(MARKET, [], 0.30, NOW)["calibrated"])

    def test_explicit_expiry_horizon_max_age_and_future_dates(self):
        cases = [
            signal(expires_at=NOW.isoformat()),
            signal(horizon_hours=1),
            signal(observed_at=(NOW - timedelta(hours=25)).isoformat()),
            signal(observed_at=(NOW + timedelta(minutes=1)).isoformat()),
        ]
        for record in cases:
            with self.subTest(record=record):
                self.assertIsNone(self.write([record]).forecast(MARKET, [], 0.30, NOW))

    def test_invalid_inputs_are_rejected_in_full(self):
        cases = [
            {"probability": math.nan},
            {"probability": math.inf},
            {"probability": True},
            {"lower_probability": 0.9},
            {"lower_probability": -0.1},
            {"confidence": "0.9"},
            {"confidence": 1.1},
            {"target_price": 1},
            {"target_price": 0},
            {"horizon_hours": 0},
            {"observed_at": "2026-10-10T10:00:00"},
            {"expires_at": "nonsense"},
            {"expires_at": (NOW - timedelta(hours=2)).isoformat()},
            {"source": " "},
            {"rationale": None},
            {"market_id": 1},
            {"token_id": ""},
            {"calibrated": "true"},
            {"probability_kind": "sure_win"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    self.write([signal(**overrides)]).forecast(MARKET, [], 0.30, NOW)

    def test_malformed_unrelated_record_cannot_hide_in_feed(self):
        provider = self.write([signal(), signal(token_id="different", probability=math.nan)])
        with self.assertRaises(ValueError):
            provider.forecast(MARKET, [], 0.30, NOW)

    def test_duplicate_tokens_are_rejected_even_across_markets(self):
        for second in (signal(), signal(market_id="other")):
            provider = self.write([signal(), second])
            with self.assertRaisesRegex(ValueError, "duplicate forecast token_id"):
                provider.forecast(MARKET, [], 0.30, NOW)

    def test_duplicate_json_properties_are_rejected(self):
        self.path.write_text('{"forecasts": [], "forecasts": []}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON property"):
            ExternalForecasts(self.path).forecast(MARKET, [], 0.30, NOW)

    def test_updates_are_loaded_without_restart(self):
        provider = self.write([signal()])
        self.assertEqual(provider.forecast(MARKET, [], 0.30, NOW)["probability"], 0.85)
        self.write([signal(probability=0.90)])
        self.assertEqual(provider.forecast(MARKET, [], 0.30, NOW)["probability"], 0.90)

    def test_naive_clock_is_rejected(self):
        with self.assertRaises(ValueError):
            self.write([signal()]).forecast(MARKET, [], 0.30, NOW.replace(tzinfo=None))


class QuantitativeForecastTests(unittest.TestCase):
    def setUp(self):
        self.provider = QuantitativeForecaster(horizon_hours=4)
        self.history = repeated_dips()

    def test_repeated_recovery_is_only_an_experimental_price_signal(self):
        actual = self.provider.forecast(MARKET, self.history, 0.30, NOW)
        self.assertIsNotNone(actual)
        self.assertEqual(actual["target_price"], 0.60)
        self.assertEqual(actual["target_touch_probability"], 1)
        self.assertGreater(actual["target_touch_lower_bound"], 0.8)
        self.assertEqual(actual["confidence"], actual["target_touch_lower_bound"])
        self.assertFalse(actual["calibrated"])
        self.assertEqual(actual["forecast_type"], "experimental")
        self.assertNotIn("probability", actual)
        self.assertNotIn("lower_probability", actual)
        self.assertGreaterEqual(actual["validation"]["holdout_events"], 8)

    def test_future_prices_cannot_change_forecast(self):
        expected = self.provider.forecast(MARKET, self.history, 0.30, NOW)
        future = [{"t": (NOW + timedelta(hours=i)).timestamp(), "p": 0.999} for i in range(1, 100)]
        self.assertEqual(expected, self.provider.forecast(MARKET, self.history + future, 0.30, NOW))

    def test_small_and_uncertain_samples_do_not_produce_signals(self):
        self.assertIsNone(self.provider.forecast(MARKET, self.history[-40:], 0.30, NOW))
        self.assertIsNone(self.provider.forecast(MARKET, self.history[-100:], 0.30, NOW))

    def test_no_signal_without_recovery_potential(self):
        flat = [{**point, "p": 0.30} for point in self.history]
        self.assertIsNone(self.provider.forecast(MARKET, flat, 0.30, NOW))
        self.assertIsNone(self.provider.forecast(MARKET, self.history, 0.59, NOW))

    def test_holdout_failure_rejects_an_excellent_training_period(self):
        # In the holdout, comparable dips recover only after the four-hour horizon.
        train_end = int(len(self.history) * 0.60)
        for i in range(train_end, len(self.history)):
            self.history[i]["p"] = 0.30 if i % 12 < 6 else 0.60
        self.assertIsNone(self.provider.forecast(MARKET, self.history, 0.30, NOW))

    def test_unsorted_duplicate_nonfinite_and_gappy_history_is_rejected(self):
        reversed_points = list(reversed(self.history))
        duplicated = self.history[:100] + [self.history[99]] + self.history[100:]
        nan_price = [dict(point) for point in self.history]
        nan_price[50]["p"] = math.nan
        gap = self.history[:100] + self.history[110:]
        for history in (reversed_points, duplicated, nan_price, gap):
            with self.subTest(length=len(history)):
                self.assertIsNone(self.provider.forecast(MARKET, history, 0.30, NOW))

    def test_stale_and_nonmatching_history_is_rejected(self):
        self.assertIsNone(self.provider.forecast(MARKET, self.history, 0.30, NOW + timedelta(hours=3)))
        self.assertIsNone(self.provider.forecast(MARKET, self.history, 0.20, NOW))

    def test_configuration_validation(self):
        for kwargs in ({"min_samples": 10}, {"min_holdout_events": 1}, {"horizon_hours": 0}, {"min_target_return": math.nan}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    QuantitativeForecaster(**kwargs)


if __name__ == "__main__":
    unittest.main()
