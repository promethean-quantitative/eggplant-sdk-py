"""Platform-fee math tests."""

from decimal import Decimal

from eggplant_sdk.fee import platform_fee

d = Decimal


def test_matches_venue_formula_at_a_known_point():
    # 6.52 shares @ 0.83 NO, rate 0.05: 6.52 × 0.05 × 0.83 × 0.17 = 0.0459986.
    fee = platform_fee(d("6.52"), d("0.83"), d("0.05"))
    assert fee == d("0.0459986")
    assert fee.quantize(d("0.001")) == d("0.046")


def test_zero_at_price_bounds():
    assert platform_fee(d(10), d(0), d("0.05")) == d(0)
    assert platform_fee(d(10), d(1), d("0.05")) == d(0)


def test_linear_in_shares():
    one = platform_fee(d(1), d("0.69"), d("0.05"))
    many = platform_fee(d("6.52"), d("0.69"), d("0.05"))
    assert many == one * d("6.52")


def test_symmetric_in_yes_no_price():
    # price×(1−price) is symmetric, so a NO fill at 0.69 and a YES fill at
    # 0.31 carry the same per-share fee.
    assert platform_fee(d(1), d("0.69"), d("0.05")) == platform_fee(d(1), d("0.31"), d("0.05"))
