"""Venue size rules and live tick-size state.

Tick sizes are plain :class:`~decimal.Decimal` throughout — deliberately
**not** a closed enum. The venue has added new grids without notice (0.0025
quarter-cent ticks landed mid-2026), and a closed enum turns that into a
parse failure on every book fetch. Price rails always derive as
``[tick, 1 − tick]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: Order size used by connection warm-up pings (the venue minimum).
FIXED_SIZE = Decimal(5)

#: The venue's minimum order size in shares.
MIN_SIZE = Decimal(5)

#: The venue's minimum order notional in USDC. The venue accepts an order
#: that clears *either* :data:`MIN_SIZE` or this notional.
MIN_NOTIONAL = Decimal(1)

#: Max decimal places the CLOB accepts in a BUY order's *taker* amount
#: (shares).
#:
#: Order sizes must be truncated to this precision or the API rejects them
#: with "invalid amounts ... taker amount a max of 2 decimals". Sizes stepped
#: in :data:`SIZE_STEP` (0.1) increments are already within it; only
#: fractional fills (e.g. a partial maker fill being hedged) need truncating
#: down to this scale.
SIZE_DECIMALS = 2

#: Granularity order sizes are floored to.
#:
#: Finer than a whole share (so a thin book's odd-lot depth past the integer
#: isn't left on the table) yet coarser than :data:`SIZE_DECIMALS`, so every
#: quoted size stays within the CLOB's 2-decimal taker-amount limit.
SIZE_STEP = Decimal("0.1")


def floor_to_size_step(size: Decimal) -> Decimal:
    """Floor a share size down to the nearest :data:`SIZE_STEP` increment.
    Exact for the terminating decimals these sizes are."""
    return (size / SIZE_STEP).to_integral_value(rounding="ROUND_FLOOR") * SIZE_STEP


def compute_order_size(
    min_ask_size: Decimal,
    max_size: Decimal,
    max_cash_deploy: Decimal,
    cost_per_unit: Decimal,
) -> Decimal | None:
    """Largest order size that:

    - is a multiple of :data:`SIZE_STEP` (floored toward zero) and at least
      :data:`MIN_SIZE`,
    - can be fully filled against the thinnest leg (``min_ask_size``),
    - does not exceed ``max_size``,
    - does not deploy more than ``max_cash_deploy`` of summed leg cost, where
      ``cost_per_unit`` is the per-unit summed best-ask cost (Σ ask).

    ``max_cash_deploy <= 0`` disables the cash cap. Returns ``None`` when no
    qualifying size reaches :data:`MIN_SIZE`.
    """
    cap = min(min_ask_size, max_size)
    if max_cash_deploy > 0 and cost_per_unit > 0:
        cap = min(cap, max_cash_deploy / cost_per_unit)
    stepped = floor_to_size_step(cap)
    return stepped if stepped >= MIN_SIZE else None


@dataclass
class TickEntry:
    """One token's tick-derived price rails plus its id pre-parsed to ``int``
    (so the signing path needs no per-order string parse)."""

    max_price: Decimal
    min_price: Decimal
    token_id_int: int

    @classmethod
    def new(cls, tick_size: Decimal, token_id_int: int) -> TickEntry:
        return cls(max_price=Decimal(1) - tick_size, min_price=tick_size, token_id_int=token_id_int)

    def retick(self, new_tick: Decimal) -> TickEntry | None:
        """Re-grid to ``new_tick``, preserving ``token_id_int``. Returns
        ``None`` when the tick is unchanged (idempotent re-delivery / no-op),
        mirroring :meth:`TickSizeCache.update_tick`."""
        if self.min_price == new_tick:
            return None
        return TickEntry.new(new_tick, self.token_id_int)


class TickSizeCache:
    """Live tick-size state per token id, kept current under
    ``tick_size_change`` WS events."""

    def __init__(self) -> None:
        self._entries: dict[str, TickEntry] = {}

    def insert(self, token_id: str, tick_size: Decimal) -> None:
        try:
            token_id_int = int(token_id)
        except ValueError:
            token_id_int = 0
        self._entries[token_id] = TickEntry.new(tick_size, token_id_int)

    def update_tick(self, token_id: str, new_tick: Decimal) -> tuple[Decimal, Decimal] | None:
        """Apply a live ``tick_size_change``. Returns ``(old_tick, new_tick)``
        when an existing entry actually changed, else ``None`` (unknown
        token, or idempotent re-delivery from a multi-connection fan-out).
        Preserves ``token_id_int``."""
        entry = self._entries.get(token_id)
        if entry is None:
            return None
        old = entry.min_price
        if old == new_tick:
            return None
        entry.min_price = new_tick
        entry.max_price = Decimal(1) - new_tick
        return (old, new_tick)

    def get(self, token_id: str) -> TickEntry | None:
        return self._entries.get(token_id)

    def __len__(self) -> int:
        return len(self._entries)
