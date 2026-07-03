"""Market-channel messages and a single-connection stream.

:class:`MarketStream` is deliberately thin: connect, subscribe, and yield
text frames with the PING/PONG liveness protocol handled internally.
Multi-connection fan-out, sharding, and reconnect policy stay with the
caller (see :mod:`eggplant_sdk.ws.util` for the phasing helpers).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

from ..book import BookSide
from ..chain import WS_MARKET_URL
from ..errors import InvalidDataError, WsError
from . import frames


def _dec(raw: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError) as e:
        raise InvalidDataError(f"unparseable {field_name}: {raw}") from e


def _lenient_dec(raw: Any) -> Decimal | None:
    """Empty-string best bid/ask means "no level" — tolerate rather than
    error."""
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError):
        return None


def _opt_ms(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _side_to_book(side: str) -> BookSide | None:
    upper = side.upper()
    if upper == "BUY":
        return BookSide.BID
    if upper == "SELL":
        return BookSide.ASK
    return None


@dataclass
class BookLevel:
    """One price level of a book snapshot."""

    price: Decimal
    size: Decimal


@dataclass
class PriceChangeEntry:
    """One entry of a ``price_change`` batch."""

    asset_id: str
    price: Decimal
    #: New absolute size at ``price`` (``0`` removes the level).
    size: Decimal
    #: ``"BUY"`` (bid ladder) or ``"SELL"`` (ask ladder).
    side: str
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None

    def book_side(self) -> BookSide | None:
        """Which book ladder this delta touches, or ``None`` for an unknown
        side."""
        return _side_to_book(self.side)


@dataclass
class MarketBook:
    """Full book snapshot (sent on subscribe and after trades)."""

    asset_id: str
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    #: Venue milliseconds.
    timestamp: int | None = None


@dataclass
class MarketPriceChange:
    """Incremental level changes."""

    price_changes: list[PriceChangeEntry] = field(default_factory=list)
    timestamp: int | None = None


@dataclass
class MarketTickSizeChange:
    asset_id: str = ""
    new_tick_size: Decimal = Decimal(0)


@dataclass
class MarketLastTradePrice:
    """A trade print. Every field beyond ``asset_id``/``price`` is
    venue-optional so a shape drift degrades to a less-informative record
    instead of a parse drop."""

    asset_id: str
    price: Decimal
    size: Decimal | None = None
    side: str | None = None
    timestamp: int | None = None


@dataclass
class MarketUnknown:
    """An event kind this SDK doesn't know yet — degrades instead of
    failing the frame (the venue adds event kinds without notice)."""

    event_type: str = ""


MarketEvent = (
    MarketBook | MarketPriceChange | MarketTickSizeChange | MarketLastTradePrice | MarketUnknown
)


def parse_market_event(text: str) -> MarketEvent:
    """Parse one market-channel text frame. Unrecognized ``event_type``\\ s
    come back as :class:`MarketUnknown` instead of failing the frame."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise InvalidDataError(f"market frame parse: {e}") from e
    if not isinstance(data, dict):
        raise InvalidDataError("market frame is not a JSON object")

    event_type = data.get("event_type", "")
    if event_type == "book":

        def levels(raw: Any, name: str) -> list[BookLevel]:
            return [
                BookLevel(
                    price=_dec(lv["price"], f"{name} price"),
                    size=_dec(lv["size"], f"{name} size"),
                )
                for lv in raw or []
            ]

        return MarketBook(
            asset_id=data.get("asset_id", ""),
            bids=levels(data.get("bids"), "book"),
            asks=levels(data.get("asks"), "book"),
            timestamp=_opt_ms(data.get("timestamp")),
        )
    if event_type == "price_change":
        return MarketPriceChange(
            price_changes=[
                PriceChangeEntry(
                    asset_id=entry.get("asset_id", ""),
                    price=_dec(entry["price"], "price_change price"),
                    size=_dec(entry["size"], "price_change size"),
                    side=entry.get("side", ""),
                    best_bid=_lenient_dec(entry.get("best_bid")),
                    best_ask=_lenient_dec(entry.get("best_ask")),
                )
                for entry in data.get("price_changes") or []
            ],
            timestamp=_opt_ms(data.get("timestamp")),
        )
    if event_type == "tick_size_change":
        return MarketTickSizeChange(
            asset_id=data.get("asset_id", ""),
            new_tick_size=_dec(data["new_tick_size"], "new_tick_size"),
        )
    if event_type == "last_trade_price":
        size = data.get("size")
        return MarketLastTradePrice(
            asset_id=data.get("asset_id", ""),
            price=_dec(data["price"], "last_trade_price price"),
            size=_dec(size, "last_trade_price size") if size is not None else None,
            side=data.get("side"),
            timestamp=_opt_ms(data.get("timestamp")),
        )
    return MarketUnknown(event_type=str(event_type))


@dataclass
class MarketStreamConfig:
    """Configuration for one market-channel connection."""

    #: Token ids to subscribe (the venue accepts ~500 per connection before
    #: delivery degrades; shard beyond that).
    token_ids: list[str]
    #: Defaults to :data:`~eggplant_sdk.chain.WS_MARKET_URL`.
    url: str = WS_MARKET_URL
    #: Request the extended event kinds (``best_bid_ask``, ``new_market``,
    #: ``market_resolved``).
    custom_features: bool = True


class _LiveSocket:
    """Shared connect + subscribe + liveness loop for both channels."""

    def __init__(self, socket: websockets.ClientConnection):
        self._socket = socket
        self._last_pong = time.monotonic()
        self._next_ping = time.monotonic() + frames.PING_INTERVAL

    @classmethod
    async def connect(cls, url: str, subscribe_frame: str) -> _LiveSocket:
        try:
            socket = await websockets.connect(url)
        except OSError as e:
            raise WsError(f"connect failed: {e}") from e
        except Exception as e:  # invalid handshake and friends
            raise WsError(f"connect failed: {e}") from e
        try:
            await socket.send(subscribe_frame)
        except ConnectionClosed as e:
            raise WsError(f"subscribe failed: {e}") from e
        return cls(socket)

    async def next_text(self) -> str | None:
        """The next data frame: the text for a data frame, ``None`` on a
        clean close, :class:`WsError` on transport failure or a
        PONG-deadline breach. PING/PONG frames are handled internally and
        never surface."""
        while True:
            now = time.monotonic()
            if now >= self._next_ping:
                # No PONG within the deadline ⇒ half-open socket; bail to the
                # caller's reconnect path rather than waiting minutes for a
                # TCP-level failure.
                if now - self._last_pong > frames.PONG_TIMEOUT:
                    raise WsError("PONG timeout (half-open socket)")
                try:
                    await self._socket.send(frames.PING)
                except ConnectionClosed as e:
                    raise WsError(f"ping failed: {e}") from e
                self._next_ping = now + frames.PING_INTERVAL
                continue

            try:
                message = await asyncio.wait_for(self._socket.recv(), timeout=self._next_ping - now)
            except TimeoutError:
                continue  # time for the next PING / deadline check
            except ConnectionClosedOK:
                return None
            except ConnectionClosed as e:
                raise WsError(f"read failed: {e}") from e

            if isinstance(message, bytes):
                continue  # binary frames: ignored
            if message == frames.PONG:
                self._last_pong = time.monotonic()
                continue
            return message

    async def close(self) -> None:
        """Politely close (e.g. for a scheduled recycle)."""
        with contextlib.suppress(ConnectionClosed):
            await self._socket.close()


class MarketStream:
    """One market-channel connection with the liveness protocol handled.

    Yields raw text frames (:meth:`next_text`) or parsed events
    (:meth:`next_event`). On an exception, drop and reconnect —
    resubscribing replays a fresh book snapshot, so no state is lost beyond
    the gap.
    """

    def __init__(self, inner: _LiveSocket):
        self._inner = inner

    @classmethod
    async def connect(cls, config: MarketStreamConfig) -> MarketStream:
        """Connect and subscribe."""
        frame = frames.market_subscribe_frame(config.token_ids, config.custom_features)
        return cls(await _LiveSocket.connect(config.url, frame))

    async def next_text(self) -> str | None:
        """The next raw data frame: the text for a data frame, ``None`` on a
        clean server close, :class:`WsError` on transport failure or a
        PONG-deadline breach (reconnect). PING/PONG never surfaces."""
        return await self._inner.next_text()

    async def next_event(self) -> MarketEvent | None:
        """The next event, parsed. Unknown event kinds surface as
        :class:`MarketUnknown` rather than erroring."""
        text = await self.next_text()
        if text is None:
            return None
        return parse_market_event(text)

    async def close(self) -> None:
        """Politely close (e.g. for a scheduled recycle)."""
        await self._inner.close()
