"""Polymarket V2 platform-fee math.

The venue charges takers ``shares × rate × (price × (1 − price))^exponent``.
With exponent ``1`` (no builder code) the formula collapses to
``shares × rate × price × (1 − price)`` — pure :class:`~decimal.Decimal`, no
floating point.

``rate`` is the raw, gross fee rate from the market's fee schedule. Referral
kickbacks are *rebates* applied by callers, never folded in here, so the
value returned is always the gross fee the venue charges at fill time. Maker
fills incur no taker fee, so price only the taker legs.
"""

from __future__ import annotations

from decimal import Decimal


def platform_fee(shares: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    """Gross taker platform fee for ``shares`` filled at ``price`` under fee
    ``rate``.

    ``shares × rate × price × (1 − price)``. The ``price × (1 − price)``
    base is the V2 exponent-1 form; it is symmetric in YES/NO and zero at the
    ``0``/``1`` bounds (no fee on a leg that fills at a degenerate price).
    Returns gross — apply any referral kickback at the call site if a net
    figure is wanted.
    """
    return shares * rate * price * (Decimal(1) - price)
