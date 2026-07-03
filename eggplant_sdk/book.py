"""Order-book depth state.

:class:`Book` reconstructs one token's full ladder from a snapshot (a
``book`` WS event or a REST seed) and keeps it live under incremental
``price_change`` deltas. The snapshot/delta application — and its idempotency
across an N-way WS connection fan-out — lives here, in one tested place.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from decimal import Decimal


class BookSide(enum.Enum):
    """Which side of the book a ``price_change`` delta touches: a ``BUY``
    tick updates the bid ladder, a ``SELL`` tick the ask ladder."""

    BID = "bid"
    ASK = "ask"


class Book:
    """Full order book for one token: every live price level on each side,
    ``price -> size``.

    ``bids`` and ``asks`` are plain dicts; use :meth:`best_bid` /
    :meth:`best_ask` for the touch (the dicts are not kept sorted).
    """

    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}

    def apply_snapshot(
        self,
        bids: Iterable[tuple[Decimal, Decimal]],
        asks: Iterable[tuple[Decimal, Decimal]],
    ) -> None:
        """Replace both ladders from a full snapshot (a ``book`` event or
        REST seed). Zero-size levels are dropped — a level only exists while
        it has resting size."""
        self.bids = {price: size for price, size in bids if size}
        self.asks = {price: size for price, size in asks if size}

    def apply_delta(self, side: BookSide, price: Decimal, size: Decimal) -> bool:
        """Apply one incremental level change from a ``price_change``.
        ``size`` is the new absolute size at ``price``; ``size == 0`` removes
        the level. Idempotent: replaying the same delta (e.g. the same tick
        arriving on another fan-out connection) is a no-op, so the
        reconstructed book converges regardless of duplication.

        Returns ``True`` if the ladder actually changed (so an idempotent
        re-delivery returns ``False``), letting callers drive work off a
        genuine depth change.
        """
        levels = self.bids if side is BookSide.BID else self.asks
        if not size:
            return levels.pop(price, None) is not None
        changed = levels.get(price) != size
        levels[price] = size
        return changed

    def best_bid(self) -> Decimal | None:
        """The highest bid price, or ``None`` on an empty ladder."""
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Decimal | None:
        """The lowest ask price, or ``None`` on an empty ladder."""
        return min(self.asks) if self.asks else None

    def ask_price_for_size(self, need: Decimal) -> Decimal | None:
        """The ask price at which cumulative ladder depth first covers
        ``need`` shares — the marginal level a taker BUY of that size must
        reach. Limit-pricing a large order off the touch alone underprices it
        (the touch may hold a fraction of the size); pricing off this level
        lets the whole order cross. When the ladder holds less than ``need``
        in total, returns the deepest level (the best truth the book offers);
        ``None`` only on an empty ladder."""
        cumulative = Decimal(0)
        last: Decimal | None = None
        for price in sorted(self.asks):
            cumulative += self.asks[price]
            last = price
            if cumulative >= need:
                break
        return last

    def ask_depth_up_to(self, max_price: Decimal) -> Decimal:
        """Cumulative ask depth at every level priced at or below
        ``max_price`` — the size a taker BUY fills without paying past
        ``max_price``. The inverse companion to :meth:`ask_price_for_size`
        (size for a price ceiling, vs. price for a size), summing the same
        best-ask-first ladder so the two stay consistent. ``0`` when the
        touch already sits above ``max_price`` (nothing fillable that cheap)
        or the ladder is empty."""
        return sum((size for price, size in self.asks.items() if price <= max_price), Decimal(0))
