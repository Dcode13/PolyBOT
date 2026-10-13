# Polymarket API integration notes

Implementation checked against official documentation and SDK source on 2026-10-10.
Public discovery uses standard-library HTTPS; optional live execution requires
`polymarket-client==0.12.0` and Python 3.11 or newer. No live trade was submitted
during development or tests. The archived `py-clob-client` is not used.

## Sources and migration details

- [Official Python SDK](https://github.com/Polymarket/py-sdk) and
  [package metadata](https://github.com/Polymarket/py-sdk/blob/main/pyproject.toml).
- [Discover Markets](https://docs.polymarket.com/market-data/discover-markets):
  Gamma `/events/keyset` returns `events` plus `next_cursor`. Requests continue
  with `after_cursor`. Each event supplies category tags and nested markets.
  CTF (`v1`) outcomes use parsed `clobTokenIds`; protocol `v2` outcomes use
  `positionIds`. Treating those identifiers interchangeably is incorrect.
- [Place Orders](https://docs.polymarket.com/trading/place-orders): FOK is all
  or nothing; BUY uses a dollar notional and SELL uses shares. The SDK accepts
  `max_price`/`min_price`, and a BUY `max_spend` fee-inclusive estimate. It is
  not a guarantee against a fee schedule change between signing and matching.
  The adapter checks the actual debit after settlement and halts on an overrun.
- [Current SDK fee parsing](https://github.com/Polymarket/py-sdk/blob/main/src/polymarket/_internal/actions/orders/market_data.py)
  resolves `/markets-by-token/{asset}` to `condition_id`, then reads
  `/clob-markets/{condition}`. `fd.r` is the fee rate and `fd.e` the exponent.
  Missing/null `fd` means fee-free only after validating the returned asset list,
  matching the official SDK. Malformed data is an error, not a zero fee.
- [Fee calculation](https://github.com/Polymarket/py-sdk/blob/main/src/polymarket/_internal/actions/orders/market.py)
  is shares × rate × `(price × (1-price)) ** exponent`. For exponent >= 1,
  `rate * 0.25 ** (exponent - 1)` bounds the fee as a fraction of notional at
  every price. Planning uses that conservative bound rather than hard-coded
  category fees; an additional rounding reserve is required. No builder code
  or additional builder fee is attached by this bot.
- [Fees](https://docs.polymarket.com/trading/fees) explains price-dependent taker
  fees and rounding. Minimum order size is a number of shares, not dollars;
  therefore a $1 budget cannot necessarily enter every market.
- [Account models](https://github.com/Polymarket/py-sdk/blob/main/src/polymarket/models/clob/account.py):
  balances and allowances are integers in six-decimal base units. Trades are
  final only when `CONFIRMED`; `MATCHED`, `MINED`, and `RETRYING` remain pending.
- [Geoblock](https://docs.polymarket.com/api-reference/geoblock): eligibility is
  checked at `https://polymarket.com/api/geoblock` before authentication and
  again before orders. Unknown eligibility stops live trading. The adapter
  does not attempt to bypass geographic restrictions.

## Durable execution contract

The engine must hold an exclusive process lock and durably create its order
intent before submitting. `place_fok(..., on_prepared=callback)` calls the callback
with the exact cash/share baseline **before** its single `post_order` call. The
callback should update the journal in a committed transaction. If persistence
fails, nothing is submitted. The engine must stop all new submissions until a
pending/unknown intent is reconciled, including after restart.

Returned dictionaries contain `status`, `confirmed`, `order_id`, `token_id`,
`side`, `shares`, `cash`, `price`, `fee`, and the baseline. Statuses are
`filled`, `rejected`, `pending`, and `unknown`. Filled `cash` is actual total
cash debited for a BUY or net credited for a SELL. A matched acknowledgement
alone never becomes a filled record. Reconciliation verifies the order ID,
asset, side, linked trade IDs, all trade confirmations, matched size, and
actual wallet balance movements. Settlement delays require subsequent reads.

Persist `order_id` and the baseline fields `token_id`, `side`, `before_cash`,
`before_shares`, `limit_price`, `requested_shares`, `max_spend`, `submitted_at`.
Call `reconcile_order(order_id, **baseline)` to resume. Never pass other fields
such as `status` to this method.

The current public `SignedOrder` has no order-hash property. If the POST response
is lost, the adapter returns `unknown`; it never resubmits. If the process crashes
after sending but before saving the returned ID, the journal's unacknowledged
intent must also cause a halt and manual account reconciliation. This is an
intentional limitation; a locally generated random ID would not prove whether
the exchange accepted the order.

Use a dedicated account with no manual trading, deposits, withdrawals, or other
bots while this bot operates: exact wallet delta reconciliation assumes account
exclusivity. Startup must reject unmanaged positions and existing open orders.
No automatic token approvals, redemption, bridging, or withdrawals are performed.

## SDK initialization caveat

The official public `SecureClient.create()` calls `_ensure_wallet_ready()` and
may deploy a missing deposit wallet. The pinned adapter deliberately uses the
SDK's `_create(private_key=..., wallet=...)` authentication factory to omit that
on-chain step, requiring an explicit existing wallet address. This private API
dependency is why the package version is exact and must be reviewed before
upgrading. The adapter uses `create_market_order()` plus `post_order()`, avoiding
the automatic allowance recovery performed by `place_market_order()`.

`preflight()`, `cash_balance()`, `token_balance()`, `open_orders()`, and
`positions()` are read-only account checks once authentication is established.
Authentication can derive API credentials; it does not place orders or deploy a
wallet in this adapter. Keys must be provided in local environment variables and
are never included in adapter exceptions or normal output.

## Verification scope

The exchange unit tests use a fake transport and SDK. They cover protocol IDs,
metadata inheritance, pagination, numeric validation, fee failure handling,
geographic checks, fee-inclusive budget enforcement, FOK use, durable callback
ordering, response-loss handling, and trade/balance reconciliation. They do not
prove profitability or that a live account has correct approvals. A read-only
network smoke check and paper-trading period are separate runtime checks.
