"""Lenient `/books` parse tests."""

from decimal import Decimal

import pytest

from eggplant_sdk.clob.books import _build_requests, parse_books
from eggplant_sdk.errors import InvalidDataError

d = Decimal

# Venue-shaped payload: string prices, a null side, extra fields, and the
# quarter-cent tick a closed enum rejects wholesale.
QUARTER_CENT_BOOK = """[{
    "market": "0xabc",
    "asset_id": "71321045679252212594626385532706912750332728571942532289631379312455583992563",
    "timestamp": "1751300000000",
    "hash": "h",
    "bids": [{"price": "0.4975", "size": "120.5"}],
    "asks": null,
    "min_order_size": "5",
    "tick_size": "0.0025",
    "neg_risk": true
}]"""


def test_parses_quarter_cent_tick():
    books = parse_books(QUARTER_CENT_BOOK)
    assert len(books) == 1
    book = books[0]
    assert book.tick_size == d("0.0025")
    assert len(book.bids) == 1
    assert book.bids[0].price == d("0.4975")
    assert book.bids[0].size == d("120.5")
    # `null` side → empty ladder.
    assert book.asks == []
    assert book.asset_id == int(
        "71321045679252212594626385532706912750332728571942532289631379312455583992563"
    )


def test_malformed_book_is_skipped_not_fatal():
    # The middle element has no tick_size: it alone is dropped.
    payload = """[
        {"asset_id": "11", "bids": [], "asks": [], "tick_size": "0.01"},
        {"asset_id": "22", "bids": [], "asks": []},
        {"asset_id": "33", "bids": [], "asks": [], "tick_size": "0.0025"}
    ]"""
    books = parse_books(payload)
    assert len(books) == 2
    assert books[0].tick_size == d("0.01")
    assert books[1].tick_size == d("0.0025")


def test_missing_sides_default_empty():
    books = parse_books('[{"asset_id": "11", "tick_size": "0.001"}]')
    assert books[0].bids == [] and books[0].asks == []


def test_non_array_body_is_an_error():
    with pytest.raises(InvalidDataError):
        parse_books('{"error": "not ok"}')


def test_numeric_tick_also_parses():
    # Defensive: a venue switch away from strings wouldn't break the fetch.
    books = parse_books('[{"asset_id": "11", "tick_size": 0.0025}]')
    assert books[0].tick_size == d("0.0025")


def test_build_requests_rejects_garbage_id():
    with pytest.raises(InvalidDataError):
        _build_requests(["not-a-number"])
    assert _build_requests(["123"]) == [{"token_id": "123"}]
