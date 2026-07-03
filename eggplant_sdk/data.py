"""Client for Polymarket's Data API (wallet positions).

Only ``/positions`` is implemented — enough to discover which events a wallet
actually holds (so tooling can act on those instead of probing every event
on-chain) and which of its positions are redeemable.

Wire numerics are ``float`` on purpose: these fields describe holdings for
discovery and reporting, and never feed order math (which is
:class:`~decimal.Decimal` end to end elsewhere in this SDK).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .chain import DATA_API_HOST
from .errors import ApiError, RateLimitError

_PAGE_LIMIT = 500

# The Data API hard-caps `offset` at 10,000: requests past it don't error or
# return a short page — they *recycle*, re-serving the offset-10,000 page.
# Callers summing `size` per row would silently double-count, so paging stops
# at the cap. A wallet with >10,500 positions has an unreachable tail; the
# remedy is redeeming resolved positions (shrinking the live set back under
# the cap so paging terminates on a partial page).
_MAX_OFFSET = 10_000


@dataclass
class Position:
    """One open position from the ``/positions`` endpoint, reduced to the
    load-bearing fields."""

    #: ERC1155 position token id as a decimal string — matches a market's
    #: YES/NO token id.
    asset: str
    #: Position size in shares.
    size: float
    condition_id: str = ""
    event_slug: str = ""
    title: str = ""
    #: ``"Yes"`` / ``"No"`` when the API includes it.
    outcome: str = ""
    negative_risk: bool = False
    #: Whether the position's market has resolved and the position can be
    #: redeemed (populated on ``redeemable=true`` queries).
    redeemable: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Position:
        return cls(
            asset=data["asset"],
            size=float(data["size"]),
            condition_id=data.get("conditionId") or "",
            event_slug=data.get("eventSlug") or "",
            title=data.get("title") or "",
            outcome=data.get("outcome") or "",
            negative_risk=bool(data.get("negativeRisk", False)),
            redeemable=bool(data.get("redeemable", False)),
        )


class DataApiClient:
    """Client for the Polymarket Data API."""

    def __init__(self, base_url: str = DATA_API_HOST):
        """A client against ``base_url`` (no trailing slash); defaults to the
        production Data API."""
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self._base_url = base_url

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> DataApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def all_positions(self, user: str, size_threshold: float = 1.0) -> list[Position]:
        """Fetch every open position for ``user``, paging until the API runs
        out.

        ``size_threshold`` is the minimum position size to return (the API
        default is 1; pass 0 for everything).
        """
        out: list[Position] = []
        offset = 0
        while True:
            page = await self._positions_page(
                user, [("sizeThreshold", str(size_threshold))], offset
            )
            full = len(page) >= _PAGE_LIMIT
            out.extend(page)
            if not full or offset >= _MAX_OFFSET:
                break
            offset += _PAGE_LIMIT
        return out

    async def all_redeemable_positions(self, user: str) -> tuple[list[Position], bool]:
        """Fetch the user's redeemable positions (``redeemable=true``),
        deduped by token id.

        Returns ``(positions, hit_cap)``: ``hit_cap`` is ``True`` when paging
        stopped at the offset cap (a tail may remain beyond it — redeem what
        was returned, then re-fetch), ``False`` on a short final page (the
        real end of the redeemable set).
        """
        out: list[Position] = []
        seen: set[str] = set()
        offset = 0
        hit_cap = False
        while True:
            page = await self._positions_page(user, [("redeemable", "true")], offset)
            n = len(page)
            added = 0
            for position in page:
                if position.asset not in seen:
                    seen.add(position.asset)
                    out.append(position)
                    added += 1
            if n < _PAGE_LIMIT:
                break  # short page → the real end of the redeemable set
            # Offset cap (or its recycle: a full page that adds nothing new)
            # → a tail may remain.
            if offset >= _MAX_OFFSET or added == 0:
                hit_cap = True
                break
            offset += _PAGE_LIMIT
        return out, hit_cap

    async def _positions_page(
        self, user: str, extra: list[tuple[str, str]], offset: int
    ) -> list[Position]:
        params = [
            ("user", user),
            ("limit", str(_PAGE_LIMIT)),
            ("offset", str(offset)),
            *extra,
        ]
        response = await self._http.get(f"{self._base_url}/positions", params=params)
        if response.status_code == 429:
            raise RateLimitError(response.headers.get("retry-after"))
        if response.status_code >= 400:
            raise ApiError(response.status_code, response.text[:300])
        return [Position.from_dict(p) for p in response.json()]
