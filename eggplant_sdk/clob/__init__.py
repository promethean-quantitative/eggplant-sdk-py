"""Polymarket CLOB: order types, EIP-712 signing, venue size rules, and the
HTTP client surfaces.

:class:`ClobClient` is the read/admin surface: authentication, market/tick
metadata, order books, open-order listing, trades, cancel-all. Order
placement and cancellation live on the dedicated write-path
:class:`~eggplant_sdk.clob.poster.Poster`, built from a client via
:meth:`ClobClient.poster`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from .. import auth
from ..auth import Credentials
from ..chain import CLOB_HOST, POLYGON
from ..errors import ApiError, InvalidDataError, RateLimitError
from ..signer import LocalSigner
from . import books, poster, signing, tick, types
from .books import BookSummary
from .poster import Poster
from .signing import ExchangeDomain, OrderIdentity, OrderSigner
from .types import (
    CancelOrdersResponse,
    ClobMarket,
    ClobTrade,
    OpenOrder,
    Page,
    SignatureType,
)

__all__ = [
    "TERMINAL_CURSOR",
    "ClobClient",
    "ClobClientBuilder",
    "OpenOrdersRequest",
    "books",
    "poster",
    "signing",
    "tick",
    "types",
]

#: The cursor value marking the final page of a cursor-paginated listing.
TERMINAL_CURSOR = "LTE="


@dataclass
class OpenOrdersRequest:
    """Filters for the open-orders and trades listings. Empty filters list
    everything owned by the API key."""

    #: A specific order id.
    id: str | None = None
    #: A market condition id.
    market: str | None = None
    #: A token id (decimal string).
    asset_id: str | None = None

    def query(self, cursor: str | None = None) -> str:
        parts = []
        for key, value in (
            ("id", self.id),
            ("market", self.market),
            ("asset_id", self.asset_id),
            ("next_cursor", cursor),
        ):
            if value is not None:
                parts.append(f"{key}={value}")
        return "?" + "&".join(parts) if parts else ""


def _normalize_host(host: str) -> str:
    return host if host.endswith("/") else host + "/"


def _default_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))


async def _parse_response(response: httpx.Response) -> Any:
    """Shared response handling: 429 → :class:`RateLimitError`, other
    non-2xx → :class:`ApiError`, else parse JSON (floats as
    :class:`~decimal.Decimal`)."""
    if response.status_code == 429:
        raise RateLimitError(response.headers.get("retry-after"))
    text = response.text
    if response.status_code >= 400:
        raise ApiError(response.status_code, text[:300])
    try:
        return json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as e:
        raise InvalidDataError(f"response is not JSON: {e}") from e


async def _fetch_server_time(http: httpx.AsyncClient, host: str) -> int:
    response = await http.get(f"{host}time")
    text = response.text
    if response.status_code >= 400:
        raise ApiError(response.status_code, text[:300])
    try:
        return int(text.strip().strip('"'))
    except ValueError as e:
        raise InvalidDataError(f"unparseable server time: {text}") from e


class ClobClientBuilder:
    """Builder for :class:`ClobClient`: host, chain, and the
    signature-type/funder pair, finished by either a network handshake
    (:meth:`authenticate`) or saved credentials (:meth:`with_credentials`)."""

    def __init__(self) -> None:
        self._host = CLOB_HOST
        self._chain_id = POLYGON
        self._signature_type = SignatureType.EOA
        self._funder: str | None = None
        self._nonce: int | None = None
        self._use_server_time = False

    def host(self, host: str) -> ClobClientBuilder:
        """CLOB REST host. Default :data:`~eggplant_sdk.chain.CLOB_HOST`."""
        self._host = host
        return self

    def chain_id(self, chain_id: int) -> ClobClientBuilder:
        """Chain id for L1 auth and order signing. Default
        :data:`~eggplant_sdk.chain.POLYGON`. Taken from the builder, never
        from the signer — a missing signer chain id must not silently change
        what gets signed."""
        self._chain_id = chain_id
        return self

    def signature_type(self, signature_type: SignatureType) -> ClobClientBuilder:
        """The signature type orders will carry. Default
        :attr:`SignatureType.EOA`."""
        self._signature_type = signature_type
        return self

    def funder(self, funder: str) -> ClobClientBuilder:
        """The funding wallet, required for signature types 1/2/3 (proxy,
        Safe, deposit wallet). Derive proxy/Safe addresses with
        :func:`eggplant_sdk.chain.derive_proxy_wallet` /
        :func:`eggplant_sdk.chain.derive_safe_wallet`."""
        self._funder = funder
        return self

    def nonce(self, nonce: int) -> ClobClientBuilder:
        """L1 auth nonce. Each nonce maps to one API key per wallet;
        default ``0``."""
        self._nonce = nonce
        return self

    def use_server_time(self, use_server_time: bool) -> ClobClientBuilder:
        """Sign L1 timestamps with the venue's clock (``GET /time``) instead
        of the local one. Default off."""
        self._use_server_time = use_server_time
        return self

    async def create_api_key(self, signer: LocalSigner) -> Credentials:
        """Create a fresh API key via the L1 handshake
        (``POST /auth/api-key``)."""
        return await self._api_key_request(signer, "POST", "auth/api-key")

    async def derive_api_key(self, signer: LocalSigner) -> Credentials:
        """Re-derive the wallet's existing API key
        (``GET /auth/derive-api-key``)."""
        return await self._api_key_request(signer, "GET", "auth/derive-api-key")

    async def authenticate(self, signer: LocalSigner) -> ClobClient:
        """The full handshake: try create, and fall back to derive only when
        the venue answered with an HTTP error (e.g. the key already exists).
        Network and rate-limit failures propagate instead of falling
        through."""
        try:
            credentials = await self.create_api_key(signer)
        except ApiError:
            credentials = await self.derive_api_key(signer)
        return self.with_credentials(signer.address, credentials)

    def with_credentials(self, signer_address: str, credentials: Credentials) -> ClobClient:
        """Hot start from saved :class:`Credentials` — no network round trip.
        ``signer_address`` is the EOA the credentials were derived for."""
        identity = self._resolve_identity(signer_address)
        return ClobClient(
            host=_normalize_host(self._host),
            chain_id=self._chain_id,
            address=signer_address,
            identity=identity,
            credentials=credentials,
        )

    def _resolve_identity(self, signer_address: str) -> OrderIdentity:
        """Encode the venue's signature-type/funder table (see
        :class:`OrderIdentity`) with build-time validation."""
        if self._signature_type is SignatureType.EOA:
            if self._funder is not None and self._funder.lower() != signer_address.lower():
                raise InvalidDataError(
                    "signature type 0 (EOA) takes no separate funder — the EOA itself is the maker"
                )
            return OrderIdentity.eoa(signer_address)
        if self._funder is None:
            hints = {
                SignatureType.PROXY: (
                    "signature type 1 (proxy) requires .funder(proxy_wallet); "
                    "derive it with chain.derive_proxy_wallet"
                ),
                SignatureType.GNOSIS_SAFE: (
                    "signature type 2 (Safe) requires .funder(safe_wallet); "
                    "derive it with chain.derive_safe_wallet"
                ),
                SignatureType.POLY1271: (
                    "signature type 3 (POLY1271) requires .funder(deposit_wallet)"
                ),
            }
            raise InvalidDataError(hints[self._signature_type])
        if self._signature_type is SignatureType.PROXY:
            return OrderIdentity.proxy(signer_address, self._funder)
        if self._signature_type is SignatureType.GNOSIS_SAFE:
            return OrderIdentity.gnosis_safe(signer_address, self._funder)
        return OrderIdentity.poly1271(self._funder)

    async def _api_key_request(
        self, signer: LocalSigner, method: str, endpoint: str
    ) -> Credentials:
        host = _normalize_host(self._host)
        async with _default_http() as http:
            if self._use_server_time:
                timestamp = await _fetch_server_time(http, host)
            else:
                timestamp = int(time.time())
            headers = auth.l1_headers(signer, self._chain_id, timestamp, self._nonce)
            response = await http.request(method, f"{host}{endpoint}", headers=headers)
            data = await _parse_response(response)
        return Credentials.from_dict(data)


class ClobClient:
    """Authenticated CLOB client: credentials, identity, and the read/admin
    REST surface. Order placement goes through :meth:`poster`."""

    def __init__(
        self,
        host: str,
        chain_id: int,
        address: str,
        identity: OrderIdentity,
        credentials: Credentials,
    ):
        self._http = _default_http()
        #: Host with a trailing slash (``https://clob.polymarket.com/``).
        self._host = _normalize_host(host)
        self._chain_id = chain_id
        #: The signer EOA the credentials belong to (L2 ``POLY_ADDRESS``).
        self._address = address
        self._identity = identity
        self._credentials = credentials

    @staticmethod
    def builder() -> ClobClientBuilder:
        return ClobClientBuilder()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> ClobClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @property
    def host(self) -> str:
        """The REST host, trailing-slash normalized."""
        return self._host

    @property
    def chain_id(self) -> int:
        return self._chain_id

    @property
    def address(self) -> str:
        """The signer EOA address."""
        return self._address

    @property
    def identity(self) -> OrderIdentity:
        return self._identity

    @property
    def credentials(self) -> Credentials:
        return self._credentials

    def order_signer(self, domain: ExchangeDomain) -> OrderSigner:
        """An :class:`OrderSigner` for this client's identity against
        ``domain``."""
        return OrderSigner(self._chain_id, domain, self._identity, self._credentials.key)

    def poster(self) -> Poster:
        """Build the dedicated write-path poster (isolated connection
        pools)."""
        return Poster(self._host, self._address, self._credentials)

    async def server_time(self) -> int:
        """Venue clock, Unix seconds (``GET /time``, public)."""
        return await _fetch_server_time(self._http, self._host)

    async def tick_size(self, token_id: str) -> Decimal:
        """A token's current minimum tick (``GET /tick-size``, public).
        Plain :class:`~decimal.Decimal` — any venue grid parses."""
        data = await self._get_public(f"tick-size?token_id={token_id}")
        return types.lenient_decimal(data["minimum_tick_size"])

    async def neg_risk(self, token_id: str) -> bool:
        """Whether a token trades on the negRisk exchange (``GET /neg-risk``,
        public) — this decides the signing domain
        (:meth:`ExchangeDomain.ctf_v2`)."""
        data = await self._get_public(f"neg-risk?token_id={token_id}")
        return bool(data["neg_risk"])

    async def market(self, condition_id: str) -> ClobMarket:
        """One market by condition id (``GET /markets/{condition_id}``,
        public)."""
        return ClobMarket.from_dict(await self._get_public(f"markets/{condition_id}"))

    async def open_orders(self, request: OpenOrdersRequest, cursor: str | None = None) -> Page:
        """One page of open orders (``GET /data/orders``,
        L2-authenticated)."""
        data = await self._get_l2("data/orders", request.query(cursor))
        return Page.from_dict(data, OpenOrder.from_dict)

    async def all_open_orders(self, request: OpenOrdersRequest) -> list[OpenOrder]:
        """Every open order matching ``request``, paging until the terminal
        cursor."""
        out: list[OpenOrder] = []
        cursor: str | None = None
        while True:
            page = await self.open_orders(request, cursor)
            out.extend(page.data)
            next_cursor = page.next_cursor
            if not next_cursor or next_cursor in (TERMINAL_CURSOR, cursor):
                break
            cursor = next_cursor
        return out

    async def trades(self, request: OpenOrdersRequest, cursor: str | None = None) -> Page:
        """One page of the key's trades (``GET /data/trades``,
        L2-authenticated)."""
        data = await self._get_l2("data/trades", request.query(cursor))
        return Page.from_dict(data, ClobTrade.from_dict)

    async def cancel_all(self) -> CancelOrdersResponse:
        """Cancel every open order owned by the API key
        (``DELETE /cancel-all``, L2-authenticated)."""
        timestamp = int(time.time())
        headers = auth.l2_headers(
            self._address, self._credentials, timestamp, "DELETE", "/cancel-all", ""
        )
        response = await self._http.delete(f"{self._host}cancel-all", headers=headers)
        return CancelOrdersResponse.from_dict(await _parse_response(response))

    async def order_books(self, token_ids: list[str]) -> list[BookSummary]:
        """Book summaries for up to :data:`~eggplant_sdk.clob.books.MAX_BATCH_SIZE`
        tokens (``POST /books``, public, leniently parsed)."""
        return await books.fetch_books_at(self._http, f"{self._host}books", token_ids)

    async def order_book_map(self, token_ids: list[str]) -> dict[int, BookSummary]:
        """Book summaries for arbitrarily many tokens, chunked and fetched
        concurrently, keyed by asset id."""
        return await books.fetch_book_map_at(self._http, f"{self._host}books", token_ids)

    async def _get_public(self, endpoint_and_query: str) -> Any:
        response = await self._http.get(f"{self._host}{endpoint_and_query}")
        return await _parse_response(response)

    async def _get_l2(self, endpoint: str, query: str) -> Any:
        timestamp = int(time.time())
        # The L2 message covers the path only — query strings are excluded.
        headers = auth.l2_headers(
            self._address, self._credentials, timestamp, "GET", f"/{endpoint}", ""
        )
        response = await self._http.get(f"{self._host}{endpoint}{query}", headers=headers)
        return await _parse_response(response)
