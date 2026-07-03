"""User-channel messages (own trades and order lifecycle) and a
single-connection stream.

Operational notes:

- The channel delivers **maker** fills; your own taker fills are *not*
  echoed here — credit them from the POST response's
  ``making_amount``/``taking_amount``.
- Every fill on the API key is delivered, so processes sharing a key must
  filter by side (:func:`eggplant_sdk.ws.util.our_maker_side`) and dedup by
  trade id across redundant connections
  (:class:`eggplant_sdk.ws.util.SeenIds`).
- A trade's ``maker_orders`` lists *every* maker the sweep hit — filter to
  entries whose ``owner`` is your API key before crediting.
"""

from __future__ import annotations

import enum
import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..auth import ApiKey, Credentials
from ..chain import WS_USER_URL
from ..clob.types import OrderStatus, Side, lenient_decimal, parse_order_status
from ..errors import InvalidDataError
from .frames import user_subscribe_frame
from .market import _LiveSocket


class TradeStatus(enum.Enum):
    """Trade settlement status (the known set — see
    :func:`parse_trade_status`)."""

    MATCHED = "MATCHED"
    MINED = "MINED"
    CONFIRMED = "CONFIRMED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"


def parse_trade_status(raw: Any) -> TradeStatus | str:
    """Lenient parse: an unknown status comes back as the raw string."""
    if isinstance(raw, str):
        try:
            return TradeStatus(raw.upper())
        except ValueError:
            return raw
    return "" if raw is None else str(raw)


def trade_status_is_final(status: TradeStatus | str) -> bool:
    """Whether a status is final (``CONFIRMED`` / ``FAILED``).

    Gate final-status handling *before* trade-id dedup: a ``RETRYING`` first
    sighting must not swallow the later confirmation.
    """
    return status in (TradeStatus.CONFIRMED, TradeStatus.FAILED)


def _lenient_i64(raw: Any) -> int | None:
    """Venue timestamps arrive as decimal strings (sometimes numbers); an
    unparseable one degrades to ``None``."""
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _opt_api_key(raw: Any) -> ApiKey | None:
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


@dataclass
class MakerOrder:
    """One maker order matched within a trade."""

    #: Token id (decimal string).
    asset_id: str
    matched_amount: Decimal
    order_id: str
    #: Outcome (``"Yes"`` / ``"No"``).
    outcome: str
    #: API key that owns the maker order — filter to yours.
    owner: ApiKey
    price: Decimal

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MakerOrder:
        return cls(
            asset_id=data.get("asset_id") or "",
            matched_amount=lenient_decimal(data["matched_amount"]),
            order_id=data["order_id"],
            outcome=data.get("outcome") or "",
            owner=uuid.UUID(str(data["owner"])),
            price=lenient_decimal(data["price"]),
        )


@dataclass
class TradeMessage:
    """A trade the key participated in (authenticated channel)."""

    #: Trade id — the dedup key across redundant connections.
    id: str
    #: The **taker's** side. Derive your own maker side from this plus the
    #: outcomes via :func:`eggplant_sdk.ws.util.our_maker_side`.
    side: Side
    size: Decimal
    price: Decimal
    status: TradeStatus | str
    #: Market condition id (hex).
    market: str = ""
    #: Token id of the *taker* side (decimal string).
    asset_id: str = ""
    #: The taker's outcome (``"Yes"`` / ``"No"``).
    outcome: str | None = None
    #: API key of the event's owner (the key this delivery is for).
    owner: ApiKey | None = None
    trade_owner: ApiKey | None = None
    taker_order_id: str | None = None
    #: Every maker order the sweep matched — includes other participants'.
    maker_orders: list[MakerOrder] = field(default_factory=list)
    last_update: int | None = None
    matchtime: int | None = None
    timestamp: int | None = None
    fee_rate_bps: Decimal | None = None
    transaction_hash: str = ""
    #: ``"MAKER"`` / ``"TAKER"``.
    trader_side: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradeMessage:
        fee_rate = data.get("fee_rate_bps")
        return cls(
            id=data["id"],
            side=Side.parse(data.get("side")),
            size=lenient_decimal(data["size"]),
            price=lenient_decimal(data["price"]),
            status=parse_trade_status(data.get("status")),
            market=data.get("market") or "",
            asset_id=data.get("asset_id") or "",
            outcome=data.get("outcome"),
            owner=_opt_api_key(data.get("owner")),
            trade_owner=_opt_api_key(data.get("trade_owner")),
            taker_order_id=data.get("taker_order_id"),
            maker_orders=[MakerOrder.from_dict(m) for m in data.get("maker_orders") or []],
            last_update=_lenient_i64(data.get("last_update")),
            matchtime=_lenient_i64(data.get("matchtime", data.get("match_time"))),
            timestamp=_lenient_i64(data.get("timestamp")),
            fee_rate_bps=lenient_decimal(fee_rate) if fee_rate is not None else None,
            transaction_hash=data.get("transaction_hash") or "",
            trader_side=data.get("trader_side") or "",
        )


class OrderEventType(enum.Enum):
    """Order lifecycle kind (the known set — see
    :func:`parse_order_event_type`)."""

    PLACEMENT = "PLACEMENT"
    UPDATE = "UPDATE"
    CANCELLATION = "CANCELLATION"


def parse_order_event_type(raw: Any) -> OrderEventType | str:
    if isinstance(raw, str):
        try:
            return OrderEventType(raw.upper())
        except ValueError:
            return raw
    return "" if raw is None else str(raw)


@dataclass
class OrderMessage:
    """An order lifecycle event on the key (authenticated channel)."""

    #: Order id.
    id: str
    side: Side
    price: Decimal
    #: Market condition id (hex).
    market: str = ""
    #: Token id (decimal string).
    asset_id: str = ""
    msg_type: OrderEventType | str | None = None
    outcome: str | None = None
    owner: ApiKey | None = None
    order_owner: ApiKey | None = None
    original_size: Decimal | None = None
    size_matched: Decimal | None = None
    timestamp: int | None = None
    associate_trades: list[str] | None = None
    status: OrderStatus | str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrderMessage:
        original_size = data.get("original_size")
        size_matched = data.get("size_matched")
        return cls(
            id=data["id"],
            side=Side.parse(data.get("side")),
            price=lenient_decimal(data["price"]),
            market=data.get("market") or "",
            asset_id=data.get("asset_id") or "",
            msg_type=parse_order_event_type(data["type"]) if "type" in data else None,
            outcome=data.get("outcome"),
            owner=_opt_api_key(data.get("owner")),
            order_owner=_opt_api_key(data.get("order_owner")),
            original_size=lenient_decimal(original_size) if original_size is not None else None,
            size_matched=lenient_decimal(size_matched) if size_matched is not None else None,
            timestamp=_lenient_i64(data.get("timestamp")),
            associate_trades=data.get("associate_trades"),
            status=parse_order_status(data["status"]) if "status" in data else None,
        )


@dataclass
class UnknownMessage:
    """A venue event kind this SDK doesn't know yet."""

    event_type: str = ""


UserMessage = TradeMessage | OrderMessage | UnknownMessage


def parse_user_message(text: str) -> UserMessage:
    """Parse one user-channel text frame. Unknown event kinds surface as
    :class:`UnknownMessage` rather than erroring."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise InvalidDataError(f"user frame parse: {e}") from e
    if not isinstance(data, dict):
        raise InvalidDataError("user frame is not a JSON object")

    event_type = data.get("event_type", "")
    try:
        if event_type == "trade":
            return TradeMessage.from_dict(data)
        if event_type == "order":
            return OrderMessage.from_dict(data)
    except (KeyError, ValueError, TypeError) as e:
        raise InvalidDataError(f"user frame parse: {e}") from e
    return UnknownMessage(event_type=str(event_type))


@dataclass
class UserStreamConfig:
    """Configuration for one user-channel connection."""

    credentials: Credentials
    #: Defaults to :data:`~eggplant_sdk.chain.WS_USER_URL`.
    url: str = WS_USER_URL
    #: Condition ids to filter to; empty subscribes to every fill on the
    #: key. No historical replay — only live events from connect onward.
    markets: list[str] = field(default_factory=list)


class UserStream:
    """One authenticated user-channel connection with the liveness protocol
    handled.

    For redundancy, run several identically-subscribed streams and dedup by
    trade id (:class:`eggplant_sdk.ws.util.SeenIds`) — first delivery wins.
    """

    def __init__(self, inner: _LiveSocket):
        self._inner = inner

    @classmethod
    async def connect(cls, config: UserStreamConfig) -> UserStream:
        """Connect and authenticate-subscribe."""
        frame = user_subscribe_frame(config.credentials, config.markets)
        return cls(await _LiveSocket.connect(config.url, frame))

    async def next_text(self) -> str | None:
        """The next raw data frame; ``None`` means the server closed cleanly,
        :class:`WsError` means transport failure or PONG-deadline breach
        (reconnect)."""
        return await self._inner.next_text()

    async def next_message(self) -> UserMessage | None:
        """The next parsed message. Unknown event kinds surface as
        :class:`UnknownMessage` rather than erroring."""
        text = await self.next_text()
        if text is None:
            return None
        return parse_user_message(text)

    async def close(self) -> None:
        """Politely close (e.g. for a scheduled recycle)."""
        await self._inner.close()
