"""CLOB order types and their venue wire serialization.

The EIP-712 ``Order`` struct layouts (V1/V2) mirror the deployed CTF Exchange
contracts, and the JSON produced by :meth:`SignedOrder.to_wire` is exactly
what the venue's HMAC-signed POST bodies carry — field order and type strings
are load-bearing for the EIP-712 typehash.

Parsers deliberately degrade leniently: unknown enum values come back as raw
strings and optional fields default, because the venue adds statuses and
order types without notice and a strict parse fails the whole response.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from ..auth import ApiKey
from ..errors import InvalidDataError

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Side(enum.IntEnum):
    """Order side. ``UNKNOWN`` (255) absorbs venue drift on parse; it is
    never valid to *send*."""

    BUY = 0
    SELL = 1
    UNKNOWN = 255

    @property
    def wire(self) -> str:
        """The venue's wire string (``"BUY"`` / ``"SELL"``)."""
        return self.name

    @classmethod
    def parse(cls, raw: Any) -> Side:
        """Lenient parse: unknown side strings degrade to ``UNKNOWN``."""
        if isinstance(raw, str):
            upper = raw.upper()
            if upper == "BUY":
                return cls.BUY
            if upper == "SELL":
                return cls.SELL
        return cls.UNKNOWN

    @classmethod
    def from_u8(cls, value: int) -> Side:
        """Strict numeric parse (0 = BUY, 1 = SELL); anything else errors."""
        if value == 0:
            return cls.BUY
        if value == 1:
            return cls.SELL
        raise InvalidDataError(f"unable to create Side from {value}")

    def opposite(self) -> Side:
        """Flip a known side; ``UNKNOWN`` passes through untouched."""
        if self is Side.BUY:
            return Side.SELL
        if self is Side.SELL:
            return Side.BUY
        return self


class OrderType(enum.Enum):
    """Venue order types."""

    #: Good 'til Cancelled: rests on the book until explicitly cancelled.
    GTC = "GTC"
    #: Fill or Kill: fills in full immediately or cancels entirely.
    FOK = "FOK"
    #: Good 'til Date: rests until the payload's ``expiration``.
    GTD = "GTD"
    #: Fill and Kill: fills what it can immediately, cancels the remainder.
    FAK = "FAK"


def parse_order_type(raw: Any) -> OrderType | str:
    """Lenient parse: an unknown order type comes back as the raw string
    (retained for debugging) instead of failing the response."""
    if isinstance(raw, str):
        try:
            return OrderType(raw.upper())
        except ValueError:
            return raw
    return str(raw)


def order_type_wire(order_type: OrderType | str) -> str:
    return order_type.value if isinstance(order_type, OrderType) else order_type


class SignatureType(enum.IntEnum):
    """How the venue validates the order signature, and which wallet is the
    order's ``maker``.

    ====  ================  ==============  ============================================
    type  maker             signer          wallet kind
    ====  ================  ==============  ============================================
    0     EOA               EOA             plain externally-owned account
    1     proxy wallet      EOA             Magic/email proxy (derive_proxy_wallet)
    2     Safe wallet       EOA             browser Gnosis Safe (derive_safe_wallet)
    3     deposit wallet    deposit wallet  ERC-1271 deposit wallet (wrapped signature)
    ====  ================  ==============  ============================================
    """

    #: Plain EOA ECDSA (signature type 0).
    EOA = 0
    #: Polymarket proxy wallet (Magic/email login), signed by the owning EOA.
    PROXY = 1
    #: 1-of-1 Gnosis Safe (browser wallet), signed by the owning EOA.
    GNOSIS_SAFE = 2
    #: EIP-1271 deposit wallet: the wrapped Solady ``TypedDataSign`` scheme.
    #: V2 orders only.
    POLY1271 = 3

    @classmethod
    def from_u8(cls, value: int) -> SignatureType:
        try:
            return cls(value)
        except ValueError:
            raise InvalidDataError(f"unable to create SignatureType from {value}") from None


class OrderStatus(enum.Enum):
    """Venue order status (the known set — see :func:`parse_order_status`)."""

    LIVE = "LIVE"
    MATCHED = "MATCHED"
    CANCELED = "CANCELED"
    DELAYED = "DELAYED"
    UNMATCHED = "UNMATCHED"


def parse_order_status(raw: Any) -> OrderStatus | str:
    """Lenient parse: unknown statuses come back as the raw string instead of
    failing the response."""
    if isinstance(raw, str):
        try:
            return OrderStatus(raw.upper())
        except ValueError:
            return raw
    return "" if raw is None else str(raw)


# ---------------------------------------------------------------------------
# Order structs (EIP-712 layouts)
# ---------------------------------------------------------------------------

_ZERO32 = b"\x00" * 32


@dataclass
class OrderV2:
    """EIP-712 order struct for the Polymarket CTF Exchange V2.

    ``expiration`` is NOT part of the signed struct; it travels on the outer
    JSON payload (see :class:`SignableOrder`). Field order mirrors the
    on-chain contract's typehash and must not change.
    """

    salt: int = 0
    maker: str = "0x0000000000000000000000000000000000000000"
    signer: str = "0x0000000000000000000000000000000000000000"
    token_id: int = 0
    maker_amount: int = 0
    taker_amount: int = 0
    side: int = 0
    signature_type: int = 0
    timestamp: int = 0
    metadata: bytes = _ZERO32
    builder: bytes = _ZERO32


@dataclass
class OrderV1:
    """EIP-712 order struct for the legacy Polymarket CTF Exchange (V1).

    ``expiration`` is part of the signed struct. Field order mirrors the
    on-chain contract's typehash and must not change.
    """

    salt: int = 0
    maker: str = "0x0000000000000000000000000000000000000000"
    signer: str = "0x0000000000000000000000000000000000000000"
    taker: str = "0x0000000000000000000000000000000000000000"
    token_id: int = 0
    maker_amount: int = 0
    taker_amount: int = 0
    expiration: int = 0
    nonce: int = 0
    fee_rate_bps: int = 0
    side: int = 0
    signature_type: int = 0


@dataclass
class SignableOrder:
    """An order ready to sign: the struct plus the venue-level flags that
    ride the outer JSON body.

    ``expiration`` applies to V2 orders (it rides outside the signed struct);
    V1 orders carry their own in-struct expiration. ``post_only`` is only
    emitted when set: the venue rejects ``postOnly`` on non-GTC/GTD order
    types, so taker orders must omit the field entirely.
    """

    order: OrderV2 | OrderV1
    order_type: OrderType | str = OrderType.FOK
    expiration: int = 0
    post_only: bool | None = None
    defer_exec: bool | None = None


@dataclass
class SignedOrder:
    """A signed order in the exact shape the venue accepts. Serialize
    :meth:`to_wire` and POST the bytes — that JSON layout is what the
    venue-side HMAC covers.

    ``signature`` is either the 65-byte ECDSA hex (types 0/1/2) or the longer
    Poly1271 wrapped envelope; both are plain ``0x…`` strings here.
    """

    order: OrderV2 | OrderV1
    signature: str
    order_type: OrderType | str
    #: The API key that owns the order (rides as ``owner``).
    owner: ApiKey
    expiration: int = 0
    post_only: bool | None = None
    defer_exec: bool | None = None

    def to_wire(self) -> dict[str, Any]:
        """The venue's expected request-body JSON for this order.

        Salt is a JSON *number*; ids/amounts/timestamps are decimal strings;
        ``side`` is the UPPERCASE string; ``signatureType`` is a number.
        """
        order = self.order
        # CLOB expects salt as a JSON number. A salt from `generate_salt`
        # always fits JS's safe-integer range; anything larger is a caller
        # bug surfaced instead of silently truncated.
        if not 0 <= order.salt < (1 << 64):
            raise InvalidDataError(f"salt does not fit into u64: {order.salt}")
        side = Side.from_u8(order.side)

        if isinstance(order, OrderV2):
            body: dict[str, Any] = {
                "salt": order.salt,
                "maker": order.maker,
                "signer": order.signer,
                "tokenId": str(order.token_id),
                "makerAmount": str(order.maker_amount),
                "takerAmount": str(order.taker_amount),
                "side": side.wire,
                "expiration": str(self.expiration),
                "signatureType": order.signature_type,
                "timestamp": str(order.timestamp),
                "metadata": "0x" + order.metadata.hex(),
                "builder": "0x" + order.builder.hex(),
                "signature": self.signature,
            }
        else:
            body = {
                "salt": order.salt,
                "maker": order.maker,
                "signer": order.signer,
                "taker": order.taker,
                "tokenId": str(order.token_id),
                "makerAmount": str(order.maker_amount),
                "takerAmount": str(order.taker_amount),
                "side": side.wire,
                "expiration": str(order.expiration),
                "nonce": str(order.nonce),
                "feeRateBps": str(order.fee_rate_bps),
                "signatureType": order.signature_type,
                "signature": self.signature,
            }

        wire: dict[str, Any] = {
            "order": body,
            "orderType": order_type_wire(self.order_type),
            "owner": str(self.owner),
        }
        if self.post_only is not None:
            wire["postOnly"] = self.post_only
        if self.defer_exec is not None:
            wire["deferExec"] = self.defer_exec
        return wire


# ---------------------------------------------------------------------------
# REST response types (lenient by design)
# ---------------------------------------------------------------------------


def lenient_decimal(value: Any, default: Decimal = Decimal(0)) -> Decimal:
    """Accept a decimal as string or number; empty/missing/``None`` mean the
    default. The venue sends ``""`` for the untouched side of a fill."""
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return default
    try:
        return Decimal(text)
    except InvalidOperation:
        return default


@dataclass
class PostOrderResponse:
    """Response to ``POST /order(s)`` — one per submitted order."""

    success: bool
    error_msg: str | None = None
    #: Filled amount on the maker side of this order (shares for SELL, USDC
    #: for BUY), zero when the order rested.
    making_amount: Decimal = Decimal(0)
    #: Filled amount on the taker side of this order.
    taking_amount: Decimal = Decimal(0)
    order_id: str = ""
    status: OrderStatus | str = ""
    transaction_hashes: list[str] = field(default_factory=list)
    trade_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PostOrderResponse:
        return cls(
            success=bool(data.get("success", False)),
            error_msg=data.get("errorMsg"),
            making_amount=lenient_decimal(data.get("makingAmount")),
            taking_amount=lenient_decimal(data.get("takingAmount")),
            order_id=data.get("orderID") or "",
            status=parse_order_status(data.get("status")),
            transaction_hashes=data.get("transactionsHashes")
            or data.get("transactionHashes")
            or [],
            trade_ids=data.get("tradeIds") or [],
        )

    def is_accepted(self) -> bool:
        """Venue accepted the order: HTTP-level success and no error message."""
        return self.success and not (self.error_msg or "")


@dataclass
class CancelOrdersResponse:
    """Response to ``DELETE /order(s)`` and ``DELETE /cancel-all``."""

    canceled: list[str] = field(default_factory=list)
    #: ``order id -> reason`` for every order the venue declined to cancel.
    not_canceled: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CancelOrdersResponse:
        return cls(
            canceled=data.get("canceled") or [],
            not_canceled=data.get("notCanceled") or data.get("not_canceled") or {},
        )


@dataclass
class Page:
    """One page of a cursor-paginated listing. ``next_cursor == "LTE="``
    marks the end (see :data:`eggplant_sdk.clob.TERMINAL_CURSOR`)."""

    data: list[Any] = field(default_factory=list)
    next_cursor: str = ""
    limit: int = 0
    count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any], parse_item) -> Page:
        return cls(
            data=[parse_item(item) for item in data.get("data") or []],
            next_cursor=data.get("next_cursor") or "",
            limit=int(data.get("limit") or 0),
            count=int(data.get("count") or 0),
        )


@dataclass
class OpenOrder:
    """One open order from ``GET /data/orders``.

    Deliberately lenient: every non-essential field defaults instead of
    failing the page — a strict parse of this listing is a startup-outage
    vector.
    """

    id: str
    status: OrderStatus | str = ""
    owner: str = ""
    #: The market condition id.
    market: str = ""
    #: Token id (decimal string).
    asset_id: str = ""
    side: Side = Side.UNKNOWN
    original_size: Decimal = Decimal(0)
    size_matched: Decimal = Decimal(0)
    price: Decimal = Decimal(0)
    outcome: str = ""
    #: Unix seconds.
    created_at: int | None = None
    order_type: OrderType | str | None = None
    associate_trades: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OpenOrder:
        created_at = data.get("created_at")
        return cls(
            id=data["id"],
            status=parse_order_status(data.get("status")),
            owner=data.get("owner") or "",
            market=data.get("market") or "",
            asset_id=data.get("asset_id") or "",
            side=Side.parse(data.get("side")),
            original_size=lenient_decimal(data.get("original_size")),
            size_matched=lenient_decimal(data.get("size_matched")),
            price=lenient_decimal(data.get("price")),
            outcome=data.get("outcome") or "",
            created_at=int(created_at) if created_at is not None else None,
            order_type=parse_order_type(data["order_type"]) if "order_type" in data else None,
            associate_trades=data.get("associate_trades") or [],
        )


@dataclass
class ClobTrade:
    """One trade from ``GET /data/trades``, reduced to the load-bearing
    fields."""

    id: str
    taker_order_id: str = ""
    market: str = ""
    asset_id: str = ""
    side: Side = Side.UNKNOWN
    size: Decimal = Decimal(0)
    price: Decimal = Decimal(0)
    status: str = ""
    outcome: str = ""
    trader_side: str = ""
    match_time: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClobTrade:
        return cls(
            id=data["id"],
            taker_order_id=data.get("taker_order_id") or "",
            market=data.get("market") or "",
            asset_id=data.get("asset_id") or "",
            side=Side.parse(data.get("side")),
            size=lenient_decimal(data.get("size")),
            price=lenient_decimal(data.get("price")),
            status=data.get("status") or "",
            outcome=data.get("outcome") or "",
            trader_side=data.get("trader_side") or "",
            match_time=data.get("match_time") or "",
        )


@dataclass
class ClobToken:
    """One outcome token of a :class:`ClobMarket`."""

    #: Token id (decimal string).
    token_id: str = ""
    outcome: str = ""
    price: Decimal = Decimal(0)
    winner: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClobToken:
        return cls(
            token_id=data.get("token_id") or "",
            outcome=data.get("outcome") or "",
            price=lenient_decimal(data.get("price")),
            winner=bool(data.get("winner", False)),
        )


@dataclass
class ClobMarket:
    """One market from ``GET /markets/{condition_id}``, reduced and lenient."""

    condition_id: str = ""
    question_id: str = ""
    question: str = ""
    market_slug: str = ""
    active: bool = False
    closed: bool = False
    accepting_orders: bool = False
    minimum_order_size: Decimal = Decimal(0)
    #: The market's price grid — a plain :class:`~decimal.Decimal`, never a
    #: closed enum.
    minimum_tick_size: Decimal = Decimal(0)
    neg_risk: bool = False
    end_date_iso: str | None = None
    tokens: list[ClobToken] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClobMarket:
        return cls(
            condition_id=data.get("condition_id") or "",
            question_id=data.get("question_id") or "",
            question=data.get("question") or "",
            market_slug=data.get("market_slug") or "",
            active=bool(data.get("active", False)),
            closed=bool(data.get("closed", False)),
            accepting_orders=bool(data.get("accepting_orders", False)),
            minimum_order_size=lenient_decimal(data.get("minimum_order_size")),
            minimum_tick_size=lenient_decimal(data.get("minimum_tick_size")),
            neg_risk=bool(data.get("neg_risk", False)),
            end_date_iso=data.get("end_date_iso"),
            tokens=[ClobToken.from_dict(t) for t in data.get("tokens") or []],
        )
