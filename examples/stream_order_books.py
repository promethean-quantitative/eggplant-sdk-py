"""Seed order books over REST, then keep them live on the market channel (no
credentials needed).

    TOKEN_IDS=<id>[,<id>…] python examples/stream_order_books.py
"""

import asyncio
import os

import httpx

from eggplant_sdk.book import Book
from eggplant_sdk.chain import CLOB_HOST
from eggplant_sdk.clob.books import fetch_books_at
from eggplant_sdk.ws.market import (
    MarketBook,
    MarketPriceChange,
    MarketStream,
    MarketStreamConfig,
    MarketTickSizeChange,
)


async def main() -> None:
    token_ids = [t for t in os.environ.get("TOKEN_IDS", "").split(",") if t]
    if not token_ids:
        raise SystemExit("set TOKEN_IDS=<decimal token id>[,<id>…]")

    books: dict[str, Book] = {token_id: Book() for token_id in token_ids}

    # REST seed (public POST /books).
    async with httpx.AsyncClient() as http:
        for summary in await fetch_books_at(http, f"{CLOB_HOST}/books", token_ids):
            book = books.get(str(summary.asset_id))
            if book is not None:
                book.apply_snapshot(
                    ((lv.price, lv.size) for lv in summary.bids),
                    ((lv.price, lv.size) for lv in summary.asks),
                )
    print(f"seeded {len(books)} books over REST")

    # Live updates. Reconnect on any error — the resubscribe replays a fresh
    # snapshot, so no state is lost beyond the gap.
    stream = await MarketStream.connect(MarketStreamConfig(token_ids=token_ids))
    while (event := await stream.next_event()) is not None:
        if isinstance(event, MarketBook):
            book = books.setdefault(event.asset_id, Book())
            book.apply_snapshot(
                ((lv.price, lv.size) for lv in event.bids),
                ((lv.price, lv.size) for lv in event.asks),
            )
        elif isinstance(event, MarketPriceChange):
            for entry in event.price_changes:
                side = entry.book_side()
                book = books.get(entry.asset_id)
                if side is not None and book is not None:
                    # Idempotent: duplicate deliveries are no-ops.
                    book.apply_delta(side, entry.price, entry.size)
        elif isinstance(event, MarketTickSizeChange):
            print(f"tick change on {event.asset_id}: now {event.new_tick_size}")
            continue
        else:
            continue

        for token_id, book in books.items():
            print(f"{token_id[:12]}…  bid {book.best_bid()}  ask {book.best_ask()}")


if __name__ == "__main__":
    asyncio.run(main())
