"""Merge/convert **safety net**: systematically settle every negRisk position
a wallet holds, not just the one event a caller just traded.

Where the :mod:`~eggplant_sdk.convert` worker converts a single event right
after a fill, ``sweep`` discovers the wallet's *actual* holdings and runs the
same cycle over all of them — mopping up orphans a normal flow can leave behind:
a convert that errored or was quota-throttled, a partial fill, a crash
mid-cycle. It is purely additive and idempotent: safe to run by hand or on a
cron.

How it discovers work (no persisted event file needed):

1. :meth:`~eggplant_sdk.data.DataApiClient.all_positions` lists the wallet's
   open positions.
2. Each distinct **negRisk** event those positions belong to is resolved to its
   full leg set from Gamma
   (:meth:`~eggplant_sdk.gamma.GammaClient.fetch_events_by_slug`). Gamma is
   needed because convert requires each leg's ``question_id``, which the Data
   API doesn't carry. A slug Gamma can't resolve is skipped (best-effort), not
   fatal.
3. Each event is classified **from the Data API sizes alone** — no on-chain
   reads in the scan: a leg holding YES+NO is mergeable; leftover NO is
   convertible (a lone NO leg only past the single-leg dust floor, since
   converting it alone frees 0 USDC).

The scan's amounts are approximate (the API's ``size`` is a float); the
**authoritative** merge/convert amounts come from on-chain balances at execute
time, which :func:`~eggplant_sdk.convert.process_job` re-reads.

Layers mirror :mod:`eggplant_sdk.convert`: the pure classification
(:func:`leg_sizes`, :func:`classify_event`) has no I/O; the discovery +
execution engine (:func:`plan_sweep`, :func:`sweep_all`) reads over the network.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field

from .convert import ConvertDelays, ConvertJob, ConvertLeg, convert_legs, process_job
from .data import DataApiClient, Position
from .errors import EggplantError
from .gamma import GammaClient
from .relayer import RelayerClient
from .signer import LocalSigner

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventClassification:
    """Merge/convert classification for one event, derived from Data API
    sizes."""

    #: Legs holding both YES and NO (and a condition id) — mergeable pairs.
    merge_pairs: int
    #: Total shares recoverable by merging those pairs (``Σ min(yes, no)``).
    merge_shares: float
    #: Legs holding leftover NO after netting out any merge — convert inputs.
    convert_legs: int
    #: Smallest leftover-NO across the convert legs (``0.0`` when none).
    convert_min_no: float
    #: Any mergeable pair present.
    mergeable: bool
    #: A convert frees USDC: >= 2 leftover-NO legs, or a lone one above the
    #: single-leg dust floor.
    convertible: bool

    def actionable(self) -> bool:
        """Whether this event has any merge or convert work worth
        submitting."""
        return self.mergeable or self.convertible


def leg_sizes(legs: list[ConvertLeg], positions: list[Position]) -> list[tuple[float, float]]:
    """Sum the wallet's per-leg ``(no_shares, yes_shares)`` for an event's legs
    from Data API positions.

    Maps each position's token id back to its leg and side. Legs the wallet
    doesn't hold read as ``(0.0, 0.0)``; the result is always ``len(legs)``
    long, in leg order.
    """
    by_token: dict[int, tuple[int, bool]] = {}
    for i, leg in enumerate(legs):
        by_token[leg.no_token_id] = (i, True)
        if leg.yes_token_id is not None:
            by_token[leg.yes_token_id] = (i, False)

    sizes = [[0.0, 0.0] for _ in legs]
    for p in positions:
        try:
            tid = int(p.asset)
        except ValueError:
            continue
        hit = by_token.get(tid)
        if hit is None:
            continue
        i, is_no = hit
        sizes[i][0 if is_no else 1] += p.size

    return [(no, yes) for no, yes in sizes]


def classify_event(
    legs: list[ConvertLeg],
    sizes: list[tuple[float, float]],
    single_leg_min_qty: float,
) -> EventClassification:
    """Classify one event's held sizes into merge/convert work.

    ``sizes[i]`` is leg ``i``'s ``(no_shares, yes_shares)`` (see
    :func:`leg_sizes`); ``single_leg_min_qty`` is the leftover-NO floor (in
    shares) below which a *lone* convertible leg is left alone (converting it
    alone frees 0 USDC, so it isn't worth the gas). Multi-leg converts ignore
    the floor.
    """
    merge_pairs = 0
    merge_shares = 0.0
    convert_legs = 0
    min_no = math.inf

    for leg, (no, yes) in zip(legs, sizes, strict=False):
        if no > 0.0 and yes > 0.0 and leg.condition_id is not None:
            merge_pairs += 1
            merge_shares += min(no, yes)
        remaining_no = no - min(no, yes)
        if remaining_no > 0.0:
            convert_legs += 1
            min_no = min(min_no, remaining_no)

    mergeable = merge_pairs > 0
    convertible = convert_legs > 1 or (convert_legs == 1 and min_no >= single_leg_min_qty)

    return EventClassification(
        merge_pairs=merge_pairs,
        merge_shares=merge_shares,
        convert_legs=convert_legs,
        convert_min_no=min_no if math.isfinite(min_no) else 0.0,
        mergeable=mergeable,
        convertible=convertible,
    )


# ---------------------------------------------------------------------------
# Discovery + execution engine
# ---------------------------------------------------------------------------


@dataclass
class HeldEvent:
    """A held negRisk event resolved to its full leg set (from Gamma)."""

    slug: str
    title: str
    legs: list[ConvertLeg]


@dataclass
class SweepReport:
    """One held event's classification, tagged with its slug and title for
    reporting."""

    slug: str
    title: str
    classification: EventClassification


@dataclass
class SweepOptions:
    """Knobs for :func:`sweep_all` / :func:`plan_sweep`. The defaults are a
    workable starting point."""

    #: Data API size floor for discovery (shares; ``0.0`` = everything).
    min_shares: float = 0.0
    #: Restrict the sweep to a single event slug (``None`` = every held negRisk
    #: event).
    only_slug: str | None = None
    #: Merge/convert cycle tuning — settle waits, gas budget, and the
    #: single-leg dust floor (whose raw form is the authoritative guard inside
    #: :func:`~eggplant_sdk.convert.process_job`).
    delays: ConvertDelays = field(default_factory=ConvertDelays)
    #: Max concurrent Gamma event resolutions during discovery.
    gamma_concurrency: int = 8


@dataclass
class SweepSummary:
    """What :func:`sweep_all` did."""

    #: Distinct held negRisk events discovered.
    held_events: int = 0
    #: Events with actionable merge/convert work.
    actionable: int = 0
    #: Events whose cycle submitted successfully.
    executed: int = 0
    #: Events whose cycle failed.
    failed: int = 0


def _single_leg_min_qty(delays: ConvertDelays) -> float:
    """The single-leg dust floor in shares, from the raw 6-dp form the
    authoritative on-chain guard uses."""
    return delays.single_leg_min_qty_raw / 1_000_000.0


async def _resolve_event(gamma: GammaClient, slug: str) -> HeldEvent | None:
    """Resolve one held event slug to its full leg set via Gamma.

    Best-effort: a Gamma error (rate-limit, transient) or an event that is
    non-negRisk / single-leg yields ``None`` and is skipped, never fatal.
    """
    try:
        events = await gamma.fetch_events_by_slug(slug)
    except EggplantError as e:
        logger.warning("sweep: Gamma resolve failed for %s, skipping (%s)", slug, e)
        return None

    event = next((e for e in events if e.slug == slug), events[0] if events else None)
    if event is None or not event.neg_risk:
        return None
    markets = event.markets or []
    ids = [m.market_ids() for m in markets]
    legs = convert_legs([mi for mi in ids if mi is not None])
    # negRisk convert/merge needs 2+ legs (the same gate the bot uses).
    if len(legs) < 2:
        return None
    return HeldEvent(slug=event.slug, title=event.title, legs=legs)


async def _discover(
    data: DataApiClient,
    gamma: GammaClient,
    wallet: str,
    opts: SweepOptions,
) -> tuple[list[Position], list[HeldEvent]]:
    """Fetch positions and resolve the distinct held negRisk events to their
    full leg sets (bounded-concurrency Gamma lookups, order preserved)."""
    positions = await data.all_positions(wallet, opts.min_shares)

    seen: set[str] = set()
    slugs: list[str] = []
    for p in positions:
        if not p.negative_risk or not p.event_slug:
            continue
        if opts.only_slug is not None and p.event_slug != opts.only_slug:
            continue
        if p.event_slug in seen:
            continue
        seen.add(p.event_slug)
        slugs.append(p.event_slug)

    sem = asyncio.Semaphore(max(opts.gamma_concurrency, 1))

    async def resolve(slug: str) -> HeldEvent | None:
        async with sem:
            return await _resolve_event(gamma, slug)

    resolved = await asyncio.gather(*(resolve(s) for s in slugs))
    events = [e for e in resolved if e is not None]
    return positions, events


def _classify(
    events: list[HeldEvent], positions: list[Position], delays: ConvertDelays
) -> list[SweepReport]:
    """Classify every held event from the discovered positions."""
    floor = _single_leg_min_qty(delays)
    return [
        SweepReport(
            slug=he.slug,
            title=he.title,
            classification=classify_event(he.legs, leg_sizes(he.legs, positions), floor),
        )
        for he in events
    ]


async def plan_sweep(
    data: DataApiClient,
    gamma: GammaClient,
    wallet: str,
    opts: SweepOptions | None = None,
) -> list[SweepReport]:
    """Discover the wallet's held negRisk events and report the merge/convert
    work each has — **submits nothing** (a dry run).

    Classification is from Data API sizes, so amounts are approximate; the
    authoritative amounts come from on-chain balances at :func:`sweep_all` time.
    """
    opts = opts or SweepOptions()
    positions, events = await _discover(data, gamma, wallet, opts)
    return _classify(events, positions, opts.delays)


async def sweep_all(
    signer: LocalSigner,
    relayer: RelayerClient,
    data: DataApiClient,
    gamma: GammaClient,
    rpc_url: str,
    wallet: str,
    opts: SweepOptions | None = None,
) -> SweepSummary:
    """Settle every actionable held negRisk event: merge YES+NO pairs and
    convert leftover NO, one event at a time.

    Discovers holdings, classifies them, and runs
    :func:`~eggplant_sdk.convert.process_job` over each actionable event
    **sequentially** — the wallet runs one relayer action at a time, and
    ``process_job`` re-reads on-chain balances at action time (so the API-size
    scan only decides *which* events to touch, never the amounts). Idempotent
    and resumable: a re-run simply finds less to do.

    Assumes the wallet's approvals are already in place — the collateral
    adapter must be an ERC-1155 operator for it (see :mod:`eggplant_sdk.approval`).
    """
    opts = opts or SweepOptions()
    positions, events = await _discover(data, gamma, wallet, opts)
    reports = _classify(events, positions, opts.delays)

    summary = SweepSummary(held_events=len(events))
    for he, report in zip(events, reports, strict=True):
        if not report.classification.actionable():
            continue
        summary.actionable += 1
        job = ConvertJob(slug=he.slug, legs=he.legs)
        try:
            detail = await process_job(job, signer, relayer, rpc_url, wallet, opts.delays)
            logger.info("sweep event settled: %s -> %s", he.slug, detail)
            summary.executed += 1
        except EggplantError as e:
            logger.warning("sweep event failed: %s (%s)", he.slug, e)
            summary.failed += 1

    return summary
