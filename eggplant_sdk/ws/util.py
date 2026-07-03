"""Multi-connection reader plumbing: staggered recycle phasing, maker-side
classification, and bounded first-delivery dedup.

A recycle is a *scheduled* clean close + immediate reconnect. Half-open
sockets (NAT drops, server stalls) are caught separately by each reader's
PONG liveness deadline; recycling bounds the age of every connection so
subtler degradation (silent subscription loss, a stale LB path) can't
accumulate. Offsets are phased so that redundant peers never refresh together
— one always stays subscribed to cover the brief gap, which is why recycling
disables itself without a peer.
"""

from __future__ import annotations

from collections import deque

from ..clob.types import Side


def recycle_offset(conn_id: int, connections: int, interval_secs: float) -> float | None:
    """Phase offset (seconds) for connection ``conn_id``'s recycle timer, or
    ``None`` when recycling is off.

    Off means ``interval_secs == 0``, or fewer than two connections — a lone
    connection has no peer to cover the refresh gap and relies on the
    reader's PONG liveness deadline instead.

    The N connections recycle at phases ``1/N, 2/N, … N/N`` of the period, so
    they are evenly spread and none fires at boot (phase ``0``).
    """
    if interval_secs == 0 or connections < 2:
        return None
    return interval_secs * (conn_id + 1) / connections


def market_recycle_offset(
    shard_id: int,
    copy: int,
    num_shards: int,
    redundancy: int,
    interval_secs: float,
) -> float | None:
    """Phase offset (seconds) for the recycle timer of shard ``shard_id``'s
    redundant copy ``copy``, or ``None`` when recycling is off.

    Mirrors :func:`recycle_offset`, but phases by a slot ordered
    *copy-major, shard-minor* (``copy * num_shards + shard_id``) over all
    ``num_shards * redundancy`` connections: every connection lands on a
    distinct, evenly spread phase, and a shard's redundant copies sit exactly
    ``period / redundancy`` apart, so one is always fully reconnected before
    its peer recycles. ``redundancy < 2`` ⇒ ``None`` (a lone copy has no
    same-shard peer to cover the refresh gap); ``interval_secs == 0`` ⇒
    ``None`` too.
    """
    if redundancy < 2:
        return None
    total = num_shards * redundancy
    slot = copy * num_shards + shard_id
    return recycle_offset(slot, total, interval_secs)


def our_maker_side(
    taker_side: Side,
    taker_outcome: str | None,
    maker_outcome: str,
) -> Side | None:
    """Our side as the *maker* of a user-channel trade, derived from the
    taker side and the two outcomes.

    The trade's top-level ``side``/``outcome`` describe the taker; a maker
    order carries its own ``outcome`` but no side. Because YES+NO prices sum
    to 1 ("buy YES" == "sell NO"): a *matching* outcome means we took the
    opposite side of the same token; a *differing* outcome means we are on
    the taker's side in the complementary token. Returns ``None`` when the
    side or outcome is unknown.

    Processes sharing one API key use this to filter the shared fill feed to
    the side each of them owns.
    """
    if taker_outcome is None:
        return None
    if taker_side not in (Side.BUY, Side.SELL):
        return None
    same = maker_outcome.upper() == taker_outcome.upper()
    return taker_side.opposite() if same else taker_side


class SeenIds:
    """Bounded FIFO set of ids already handled.

    Share one across redundant user-channel connections so the first to
    deliver a given trade wins and the rest drop it; keep it for the process
    lifetime so reconnects don't replay old fills.
    """

    def __init__(self, cap: int):
        self._set: set[str] = set()
        self._order: deque[str] = deque()
        self._cap = cap

    def insert(self, id_: str) -> bool:
        """Record ``id_``. Returns ``True`` if it was newly seen (handle
        it), ``False`` if already present (duplicate — drop it). At capacity
        the oldest id is evicted FIFO."""
        if id_ in self._set:
            return False
        self._set.add(id_)
        self._order.append(id_)
        if len(self._order) > self._cap:
            oldest = self._order.popleft()
            self._set.discard(oldest)
        return True

    def contains(self, id_: str) -> bool:
        """Whether ``id_`` has been seen (without recording it)."""
        return id_ in self._set

    def __contains__(self, id_: str) -> bool:
        return id_ in self._set
