"""Client for Polymarket's Gamma API (event and market metadata).

Two access patterns: keyset paging over the open-event universe
(:meth:`GammaClient.fetch_keyset_page`) and slug-targeted resolution
(:meth:`GammaClient.fetch_events_by_slug`, e.g. to resolve exactly the events
a wallet holds positions in).

Wire numerics are ``float`` on purpose: Gamma serves floats, and these fields
(volumes, indicative prices, fee rates) inform discovery and display — they
never feed order math, which is :class:`~decimal.Decimal` end to end
elsewhere in this SDK.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from .chain import GAMMA_HOST
from .convert import MarketIds
from .errors import ApiError, RateLimitError


@dataclass
class FeeSchedule:
    """A market's platform-fee parameters (see
    :func:`eggplant_sdk.fee.platform_fee`)."""

    #: Gross taker fee rate.
    rate: float | None = None
    rebate_rate: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeeSchedule:
        return cls(rate=data.get("rate"), rebate_rate=data.get("rebateRate"))


@dataclass
class GammaTag:
    """A Gamma category tag. Only ``slug`` is load-bearing; the rest of the
    tag object (id, label, timestamps) is ignored."""

    slug: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GammaTag:
        return cls(slug=data.get("slug") or "")


@dataclass
class GammaMarket:
    """One market (leg) of a Gamma event."""

    active: bool | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    #: The leg's display title within its event (e.g. an outcome name).
    group_item_title: str | None = None
    fee_schedule: FeeSchedule | None = None
    #: ``[YES, NO]`` token ids. Gamma delivers this as a JSON-encoded
    #: *string* (``"[\"123\",\"456\"]"``); the parser unwraps it.
    clob_token_ids: list[str] | None = None
    tick_size: float | None = None
    seconds_delay: int | None = None
    #: Per-game sports market kind (e.g. ``"moneyline"``, ``"totals"``). Set
    #: only on individual games; absent on season-long futures and non-sports
    #: markets — its presence is what distinguishes a single game from a
    #: futures market under the same Sports tag.
    sports_market_type: str | None = None
    question_id: str | None = None
    condition_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GammaMarket:
        raw_token_ids = data.get("clobTokenIds")
        clob_token_ids = None
        if raw_token_ids is not None:
            try:
                clob_token_ids = json.loads(raw_token_ids)
            except (json.JSONDecodeError, TypeError):
                clob_token_ids = None
        fee_schedule = data.get("feeSchedule")
        return cls(
            active=data.get("active"),
            best_bid=data.get("bestBid"),
            best_ask=data.get("bestAsk"),
            group_item_title=data.get("groupItemTitle"),
            fee_schedule=FeeSchedule.from_dict(fee_schedule) if fee_schedule else None,
            clob_token_ids=clob_token_ids,
            tick_size=data.get("orderPriceMinTickSize"),
            seconds_delay=data.get("secondsDelay"),
            sports_market_type=data.get("sportsMarketType"),
            question_id=data.get("questionID"),
            condition_id=data.get("conditionId"),
        )

    def yes_token_id(self) -> str | None:
        """The market's YES token id (``clobTokenIds[0]``)."""
        if not self.clob_token_ids:
            return None
        return self.clob_token_ids[0]

    def no_token_id(self) -> str | None:
        """The market's NO token id (``clobTokenIds[1]``)."""
        if not self.clob_token_ids or len(self.clob_token_ids) < 2:
            return None
        return self.clob_token_ids[1]

    def market_ids(self) -> MarketIds | None:
        """The market's on-chain identifiers in the shape
        :func:`eggplant_sdk.convert.convert_legs` consumes. ``None`` when the
        market has no NO token id."""
        no_token_id = self.no_token_id()
        if no_token_id is None:
            return None
        return MarketIds(
            question_id=self.question_id,
            condition_id=self.condition_id,
            yes_token_id=self.yes_token_id(),
            no_token_id=no_token_id,
        )


@dataclass
class GammaEvent:
    """One Gamma event with its nested markets."""

    id: str
    slug: str
    title: str
    neg_risk: bool = False
    volume24hr: float | None = None
    volume_1wk: float | None = None
    #: RFC3339. For tournament futures this is the resolution deadline, not
    #: a start.
    end_date: str | None = None
    #: Top-level scheduled start (RFC3339). For individual sports games this
    #: is the kickoff instant; absent on most non-game events.
    start_time: str | None = None
    #: Event-level open instant (RFC3339) — ~market-creation time, set on
    #: every event.
    start_date: str | None = None
    #: Gamma category tags (e.g. ``sports``, ``esports``, ``golf``).
    tags: list[GammaTag] = field(default_factory=list)
    markets: list[GammaMarket] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GammaEvent:
        markets = data.get("markets")
        return cls(
            id=data["id"],
            slug=data["slug"],
            title=data["title"],
            neg_risk=bool(data.get("negRisk", False)),
            volume24hr=data.get("volume24hr"),
            volume_1wk=data.get("volume1wk"),
            end_date=data.get("endDate"),
            start_time=data.get("startTime"),
            start_date=data.get("startDate"),
            tags=[GammaTag.from_dict(t) for t in data.get("tags") or []],
            markets=[GammaMarket.from_dict(m) for m in markets] if markets is not None else None,
        )


@dataclass
class KeysetResponse:
    """One page of ``GET /events/keyset``."""

    events: list[GammaEvent] = field(default_factory=list)
    #: Pass back as ``after_cursor``; ``None`` on the last page.
    next_cursor: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KeysetResponse:
        return cls(
            events=[GammaEvent.from_dict(e) for e in data.get("events") or []],
            next_cursor=data.get("next_cursor"),
        )


def _url_encode(value: str) -> str:
    """RFC3986 unreserved-set percent-encoding."""
    return quote(value, safe="")


class GammaClient:
    """Client for the Gamma API."""

    def __init__(self, base_url: str = GAMMA_HOST):
        """A client against ``base_url`` (no trailing slash); defaults to the
        production Gamma API."""
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self._base_url = base_url

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> GammaClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def fetch_keyset_page(
        self,
        cursor: str | None = None,
        limit: int = 50,
        tag_slug: str | None = None,
    ) -> KeysetResponse:
        """One keyset page of open events (``GET /events/keyset``). Page
        through by feeding each response's ``next_cursor`` back as
        ``cursor``; ``tag_slug`` optionally filters by category."""
        url = f"{self._base_url}/events/keyset?limit={limit}&closed=false"
        if cursor is not None:
            url += f"&after_cursor={_url_encode(cursor)}"
        if tag_slug is not None:
            url += f"&tag_slug={_url_encode(tag_slug)}"
        return KeysetResponse.from_dict(await self._get_json(url))

    async def fetch_events_by_slug(self, slug: str) -> list[GammaEvent]:
        """Fetch the event(s) matching ``slug`` (normally one), with their
        nested markets. Lets a caller resolve a specific event instead of
        paging the universe — e.g. resolving exactly the events a wallet
        holds."""
        url = f"{self._base_url}/events?slug={_url_encode(slug)}"
        return [GammaEvent.from_dict(e) for e in await self._get_json(url)]

    async def _get_json(self, url: str) -> Any:
        response = await self._http.get(url)
        if response.status_code == 429:
            raise RateLimitError(response.headers.get("retry-after"))
        if response.status_code >= 400:
            raise ApiError(response.status_code, response.text[:300])
        return response.json()
