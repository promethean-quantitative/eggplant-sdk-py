# 🍆 eggplant-sdk-py 🐍

A highly performant python sdk for Polymarket.

[![PyPI](https://img.shields.io/pypi/v/eggplant-sdk)](https://pypi.org/project/eggplant-sdk/)
[![CI](https://github.com/promethean-quantitative/eggplant-sdk-py/actions/workflows/ci.yml/badge.svg)](https://github.com/promethean-quantitative/eggplant-sdk-py/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A complete, standalone Polymarket client — signing, posting, cancels,
relayer operations, and streaming — built for correctness, with zero
dependency on the official SDK. This is the Python sibling of
[`eggplant-sdk-rs`](https://github.com/promethean-quantitative/eggplant-sdk-rs);
the two share one API shape and one set of golden test vectors.

## Why

- **Resilient by design.** Tick sizes are plain `Decimal`s (a closed enum
  breaks the day the venue adds a grid), wire types degrade leniently
  instead of failing whole responses, order books parse per-element, cancel
  bookkeeping distinguishes terminal from transient misses, and the Data
  API's offset-cap recycling and the relayer's non-429 quota responses are
  handled.
- **Complete for real trading.** All four signature types, gasless
  merge/split/convert/redeem through the relayer, order books, positions,
  Gamma metadata, and both WS channels with the liveness protocol that keeps
  half-open sockets from silently eating your fills.
- **Financially strict.** `decimal.Decimal` on every order-affecting path —
  no floats where money moves.
- **Async-first.** `httpx` + `websockets` end to end; one event loop drives
  reads, writes, and streams.

## Feature matrix

> ⚠️ **Not (yet) at feature parity with the official Polymarket SDK.** The
> first table is what this SDK supports (alongside the Rust sibling); the
> second lists official-SDK surfaces neither eggplant SDK implements yet.

### Supported

| Capability                                                     | eggplant-sdk-py                                                | eggplant-sdk-rs          | official `polymarket_client_sdk_v2`    |
| -------------------------------------------------------------- | -------------------------------------------------------------- | ------------------------ | -------------------------------------- |
| L1 auth (create/derive API key)                                | ✓ (golden-vector pinned)                                       | ✓                        | ✓                                      |
| Signature types 0 / 1 / 2 / Poly1271                           | ✓ all four, one signer                                         | ✓                        | ✓                                      |
| Hot posting path                                               | partial — isolated order/cancel pools + warm-up; no pinned DNS | ✓ pinned DNS, warm tiers | —                                      |
| Batch + singular place/cancel endpoint switching               | ✓                                                              | ✓                        | —                                      |
| Cancel bookkeeping (`partition_cancels`, terminal reasons)     | ✓                                                              | ✓                        | —                                      |
| Relayer v2: SAFE / SAFE-CREATE / DepositWallet batches         | ✓                                                              | ✓                        | —                                      |
| negRisk merge / **split** / convert / redeem + planning engine | ✓                                                              | ✓                        | split/merge/redeem as raw on-chain txs |
| Lenient order books (`tick_size: Decimal`, per-book parse)     | ✓                                                              | ✓                        | closed tick enum (breaks on new grids) |
| Market WS: typed events, text PING/PONG liveness               | ✓                                                              | ✓ (+ zero-copy parsing)  | owned types                            |
| User WS: fills/orders + maker-side derivation + dedup          | ✓                                                              | ✓                        | types only                             |
| Gamma / Data API clients                                       | ✓                                                              | ✓                        | ✓                                      |
| Wallet CREATE2 derivation (proxy + Safe)                       | ✓                                                              | ✓                        | ✓                                      |

### Not (yet) supported

| Capability                                               | eggplant (py & rs) | official `polymarket_client_sdk_v2` |
| -------------------------------------------------------- | ------------------ | ----------------------------------- |
| Midpoint / spread / price quote endpoints¹               | —                  | ✓                                   |
| Last-trade-price REST endpoint¹                          | —                  | ✓                                   |
| Market listings & pagination (incl. sampling/simplified) | —                  | ✓                                   |
| Prices-history endpoint                                  | —                  | ✓                                   |
| Single-order lookup (`GET` one order by id)              | —                  | ✓                                   |
| API-key management (list / delete keys)                  | —                  | ✓                                   |
| Balance / allowance helpers (read + update)              | —                  | ✓                                   |
| Notifications (fetch / dismiss)                          | —                  | ✓                                   |
| Rewards & earnings endpoints                             | —                  | ✓                                   |
| Order scoring (`are_orders_scoring`)                     | —                  | ✓                                   |
| Market-order builder with automatic marketable pricing²  | —                  | ✓                                   |

¹ Midpoint/spread (and a live last-trade feed) are one-liners off a local
`Book` maintained from `/books` + the market channel (§4) — the REST quote
endpoints just aren't wrapped.
² Taker orders are placed as explicit FOK/FAK limits here (see §3); there is
no amount-in, price-discovered market-order helper.

## Install

From [PyPI](https://pypi.org/project/eggplant-sdk/):

```sh
pip install eggplant-sdk
```

Python ≥ 3.11. `eth-account`/`eth-keys` supply the key handling; every
price/size the SDK takes or returns on order paths is a `decimal.Decimal`.

## Usage

### 1. Authenticate

One builder covers all four venue signature types. The chain id always comes
from the builder (default Polygon, 137) — never silently from the signer.

```python
import os
from eggplant_sdk import ClobClient, LocalSigner
from eggplant_sdk.chain import POLYGON, derive_proxy_wallet, derive_safe_wallet
from eggplant_sdk.clob.types import SignatureType

signer = LocalSigner(os.environ["POLYMARKET_PRIVATE_KEY"])

# Type 0 (EOA holds the funds) — the default:
client = await ClobClient.builder().authenticate(signer)

# Type 1 (Magic/email proxy wallet) or 2 (browser Gnosis Safe): the wallet
# address is deterministic from your EOA —
proxy = derive_proxy_wallet(signer.address, POLYGON)
client = await (
    ClobClient.builder()
    .signature_type(SignatureType.PROXY)  # or GNOSIS_SAFE + derive_safe_wallet
    .funder(proxy)
    .authenticate(signer)
)

# Type 3 (ERC-1271 deposit wallet):
client = await (
    ClobClient.builder()
    .signature_type(SignatureType.POLY1271)
    .funder("0xYourDepositWallet")
    .authenticate(signer)
)
```

| type                          | who holds funds (`maker`) | who signs                |
| ----------------------------- | ------------------------- | ------------------------ |
| 0 `EOA`                       | the EOA itself            | EOA                      |
| 1 `PROXY` (Magic/email)       | proxy wallet              | EOA                      |
| 2 `GNOSIS_SAFE` (browser)     | 1-of-1 Safe               | EOA                      |
| 3 `POLY1271` (deposit wallet) | deposit wallet            | deposit wallet (wrapped) |

`authenticate` runs the L1 handshake: it signs the `ClobAuth` attestation,
tries `POST /auth/api-key`, and falls back to deriving the existing key. The
pieces are also exposed directly, which is what a hot restart wants — persist
the credentials once and skip the network round trip forever after:

```python
creds = await ClobClient.builder().derive_api_key(signer)  # or create_api_key
client = (
    ClobClient.builder()
    .signature_type(SignatureType.POLY1271)
    .funder(deposit_wallet)
    .with_credentials(signer.address, creds)  # no network
)
```

`Credentials` redacts its secret and passphrase from `repr()` output.

### 2. Read market data

```python
# CLOB metadata (public endpoints):
venue_time = await client.server_time()
tick = await client.tick_size(token_id)      # plain Decimal — any grid parses
neg_risk = await client.neg_risk(token_id)   # picks the signing domain below
market = await client.market(condition_id)   # tokens, tick, accepting_orders, …

# Order books — POST /books, parsed leniently (a malformed book is skipped,
# never poisons the batch). Chunked + concurrent past 500 ids:
books = await client.order_books([token_id])
by_id = await client.order_book_map(thousands_of_ids)

# Your open orders and trades (L2-authenticated, cursor-paged):
from eggplant_sdk.clob import OpenOrdersRequest
open_orders = await client.all_open_orders(OpenOrdersRequest())
trades = await client.trades(OpenOrdersRequest())
```

Event metadata and wallet holdings ride their own hosts, no credentials
needed:

```python
from eggplant_sdk.data import DataApiClient
from eggplant_sdk.gamma import GammaClient

# Page the open-event universe, or resolve one event by slug:
async with GammaClient() as gamma:
    page = await gamma.fetch_keyset_page(None, 50)  # feed back page.next_cursor
    events = await gamma.fetch_events_by_slug("some-event-slug")

# A wallet's positions (and what's redeemable):
async with DataApiClient() as data:
    positions = await data.all_positions("0xWallet…", 1.0)
    redeemable, hit_cap = await data.all_redeemable_positions("0xWallet…")
```

### 3. Place and cancel orders

Three steps: build a signer for the market's exchange domain, sign, POST
through the poster. Amounts are raw 6-decimal units; for a **BUY** the maker
amount is USDC (`size × price`) and the taker amount is shares (`size`); a
**SELL** swaps them.

```python
import time
from decimal import Decimal
from eggplant_sdk.clob.poster import PostTimings
from eggplant_sdk.clob.signing import (
    ExchangeDomain, build_signable_order_side, generate_salt, to_fixed_usdc,
)
from eggplant_sdk.clob.types import OrderType, Side

# Once per market family: negRisk and regular markets verify against
# different exchange contracts.
order_signer = client.order_signer(ExchangeDomain.ctf_v2(neg_risk))

# Once per process: the poster owns the write-path connection pools.
poster = client.poster()

# A resting BUY: 10 shares at 0.45, GTC + post-only (rests as maker
# liquidity; rejected rather than crossing).
size, price = Decimal(10), Decimal("0.45")
signable = build_signable_order_side(
    int(token_id),
    to_fixed_usdc(size * price),  # maker: USDC in
    to_fixed_usdc(size),          # taker: shares out
    client.identity,
    time.time_ns() // 1_000_000,
    OrderType.GTC,
    generate_salt(),
    True,  # post_only — only ever emitted for GTC/GTD
    Side.BUY,
)
signed = order_signer.sign_order(signable, signer)

timings = PostTimings()
posts = await poster.post_orders([signed], timings, int(time.time()))
response = posts[0].response
assert response.is_accepted(), response.error_msg

# Cancel by id (batched DELETE /orders; ≤1000 ids per request — use
# cancel_in_batches for more):
await poster.cancel_orders([response.order_id])
```

Taker orders are the same call with `OrderType.FOK`/`FAK` and
`post_only=False`. **A taker fill is credited from the POST response**
(`making_amount`/`taking_amount`) — it is _not_ echoed on the user channel,
which only carries maker fills.

Batch helpers for maker flows: `place_resting` / `place_resting_sell` sign
and POST whole ladders of GTC post-only orders, `place_marketable_sell`
fires a FAK whose limit is its own safety floor, and `deep_warm_up` keeps
the sign→POST pipeline hot with unfillable FOKs. `cancel_orders_by_side`
clears one side of the book without touching the other.

When a cancel response comes back, don't guess — partition it:

```python
from eggplant_sdk import EggplantError
from eggplant_sdk.clob.poster import partition_cancels

batch = [(0, 0, order_id)]
try:
    result = await poster.cancel_orders(ids)
except EggplantError:
    result = None  # transport failure: partition retries everything
done, retry = partition_cancels(batch, result, lambda leg: leg[2])
# `done`: confirmed cancelled OR terminally gone (already filled/expired/…).
# `retry`: transient misses — a still-live order is never silently dropped.
```

### 4. Keep a live order book

Seed over REST, then apply the market channel. Reconnect on any exception —
the resubscribe replays a fresh snapshot, so no state is lost beyond the gap.

```python
from eggplant_sdk.book import Book
from eggplant_sdk.ws.market import (
    MarketBook, MarketPriceChange, MarketStream, MarketStreamConfig,
    MarketTickSizeChange,
)

book = Book()
stream = await MarketStream.connect(MarketStreamConfig(token_ids=[token_id]))

while (event := await stream.next_event()) is not None:
    if isinstance(event, MarketBook):
        book.apply_snapshot(
            ((lv.price, lv.size) for lv in event.bids),
            ((lv.price, lv.size) for lv in event.asks),
        )
    elif isinstance(event, MarketPriceChange):
        for entry in event.price_changes:
            if (side := entry.book_side()) is not None:
                # Idempotent: duplicate deliveries are no-ops.
                book.apply_delta(side, entry.price, entry.size)
    elif isinstance(event, MarketTickSizeChange):
        ...  # re-grid quotes
    best_ask = book.best_ask()
```

The stream owns the venue's text `PING`/`PONG` liveness protocol internally;
a half-open socket surfaces as a `WsError` within ~30s instead of hanging
for minutes. For redundant fan-outs, `ws.util` has the staggered recycle
phasing that keeps long-lived connections fresh without two peers ever
refreshing at once.

### 5. Watch your fills

The user channel delivers **every** maker fill on the API key, on every
connection you open, with statuses that evolve (`MATCHED` → … →
`CONFIRMED`/`FAILED`). Handle them in this order: gate on a final status
**before** deduping (a `RETRYING` first sighting must not swallow the
confirmation), dedup by trade id, filter `maker_orders` to your own key, and
derive your side — the trade's top-level `side` describes the _taker_.

```python
from eggplant_sdk.ws.user import (
    TradeMessage, UserStream, UserStreamConfig, trade_status_is_final,
)
from eggplant_sdk.ws.util import SeenIds, our_maker_side

our_key = client.credentials.key
stream = await UserStream.connect(UserStreamConfig(credentials=client.credentials))
seen = SeenIds(1024)

while (message := await stream.next_message()) is not None:
    if not isinstance(message, TradeMessage):
        continue
    if not trade_status_is_final(message.status) or not seen.insert(message.id):
        continue
    for maker in message.maker_orders:
        if maker.owner != our_key:
            continue
        side = our_maker_side(message.side, message.outcome, maker.outcome)
        print(f"filled {side} {maker.matched_amount} @ {maker.price}")
```

For redundancy, open several identically-subscribed `UserStream`s behind one
shared `SeenIds` — first delivery wins.

### 6. Merge / split / convert / redeem (relayer)

Gasless position operations ride Polymarket's relayer as `DepositWallet`
batches (requires relayer API credentials from the builder program). The
negRisk math in one line each: **merge** burns YES+NO on a leg for $1;
**convert** burns NO across `k` legs of one event and frees `(k−1)·amount`;
**split** is merge's inverse; **redeem** collects a resolved market.

The engine turns live balances into the minimal submission plan — merges
before converts, tier decomposition, gas-budget chunking, wallet-busy
retries, and a final wrap of the freed USDC.e into pUSD:

```python
from eggplant_sdk.convert import (
    ConvertJob, convert_legs, fmt_usdc, plan_jobs, process_job,
)
from eggplant_sdk.gamma import GammaClient
from eggplant_sdk.relayer import RelayerClient

# Legs come straight off Gamma:
async with GammaClient() as gamma:
    event = (await gamma.fetch_events_by_slug(slug))[0]
legs = convert_legs(
    ids for ids in (m.market_ids() for m in event.markets or []) if ids is not None
)
job = ConvertJob(slug=slug, legs=legs)

# Read-only dry run — one RPC balance snapshot, no submissions:
plans, _snapshot = await plan_jobs([job], rpc_url, wallet, 100_000)
print(f"would free {fmt_usdc(plans[0].proceeds)} USDC.e")

# The full cycle (plan → submit chunks → settle → wrap):
relayer = RelayerClient(relayer_api_key, relayer_api_key_address)
detail = await process_job(job, signer, relayer, rpc_url, wallet)
```

For a long-running process, spawn `convert_worker` with an `asyncio.Queue`
and queue `ConvertJob`s as fills land — bursts coalesce into one shared
cycle. Standalone builders (`build_merge_calldata`, `build_split_calldata`,
`redeem_calls`, `split_calls`, …) need no RPC at all and feed
`RelayerClient.submit_deposit_wallet_batch` directly.

> ⚠ `splitPosition` is the least-exercised call in this SDK. Its 2-arg form
> mirrors the merge exactly, but verify against the deployed adapter (or
> split a dust amount first) before trusting it with size.

Error semantics worth handling: `RelayerQuotaExhaustedError` means retry on
**your own** fixed backoff (the relayer's `resets in` hint claims ~an hour;
the quota actually frees in well under a minute), and `err.is_wallet_busy()`
means a prior action is still settling and _nothing was submitted_ — waiting
and retrying is always safe.

### 7. One-time approvals

A fresh wallet must approve the exchange contracts before it can trade. For
the Safe path (type 2), `approval.ensure_approvals(signer, relayer,
rpc_url)` deploys the Safe if missing and grants pUSD + CTF approvals,
idempotently. Deposit wallets (type 3) batch the same `approve` /
`setApprovalForAll` calldata through `submit_deposit_wallet_batch` instead.

### 8. Handle errors

One `EggplantError` hierarchy end to end. The variants that should change
your control flow:

- `RateLimitError` — HTTP 429 anywhere. The poster can also fan a
  `RateLimitSignal` into a supervising task (`set_rate_limit_signal`) so a
  breaker can pull quotes instead of hammering.
- `RelayerQuotaExhaustedError` / `err.is_wallet_busy()` — see §6.
- `ApiError` — non-2xx from a read endpoint, body attached.
- `InvalidDataError` — unparseable input/response, and the poster's
  transport failures (timeout included). On a failed _place_, reconcile
  against `all_open_orders` before assuming nothing rested — a lost ACK is
  not a lost order.
- `WsError` — WebSocket transport failure or liveness (PONG deadline)
  breach; reconnect.

Wire parsers themselves rarely error: unknown enum values come back as raw
strings and optional fields default, because a strict parse of a drifting
venue is an outage waiting to happen.

## Venue notes

Rules and defaults the SDK encodes:

- **Venue minimums**: an order needs ≥ 5 shares (`tick.MIN_SIZE`) _or_ $1
  notional (`tick.MIN_NOTIONAL`); share sizes max 2 decimals
  (`SIZE_DECIMALS`, helpers `floor_to_size_step` / `compute_order_size`);
  prices live on `[tick, 1 − tick]` (`TickEntry`).
- **Salts are masked to 2^53 − 1** (`generate_salt`) so the venue's JS
  tooling round-trips them; the wire serializes salt as a JSON _number_.
- **Reuse one `Poster`**; call `warm_up()` periodically (and size
  `set_warm_sizes` to your batch shape) so an order POST rides a reused
  connection instead of a fresh TLS handshake. Cancels ride their own pool
  by design.
- **Data API offsets cap at 10,000** and then _recycle_ the same page —
  `all_positions` stops there on purpose. Redeem to shrink a huge wallet.
- **Order books can exceed 500 ids per request** only via chunking —
  `order_book_map` does it concurrently for you.
- **WS liveness is text `"PING"`/`"PONG"`**, not WebSocket ping opcodes; the
  streams enforce the 3-missed-pings deadline for you.

## Examples

Runnable walkthroughs in [`examples/`](examples/) — venue writes are gated
behind `EGGPLANT_LIVE_TRADE=1`, everything else is read-only:

| example               | shows                                                                          |
| --------------------- | ------------------------------------------------------------------------------ |
| `quickstart`          | authenticate (any signature type) → read grid → sign → optionally place+cancel |
| `stream_order_books`  | REST seed + market channel + `Book` maintenance (no credentials)               |
| `stream_user_fills`   | user channel with dedup, final-status gating, own-maker filtering              |
| `positions`           | Data API holdings + redeemable listing                                         |
| `convert_merge_split` | Gamma → legs → read-only plan → optional relayer cycle; split builders         |
| `approvals_bootstrap` | Safe-path approvals bootstrap                                                  |
| `redeem`              | discover redeemable positions → drain them in gas-bounded batches              |
| `sweep`               | discover holdings → dry-run report → optional merge/convert of every held event |

```sh
POLYMARKET_PRIVATE_KEY=0x… python examples/quickstart.py
TOKEN_IDS=<id> python examples/stream_order_books.py
```

## Modules

| module                                   | contents                                                                                                                                                                                                       |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `clob`                                   | `ClobClient` (auth, market/tick metadata, open orders, trades, cancel-all), `signing` (all-type `OrderSigner`), `poster` (`Poster` write path), `books` (lenient `/books`), `tick` (venue size rules), `types` |
| `relayer`                                | `RelayerClient`: SAFE / SAFE-CREATE / DepositWallet batch submission + EIP-712 hash builders                                                                                                                   |
| `convert`                                | merge/split/convert/redeem calldata, the tier planner, and the balance-read → plan → submit → wrap engine                                                                                                      |
| `redeem`                                 | discover a wallet's redeemable positions and drain them in gas-bounded batches                                                                                                                                 |
| `sweep`                                  | discover every held negRisk event (Data API + Gamma) and run the merge/convert cycle over all of them — the safety net ([docs/SWEEP.md](docs/SWEEP.md))                                                          |
| `approval`                               | Safe-path approvals bootstrap                                                                                                                                                                                  |
| `ws`                                     | market + user streams, liveness, recycle phasing, `our_maker_side`, `SeenIds`                                                                                                                                  |
| `gamma`, `data`                          | event metadata and wallet positions                                                                                                                                                                            |
| `book`, `fee`, `chain`, `auth`, `signer` | order-book state, fee math, contracts/hosts/CREATE2, L1/L2 primitives, local key signing                                                                                                                       |

## Verification

- Golden vectors shared with the Rust sibling (which cross-checked them
  against the official client): L1/L2 auth signatures, CREATE2 wallet
  addresses, relayer EIP-712 typehashes and a create-proxy hash.
- Differential tests: the precomputed EIP-712 signing fast path equals
  `eth-account`'s generic `encode_typed_data` for every known exchange
  domain.
- `EGGPLANT_LIVE=1 pytest tests/test_live.py` runs read-only smoke tests
  against the production venue.

The signing stack is pinned byte-for-byte to the Rust sibling by the shared
vectors; still, validate any new integration with small sizes first.

## Notes

- Python ≥ 3.11.
- Relayer operations require API credentials from Polymarket's builder
  program.

## Contributing

Dev setup, the test/lint workflow, project conventions, and the release
process are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

This software places real orders and moves real funds. It is provided as-is,
without warranty of any kind; nothing here is financial advice. Validate
every integration with minimum sizes first.
