"""negRisk position operations through the relayer: **convert**, **merge**,
**redeem**, and **split**, plus a planning engine that turns raw balances
into minimal relayer submissions.

Layers, from pure to orchestrated:

1. **Calldata builders** (:func:`build_convert_calldata` and friends) — pure
   ABI encoding against the negRisk adapter / CTF, always available.
2. **Planner** (:func:`gas_chunks`, :func:`plan_calls`, :class:`ConvertTier`,
   :class:`EventPlan`) — pure decomposition of balances into an ordered,
   gas-budgeted call list.
3. **Engine** (:func:`plan_jobs`, :func:`process_jobs`,
   :func:`convert_worker`, the wrap helpers) — reads live balances over
   JSON-RPC, submits through :class:`~eggplant_sdk.relayer.RelayerClient`,
   retries wallet-busy, and wraps the freed USDC.e to pUSD.

The negRisk math in one line each: **merge** burns YES+NO on one leg for $1;
**convert** burns NO across ``k`` legs of one event, minting YES on the
complement and freeing ``(k−1)·amount``; **split** is merge's inverse
(collateral → YES+NO); **redeem** collects a resolved market's payout.

``question_id``/``condition_id`` are 32-byte values (``bytes``); token ids
and raw amounts are ``int`` (6-dp raw units).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from . import _rpc
from .chain import (
    COLLATERAL,
    COLLATERAL_ONRAMP,
    CTF,
    NEG_RISK_COLLATERAL_ADAPTER,
    POLYGON,
    USDC_E,
)
from .errors import EggplantError, InvalidDataError, RelayerQuotaExhaustedError
from .relayer import DepositWalletCall, RelayerClient
from .signer import LocalSigner

logger = logging.getLogger(__name__)

#: Max question ids per on-chain ``convertPositions`` index set.
#:
#: The proven per-call width; wider tiers are truncated with compensating
#: post-merges (see :class:`ConvertTier`). Distinct from the
#: calls-per-submission cap (:attr:`ConvertDelays.max_calls_per_submission`).
MAX_LEGS_PER_CONVERT = 20

#: Static merge gas estimate for chunk packing.
#:
#: Merges are cheap and flat; converts scale with the whole event (the
#: adapter splits collateral across every complement leg and merges a full
#: set across all of the event's conditions — see :func:`convert_gas`).
#: Weights carry margin: overestimating costs an extra submission;
#: underestimating gets the whole batch rejected as "would revert".
MERGE_GAS = 400_000


def convert_gas(n_event_legs: int) -> int:
    """Gas estimate for one convert on an event with ``n_event_legs`` legs."""
    return 700_000 + 175_000 * n_event_legs


@dataclass(frozen=True)
class ConvertDelays:
    """Tuning for the merge/convert cycle. The defaults are workable
    starting points; adjust to your relayer quota and event sizes."""

    #: Wait after a fill before starting the cycle (lets fills settle
    #: on-chain before the balance read), seconds.
    fill_to_convert: float = 5.0
    #: Wait for on-chain settlement after each relayer submission, seconds.
    settle: float = 5.0
    #: Max times to retry a submission while the relayer reports the wallet
    #: busy with a prior still-settling action.
    wallet_busy_max_retries: int = 10
    #: Minimum leftover-NO (raw, 6-dp) on a *single* leg before it's worth a
    #: convert. Below this, a lone NO leg is left in place rather than
    #: spending gas to mint YES dust for 0 USDC. Multi-leg converts ignore
    #: this.
    single_leg_min_qty_raw: int = 100_000
    #: Max ``DepositWallet`` calls packed into one relayer submission.
    max_calls_per_submission: int = 20
    #: Gas budget per relayer submission. Converts on big events are heavy;
    #: a batch that exceeds what one transaction can execute is rejected by
    #: the relayer's simulation as "would revert". Chunks pack greedily under
    #: this budget; a lone over-budget call still submits by itself.
    max_gas_per_submission: int = 8_000_000
    #: Fixed backoff after a failed cycle (anti-spin damp), seconds. The
    #: relayer's 429 ``resets in`` hint is deliberately ignored — it reports
    #: ~3600s while the quota actually frees in well under a minute.
    retry_backoff: float = 45.0


@dataclass
class ConvertLeg:
    """One market leg's on-chain identifiers, parsed and ready for
    planning."""

    question_id: bytes
    condition_id: bytes | None = None
    yes_token_id: int | None = None
    no_token_id: int = 0


@dataclass
class ConvertJob:
    """One event's convert work: its legs plus caller-side bookkeeping
    carried through the worker queue."""

    slug: str
    legs: list[ConvertLeg]
    #: Advisory amount the caller expected to free (the engine plans from
    #: live balances; this rides along for the caller's accounting).
    amount_raw: int = 0
    #: Prior failed attempts, echoed back on :class:`ConvertResult` so the
    #: caller can apply a retry cap across the queue round-trip.
    attempts: int = 0
    #: When the job was queued (``time.monotonic()``) — the anchor for the
    #: worker's fill-settle wait, so a job that aged in the queue isn't
    #: re-delayed in full.
    queued_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class MarketIds:
    """One market's identifiers as string-typed API data (the shapes Gamma
    and the Data API deliver them in)."""

    #: Hex, with or without ``0x``.
    question_id: str | None
    #: Hex, with or without ``0x``.
    condition_id: str | None
    #: Decimal token id.
    yes_token_id: str | None
    #: Decimal token id.
    no_token_id: str


def _parse_b256(raw: str) -> bytes | None:
    try:
        value = bytes.fromhex(raw.removeprefix("0x"))
    except ValueError:
        return None
    return value if len(value) == 32 else None


def convert_legs(markets: Iterable[MarketIds]) -> list[ConvertLeg]:
    """Parse :class:`ConvertLeg` out of string-typed market ids.

    Skips any market missing a parseable ``question_id`` or ``no_token_id``
    — unparseable legs are silently unplannable rather than fatal.
    """
    legs = []
    for market in markets:
        if market.question_id is None:
            continue
        question_id = _parse_b256(market.question_id)
        if question_id is None:
            continue
        try:
            no_token_id = int(market.no_token_id)
        except ValueError:
            continue
        yes_token_id = None
        if market.yes_token_id is not None:
            try:
                yes_token_id = int(market.yes_token_id)
            except ValueError:
                yes_token_id = None
        condition_id = _parse_b256(market.condition_id) if market.condition_id else None
        legs.append(
            ConvertLeg(
                question_id=question_id,
                condition_id=condition_id,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
            )
        )
    return legs


def market_id_from_question_id(question_id: bytes) -> bytes:
    """Derive the shared negRisk event ``marketId`` from any question ID.

    The last byte encodes the per-question index; zeroing it yields the
    shared event identifier.
    """
    return question_id[:31] + b"\x00"


def index_set(question_ids: list[bytes]) -> int:
    """The index-set bitmap for a set of question IDs.

    Each question ID's last byte is its position index within the event.
    """
    bits = 0
    for qid in question_ids:
        bits |= 1 << qid[31]
    return bits


def build_convert_calldata(question_ids: list[bytes], amount: int) -> bytes:
    """Calldata for ``NegRiskAdapter.convertPositions``: burn ``amount`` NO
    on every leg in ``question_ids``, minting ``amount`` YES on the event's
    complement."""
    if not question_ids:
        raise InvalidDataError("convert requires at least one question_id")
    market_id = market_id_from_question_id(question_ids[0])
    return _rpc.encode_call(
        "convertPositions(bytes32,uint256,uint256)",
        ["bytes32", "uint256", "uint256"],
        [market_id, index_set(question_ids), amount],
    )


def build_merge_calldata(condition_id: bytes, amount: int) -> bytes:
    """Calldata to merge ``amount`` YES+NO on ``condition_id`` back into
    ``amount`` collateral, through the pUSD
    :data:`~eggplant_sdk.chain.NEG_RISK_COLLATERAL_ADAPTER`.

    The adapter mirrors the CTF ABI, so this is the CTF-mirror
    ``mergePositions(collateralToken, parentCollectionId, conditionId,
    partition, amount)`` with pUSD collateral, a zero parent collection, and
    the binary ``[1, 2]`` partition — verified against a live UI merge (tx
    ``0xe92073a6…``). The legacy-style ``mergePositions(bytes32,uint256)``
    overload the old adapter took still exists in this adapter's bytecode but
    REVERTS.
    """
    return build_merge_calldata_ctf(COLLATERAL, condition_id, amount)


def build_split_calldata(condition_id: bytes, amount: int) -> bytes:
    """Calldata to split ``amount`` collateral into ``amount`` YES+NO on
    ``condition_id`` (merge's inverse), through the pUSD
    :data:`~eggplant_sdk.chain.NEG_RISK_COLLATERAL_ADAPTER` — the CTF-mirror
    ``splitPosition(collateralToken, parentCollectionId, conditionId,
    partition, amount)``.

    ⚠ The least-exercised call in this SDK, and the only one **not** verified
    against a live on-chain tx. The heavily exercised merge takes the exact
    symmetric CTF-mirror form through this same adapter, so this is the
    consistent shape — but split a dust amount first before trusting it with
    size. Splitting also requires the adapter to be approved to pull the
    wallet's pUSD collateral.
    """
    return build_split_calldata_ctf(COLLATERAL, condition_id, amount)


def build_split_calldata_ctf(collateral: str, condition_id: bytes, amount: int) -> bytes:
    """Calldata for the plain CTF ``splitPosition`` (non-negRisk markets):
    ``parentCollectionId`` zero and the binary partition ``[1, 2]``."""
    return _rpc.encode_call(
        "splitPosition(address,bytes32,bytes32,uint256[],uint256)",
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        [collateral, b"\x00" * 32, condition_id, [1, 2], amount],
    )


def build_merge_calldata_ctf(collateral: str, condition_id: bytes, amount: int) -> bytes:
    """Calldata for the plain CTF ``mergePositions`` (non-negRisk markets) —
    the symmetric inverse of :func:`build_split_calldata_ctf`."""
    return _rpc.encode_call(
        "mergePositions(address,bytes32,bytes32,uint256[],uint256)",
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        [collateral, b"\x00" * 32, condition_id, [1, 2], amount],
    )


def build_redeem_calldata(condition_id: bytes, yes_amount: int, no_amount: int) -> bytes:
    """Calldata to redeem a resolved negRisk condition through the pUSD
    :data:`~eggplant_sdk.chain.NEG_RISK_COLLATERAL_ADAPTER`.

    The adapter mirrors the CTF ABI, so this is the CTF-mirror
    ``redeemPositions(collateralToken, parentCollectionId, conditionId,
    indexSets)`` with pUSD collateral and a zero parent collection — verified
    against live on-chain redeems (tx ``0x2252f36d…`` used ``[1, 2]``,
    ``0x7bb402a9…`` used ``[1]``). Unlike the legacy
    ``redeemPositions(bytes32,uint256[])`` (which took explicit ``[yes, no]``
    amounts and REVERTS on this adapter), the CTF-mirror form takes **index
    sets** and redeems the caller's *full* balance of each: ``yes_amount`` /
    ``no_amount`` here only decide **which** sides to include (``> 0``), not how
    much — ``YES = index set 1`` (outcome slot 0), ``NO = index set 2`` (slot
    1). Only meaningful once the condition is resolved (an unresolved redeem
    reverts in the CTF); the caller gates on that and filters both-zero legs.
    """
    index_sets = []
    if yes_amount > 0:
        index_sets.append(1)
    if no_amount > 0:
        index_sets.append(2)
    return build_redeem_calldata_ctf(COLLATERAL, condition_id, index_sets)


def build_redeem_calldata_ctf(collateral: str, condition_id: bytes, index_sets: list[int]) -> bytes:
    """Calldata for the plain CTF ``redeemPositions`` (binary markets):
    ``parentCollectionId`` zero and the outcome ``index_sets`` to redeem
    (``[1, 2]`` covers both slots of a binary condition).

    Unlike :func:`build_redeem_calldata`, the CTF redeems the caller's *full*
    balance of the given index sets (no per-token amounts) and pays out in
    ``collateral`` — so pass the collateral token the condition was prepared
    with. Only meaningful once the condition is resolved.
    """
    return _rpc.encode_call(
        "redeemPositions(address,bytes32,bytes32,uint256[])",
        ["address", "bytes32", "bytes32", "uint256[]"],
        [collateral, b"\x00" * 32, condition_id, index_sets],
    )


def redeem_calls(redeems: list[tuple[bytes, int, int]]) -> list[DepositWalletCall]:
    """``DepositWallet`` calls to redeem resolved negRisk positions, one per
    condition.

    ``redeems`` is ``(condition_id, yes_amount, no_amount)``; every call
    targets the shared :data:`~eggplant_sdk.chain.NEG_RISK_COLLATERAL_ADAPTER`. The
    caller filters out legs with both amounts zero and gates on resolution.
    """
    return [
        DepositWalletCall(
            target=NEG_RISK_COLLATERAL_ADAPTER,
            data=build_redeem_calldata(cid, yes, no),
        )
        for cid, yes, no in redeems
    ]


def merge_calls(merges: list[tuple[bytes, int]]) -> list[DepositWalletCall]:
    """``DepositWallet`` calls to merge YES+NO pairs, one per condition.
    ``merges`` is ``(condition_id, amount)``."""
    return [
        DepositWalletCall(
            target=NEG_RISK_COLLATERAL_ADAPTER,
            data=build_merge_calldata(cid, amount),
        )
        for cid, amount in merges
    ]


def split_calls(splits: list[tuple[bytes, int]]) -> list[DepositWalletCall]:
    """``DepositWallet`` calls to split collateral into YES+NO on negRisk
    conditions, one per condition.

    ``splits`` is ``(condition_id, amount)``. Mirrors :func:`merge_calls`;
    see :func:`build_split_calldata` for the net-new caveat.
    """
    return [
        DepositWalletCall(
            target=NEG_RISK_COLLATERAL_ADAPTER,
            data=build_split_calldata(cid, amount),
        )
        for cid, amount in splits
    ]


def wrap_calls(wallet: str, amount: int, include_approve: bool) -> list[DepositWalletCall]:
    """Calldata for wrapping ``amount`` USDC.e into pUSD via the onramp, with
    an unlimited-approve call prepended when ``include_approve``."""
    calls = []
    if include_approve:
        calls.append(
            DepositWalletCall(
                target=USDC_E,
                data=_rpc.encode_call(
                    "approve(address,uint256)",
                    ["address", "uint256"],
                    [COLLATERAL_ONRAMP, (1 << 256) - 1],
                ),
            )
        )
    calls.append(
        DepositWalletCall(
            target=COLLATERAL_ONRAMP,
            data=_rpc.encode_call(
                "wrap(address,address,uint256)",
                ["address", "address", "uint256"],
                [USDC_E, wallet, amount],
            ),
        )
    )
    return calls


def fmt_usdc(raw: int) -> str:
    """Render a raw 6-dp USDC amount as a decimal string (e.g.
    ``12.345678``)."""
    return f"{raw // 1_000_000}.{raw % 1_000_000:06d}"


# ---------------------------------------------------------------------------
# The planner (pure)
# ---------------------------------------------------------------------------


@dataclass
class NoHolding:
    """One leg still holding NO after its YES+NO merge is netted out — the
    unit the tier planner works over."""

    question_id: bytes
    condition_id: bytes | None
    #: NO left after ``min(yes, no)`` is merged away (or merely offset, when
    #: the leg has no condition id to merge through).
    remaining_no: int


@dataclass
class ConvertTier:
    """One convert call: burn ``amount`` NO across ``question_ids``.

    ``post_merges`` are the compensating merges for members truncated past
    the per-call leg cap — the convert mints ``amount`` YES on every leg
    outside its index set, so merging that YES against a truncated member's
    still-held NO frees the same ``$amount`` the untruncated convert would
    have. They MUST execute after this tier's convert (they consume the YES
    it mints).
    """

    question_ids: list[bytes] = field(default_factory=list)
    amount: int = 0
    post_merges: list[tuple[bytes, int]] = field(default_factory=list)


@dataclass
class EventPlan:
    """Deterministic full merge/convert plan for one event, computed from a
    single balance snapshot."""

    #: Pre-convert YES+NO merges, ``(condition_id, amount)``.
    merges: list[tuple[bytes, int]] = field(default_factory=list)
    #: Convert tiers in execution order.
    tiers: list[ConvertTier] = field(default_factory=list)
    #: Exact USDC.e (raw, 6-dp) the plan frees: each merge frees its amount,
    #: a convert across ``k`` legs frees ``(k−1)·amount``.
    proceeds: int = 0
    #: The event's total leg count — drives the per-convert gas estimate
    #: (the adapter's convert cost scales with the whole event, not the
    #: index set).
    n_legs: int = 0


@dataclass
class PlannedCall:
    """One planned ``DepositWallet`` call plus its gas estimate for chunk
    packing."""

    call: DepositWalletCall
    gas: int


@dataclass
class WrapSnapshot:
    """Pre-submission USDC.e snapshot driving the folded wrap."""

    balance: int
    allowance: int


def classify_event(
    legs: list[ConvertLeg], pairs: list[tuple[int, int]]
) -> tuple[list[tuple[bytes, int]], list[NoHolding]]:
    """Classify per-leg ``(no, yes)`` balances into pre-convert merges and
    the remaining-NO holdings the tier planner works over.

    A leg holding both sides merges ``min(yes, no)`` when it has a condition
    id; either way the NO left over is ``no − min(yes, no)`` — without a
    condition id the YES can't merge but still offsets that much NO (it is
    never converted).
    """
    merges: list[tuple[bytes, int]] = []
    holdings: list[NoHolding] = []

    for leg, (no_bal, yes_bal) in zip(legs, pairs, strict=False):
        if yes_bal > 0 and no_bal > 0 and leg.condition_id is not None:
            merges.append((leg.condition_id, min(yes_bal, no_bal)))

        remaining_no = no_bal - min(yes_bal, no_bal)
        if remaining_no > 0:
            holdings.append(
                NoHolding(
                    question_id=leg.question_id,
                    condition_id=leg.condition_id,
                    remaining_no=remaining_no,
                )
            )
    return merges, holdings


def plan_tiers(
    holdings: list[NoHolding], single_leg_min_qty_raw: int, max_legs: int
) -> list[ConvertTier]:
    """Decompose remaining-NO holdings into convert tiers.

    Distinct holding sizes ``L1 < L2 < …`` define the tiers: tier 1 converts
    ``L1`` across every NO-holding leg, tier 2 converts ``L2 − L1`` across
    the legs holding more than ``L1``, and so on — the fixpoint a
    read→convert→re-read loop converges to, but computable from one balance
    snapshot because conditional-token math is deterministic.

    A tier wider than ``max_legs`` is truncated to its first ``max_legs``
    members (input order): the convert then mints ``amount`` YES on each
    truncated-out member, so cid-bearing ones get a compensating post-merge
    freeing the same ``$amount`` the untruncated convert would have.

    A lone-member tier is necessarily the last (membership shrinks as levels
    rise); converting it frees 0 USDC, so it is kept for position hygiene
    only when ``amount ≥ single_leg_min_qty_raw``.
    """
    levels = sorted({h.remaining_no for h in holdings})

    tiers: list[ConvertTier] = []
    prev = 0
    for level in levels:
        amount = level - prev
        prev = level
        members = [h for h in holdings if h.remaining_no >= level]
        if len(members) == 1 and amount < single_leg_min_qty_raw:
            break
        tiers.append(
            ConvertTier(
                question_ids=[h.question_id for h in members[:max_legs]],
                amount=amount,
                post_merges=[
                    (h.condition_id, amount)
                    for h in members[max_legs:]
                    if h.condition_id is not None
                ],
            )
        )
    return tiers


def plan_event(merges: list[tuple[bytes, int]], tiers: list[ConvertTier], n_legs: int) -> EventPlan:
    """Assemble an event's plan and compute the exact USDC.e it frees.
    ``n_legs`` is the event's total leg count (drives the per-convert gas
    estimate)."""
    proceeds = sum(amount for _, amount in merges)
    for tier in tiers:
        proceeds += max(len(tier.question_ids) - 1, 0) * tier.amount
        proceeds += sum(amount for _, amount in tier.post_merges)
    return EventPlan(merges=merges, tiers=tiers, proceeds=proceeds, n_legs=n_legs)


def plan_event_from_balances(
    legs: list[ConvertLeg], pairs: list[tuple[int, int]], single_leg_min_qty_raw: int
) -> EventPlan:
    """Compute an event's plan from a balance snapshot: per-leg ``(no, yes)``
    balance pairs in leg order (the pure entry into the planner for callers
    bringing their own balance source)."""
    merges, holdings = classify_event(legs, pairs)
    tiers = plan_tiers(holdings, single_leg_min_qty_raw, MAX_LEGS_PER_CONVERT)
    return plan_event(merges, tiers, len(legs))


def plan_calls(plans: list[EventPlan]) -> list[PlannedCall]:
    """Flatten per-event plans into one ordered, gas-weighted merge/convert
    call list.

    Order is load-bearing: each event's merges precede its converts (tier
    amounts assume post-merge balances), and each tier's post-merges directly
    follow that tier's convert (they consume the YES it mints). Calls execute
    in order inside a relayer submission, and chunked submissions are
    serialized, so the order survives chunking.

    Every call here is also individually valid against the *pre-batch*
    balance snapshot. Two batch shapes are known to be rejected by the
    relayer's pre-submission simulation ("batch would revert"), and both are
    avoided here:

    - an in-batch wrap sized to include the batch's proceeds (the wrap
      instead goes out as its own balance-read-sized submission after the
      batch lands);
    - a batch whose summed gas exceeds what one transaction can execute
      (hence the gas weights here and the budget chunking in
      :func:`gas_chunks`).
    """
    calls: list[PlannedCall] = []
    for plan in plans:
        for call in merge_calls(plan.merges):
            calls.append(PlannedCall(call=call, gas=MERGE_GAS))
        for tier in plan.tiers:
            calls.append(
                PlannedCall(
                    call=DepositWalletCall(
                        target=NEG_RISK_COLLATERAL_ADAPTER,
                        data=build_convert_calldata(tier.question_ids, tier.amount),
                    ),
                    gas=convert_gas(plan.n_legs),
                )
            )
            for call in merge_calls(tier.post_merges):
                calls.append(PlannedCall(call=call, gas=MERGE_GAS))
    return calls


def gas_chunks(planned: list[PlannedCall], max_calls: int, max_gas: int) -> list[range]:
    """Greedy contiguous chunking under both a call-count cap and a gas
    budget.

    Returns index ranges into the planned-call list; concatenated they cover
    it exactly, preserving order (which is load-bearing — see
    :func:`plan_calls`). A single call over the gas budget still gets a chunk
    of its own: calls are atomic, and the relayer historically accepted lone
    big-event converts.
    """
    max_calls = max(max_calls, 1)
    ranges: list[range] = []
    start = 0
    gas = 0
    for i, p in enumerate(planned):
        count = i - start
        if count > 0 and (count >= max_calls or gas + p.gas > max_gas):
            ranges.append(range(start, i))
            start = i
            gas = 0
        gas += p.gas
    if start < len(planned):
        ranges.append(range(start, len(planned)))
    return ranges


# ---------------------------------------------------------------------------
# The engine: live balance reads + relayer orchestration
# ---------------------------------------------------------------------------


@dataclass
class ConvertResult:
    """Outcome of one convert job, echoed per consumed job."""

    slug: str
    amount_raw: int
    n_legs: int
    success: bool
    #: Set when the failure was relayer quota exhaustion — retry these on a
    #: fixed backoff (they self-terminate once the quota frees); cap retries
    #: on non-quota failures.
    quota_blocked: bool
    #: Echo of the job's ``attempts`` for the caller's retry cap.
    attempts: int
    detail: str


def _fill_residual(batch: list[ConvertJob], now: float, fill_delay: float) -> float:
    """Remaining fill-settle wait for the youngest job in the batch:
    ``fill_delay`` measured from each job's ``queued_at``, so a job that
    already aged in the queue doesn't wait the full delay over again."""
    if not batch:
        return 0.0
    return max(max(fill_delay - (now - job.queued_at), 0.0) for job in batch)


def _dedup_by_slug(batch: list[ConvertJob]) -> list[ConvertJob]:
    """First occurrence per slug wins (same slug ⇒ same legs); input order
    kept."""
    seen: set[str] = set()
    return [job for job in batch if not (job.slug in seen or seen.add(job.slug))]


async def convert_worker(
    queue: asyncio.Queue[ConvertJob],
    signer: LocalSigner,
    relayer: RelayerClient,
    rpc_url: str,
    proxy_wallet: str,
    result_queue: asyncio.Queue[ConvertResult],
    delays: ConvertDelays | None = None,
) -> None:
    """Background merge/convert worker: drains queued jobs into one coalesced
    cycle per pass.

    Each pass waits out the youngest job's fill-settle delay, drains whatever
    else queued meanwhile (a burst of fills becomes one shared cycle instead
    of N serialized ones), dedups by slug, and runs :func:`process_jobs` over
    the distinct events. Exactly one :class:`ConvertResult` is put per
    *consumed* job — duplicates included — so callers that count queued jobs
    and decrement per result never stall on a missing result.
    """
    delays = delays if delays is not None else ConvertDelays()
    logger.info("convert worker started (wallet=%s)", proxy_wallet)

    while True:
        first = await queue.get()
        batch = [first]
        await asyncio.sleep(_fill_residual(batch, time.monotonic(), delays.fill_to_convert))
        # Single drain: jobs that arrived during the wait join this cycle;
        # later arrivals start the next pass immediately after this one
        # resolves.
        while True:
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        # Just-drained jobs may be younger than the fill delay; top up so
        # every job's fills have settled on-chain before the balance read.
        await asyncio.sleep(_fill_residual(batch, time.monotonic(), delays.fill_to_convert))

        uniques = _dedup_by_slug(batch)
        logger.info("convert cycle starting (%d jobs, %d events)", len(batch), len(uniques))
        try:
            detail = await process_jobs(uniques, signer, relayer, rpc_url, proxy_wallet, delays)
        except RelayerQuotaExhaustedError as e:
            # The relayer's hint is unreliable — logged for forensics only;
            # the caller retries on the fixed backoff, and this sleep is an
            # anti-spin damp.
            logger.warning("relayer quota exhausted (resets_in_secs=%d)", e.resets_in_secs)
            for job in batch:
                await result_queue.put(
                    ConvertResult(
                        slug=job.slug,
                        amount_raw=job.amount_raw,
                        n_legs=len(job.legs),
                        success=False,
                        quota_blocked=True,
                        attempts=job.attempts,
                        detail="quota exhausted",
                    )
                )
            await asyncio.sleep(delays.retry_backoff)
        except EggplantError as e:
            logger.error("convert cycle failed: %s", e)
            for job in batch:
                await result_queue.put(
                    ConvertResult(
                        slug=job.slug,
                        amount_raw=job.amount_raw,
                        n_legs=len(job.legs),
                        success=False,
                        quota_blocked=False,
                        attempts=job.attempts,
                        detail=str(e),
                    )
                )
            await asyncio.sleep(delays.retry_backoff)
        else:
            logger.info("convert cycle complete: %s", detail)
            for job in batch:
                await result_queue.put(
                    ConvertResult(
                        slug=job.slug,
                        amount_raw=job.amount_raw,
                        n_legs=len(job.legs),
                        success=True,
                        quota_blocked=False,
                        attempts=job.attempts,
                        detail=detail,
                    )
                )


async def _submit_and_settle(
    signer: LocalSigner,
    relayer: RelayerClient,
    wallet: str,
    calls: list[DepositWalletCall],
    label: str,
    settle: float,
) -> str:
    response = await relayer.submit_deposit_wallet_batch(signer, wallet, POLYGON, calls)
    logger.info("%s submitted (tx_id=%s), waiting for settlement", label, response.transaction_id)
    await asyncio.sleep(settle)
    return response.transaction_id


async def submit_and_settle_with_busy_retry(
    signer: LocalSigner,
    relayer: RelayerClient,
    wallet: str,
    calls: list[DepositWalletCall],
    label: str,
    settle: float,
    max_retries: int,
) -> str:
    """Submit a relayer batch and wait for settlement, retrying while the
    relayer reports the wallet busy.

    A "wallet busy: active action exists" rejection means a prior action for
    this wallet is still settling and *nothing was submitted*, so retrying is
    safe — there is no double-submission risk. The condition clears once the
    prior action lands, so wait ``settle`` and try again, up to
    ``max_retries`` times. Any non-busy error propagates immediately.
    """
    attempt = 0
    while True:
        try:
            return await _submit_and_settle(signer, relayer, wallet, calls, label, settle)
        except EggplantError as e:
            if e.is_wallet_busy() and attempt < max_retries:
                attempt += 1
                logger.warning(
                    "%s deferred: wallet busy, waiting %.0fs then retrying (%d/%d)",
                    label,
                    settle,
                    attempt,
                    max_retries,
                )
                await asyncio.sleep(settle)
                continue
            raise


def _batch_token_ids(legs: list[ConvertLeg]) -> list[int]:
    """Flatten the legs' token ids into one ``balanceOfBatch`` query: per
    leg, the NO token id, then the YES token id when known.
    :func:`_unpack_balances` walks the same layout back out."""
    ids = []
    for leg in legs:
        ids.append(leg.no_token_id)
        if leg.yes_token_id is not None:
            ids.append(leg.yes_token_id)
    return ids


def _token_count(legs: list[ConvertLeg]) -> int:
    """Number of token ids :func:`_batch_token_ids` emits for these legs."""
    return sum(1 + (leg.yes_token_id is not None) for leg in legs)


def _unpack_balances(legs: list[ConvertLeg], balances: list[int]) -> list[tuple[int, int]]:
    """Inverse of :func:`_batch_token_ids`: split the flat balance array back
    into per-leg ``(no, yes)`` pairs. Legs without a YES token id get a zero
    YES balance. Errors when the array length doesn't match the queried
    layout."""
    cursor = iter(balances)
    pairs = []
    try:
        for leg in legs:
            no_bal = next(cursor)
            yes_bal = next(cursor) if leg.yes_token_id is not None else 0
            pairs.append((no_bal, yes_bal))
    except StopIteration:
        raise InvalidDataError("balanceOfBatch returned fewer balances than queried") from None
    if next(cursor, None) is not None:
        raise InvalidDataError("balanceOfBatch returned more balances than queried")
    return pairs


def _unpack_multi(jobs: list[ConvertJob], balances: list[int]) -> list[list[tuple[int, int]]]:
    """Split one combined multi-job ``balanceOfBatch`` result back into
    per-job, per-leg ``(no, yes)`` pairs (jobs were concatenated in
    order)."""
    out = []
    offset = 0
    for job in jobs:
        n = _token_count(job.legs)
        chunk = balances[offset : offset + n]
        if len(chunk) < n:
            raise InvalidDataError("balanceOfBatch returned fewer balances than queried")
        out.append(_unpack_balances(job.legs, chunk))
        offset += n
    if offset != len(balances):
        raise InvalidDataError("balanceOfBatch returned more balances than queried")
    return out


async def _read_balances_multi(
    jobs: list[ConvertJob], rpc_url: str, wallet: str
) -> list[list[tuple[int, int]]]:
    """Read every job's CTF balances in a single ``balanceOfBatch``
    ``eth_call`` (same-block consistent across all events, one billed RPC
    call total).

    Propagates RPC errors instead of masking them as zero balances: an
    ERC1155 read returns 0 for an unheld token, so a masked *failed read*
    would silently skip converting a real position and look like "nothing to
    do".
    """
    ids: list[int] = []
    for job in jobs:
        ids.extend(_batch_token_ids(job.legs))
    if not ids:
        return [[] for _ in jobs]

    balances = await _rpc.erc1155_balance_of_batch(rpc_url, CTF, [wallet] * len(ids), ids)
    return _unpack_multi(jobs, balances)


async def _read_wrap_snapshot(rpc_url: str, wallet: str) -> WrapSnapshot:
    """Read the wallet's USDC.e balance and onramp allowance — the folded
    wrap's inputs."""
    balance, allowance = await asyncio.gather(
        _rpc.erc20_balance_of(rpc_url, USDC_E, wallet),
        _rpc.erc20_allowance(rpc_url, USDC_E, wallet, COLLATERAL_ONRAMP),
    )
    return WrapSnapshot(balance=balance, allowance=allowance)


async def plan_jobs(
    jobs: list[ConvertJob], rpc_url: str, wallet: str, single_leg_min_qty_raw: int
) -> tuple[list[EventPlan], WrapSnapshot]:
    """Read live balances and produce per-job :class:`EventPlan` plus the
    wrap snapshot — submits nothing. Drives :func:`process_jobs` and
    read-only planning reports."""
    balances, snapshot = await asyncio.gather(
        _read_balances_multi(jobs, rpc_url, wallet),
        _read_wrap_snapshot(rpc_url, wallet),
    )
    plans = [
        plan_event_from_balances(job.legs, pairs, single_leg_min_qty_raw)
        for job, pairs in zip(jobs, balances, strict=True)
    ]
    return plans, snapshot


async def process_jobs(
    jobs: list[ConvertJob],
    signer: LocalSigner,
    relayer: RelayerClient,
    rpc_url: str,
    wallet: str,
    delays: ConvertDelays | None = None,
) -> str:
    """Run the full merge→convert→wrap cycle for one or more events in as
    few relayer submissions as possible.

    One balance snapshot plans everything (:func:`plan_jobs`); every event's
    merges and convert tiers ride a single relayer submission when they fit,
    followed by exactly one settle wait, then one balance-read-sized wrap
    submission. The wrap cannot ride the batch: the relayer's pre-submission
    simulation evaluates each call against pre-batch state, where the
    proceeds don't exist yet. A failed submission (other than quota
    exhaustion) re-reads balances and rebuilds, absorbing mid-flight balance
    changes.
    """
    delays = delays if delays is not None else ConvertDelays()
    # Re-plans after a failed submission round (fresh balance read each time).
    rebuild_attempts = 2

    last_err: EggplantError | None = None
    for attempt in range(rebuild_attempts + 1):
        if attempt > 0:
            # A failed round may have landed earlier chunks that haven't
            # settled yet; re-reading too soon would re-plan (and re-submit)
            # their calls, which would then revert on-chain against the
            # already-burned balances.
            await asyncio.sleep(delays.settle)

        try:
            plans, snapshot = await plan_jobs(jobs, rpc_url, wallet, delays.single_leg_min_qty_raw)
            planned = plan_calls(plans)
            if not planned:
                # USDC.e already sitting in the wallet still gets wrapped.
                if snapshot.balance > 0:
                    await _wrap_best_effort(signer, relayer, rpc_url, wallet, delays, "leftover")
                    return f"no-op: wrapped leftover {fmt_usdc(snapshot.balance)} USDC.e"
                return "no-op: nothing to merge/convert/wrap"

            merges = sum(len(p.merges) + sum(len(t.post_merges) for t in p.tiers) for p in plans)
            converts = sum(len(p.tiers) for p in plans)
            proceeds = sum(p.proceeds for p in plans)
            gas_estimate = sum(p.gas for p in planned)
            ranges = gas_chunks(
                planned, delays.max_calls_per_submission, delays.max_gas_per_submission
            )
            n_chunks = len(ranges)
            logger.info(
                "convert cycle planned: events=%d merges=%d converts=%d proceeds=%s "
                "gas_estimate=%d chunks=%d attempt=%d",
                len(jobs),
                merges,
                converts,
                fmt_usdc(proceeds),
                gas_estimate,
                n_chunks,
                attempt,
            )

            calls = [p.call for p in planned]
            tx_id = ""
            for i, chunk_range in enumerate(ranges):
                label = f"cycle chunk {i + 1}/{n_chunks}"
                tx_id = await submit_and_settle_with_busy_retry(
                    signer,
                    relayer,
                    wallet,
                    calls[chunk_range.start : chunk_range.stop],
                    label,
                    delays.settle,
                    delays.wallet_busy_max_retries,
                )

            # Wrap the freed USDC.e in its own submission, sized by a fresh
            # balance read (the last chunk's settle wait has elapsed, so the
            # read sees the proceeds plus any pre-existing leftover).
            await _wrap_best_effort(signer, relayer, rpc_url, wallet, delays, "post-batch")

            return (
                f"events={len(jobs)} merges={merges} converts={converts} "
                f"proceeds={fmt_usdc(proceeds)} chunks={n_chunks} tx={tx_id}"
            )
        except RelayerQuotaExhaustedError:
            # The caller sleeps out the quota reset; a salvage wrap submission
            # would just burn another rejected request against the same quota.
            raise
        except EggplantError as e:
            logger.warning(
                "convert round failed; re-reading and rebuilding (attempt=%d): %s", attempt, e
            )
            last_err = e

    # Every round failed. Salvage: wrap whatever USDC.e the chunks that did
    # land freed, so it isn't stranded until the next cycle.
    await _wrap_best_effort(signer, relayer, rpc_url, wallet, delays, "salvage")
    raise last_err if last_err else InvalidDataError("convert cycle failed")


async def _wrap_best_effort(
    signer: LocalSigner,
    relayer: RelayerClient,
    rpc_url: str,
    wallet: str,
    delays: ConvertDelays,
    context: str,
) -> None:
    """Best-effort wrap of whatever USDC.e the wallet holds. A failure is
    logged, never fatal: the merges/converts that freed the cash already
    landed on-chain, and unwrapped USDC.e is picked up by the next cycle's
    leftover wrap."""
    try:
        await wrap_with_busy_retry(
            signer, relayer, rpc_url, wallet, delays.wallet_busy_max_retries, delays.settle
        )
    except EggplantError as e:
        logger.warning("%s wrap failed: %s", context, e)


async def process_job(
    job: ConvertJob,
    signer: LocalSigner,
    relayer: RelayerClient,
    rpc_url: str,
    wallet: str,
    delays: ConvertDelays | None = None,
) -> str:
    """Single-event wrapper around :func:`process_jobs`."""
    return await process_jobs([job], signer, relayer, rpc_url, wallet, delays)


async def process_merge(
    merges: list[tuple[bytes, int]],
    signer: LocalSigner,
    relayer: RelayerClient,
    wallet: str,
) -> str:
    """Submit a batch of YES+NO merges, returning the transaction id."""
    calls = merge_calls(merges)
    response = await relayer.submit_deposit_wallet_batch(signer, wallet, POLYGON, calls)
    return response.transaction_id


async def process_convert(
    question_ids: list[bytes],
    amount_raw: int,
    signer: LocalSigner,
    relayer: RelayerClient,
    wallet: str,
    settle: float,
) -> str:
    """Submit a convert of NO positions and wait for settlement, returning
    the transaction id. Wrapping the freed USDC.e is left to the caller (via
    :func:`wrap_with_busy_retry`), mirroring :func:`process_merge`."""
    convert_data = build_convert_calldata(question_ids, amount_raw)
    return await _submit_and_settle(
        signer,
        relayer,
        wallet,
        [DepositWalletCall(target=NEG_RISK_COLLATERAL_ADAPTER, data=convert_data)],
        "convert",
        settle,
    )


async def wrap_usdc_e(
    signer: LocalSigner, relayer: RelayerClient, rpc_url: str, wallet: str
) -> None:
    """Wrap the wallet's whole USDC.e balance into pUSD, including an
    unlimited approve when the onramp allowance falls short."""
    snapshot = await _read_wrap_snapshot(rpc_url, wallet)

    if snapshot.balance == 0:
        logger.info("no USDC.e to wrap")
        return

    include_approve = snapshot.allowance < snapshot.balance
    if include_approve:
        logger.info("including USDC approve in wrap batch (balance=%d)", snapshot.balance)
    logger.info("wrapping %s USDC.e to pUSD", fmt_usdc(snapshot.balance))
    calls = wrap_calls(wallet, snapshot.balance, include_approve)

    response = await relayer.submit_deposit_wallet_batch(signer, wallet, POLYGON, calls)
    logger.info("wrap submitted (tx_id=%s)", response.transaction_id)


async def wrap_with_busy_retry(
    signer: LocalSigner,
    relayer: RelayerClient,
    rpc_url: str,
    wallet: str,
    max_retries: int,
    retry_wait: float,
) -> None:
    """Wrap USDC.e → pUSD after a merge/convert, retrying while the wallet is
    busy.

    The merge/convert just submitted can still be settling when the wrap
    goes out, and the relayer rejects the concurrent action with "wallet
    busy". The condition clears once the prior action lands on-chain, so
    wait ``retry_wait`` and try again, up to ``max_retries`` times.
    """
    attempt = 0
    while True:
        try:
            await wrap_usdc_e(signer, relayer, rpc_url, wallet)
            return
        except EggplantError as e:
            if e.is_wallet_busy() and attempt < max_retries:
                attempt += 1
                logger.warning(
                    "wrap deferred: wallet busy, waiting %.0fs then retrying (%d/%d)",
                    retry_wait,
                    attempt,
                    max_retries,
                )
                await asyncio.sleep(retry_wait)
                continue
            raise
