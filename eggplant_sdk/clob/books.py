"""Batch order-book summary fetch (``POST /books``), parsed into lenient
types.

``tick_size`` is a plain :class:`~decimal.Decimal` — deliberately not an
enum. The venue launches new price grids without notice (the 0.0025
quarter-cent tick, 2026-07), and a closed tick enum makes the *whole* batch
response fail to parse — at startup, that can keep a client from booting at
all. Here any grid parses, and each book is parsed independently, so one
malformed element is skipped and logged instead of poisoning its batch.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from ..errors import ApiError, InvalidDataError, RateLimitError

logger = logging.getLogger(__name__)

#: Max token ids per ``/books`` POST.
MAX_BATCH_SIZE = 500


@dataclass
class BookLevel:
    """One price level of a book summary. The venue sends decimal strings;
    any price parses (no grid assumption)."""

    price: Decimal
    size: Decimal


@dataclass
class BookSummary:
    """One order-book summary from ``POST /books``, reduced to the
    load-bearing fields.

    ``tick_size`` is a plain :class:`~decimal.Decimal` so every venue grid
    parses — including ticks outside the historical
    ``{0.1, 0.01, 0.001, 0.0001}`` set.
    """

    #: Token id (arrives as a decimal string).
    asset_id: int
    #: The market's current price grid (minimum tick).
    tick_size: Decimal
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BookSummary:
        def levels(raw: Any) -> list[BookLevel]:
            # A missing or null side is an empty ladder (the venue sends
            # `null`, not `[]`).
            return [
                BookLevel(price=Decimal(str(lv["price"])), size=Decimal(str(lv["size"])))
                for lv in raw or []
            ]

        return cls(
            asset_id=int(data["asset_id"]),
            tick_size=Decimal(str(data["tick_size"])),
            bids=levels(data.get("bids")),
            asks=levels(data.get("asks")),
        )


def _build_requests(token_ids: list[str]) -> list[dict[str, str]]:
    """Validate every id parses as an integer token id and build the POST
    body entries."""
    requests = []
    for token_id in token_ids:
        try:
            int(token_id)
        except ValueError as e:
            raise InvalidDataError(f"invalid token ID {token_id}: {e}") from e
        requests.append({"token_id": token_id})
    return requests


def parse_books(text: str) -> list[BookSummary]:
    """Parse a ``/books`` response body leniently: the top level must be a
    JSON array, but each element parses independently — a malformed book is
    skipped with a warning instead of failing the batch. This is the property
    a strict typed parse lacks: one unexpected value (e.g. a new tick size)
    poisons every book in the response."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise InvalidDataError(f"books response is not JSON: {e}") from e
    if not isinstance(raw, list):
        raise InvalidDataError("books response is not a JSON array")

    books: list[BookSummary] = []
    skipped = 0
    for element in raw:
        try:
            books.append(BookSummary.from_dict(element))
        except Exception as e:
            skipped += 1
            asset_id = element.get("asset_id", "") if isinstance(element, dict) else ""
            logger.warning("skipping unparseable book summary asset_id=%s: %s", asset_id, e)
    if skipped:
        logger.warning("books response had %d unparseable entries (%d parsed)", skipped, len(books))
    return books


async def fetch_books_at(
    http: httpx.AsyncClient, url: str, token_ids: list[str]
) -> list[BookSummary]:
    """One raw ``POST /books`` round trip for ``token_ids``, leniently
    parsed. ``url`` is the full endpoint (``{host}/books``)."""
    requests = _build_requests(token_ids)
    response = await http.post(url, json=requests)
    if response.status_code == 429:
        raise RateLimitError(response.headers.get("retry-after"))
    text = response.text
    if response.status_code >= 400:
        raise ApiError(response.status_code, text[:300])
    return parse_books(text)


async def fetch_book_map_at(
    http: httpx.AsyncClient, url: str, token_ids: list[str]
) -> dict[int, BookSummary]:
    """Fetch books for arbitrarily many ids, chunked at
    :data:`MAX_BATCH_SIZE` with the chunks in flight concurrently, keyed by
    asset id."""
    chunks = [token_ids[i : i + MAX_BATCH_SIZE] for i in range(0, len(token_ids), MAX_BATCH_SIZE)]
    results = await asyncio.gather(*(fetch_books_at(http, url, chunk) for chunk in chunks))
    book_map: dict[int, BookSummary] = {}
    for books in results:
        book_map.update((book.asset_id, book) for book in books)
    logger.debug(
        "order book fetch complete: %d tokens, %d books, %d chunks",
        len(token_ids),
        len(book_map),
        len(chunks),
    )
    return book_map
