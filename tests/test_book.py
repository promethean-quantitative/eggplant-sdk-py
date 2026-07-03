"""Order-book state tests."""

from decimal import Decimal

from eggplant_sdk.book import Book, BookSide

d = Decimal


def ladder(levels: list[tuple[str, str]]) -> Book:
    book = Book()
    book.apply_snapshot([], [(d(p), d(s)) for p, s in levels])
    return book


def test_ask_price_for_size_walks_to_covering_level():
    # 30 at the touch, 50 behind, 100 deep: a 100-share taker must reach 0.13.
    book = ladder([("0.11", "30"), ("0.12", "50"), ("0.13", "100")])
    assert book.ask_price_for_size(d(100)) == d("0.13")
    # The touch alone covers a small order.
    assert book.ask_price_for_size(d(30)) == d("0.11")
    # Boundary: exactly the first two levels.
    assert book.ask_price_for_size(d(80)) == d("0.12")


def test_ask_price_for_size_exhausted_ladder_returns_deepest():
    book = ladder([("0.11", "30"), ("0.12", "50")])
    assert book.ask_price_for_size(d(500)) == d("0.12")


def test_ask_price_for_size_empty_ladder_is_none():
    assert Book().ask_price_for_size(d(10)) is None


def test_ask_depth_up_to_sums_levels_within_ceiling():
    book = ladder([("0.11", "30"), ("0.12", "50"), ("0.13", "100")])
    # Touch only.
    assert book.ask_depth_up_to(d("0.11")) == d(30)
    # First two levels (inclusive boundary).
    assert book.ask_depth_up_to(d("0.12")) == d(80)
    # A ceiling between levels takes everything at or below it, nothing above.
    assert book.ask_depth_up_to(d("0.125")) == d(80)
    # Whole ladder.
    assert book.ask_depth_up_to(d("0.13")) == d(180)
    # Below the touch: nothing fillable that cheap.
    assert book.ask_depth_up_to(d("0.10")) == d(0)
    # Empty ladder.
    assert Book().ask_depth_up_to(d("0.50")) == d(0)


def test_apply_delta_is_idempotent_and_reports_change():
    book = Book()
    assert book.apply_delta(BookSide.ASK, d("0.11"), d(30))
    # Same delta again (fan-out re-delivery): no change.
    assert not book.apply_delta(BookSide.ASK, d("0.11"), d(30))
    # Size change at the level: change.
    assert book.apply_delta(BookSide.ASK, d("0.11"), d(40))
    # Zero size removes; removing again is a no-op.
    assert book.apply_delta(BookSide.ASK, d("0.11"), d(0))
    assert not book.apply_delta(BookSide.ASK, d("0.11"), d(0))
    assert not book.asks


def test_snapshot_drops_zero_size_levels():
    book = Book()
    book.apply_snapshot(
        [(d("0.5"), d(10)), (d("0.4"), d(0))],
        [(d("0.6"), d(0)), (d("0.7"), d(5))],
    )
    assert len(book.bids) == 1
    assert len(book.asks) == 1


def test_best_bid_ask():
    book = Book()
    book.apply_snapshot(
        [(d("0.4"), d(1)), (d("0.45"), d(1))],
        [(d("0.55"), d(1)), (d("0.6"), d(1))],
    )
    assert book.best_bid() == d("0.45")
    assert book.best_ask() == d("0.55")
    assert Book().best_bid() is None
    assert Book().best_ask() is None
