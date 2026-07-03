"""Gamma → legs → read-only convert plan → optional relayer cycle.

    EVENT_SLUG=<slug> WALLET=0x… RPC_URL=https://… python examples/convert_merge_split.py

Environment:
- ``EVENT_SLUG`` (required) — the negRisk event to plan.
- ``WALLET`` (required) — the deposit wallet holding the positions.
- ``RPC_URL`` (required) — a Polygon JSON-RPC endpoint for balance reads.
- ``EGGPLANT_LIVE_TRADE=1`` plus ``POLYMARKET_PRIVATE_KEY``,
  ``RELAYER_API_KEY``, ``RELAYER_API_KEY_ADDRESS`` — actually run the full
  merge→convert→wrap cycle through the relayer. Off by default: the example
  prints the dry-run plan instead.

The standalone builders (``build_merge_calldata``, ``build_split_calldata``,
``redeem_calls``, ``split_calls``, …) need no RPC at all and feed
``RelayerClient.submit_deposit_wallet_batch`` directly.
"""

import asyncio
import os

from eggplant_sdk import PRIVATE_KEY_VAR, LocalSigner
from eggplant_sdk.convert import (
    ConvertJob,
    convert_legs,
    fmt_usdc,
    plan_jobs,
    process_job,
)
from eggplant_sdk.gamma import GammaClient
from eggplant_sdk.relayer import RelayerClient


async def main() -> None:
    slug = os.environ.get("EVENT_SLUG") or exit("set EVENT_SLUG=<gamma event slug>")
    wallet = os.environ.get("WALLET") or exit("set WALLET=<deposit wallet>")
    rpc_url = os.environ.get("RPC_URL") or exit("set RPC_URL=<polygon rpc>")

    # Legs come straight off Gamma.
    async with GammaClient() as gamma:
        events = await gamma.fetch_events_by_slug(slug)
    if not events:
        raise SystemExit(f"no event for slug {slug!r}")
    markets = events[0].markets or []
    legs = convert_legs(m for m in (mkt.market_ids() for mkt in markets) if m is not None)
    print(f"{slug}: {len(legs)} plannable legs")

    job = ConvertJob(slug=slug, legs=legs)

    # Read-only dry run — one RPC balance snapshot, no submissions.
    plans, snapshot = await plan_jobs([job], rpc_url, wallet, 100_000)
    plan = plans[0]
    print(
        f"would merge {len(plan.merges)}, convert {len(plan.tiers)} tier(s), "
        f"freeing {fmt_usdc(plan.proceeds)} USDC.e "
        f"(wallet already holds {fmt_usdc(snapshot.balance)} unwrapped)"
    )

    if os.environ.get("EGGPLANT_LIVE_TRADE") != "1":
        print("\nEGGPLANT_LIVE_TRADE=1 (plus relayer credentials) to run the full cycle")
        return

    # The full cycle (plan → submit chunks → settle → wrap). Relayer
    # operations require API credentials from Polymarket's builder program.
    signer = LocalSigner(os.environ[PRIVATE_KEY_VAR])
    async with RelayerClient(
        os.environ["RELAYER_API_KEY"], os.environ["RELAYER_API_KEY_ADDRESS"]
    ) as relayer:
        detail = await process_job(job, signer, relayer, rpc_url, wallet)
    print(f"cycle complete: {detail}")


if __name__ == "__main__":
    asyncio.run(main())
