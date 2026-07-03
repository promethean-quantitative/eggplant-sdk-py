"""The order write path: a dedicated poster with isolated connection pools
and L2 signing over the exact bytes sent.

Design notes carried over from the Rust sibling SDK:

- **Two isolated pools**: order POSTs and cancels each get their own HTTP
  client, so a placement burst can never push a latency-critical cancel onto
  a cold connection.
- **Warm-up**: :meth:`Poster.warm_up` pings a small hot set of connections;
  :meth:`Poster.warm_reserve` holds a larger fleet of completed TLS
  handshakes for multi-leg bursts.
- **Sign-what-you-send**: the L2 HMAC covers the exact serialized bytes that
  go on the wire.

The Rust sibling additionally pins the venue's DNS and hand-tunes its
connection pools; those latency extras have no Python equivalent here (see
the feature matrix in the README).

All order writes should come through here; :class:`~eggplant_sdk.clob.ClobClient`
covers reads and admin calls.
"""

from __future__ import annotations

import asyncio
import base64
import enum
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, TypeVar

import httpx

from ..auth import (
    POLY_ADDRESS,
    POLY_API_KEY,
    POLY_PASSPHRASE,
    POLY_SIGNATURE,
    POLY_TIMESTAMP,
    Credentials,
)
from ..errors import EggplantError, InvalidDataError, RateLimitError
from ..signer import LocalSigner
from .signing import (
    OrderSigner,
    build_signable_order,
    build_signable_order_side,
    generate_salt,
    to_fixed_usdc,
)
from .tick import FIXED_SIZE, TickEntry
from .types import (
    CancelOrdersResponse,
    OrderStatus,
    OrderType,
    PostOrderResponse,
    Side,
    SignedOrder,
)

if TYPE_CHECKING:
    from . import ClobClient

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30.0

#: Max order ids per ``DELETE /orders`` payload.
#:
#: The CLOB cancel API rejects any payload over 1000 ids with a 400 ("Too
#: many orders in payload, max allowed: 1000") — larger chunks fail the whole
#: pass once a key holds >1000 resting orders.
CANCEL_BATCH_SIZE = 1000

#: Order-pool connections pinged every :meth:`Poster.warm_up` pass, sized for
#: a burst of concurrent place POSTs.
WARM_CONNECTIONS = 4
#: Cancel-pool warm connections: cancels are low-volume but latency-critical,
#: so a few stay hot and isolated.
CANCEL_WARM_CONNECTIONS = 4


class CancelEndpoint(enum.Enum):
    """Which CLOB endpoint urgent cancels (:meth:`Poster.cancel_for_reprice`)
    use. Bulk lifecycle cancels always use the batched ``DELETE /orders``
    regardless."""

    #: Batched ``DELETE /orders`` — one request carrying every order id.
    ORDERS = "orders"
    #: Singular ``DELETE /order`` — one request per id, fired concurrently.
    #: The two endpoints carry separate venue rate limits.
    ORDER = "order"

    def flipped(self) -> CancelEndpoint:
        """The *other* endpoint — useful when a cancel-path 429 makes a
        caller flip to the alternate ``DELETE`` path. An involution."""
        return CancelEndpoint.ORDER if self is CancelEndpoint.ORDERS else CancelEndpoint.ORDERS

    def as_config_str(self) -> str:
        """The lowercase token for config round-trips."""
        return self.value


class OrderEndpoint(enum.Enum):
    """Which CLOB endpoint :meth:`Poster.post_one` places through. Batch
    placement (:func:`post_signed`) always uses ``POST /orders``
    regardless."""

    #: Batched ``POST /orders`` — one request carrying a chunk of orders.
    ORDERS = "orders"
    #: Singular ``POST /order`` — one request per order.
    ORDER = "order"


class RateLimitEndpoint(enum.Enum):
    """Which CLOB call path tripped the rate-limit breaker."""

    #: ``POST /order(s)`` — order placement.
    PLACE_ORDER = "place order"
    #: ``DELETE /order(s)`` — order cancellation.
    CANCEL = "cancel"

    def as_str(self) -> str:
        """Human label; reads naturally as "on the {…} endpoint"."""
        return self.value


class RateLimitSignal:
    """Rate-limit breaker handle shared between the poster and a supervising
    task: an event the task awaits plus the endpoint of the most recent 429,
    so the wake-up can name which call path was throttled."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._endpoint = RateLimitEndpoint.PLACE_ORDER

    async def notified(self) -> None:
        """Resolves when a 429 has tripped the breaker. Pair with
        :meth:`endpoint` to read which call path was throttled."""
        await self._event.wait()
        self._event.clear()

    def endpoint(self) -> RateLimitEndpoint:
        """The endpoint that tripped the most recent 429."""
        return self._endpoint

    def signal(self, endpoint: RateLimitEndpoint) -> None:
        """Record the throttled endpoint and wake the supervisor."""
        self._endpoint = endpoint
        self._event.set()


class ApiCallCounters:
    """Session-total counts of place/cancel HTTP requests actually sent.

    Incremented once per request as it goes on the wire — including failures
    and timeouts, which still count against the venue's rate limits. Cancels
    split by endpoint: singular ``DELETE /order`` and batched
    ``DELETE /orders`` carry separate venue rate limits. Warm-up pings are
    excluded. Share one instance between the poster and whatever reads the
    rates (single event loop assumed).
    """

    def __init__(self) -> None:
        self._place = 0
        self._cancel_order = 0
        self._cancel_orders = 0

    def place_total(self) -> int:
        return self._place

    def cancel_order_total(self) -> int:
        return self._cancel_order

    def cancel_orders_total(self) -> int:
        return self._cancel_orders

    def _record_place(self) -> None:
        self._place += 1

    def _record_cancel(self, batched: bool) -> None:
        if batched:
            self._cancel_orders += 1
        else:
            self._cancel_order += 1


@dataclass
class PostTimings:
    """Timings for one place round, for callers that watch their send
    latency."""

    #: JSON serialization + HMAC of the request body, ms.
    serialize_ms: float = 0.0
    #: Round trip of the slowest chunk, ms.
    network_ms: float = 0.0
    #: Whole :func:`post_signed` round, ms.
    post_ms: float = 0.0
    #: ``POST /orders`` chunks the round was split into.
    chunk_count: int = 0


@dataclass
class LegPost:
    """Outcome of one posted order: the venue's response plus the POST's
    round trip."""

    response: PostOrderResponse
    rtt_ms: float


def _new_client() -> httpx.AsyncClient:
    """An HTTP/1.1 client with its own connection pool and long keep-alive.
    Each call yields an isolated pool — orders and cancels use separate ones
    so a POST burst can't occupy a connection a cancel needs."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(_REQUEST_TIMEOUT),
        limits=httpx.Limits(max_keepalive_connections=32, keepalive_expiry=300.0),
        http2=False,
    )


class Poster:
    """The dedicated order-write client. Build from an authenticated
    :class:`~eggplant_sdk.clob.ClobClient` via
    :meth:`~eggplant_sdk.clob.ClobClient.poster`."""

    def __init__(self, host: str, address: str, credentials: Credentials):
        try:
            self._hmac_key = base64.urlsafe_b64decode(credentials.secret())
        except ValueError as e:
            raise InvalidDataError(f"invalid HMAC secret: {e}") from e

        self._orders_url = f"{host}orders"
        self._order_url = f"{host}order"
        self._client = _new_client()
        self._cancel_client = _new_client()

        self._base_headers = {
            "Content-Type": "application/json",
            POLY_ADDRESS: address,
            POLY_API_KEY: str(credentials.key),
            POLY_PASSPHRASE: credentials.passphrase(),
        }

        self._rate_limit: RateLimitSignal | None = None
        self._call_counters: ApiCallCounters | None = None
        #: Endpoint :meth:`cancel_for_reprice` uses; every other cancel path
        #: always batches ``DELETE /orders``.
        self._cancel_endpoint = CancelEndpoint.ORDERS
        #: Endpoint :meth:`post_one` uses; :func:`post_signed` always batches.
        self._order_endpoint = OrderEndpoint.ORDERS
        self._warm_connections = WARM_CONNECTIONS
        self._cancel_warm_connections = CANCEL_WARM_CONNECTIONS
        #: TOTAL order-pool connections held open by the slower
        #: :meth:`warm_reserve` cadence. ``0`` (default) disables the tier.
        self._warm_reserve_connections = 0

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._cancel_client.aclose()

    async def __aenter__(self) -> Poster:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def set_warm_sizes(self, hot: int, cancel: int, reserve: int) -> None:
        """Size the warm-connection tiers: ``hot`` order-pool and ``cancel``
        cancel-pool connections are pinged on every :meth:`warm_up` pass;
        ``reserve`` is the total order-pool fleet held open by the slower
        :meth:`warm_reserve` cadence (``0`` disables it). Set once at startup
        before the poster is shared across tasks."""
        self._warm_connections = hot
        self._cancel_warm_connections = cancel
        self._warm_reserve_connections = reserve

    def set_rate_limit_signal(self, signal: RateLimitSignal) -> None:
        """Wire a signal fired on any CLOB 429 so a supervising task can trip
        a rate-limit breaker. Set once at startup before sharing the
        poster."""
        self._rate_limit = signal

    def set_call_counters(self, counters: ApiCallCounters) -> None:
        """Wire shared place/cancel request tallies. Set once at startup;
        left unwired, nothing is counted."""
        self._call_counters = counters

    def set_cancel_endpoint(self, endpoint: CancelEndpoint) -> None:
        """Select the endpoint :meth:`cancel_for_reprice` uses. Set once at
        startup. Every other cancel path stays on the batched ``/orders``."""
        self._cancel_endpoint = endpoint

    def set_order_endpoint(self, endpoint: OrderEndpoint) -> None:
        """Select the endpoint :meth:`post_one` uses. Set once at startup.
        Batch placement always uses ``/orders``."""
        self._order_endpoint = endpoint

    def _signal_rate_limit(self, endpoint: RateLimitEndpoint) -> None:
        if self._rate_limit is not None:
            self._rate_limit.signal(endpoint)

    async def warm_up(self) -> list[float]:
        """Ping each warm connection on both pools concurrently and return
        the per-ping latency in ms. A ping near the others is a reused (warm)
        connection; a spike is a fresh TLS handshake — how warmth is
        observed."""
        pings = [self._warm_single(self._client) for _ in range(self._warm_connections)]
        pings += [
            self._warm_single(self._cancel_client) for _ in range(self._cancel_warm_connections)
        ]
        return list(await asyncio.gather(*pings))

    async def warm_reserve(self) -> list[float]:
        """Ping ``warm_reserve_connections`` order-pool connections
        concurrently — concurrent in-flight requests are what force the pool
        to open and retain that many distinct connections. Run on a slower
        cadence than :meth:`warm_up`. No-op when sized 0."""
        if self._warm_reserve_connections == 0:
            return []
        pings = [self._warm_single(self._client) for _ in range(self._warm_reserve_connections)]
        return list(await asyncio.gather(*pings))

    async def _warm_single(self, client: httpx.AsyncClient) -> float:
        started = time.perf_counter()
        try:
            response = await client.post(
                self._orders_url,
                content=b"[]",
                headers={"Content-Type": "application/json"},
            )
            logger.debug("CLOB path kept warm (http %s)", response.status_code)
        except httpx.HTTPError as e:
            logger.warning("CLOB warm-up failed (non-fatal): %s", e)
        return (time.perf_counter() - started) * 1000.0

    def _sign_body(self, api_ts: int, sign_path: bytes, body: bytes) -> str:
        message = str(api_ts).encode() + sign_path + body
        digest = hmac.new(self._hmac_key, message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode()

    def _headers(self, signature: str, api_ts: int) -> dict[str, str]:
        return {
            **self._base_headers,
            POLY_SIGNATURE: signature,
            POLY_TIMESTAMP: str(api_ts),
        }

    async def _round_trip(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, bytes]:
        try:
            response = await client.request(method, url, headers=headers, content=body)
        except httpx.TimeoutException as e:
            raise InvalidDataError(f"{method} timed out: {e}") from e
        except httpx.HTTPError as e:
            raise InvalidDataError(f"{method} failed: {e}") from e
        return response.status_code, response.content

    async def post_orders(
        self, orders: list[SignedOrder], timings: PostTimings, api_ts: int
    ) -> list[LegPost]:
        """POST a batch of signed orders to ``POST /orders``. The L2 HMAC
        covers the exact serialized bytes sent. ``api_ts`` is the L2
        timestamp (Unix seconds)."""
        t0 = time.perf_counter()
        body = json.dumps([o.to_wire() for o in orders], separators=(",", ":")).encode()
        signature = self._sign_body(api_ts, b"POST/orders", body)
        timings.serialize_ms = (time.perf_counter() - t0) * 1000.0

        if self._call_counters is not None:
            self._call_counters._record_place()
        t1 = time.perf_counter()
        status, resp_body = await self._round_trip(
            self._client, "POST", self._orders_url, self._headers(signature, api_ts), body
        )
        rtt_ms = (time.perf_counter() - t1) * 1000.0
        timings.network_ms = rtt_ms

        if status == 429:
            self._signal_rate_limit(RateLimitEndpoint.PLACE_ORDER)
            raise RateLimitError()
        if status >= 400:
            raise InvalidDataError(f"CLOB API {status}: {resp_body.decode(errors='replace')}")

        try:
            parsed = json.loads(resp_body)
            responses = [PostOrderResponse.from_dict(r) for r in parsed]
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            raise InvalidDataError(f"response parse failed: {e}") from e
        if len(responses) != len(orders):
            raise InvalidDataError(f"expected {len(orders)} responses, got {len(responses)}")
        return [LegPost(response=r, rtt_ms=rtt_ms) for r in responses]

    async def _post_order_single(
        self, order: SignedOrder, timings: PostTimings, api_ts: int
    ) -> LegPost:
        """POST a **single** order to the singular ``POST /order`` endpoint:
        the body is a JSON object (not a 1-element array) and the sign path
        is ``POST/order``. 429 handling, timeout, and headers are identical
        to :meth:`post_orders`."""
        t0 = time.perf_counter()
        body = json.dumps(order.to_wire(), separators=(",", ":")).encode()
        signature = self._sign_body(api_ts, b"POST/order", body)
        timings.serialize_ms = (time.perf_counter() - t0) * 1000.0

        if self._call_counters is not None:
            self._call_counters._record_place()
        t1 = time.perf_counter()
        status, resp_body = await self._round_trip(
            self._client, "POST", self._order_url, self._headers(signature, api_ts), body
        )
        rtt_ms = (time.perf_counter() - t1) * 1000.0
        timings.network_ms = rtt_ms

        if status == 429:
            self._signal_rate_limit(RateLimitEndpoint.PLACE_ORDER)
            raise RateLimitError()
        if status >= 400:
            raise InvalidDataError(f"CLOB API {status}: {resp_body.decode(errors='replace')}")
        try:
            response = PostOrderResponse.from_dict(json.loads(resp_body))
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            raise InvalidDataError(f"response parse failed: {e}") from e
        return LegPost(response=response, rtt_ms=rtt_ms)

    async def post_one(self, order: SignedOrder, timings: PostTimings, api_ts: int) -> LegPost:
        """Place one order, honoring :meth:`set_order_endpoint`:
        :attr:`OrderEndpoint.ORDER` sends a singular ``POST /order``,
        :attr:`OrderEndpoint.ORDERS` (default) a 1-element batched
        ``POST /orders``. The caller owns any concurrency."""
        if self._order_endpoint is OrderEndpoint.ORDER:
            return await self._post_order_single(order, timings, api_ts)
        posts = await self.post_orders([order], timings, api_ts)
        if not posts:
            raise InvalidDataError("empty response")
        return posts[0]

    async def _send_delete(self, url: str, sign_path: bytes, body: bytes) -> CancelOrdersResponse:
        """Sign (L2 HMAC over ``{ts}{sign_path}{body}``) and send a
        ``DELETE`` carrying ``body``. A 429 trips the rate-limit breaker."""
        timestamp = int(time.time())
        signature = self._sign_body(timestamp, sign_path, body)

        if self._call_counters is not None:
            # `sign_path` is exactly one of `DELETE/orders` (batched) or
            # `DELETE/order` (singular).
            self._call_counters._record_cancel(sign_path == b"DELETE/orders")

        status, resp_body = await self._round_trip(
            self._cancel_client, "DELETE", url, self._headers(signature, timestamp), body
        )
        if status == 429:
            self._signal_rate_limit(RateLimitEndpoint.CANCEL)
            raise RateLimitError()
        if status >= 400:
            raise InvalidDataError(
                f"CLOB cancel API {status}: {resp_body.decode(errors='replace')}"
            )
        try:
            return CancelOrdersResponse.from_dict(json.loads(resp_body))
        except (json.JSONDecodeError, TypeError) as e:
            raise InvalidDataError(f"cancel response parse failed: {e}") from e

    async def cancel_orders(self, order_ids: list[str]) -> CancelOrdersResponse:
        """Cancel a specific set of orders by id (batched ``DELETE /orders``)."""
        body = json.dumps(order_ids, separators=(",", ":")).encode()
        return await self._send_delete(self._orders_url, b"DELETE/orders", body)

    async def _cancel_single(self, order_id: str) -> CancelOrdersResponse:
        """Cancel a single order by id (singular ``DELETE /order``, body
        ``{"orderID":"<id>"}``)."""
        return await self._send_delete(
            self._order_url, b"DELETE/order", cancel_order_body(order_id)
        )

    async def cancel_for_reprice(self, order_ids: list[str]) -> CancelOrdersResponse:
        """Cancel resting orders for an *urgent* pull (reprice/fill),
        honoring :meth:`set_cancel_endpoint`: :attr:`CancelEndpoint.ORDERS`
        (default) sends one batched ``DELETE /orders``;
        :attr:`CancelEndpoint.ORDER` fans out via :meth:`cancel_singly`."""
        if self._cancel_endpoint is CancelEndpoint.ORDERS or not order_ids:
            return await self.cancel_orders(order_ids)
        return await self.cancel_singly(order_ids)

    async def cancel_singly(self, order_ids: list[str]) -> CancelOrdersResponse:
        """Cancel a set of orders as one concurrent singular ``DELETE
        /order`` apiece, merging the responses. A per-id transport error is
        folded into ``not_canceled`` (the order stayed resting — a "miss"),
        so the caller's accounting is unchanged; a 429 still trips the
        breaker. Only every id failing raises."""
        if not order_ids:
            return CancelOrdersResponse()

        results = await asyncio.gather(
            *(self._cancel_single(order_id) for order_id in order_ids),
            return_exceptions=True,
        )

        merged = CancelOrdersResponse()
        first_err: BaseException | None = None
        ok_count = 0
        for order_id, result in zip(order_ids, results, strict=True):
            if isinstance(result, BaseException):
                merged.not_canceled[order_id] = str(result)
                if first_err is None:
                    first_err = result
            else:
                ok_count += 1
                merged.canceled.extend(result.canceled)
                merged.not_canceled.update(result.not_canceled)

        # Non-empty input with zero successes means every request errored:
        # surface the first as a failure rather than a silent all-miss.
        if ok_count == 0:
            raise first_err if first_err else InvalidDataError("all singular cancels failed")
        return merged


def cancel_order_body(order_id: str) -> bytes:
    """The singular ``DELETE /order`` request body: ``{"orderID":"<id>"}``.

    Kept pure and separate so the exact bytes signed are the exact bytes
    sent (the HMAC covers the body).
    """
    return json.dumps({"orderID": order_id}, separators=(",", ":")).encode()


def cancel_reason_is_terminal(reason: str) -> bool:
    """Does a CLOB ``not_canceled`` reason mean the order no longer exists,
    so retrying the cancel is pointless?

    ``True`` for terminal states — already filled, matched, executed,
    completed, expired, already canceled, or simply not on the book anymore.
    **``False`` for anything unrecognized**: an unknown reason is treated as
    a transient miss so the order is *retried*, never silently abandoned — a
    still-live order must never be dropped from tracking. Match is lowercased
    + substring to tolerate the venue's (undocumented) wording drift.

    Caveat: the singular ``/order`` fan-out folds per-id *transport* errors
    into the same ``not_canceled`` map, so a transport error string
    containing one of these substrings would be misread as terminal. The
    default endpoint is the batched ``/orders``, whose ``not_canceled`` only
    carries genuine CLOB reasons.
    """
    terminal = (
        "filled",  # "order already filled"
        "not found",  # "order not found" (off-book: filled / canceled / expired)
        "matched",  # "order already matched"
        "executed",  # "order already executed"
        "complete",  # "order complete" / "completed"
        "already cancel",  # "order already canceled" / "...cancelled"
        "expired",  # "order expired"
    )
    lowered = reason.lower()
    return any(t in lowered for t in terminal)


T = TypeVar("T")

#: A cancel target: ``(group index, item index, order_id)`` — a convenient
#: shape for multi-leg engines feeding :func:`partition_cancels`.
CancelLeg = tuple[int, int, str]


def partition_cancels(
    batch: list[T],
    result: CancelOrdersResponse | None,
    get_id: Callable[[T], str],
) -> tuple[list[T], list[T]]:
    """Partition a flushed cancel batch into ``(done, retry)`` items.

    Generic over the batch element; ``get_id`` extracts the order id.
    ``done`` items are confirmed gone — drop them from tracking; ``retry``
    items are requeued for another attempt.

    ``result`` is the cancel response, or ``None`` for a whole-POST transport
    error — then every item is presumed still resting and retried. On success
    an item is ``done`` if the venue canceled it *or* reported a terminal
    ``not_canceled`` reason (see :func:`cancel_reason_is_terminal`); anything
    else — a non-terminal/unknown reason, or an id the venue omitted from
    both lists — is retried, so a still-live order is never dropped from
    tracking on a transient failure.
    """
    if result is None:
        return [], list(batch)  # transport failure: retry everything
    canceled = set(result.canceled)
    done: list[T] = []
    retry: list[T] = []
    for item in batch:
        order_id = get_id(item)
        reason = result.not_canceled.get(order_id)
        gone = order_id in canceled or (reason is not None and cancel_reason_is_terminal(reason))
        (done if gone else retry).append(item)
    return done, retry


async def cancel_in_batches(poster: Poster, ids: list[str]) -> CancelOrdersResponse:
    """Cancel a set of order ids in :data:`CANCEL_BATCH_SIZE` chunks, merging
    the responses."""
    merged = CancelOrdersResponse()
    for start in range(0, len(ids), CANCEL_BATCH_SIZE):
        response = await poster.cancel_orders(ids[start : start + CANCEL_BATCH_SIZE])
        merged.canceled.extend(response.canceled)
        merged.not_canceled.update(response.not_canceled)
    return merged


async def fetch_order_ids_by_side(clob: ClobClient, side: Side) -> list[str]:
    """Page the key's open orders and collect the ids resting on ``side``."""
    from . import OpenOrdersRequest

    orders = await clob.all_open_orders(OpenOrdersRequest())
    return [order.id for order in orders if order.side is side]


async def cancel_orders_by_side(
    clob: ClobClient, poster: Poster, side: Side
) -> CancelOrdersResponse:
    """Cancel every open order on the key resting on ``side``, leaving the
    other side untouched."""
    ids = await fetch_order_ids_by_side(clob, side)
    if not ids:
        return CancelOrdersResponse()
    logger.info("cancelling %d orders on side %s", len(ids), side.wire)
    return await cancel_in_batches(poster, ids)


async def cancel_buy_orders(clob: ClobClient, poster: Poster) -> CancelOrdersResponse:
    """Cancel every resting BUY order on the key."""
    return await cancel_orders_by_side(clob, poster, Side.BUY)


async def cancel_sell_orders(clob: ClobClient, poster: Poster) -> CancelOrdersResponse:
    """Cancel every resting SELL order on the key."""
    return await cancel_orders_by_side(clob, poster, Side.SELL)


async def fetch_sell_order_ids(clob: ClobClient) -> list[str]:
    """Page the open orders and collect every resting SELL id — the fetch
    half of :func:`cancel_sell_orders`, for callers that loop
    "fetch → cancel → re-fetch" until empty."""
    return await fetch_order_ids_by_side(clob, Side.SELL)


async def post_signed(
    signed_orders: list[SignedOrder],
    poster: Poster,
    timings: PostTimings,
    max_orders_per_post: int,
    api_ts: int,
) -> list[LegPost]:
    """POST pre-signed orders in ``max_orders_per_post``-sized chunks (always
    the batched ``POST /orders``), chunks in flight concurrently."""
    started = time.perf_counter()

    chunk_size = max(max_orders_per_post, 1)
    chunks = [signed_orders[i : i + chunk_size] for i in range(0, len(signed_orders), chunk_size)]

    async def _post(chunk: list[SignedOrder]) -> tuple[list[LegPost], PostTimings]:
        chunk_timings = PostTimings()
        posts = await poster.post_orders(chunk, chunk_timings, api_ts)
        return posts, chunk_timings

    results = await asyncio.gather(*(_post(chunk) for chunk in chunks))

    timings.post_ms = (time.perf_counter() - started) * 1000.0
    timings.chunk_count = len(chunks)

    leg_posts: list[LegPost] = []
    for posts, chunk_timings in results:
        timings.serialize_ms = max(timings.serialize_ms, chunk_timings.serialize_ms)
        timings.network_ms = max(timings.network_ms, chunk_timings.network_ms)
        leg_posts.extend(posts)
    return leg_posts


@dataclass
class RestingPlacement:
    """One resting order to place.

    Carries the token's tick data (which holds the token id), the price to
    rest at, and the size in shares.
    """

    tick: TickEntry
    price: Decimal
    size: Decimal
    #: Displayed size resting AHEAD at ``price`` the instant of placement —
    #: the queue position. ``0`` means the order joined an empty level (front
    #: of queue). Carried through for callers that track fill likelihood;
    #: ignore it otherwise.
    queue_ahead: Decimal = Decimal(0)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


async def place_resting(
    placements: list[RestingPlacement],
    poster: Poster,
    signer: LocalSigner,
    order_signer: OrderSigner,
    max_orders_per_post: int,
) -> list[LegPost]:
    """Sign and POST a batch of resting BUY orders as ``GTC`` + post-only.

    Post-only makes them rest as maker liquidity (rejected rather than
    crossing the spread). Returns one :class:`LegPost` per placement, in
    order; the caller records each ``order_id`` and owns the cancel/replace
    lifecycle.
    """
    if not placements:
        return []

    ts = _now_ms()
    signed_orders = []
    for p in placements:
        signable = build_signable_order(
            p.tick.token_id_int,
            to_fixed_usdc(p.size * p.price),
            to_fixed_usdc(p.size),
            order_signer.identity,
            ts,
            OrderType.GTC,
            generate_salt(),
            True,
        )
        signed_orders.append(order_signer.sign_order(signable, signer))

    timings = PostTimings()
    return await post_signed(signed_orders, poster, timings, max_orders_per_post, int(time.time()))


async def _place_sell_orders(
    placements: list[RestingPlacement],
    poster: Poster,
    signer: LocalSigner,
    order_signer: OrderSigner,
    max_orders_per_post: int,
    order_type: OrderType,
    post_only: bool,
) -> list[LegPost]:
    """Shared body behind :func:`place_resting_sell` (GTC post-only maker)
    and :func:`place_marketable_sell` (FAK taker): the two differ only in
    ``order_type`` + ``post_only``, so both ride one tested signing path."""
    if not placements:
        return []

    ts = _now_ms()
    signed_orders = []
    for p in placements:
        # SELL: give `size` shares, receive `size × price` USDC — the swap
        # of the BUY amounts in `place_resting`.
        signable = build_signable_order_side(
            p.tick.token_id_int,
            to_fixed_usdc(p.size),
            to_fixed_usdc(p.size * p.price),
            order_signer.identity,
            ts,
            order_type,
            generate_salt(),
            post_only,
            Side.SELL,
        )
        signed_orders.append(order_signer.sign_order(signable, signer))

    timings = PostTimings()
    return await post_signed(signed_orders, poster, timings, max_orders_per_post, int(time.time()))


async def place_resting_sell(
    placements: list[RestingPlacement],
    poster: Poster,
    signer: LocalSigner,
    order_signer: OrderSigner,
    max_orders_per_post: int,
) -> list[LegPost]:
    """Sign and POST a batch of resting SELL orders as ``GTC`` + post-only.

    For a SELL the maker gives ``size`` shares and receives ``size × price``
    USDC, so the maker/taker amounts are the swap of :func:`place_resting`'s,
    and each placement carries the *sold* token's tick.
    """
    return await _place_sell_orders(
        placements, poster, signer, order_signer, max_orders_per_post, OrderType.GTC, True
    )


async def place_marketable_sell(
    placement: RestingPlacement,
    poster: Poster,
    signer: LocalSigner,
    order_signer: OrderSigner,
) -> LegPost:
    """Place a single **marketable** (taker) SELL — an
    :attr:`~eggplant_sdk.clob.types.OrderType.FAK` order with ``post_only``
    off, so it crosses an existing bid instead of resting.

    ``placement.price`` is the FAK **limit** — for a SELL the *minimum*
    acceptable price, so the order fills only against bids at or above it and
    kills the rest (its own safety floor: a book that moved down yields a
    no-fill rather than a worse-than-shown sale). FAK leaves no resting
    residue. The caller credits the fill from the response's
    ``making_amount``/``taking_amount`` — a taker fill is not echoed on the
    user-WS maker channel.
    """
    posts = await _place_sell_orders(
        [placement], poster, signer, order_signer, 1, OrderType.FAK, False
    )
    if not posts:
        raise InvalidDataError("empty marketable sell response")
    return posts[0]


def _build_warmup_orders(
    entries: list[TickEntry], order_signer: OrderSigner, signer: LocalSigner
) -> list[SignedOrder]:
    ts = _now_ms()
    orders = []
    for tick in entries:
        try:
            signable = build_signable_order(
                tick.token_id_int,
                to_fixed_usdc(FIXED_SIZE * tick.min_price),
                to_fixed_usdc(FIXED_SIZE),
                order_signer.identity,
                ts,
                OrderType.FOK,
                generate_salt(),
                False,
            )
            orders.append(order_signer.sign_order(signable, signer))
        except EggplantError:
            continue
    return orders


async def deep_warm_up(
    entries: list[TickEntry],
    order_signer: OrderSigner,
    signer: LocalSigner,
    poster: Poster,
    max_per_post: int,
) -> list[float]:
    """Warm the whole sign→POST path with minimum-size FOK orders priced at
    each token's tick floor.

    These are real requests that exercise TLS, HMAC, and the venue pipeline
    end to end. An FOK at the price floor cannot rest and essentially cannot
    fill; a fill would be logged as an unexpected bargain. Returns the
    per-post RTTs (empty on failure — warm-up is always non-fatal).
    """
    if not entries:
        return []
    warmup_orders = _build_warmup_orders(entries, order_signer, signer)
    if not warmup_orders:
        return []

    timings = PostTimings()
    try:
        leg_posts = await post_signed(
            warmup_orders, poster, timings, max_per_post, int(time.time())
        )
    except EggplantError as e:
        logger.debug("deep warmup failed (non-fatal): %s", e)
        return []

    for lp in leg_posts:
        if lp.response.status is OrderStatus.MATCHED:
            logger.warning(
                "warmup FOK FILLED — unexpected bargain (order_id=%s)", lp.response.order_id
            )
    logger.debug("deep warmup complete: %d orders, %.1fms", len(leg_posts), timings.network_ms)
    return [lp.rtt_ms for lp in leg_posts]
