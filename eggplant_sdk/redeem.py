"""Redeem every resolved position a wallet holds, draining the redeemable set
to empty in gas-bounded relayer batches.

:meth:`~eggplant_sdk.data.DataApiClient.all_redeemable_positions` reports which
of a wallet's positions have resolved and can be redeemed; :func:`redeem_all`
collects them all and dispatches each to the right on-chain redeem call for its
market shape — a multi-outcome condition through the adapter with its exact
held amounts, a binary condition through the CTF with its outcome index sets.
Both need the wallet's on-chain balances, read here in one batched
``balanceOfBatch`` per pass (the Data API's ``size`` is a float and would
misround the raw unit) — to size the adapter redeems, and to skip conditions
the wallet no longer holds. A too-high amount, or a wrong outcome order,
reverts on-chain, so a mis-built call fails safe instead of losing funds.

Redeeming is idempotent: a redeemed position drops out of the ``redeemable``
filter, so a re-fetch never returns it twice. That, plus the Data API's offset
cap (see :mod:`eggplant_sdk.data`), is why :func:`redeem_all` drains in passes —
redeem a page-set, re-fetch the now-smaller set, and repeat until it is empty.

Layers mirror :mod:`eggplant_sdk.convert`: the pure grouping
(:func:`group_redeemable`, :func:`build_redemptions`) has no dependencies; the
live engine (:func:`redeem_all`, :func:`plan_redeem`) reads balances over
JSON-RPC.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from . import _rpc
from .chain import CTF, NEG_RISK_ADAPTER, POLYGON, contract_config
from .convert import (
    build_redeem_calldata,
    build_redeem_calldata_ctf,
    submit_and_settle_with_busy_retry,
)
from .data import DataApiClient, Position
from .errors import EggplantError
from .relayer import DepositWalletCall, RelayerClient
from .signer import LocalSigner

logger = logging.getLogger(__name__)

#: How many token balances to read per ``balanceOfBatch`` ``eth_call``.
_BALANCE_READ_CHUNK = 500

#: Both outcome slots of a binary condition; the CTF redeems whatever balance
#: is held of each.
_BINARY_INDEX_SETS = [1, 2]


@dataclass
class RedeemGroup:
    """One resolved condition's held outcome tokens, grouped from redeemable
    positions.

    ``yes_token`` / ``no_token`` are the held ERC-1155 token ids (``None`` ⇒
    that side isn't held). ``neg_risk`` records which redeem path the condition
    takes: the adapter (multi-outcome) or the CTF (binary).
    """

    condition_id: bytes
    yes_token: int | None = None
    no_token: int | None = None
    neg_risk: bool = False

    def token_ids(self) -> list[int]:
        """The held token ids in this group (for the balance read)."""
        return [t for t in (self.yes_token, self.no_token) if t is not None]


@dataclass
class Redemption:
    """One condition ready to redeem: the held YES/NO amounts (raw 6-dp) and
    which redeem path it takes."""

    condition_id: bytes
    yes_amount: int
    no_amount: int
    neg_risk: bool


def _parse_b256(raw: str) -> bytes | None:
    try:
        value = bytes.fromhex(raw.removeprefix("0x"))
    except ValueError:
        return None
    return value if len(value) == 32 else None


def group_redeemable(positions: list[Position]) -> list[RedeemGroup]:
    """Group redeemable positions by condition id, extracting the held YES/NO
    token ids and tagging each condition's redeem path.

    Rows with an unparseable condition id or token id are dropped; first-seen
    condition order is preserved.
    """
    groups: dict[bytes, RedeemGroup] = {}
    for position in positions:
        cid = _parse_b256(position.condition_id)
        if cid is None:
            continue
        try:
            asset = int(position.asset)
        except ValueError:
            continue
        group = groups.get(cid)
        if group is None:
            group = RedeemGroup(condition_id=cid, neg_risk=position.negative_risk)
            groups[cid] = group
        if position.outcome.lower() == "yes":
            group.yes_token = asset
        elif position.outcome.lower() == "no":
            group.no_token = asset
    return list(groups.values())


def build_redemptions(
    groups: list[RedeemGroup], balances: dict[int, int], min_raw: int
) -> list[Redemption]:
    """Pair grouped positions with their on-chain balances into
    :class:`Redemption` records.

    ``balances`` is keyed by token id (as read from ``balanceOfBatch``). A
    condition that nets to zero on-chain (a redeemed-but-lagging API row) or
    holds less than ``min_raw`` (raw 6-dp) in total is dropped.
    """
    redemptions: list[Redemption] = []
    for group in groups:
        yes = balances.get(group.yes_token, 0) if group.yes_token is not None else 0
        no = balances.get(group.no_token, 0) if group.no_token is not None else 0
        total = yes + no
        if total == 0 or total < min_raw:
            continue
        redemptions.append(
            Redemption(
                condition_id=group.condition_id,
                yes_amount=yes,
                no_amount=no,
                neg_risk=group.neg_risk,
            )
        )
    return redemptions


# ---------------------------------------------------------------------------
# The engine: live balance reads + relayer orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RedeemOptions:
    """Knobs for :func:`redeem_all`. The defaults are a workable starting
    point; adjust to your relayer quota."""

    #: Skip a condition whose total held (yes+no, raw 6-dp) is below this.
    min_shares_raw: int = 0
    #: ``DepositWallet`` calls (one per condition) per relayer submission.
    batch_size: int = 20
    #: Stop after this many conditions across the whole call (``0`` = no cap).
    max_conditions: int = 0
    #: Wait for on-chain settlement after each submission, seconds.
    settle: float = 5.0
    #: Retries while the relayer reports the wallet busy.
    wallet_busy_max_retries: int = 10
    #: Whole-batch resubmits on a transient relayer/RPC failure before the
    #: batch is skipped (counted failed), so one wedged batch can't stall the
    #: drain.
    batch_max_retries: int = 5
    #: Collateral token a binary condition pays out in (the token it was
    #: prepared with). ``None`` uses the venue default. A wrong value is a
    #: fail-safe no-op — the redeem finds no matching balance — so override it
    #: for markets collateralized in a different token.
    ctf_collateral: str | None = None


@dataclass
class RedeemSummary:
    """What :func:`redeem_all` did."""

    #: Conditions redeemed (submitted successfully).
    redeemed: int = 0
    #: Conditions whose batch failed after retries and was skipped.
    failed: int = 0
    #: Drain passes run.
    passes: int = 0


def _venue_collateral() -> str:
    """The venue's default collateral token (what current markets pay out
    in)."""
    config = contract_config(POLYGON, False)
    return config.collateral if config is not None else "0x" + "00" * 20


def _build_calls(redemptions: list[Redemption], ctf_collateral: str) -> list[DepositWalletCall]:
    """Build the relayer calls for a set of redemptions, routing each to the
    adapter or the CTF by its redeem path."""
    calls: list[DepositWalletCall] = []
    for r in redemptions:
        if r.neg_risk:
            calls.append(
                DepositWalletCall(
                    target=NEG_RISK_ADAPTER,
                    data=build_redeem_calldata(r.condition_id, r.yes_amount, r.no_amount),
                )
            )
        else:
            calls.append(
                DepositWalletCall(
                    target=CTF,
                    data=build_redeem_calldata_ctf(
                        ctf_collateral, r.condition_id, _BINARY_INDEX_SETS
                    ),
                )
            )
    return calls


async def _read_balances(rpc_url: str, wallet: str, ids: list[int]) -> dict[int, int]:
    """Read ``balanceOf`` for every token id, chunked into bounded
    ``balanceOfBatch`` calls."""
    out: dict[int, int] = {}
    for start in range(0, len(ids), _BALANCE_READ_CHUNK):
        chunk = ids[start : start + _BALANCE_READ_CHUNK]
        balances = await _rpc.erc1155_balance_of_batch(rpc_url, CTF, [wallet] * len(chunk), chunk)
        out.update(zip(chunk, balances, strict=True))
    return out


async def _read_and_plan(
    positions: list[Position], rpc_url: str, wallet: str, min_raw: int
) -> list[Redemption]:
    """Group ``positions``, read their exact on-chain balances, and build the
    redemptions — the read half shared by :func:`plan_redeem` and
    :func:`redeem_all`."""
    groups = group_redeemable(positions)
    ids = [tid for group in groups for tid in group.token_ids()]
    balances = await _read_balances(rpc_url, wallet, ids)
    return build_redemptions(groups, balances, min_raw)


async def plan_redeem(
    data: DataApiClient, rpc_url: str, wallet: str, min_shares_raw: int = 0
) -> tuple[list[Redemption], bool]:
    """Preview one page-set: the redemptions :func:`redeem_all` would submit,
    without submitting anything.

    Fetches one page-set of redeemable positions and reads their balances.
    ``hit_cap`` is ``True`` when more redeemable positions remain beyond the
    Data API's offset cap (a full :func:`redeem_all` run drains them across
    passes).
    """
    positions, hit_cap = await data.all_redeemable_positions(wallet)
    redemptions = await _read_and_plan(positions, rpc_url, wallet, min_shares_raw)
    return redemptions, hit_cap


async def redeem_all(
    signer: LocalSigner,
    relayer: RelayerClient,
    data: DataApiClient,
    rpc_url: str,
    wallet: str,
    opts: RedeemOptions | None = None,
) -> RedeemSummary:
    """Redeem every resolved position ``wallet`` holds, draining the
    redeemable set to empty (or until ``max_conditions``).

    Each pass fetches one page-set of redeemable positions
    (:meth:`~eggplant_sdk.data.DataApiClient.all_redeemable_positions`), reads
    their exact balances, submits the redeems in ``batch_size`` chunks, then
    re-fetches the now-smaller set. It stops when the set is empty, a pass
    makes no progress (every batch failed), or the cap is reached. Idempotent
    and resumable — safe to re-run.

    Assumes the wallet's approvals are already in place: the adapter must be an
    ERC-1155 operator for the wallet so it can pull the multi-outcome tokens
    (see :mod:`eggplant_sdk.approval`); the CTF redeem burns the wallet's own
    tokens and needs no approval.
    """
    opts = opts if opts is not None else RedeemOptions()
    collateral = opts.ctf_collateral if opts.ctf_collateral is not None else _venue_collateral()
    summary = RedeemSummary()
    while True:
        summary.passes += 1
        positions, hit_cap = await data.all_redeemable_positions(wallet)
        if not positions:
            break
        redemptions = await _read_and_plan(positions, rpc_url, wallet, opts.min_shares_raw)
        if not redemptions:
            break
        if opts.max_conditions > 0:
            remaining = opts.max_conditions - (summary.redeemed + summary.failed)
            if remaining <= 0:
                break
            redemptions = redemptions[:remaining]

        calls = _build_calls(redemptions, collateral)
        ok, bad = await _submit_batches(signer, relayer, wallet, calls, opts)
        summary.redeemed += ok
        summary.failed += bad
        logger.info("redeem pass %d complete: %d redeemed, %d failed", summary.passes, ok, bad)

        if opts.max_conditions > 0 and summary.redeemed + summary.failed >= opts.max_conditions:
            break
        if ok == 0:
            break  # no progress — the remaining conditions keep failing
        if not hit_cap:
            break  # the redeemable set is fully drained
    return summary


async def _submit_batches(
    signer: LocalSigner,
    relayer: RelayerClient,
    wallet: str,
    calls: list[DepositWalletCall],
    opts: RedeemOptions,
) -> tuple[int, int]:
    """Submit redeem calls in serial ``batch_size`` chunks (the wallet runs one
    relayer action at a time), retrying a failed batch whole up to
    ``batch_max_retries`` before skipping it. Returns ``(redeemed, failed)``
    condition counts (one call = one condition)."""
    ok = 0
    bad = 0
    batch_size = max(opts.batch_size, 1)
    for start in range(0, len(calls), batch_size):
        chunk = calls[start : start + batch_size]
        attempt = 0
        while True:
            try:
                await submit_and_settle_with_busy_retry(
                    signer,
                    relayer,
                    wallet,
                    chunk,
                    "redeem",
                    opts.settle,
                    opts.wallet_busy_max_retries,
                )
                ok += len(chunk)
                break
            except EggplantError as e:
                if attempt < opts.batch_max_retries:
                    attempt += 1
                    logger.warning(
                        "redeem batch failed (%s); retrying whole batch %d/%d",
                        e,
                        attempt,
                        opts.batch_max_retries,
                    )
                    await asyncio.sleep(opts.settle)
                    continue
                logger.warning("redeem batch of %d skipped after retries: %s", len(chunk), e)
                bad += len(chunk)
                break
    return ok, bad
