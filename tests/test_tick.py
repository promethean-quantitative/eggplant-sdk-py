"""Venue size rules and tick-state tests."""

from decimal import Decimal

from eggplant_sdk.clob.signing import to_fixed_usdc
from eggplant_sdk.clob.tick import (
    FIXED_SIZE,
    TickEntry,
    TickSizeCache,
    compute_order_size,
)

d = Decimal


def test_tick_entry_prices_001():
    entry = TickEntry.new(d("0.01"), 111)
    assert entry.max_price == d("0.99")
    assert entry.min_price == d("0.01")


def test_tick_entry_prices_0001():
    entry = TickEntry.new(d("0.001"), 222)
    assert entry.max_price == d("0.999")
    assert entry.min_price == d("0.001")


def test_tick_entry_prices_quarter_cent():
    # The 0.0025 grid (2026-07 venue addition): rails derive as
    # [tick, 1 − tick] — no enumeration of known ticks anywhere.
    entry = TickEntry.new(d("0.0025"), 333)
    assert entry.max_price == d("0.9975")
    assert entry.min_price == d("0.0025")


def test_update_tick_onto_quarter_cent_grid():
    cache = TickSizeCache()
    cache.insert("111", d("0.01"))
    assert cache.update_tick("111", d("0.0025")) == (d("0.01"), d("0.0025"))
    entry = cache.get("111")
    assert entry.min_price == d("0.0025")
    assert entry.max_price == d("0.9975")


def test_retick_recomputes_bounds_and_preserves_token_id():
    entry = TickEntry.new(d("0.01"), 123)
    regridded = entry.retick(d("0.001"))
    assert regridded is not None
    assert regridded.min_price == d("0.001")
    assert regridded.max_price == d("0.999")
    assert regridded.token_id_int == 123


def test_retick_same_value_is_none():
    entry = TickEntry.new(d("0.01"), 123)
    assert entry.retick(d("0.01")) is None


def test_update_tick_refine_recomputes_bounds_and_preserves_token_id():
    cache = TickSizeCache()
    cache.insert("111", d("0.01"))
    token_id_int = cache.get("111").token_id_int

    assert cache.update_tick("111", d("0.001")) == (d("0.01"), d("0.001"))
    entry = cache.get("111")
    assert entry.min_price == d("0.001")
    assert entry.max_price == d("0.999")
    assert entry.token_id_int == token_id_int


def test_update_tick_coarsen_lowers_max_price():
    cache = TickSizeCache()
    cache.insert("111", d("0.001"))
    assert cache.update_tick("111", d("0.01")) == (d("0.001"), d("0.01"))
    assert cache.get("111").max_price == d("0.99")


def test_update_tick_same_value_is_noop():
    cache = TickSizeCache()
    cache.insert("111", d("0.01"))
    assert cache.update_tick("111", d("0.01")) is None
    assert cache.get("111").max_price == d("0.99")


def test_update_tick_unknown_token_does_not_insert():
    cache = TickSizeCache()
    assert cache.update_tick("999", d("0.01")) is None
    assert cache.get("999") is None
    assert len(cache) == 0


def test_compute_order_size_floors_to_size_step():
    # Cash cap disabled (max_cash_deploy = 0); cost_per_unit is unused.
    max_size = d(20)
    assert compute_order_size(d(5), max_size, d(0), d(1)) == d(5)
    # Fractional caps floor to the nearest 0.1; never round up past the cap.
    assert compute_order_size(d("5.9"), max_size, d(0), d(1)) == d("5.9")
    # Sub-step remainder is dropped, not rounded up.
    assert compute_order_size(d("5.05"), max_size, d(0), d(1)) == d(5)
    assert compute_order_size(d(9), max_size, d(0), d(1)) == d(9)
    assert compute_order_size(d(10), max_size, d(0), d(1)) == d(10)
    assert compute_order_size(d("14.99"), max_size, d(0), d(1)) == d("14.9")
    assert compute_order_size(d(20), max_size, d(0), d(1)) == d(20)
    # max_size (20) binds below min_ask_size.
    assert compute_order_size(d(25), max_size, d(0), d(1)) == d(20)
    assert compute_order_size(d(100), max_size, d(0), d(1)) == d(20)
    # Below MIN_SIZE after the floor -> skip (4.99 floors to 4.9).
    assert compute_order_size(d("4.99"), max_size, d(0), d(1)) is None
    assert compute_order_size(d(0), max_size, d(0), d(1)) is None


def test_compute_order_size_caps_by_cash():
    # cost_per_unit = Σ ask; size is capped so size * cost_per_unit <= budget.
    # 30 / 2 = 15 binds below max_size (20).
    assert compute_order_size(d(100), d(20), d(30), d(2)) == d(15)
    # 100 / 3 = 33.33... floors to 33.3 (post-cap, nearest 0.1).
    assert compute_order_size(d(100), d(100), d(100), d(3)) == d("33.3")
    # 8 / 2 = 4 < MIN_SIZE -> skip the opportunity.
    assert compute_order_size(d(100), d(20), d(8), d(2)) is None
    # Budget of 0 disables the cap.
    assert compute_order_size(d(100), d(20), d(0), d(3)) == d(20)


def test_warmup_maker_amount_tick_001():
    tick = TickEntry.new(d("0.01"), 111)
    assert to_fixed_usdc(FIXED_SIZE * tick.min_price) == 50_000


def test_warmup_maker_amount_tick_0001():
    tick = TickEntry.new(d("0.001"), 222)
    assert to_fixed_usdc(FIXED_SIZE * tick.min_price) == 5_000
