import time
import unittest
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlparse

from polybot.exchange import ExchangeError, LiveExchange, PublicExchange, normalize_event


def fixture_event(version="v1"):
    return {"id": "1", "title": "Election", "active": True, "closed": False,
            "tags": [{"label": "Politics", "slug": "politics"}], "markets": [{
                "id": "2", "conditionId": "0xcondition", "question": "Who wins?",
                "outcomes": '["Yes", "No"]', "clobTokenIds": '["11", "12"]',
                "positionIds": ["21", "22"], "version": version, "active": True,
                "closed": False, "acceptingOrders": True, "enableOrderBook": True,
                "liquidityNum": 100, "volumeNum": 200, "volume24hr": 123}]}


class PublicTests(unittest.TestCase):
    def test_expands_protocol_correct_identifiers_and_event_tags(self):
        v1 = list(normalize_event(fixture_event()))
        v2 = list(normalize_event(fixture_event("v2")))
        self.assertEqual([m["token_id"] for m in v1], ["11", "12"])
        self.assertEqual([m["token_id"] for m in v2], ["21", "22"])
        self.assertEqual(v2[0]["outcome"], "Yes")
        self.assertIn("Politics", v2[0]["tags"])
        self.assertEqual(v2[0]["liquidity"], 100)
        self.assertEqual(v2[0]["volume_24h"], 123)

    def test_unknown_version_is_not_misinterpreted_as_ctf(self):
        self.assertEqual(list(normalize_event(fixture_event("v3"))), [])

    def test_no_first_outcome_keeps_its_token_and_quote_on_both_protocols(self):
        for version, tokens in (("v1", ["11", "12"]), ("v2", ["21", "22"])):
            with self.subTest(version=version):
                event = fixture_event(version)
                event["markets"][0].update(outcomes='["No", "Yes"]',
                                            outcomePrices='["0.2", "0.8"]')
                markets = list(normalize_event(event))
                self.assertEqual(
                    [(m["token_id"], m["outcome"], m["outcome_index"], m["last_price"])
                     for m in markets],
                    [(tokens[0], "No", 0, "0.2"), (tokens[1], "Yes", 1, "0.8")],
                )

    def test_named_binary_outcomes_are_not_rewritten_as_yes_no(self):
        event = fixture_event()
        event["markets"][0]["outcomes"] = ["Team A", "Team B"]
        markets = list(normalize_event(event))
        self.assertEqual([(m["token_id"], m["outcome"]) for m in markets],
                         [("11", "Team A"), ("12", "Team B")])

    def test_keyset_pagination_and_duplicate_assets(self):
        seen = []
        def transport(url):
            seen.append(parse_qs(urlparse(url).query))
            return {"events": [fixture_event()], "next_cursor": "next" if len(seen) == 1 else None}
        markets = list(PublicExchange(transport=transport).iter_markets())
        self.assertEqual(len(markets), 2)
        self.assertEqual(seen[1]["after_cursor"], ["next"])

    def test_book_sorted_and_microstructure_validated(self):
        raw = {"asset_id": "11", "bids": [{"price": ".2", "size": "3"}, {"price": ".4", "size": "5"}],
               "asks": [{"price": ".7", "size": "2"}, {"price": ".5", "size": "8"}],
               "timestamp": str(int(time.time() * 1000)), "tick_size": ".01", "min_order_size": "5"}
        public = PublicExchange(transport=lambda _: raw)
        book = public.get_book("11")
        self.assertEqual(book["asks"][0]["price"], .5)
        self.assertEqual(book["bids"][0]["price"], .4)
        self.assertLess(abs(book["timestamp"] - time.time()), 2)
        raw["asks"][0]["price"] = "NaN"
        with self.assertRaises(ExchangeError):
            public.get_book("11")

    def test_fees_are_market_specific_and_fail_closed(self):
        payload = {"t": [{"t": "11"}], "fd": {"r": .05, "e": 1}}
        public = PublicExchange(transport=lambda url: {"condition_id": "c"} if "markets-by-token" in url else payload)
        self.assertEqual(public.get_fee("11")["max_fee_fraction"], .05)
        payload["fd"] = None
        self.assertEqual(public.get_fee("11")["fraction"], 0)
        payload["fd"] = {"r": .05}
        with self.assertRaises(ExchangeError):
            public.get_fee("11")

    def test_geo_requires_boolean(self):
        public = PublicExchange(transport=lambda _: {"blocked": "false"})
        with self.assertRaises(ExchangeError):
            public.geoblock()


class FakePublic:
    def geoblock(self):
        return {"blocked": False}

    def get_book(self, token_id):
        return {"version": "v2", "timestamp": time.time(), "min_order_size": 2,
                "tick_size": .01, "asks": [{"price": .2, "size": 20}],
                "bids": [{"price": .2, "size": 20}]}

    def get_fee(self, token_id):
        return {"fraction": .05}


class FakeSDK:
    def __init__(self):
        self.post_calls = 0
        self.order_status = "MATCHED"
        self.trade_status = "CONFIRMED"
        self.fail_post = False
        self.cash, self.shares = 11, 0
        self.signed_kwargs = None

    def get_closed_only_mode(self):
        return False

    def list_open_orders(self):
        return [NS(items=[])]

    def get_balance_allowance(self, **kwargs):
        balance = self.cash if kwargs["asset_type"] == "COLLATERAL" else self.shares
        return NS(balance=round(balance * 1_000_000), allowances={"exchange": 10**12})

    def create_market_order(self, **kwargs):
        self.signed_kwargs = kwargs
        return NS(order_type="FOK", taker_amount=4_000_000, maker_amount=800_000)

    def post_order(self, signed):
        self.post_calls += 1
        if self.fail_post:
            raise TimeoutError("secret must not reach logs")
        self.cash, self.shares = 10.18, 4
        return NS(ok=True, order_id="order1", status="matched")

    def get_order(self, order_id):
        return NS(id="order1", asset_id="11", side="BUY", size_matched=4,
                  status=self.order_status, associate_trades=["trade1"])

    def list_account_trades(self, id):
        return [NS(items=[NS(id="trade1", taker_order_id="order1", asset_id="11", side="BUY",
                            status=self.trade_status, size=4, price=.2)])]


class LiveTests(unittest.TestCase):
    def test_sdk_current_size_normalized_for_unmanaged_inventory_detection(self):
        sdk = FakeSDK()
        sdk.list_positions = lambda **kw: [NS(items=[NS(asset_id="11", current_size="6.25")])]
        result = LiveExchange(sdk, FakePublic()).positions()
        self.assertEqual(result[0]["token_id"], "11")
        self.assertEqual(result[0]["size"], 6.25)

    def test_confirmed_trade_and_net_balances_required(self):
        sdk = FakeSDK()
        exchange = LiveExchange(sdk, FakePublic())
        calls = []
        result = exchange.place_fok("11", "BUY", 4, .2, .85,
                                    on_prepared=lambda snapshot: calls.append((sdk.post_calls, snapshot)))
        self.assertEqual(calls[0][0], 0)
        self.assertEqual(calls[0][1]["before_cash"], 11)
        self.assertEqual(result["status"], "filled")
        self.assertAlmostEqual(result["cash"], .82)
        self.assertAlmostEqual(result["fee"], .02)
        self.assertEqual(sdk.signed_kwargs["order_type"], "FOK")
        self.assertEqual(sdk.signed_kwargs["max_spend"], "0.85")

    def test_matched_response_is_not_a_fill(self):
        sdk = FakeSDK()
        sdk.trade_status = "MATCHED"
        result = LiveExchange(sdk, FakePublic()).place_fok("11", "BUY", 4, .2, .85)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["cash"], 0)

    def test_submit_timeout_unknown_without_retry_or_secret(self):
        sdk = FakeSDK()
        sdk.fail_post = True
        result = LiveExchange(sdk, FakePublic()).place_fok("11", "BUY", 4, .2, .85)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(sdk.post_calls, 1)
        self.assertNotIn("secret", str(result))

    def test_ledger_failure_prevents_submission(self):
        sdk = FakeSDK()
        def fail_ledger(snapshot):
            raise OSError("disk full")
        with self.assertRaises(OSError):
            LiveExchange(sdk, FakePublic()).place_fok("11", "BUY", 4, .2, .85, on_prepared=fail_ledger)
        self.assertEqual(sdk.post_calls, 0)

    def test_over_budget_and_below_minimum_never_submit(self):
        sdk = FakeSDK()
        live = LiveExchange(sdk, FakePublic())
        with self.assertRaises(ExchangeError):
            live.place_fok("11", "BUY", 4, .2, .8)
        with self.assertRaises(ExchangeError):
            live.place_fok("11", "BUY", 1, .2, .85)
        self.assertEqual(sdk.post_calls, 0)

    def test_trade_records_must_reference_same_order(self):
        sdk = FakeSDK()
        sdk.list_account_trades = lambda **kw: [NS(items=[NS(id="trade1", taker_order_id="other", asset_id="11", side="BUY")])]
        result = LiveExchange(sdk, FakePublic()).place_fok("11", "BUY", 4, .2, .85)
        self.assertEqual(result["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
