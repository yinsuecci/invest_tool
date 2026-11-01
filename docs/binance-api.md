# Local Binance API

## Start

`python -m dca_lab.compact_app --port 59755`

Default: paper mode, no credentials loaded, automatic Earn off. The listener binds only to `127.0.0.1`.
`--enable-live` permits a separate explicit switch to live mode; it does not enable it on startup.
The existing research commands and archived reports remain unchanged.

## Credentials and permissions

`POST /api/connect` accepts `api_key` and `api_secret`, checks the spot account, and retains credentials only in process memory. Restart/disconnect clears them. No environment files, browser storage, report JSON, or logs contain credentials. Do not put credentials in URLs or shell arguments.

Read-only keys are enough for account queries. Binance documents Spot & Margin Trading permission for Flexible Earn subscriptions. Do not enable withdrawals for this application. Product availability depends on account and region; the service queries the current account rather than assuming availability.

The application signs requests with HMAC-SHA256, uses a synchronized Binance timestamp and 5000ms recvWindow, fixes the upstream host to `https://api.binance.com`, rejects redirects, and exposes no withdrawal or margin endpoints.

## Local requests

First GET `/api/session`, retaining its `dca_session` HttpOnly SameSite=Strict cookie. Send its `csrf_token` as `X-DCA-Token` on all other API calls, along with `Origin: http://127.0.0.1:59755`. POST bodies must be JSON. No CORS is enabled. Reverse proxies and remote access are intentionally unsupported.

The complete interface schema is available at `/openapi.json`.

- `GET /api/state`: redacted connection, execution mode, free USDT, selected Earn balance, circuit-breaker and automatic sweep state.
- `GET /api/operations`: the latest20 journaled operation identifiers and outcomes, including ambiguous requests, without credentials or signatures.
- `POST /api/connect`: `{ "api_key": "...", "api_secret": "..." }`. Always resets execution to paper and disables automatic Earn.
- `POST /api/disconnect`: `{}`. Clears credentials and disables automatic Earn.
- `POST /api/settings`: `{ "mode": "paper", "auto_earn": false, "reserve_usdt": "60", "product_id": "<verified product id>" }`.
- `POST /api/refresh`: `{}`. Reads actual balances, never changes funds.
- `GET /api/earn/products`: lists USDT flexible products. Unavailable/sold-out/non-redeemable products cannot be used.
- `POST /api/earn/preview`: `{}`. Computes idle amount without submitting.
- `POST /api/earn/run`: `{ "request_id": "unique-id-0001" }`. Journaled single sweep.
- `POST /api/earn/redeem`: `{ "amount": "20", "request_id": "unique-id-0002" }`. Redeems only to SPOT; subject to actual exchange limits.
- `POST /api/order/preview`: `{ "asset": "SOL", "amount_usdt": "7.5", "limit_price": "100" }`.
- `POST /api/order/test`: same order body plus `request_id`; calls Binance order/test, which does not submit to the matching engine.
- `POST /api/order`: same body plus `request_id`. Journaled BUY LIMIT IOC, never a market order; no historical report order is replayed.

All seven asset identities are allowlisted. The exchange's current spot status, tick, lot size and notional filters are checked before an order; free spot USDT must cover the requested gross amount. Amount includes a 10bp fee buffer. This is an execution adapter, not a live strategy scheduler: historical fitted parameters are not automatically promoted to real trading.

## Idle USDT

The application-managed worker runs every five minutes only when enabled. It subscribes:

`floor_to_8_decimals(min(max(spot_free_USDT - reserve_USDT, 0), personal_remaining_quota))`

It skips amounts below the current product minimum. Only a user-selected, currently purchasable/redeemable USDT flexible product is permitted. It sets `sourceAccount=SPOT` and `autoSubscribe=false`: Binance's separate automatic subscription is deliberately not enabled, so disabling this worker is effective.

The reserve is liquidity planning, not a guaranteed future trading budget. It does not borrow, transfer funds from other wallets, or automatically redeem ahead of orders. Redemption availability/arrival is not assumed; only refreshed spot free balance is spendable. The redeem endpoint is provided for integrations. Existing interest and new Earn interest are not credited to the historical strategy-vs-DCA comparison.

## Duplicate and uncertain requests

Supply a unique stable `request_id` per intended operation. Reusing it returns the recorded outcome, not another submission. The local SQLite journal is committed before a live write; an unresolved request, timeout, or malformed response blocks further fund writes, including after restart. Writes are never automatically retried. An ambiguous Earn result cannot safely be matched to a unique client ID, so the application pauses rather than risking duplicate subscriptions. The local journal is operational state, not an independent exchange ledger.

Default startup and all delivered tests use no real credentials or live fund operations. No claim of production readiness or independent strategy validation is implied.

## Official references (verified 2026-11-01)

- [Binance Spot trading endpoints](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade)
- [Binance Simple Earn flexible endpoints](https://developers.binance.com/en/docs/catalog/investment-and-services-simple-earn/api/rest-api/flexible-locked)
