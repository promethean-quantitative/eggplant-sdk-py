"""Systematically merge/convert every negRisk position a wallet holds.

.. code-block:: sh

    WALLET=0x... python examples/sweep.py                        # dry run
    WALLET=0x... EGGPLANT_LIVE_TRADE=1 python examples/sweep.py  # submit

The dry run discovers holdings (Data API + Gamma) and reports the merge/convert
work each held event has, submitting nothing. Submission additionally needs
``POLYMARKET_PRIVATE_KEY``, ``RELAYER_API_KEY``, ``RELAYER_API_KEY_ADDRESS``,
and the wallet's approvals already bootstrapped (see ``approvals_bootstrap.py``).
``SLUG=<slug>`` narrows the sweep to one event; ``MIN_SHARES=<n>`` sets the Data
API size floor.
"""

from __future__ import annotations

import asyncio
import os

from eggplant_sdk import PRIVATE_KEY_VAR
from eggplant_sdk.data import DataApiClient
from eggplant_sdk.gamma import GammaClient
from eggplant_sdk.relayer import RelayerClient
from eggplant_sdk.signer import LocalSigner
from eggplant_sdk.sweep import SweepOptions, plan_sweep, sweep_all


async def main() -> None:
    wallet = os.environ.get("WALLET")
    if not wallet:
        raise SystemExit("set WALLET=<funder address>")
    rpc_url = os.environ.get("RPC_URL", "https://polygon-rpc.com")

    opts = SweepOptions(
        only_slug=os.environ.get("SLUG"),
        min_shares=float(os.environ.get("MIN_SHARES", "0")),
    )

    data = DataApiClient()
    gamma = GammaClient()

    # Dry run: what each held negRisk event would merge/convert.
    reports = await plan_sweep(data, gamma, wallet, opts)
    actionable = [r for r in reports if r.classification.actionable()]
    print(f"{len(reports)} held negRisk event(s), {len(actionable)} actionable:")
    for r in actionable:
        line = f"  [{r.slug}] {r.title}"
        if r.classification.mergeable:
            line += f"  merge {r.classification.merge_pairs} pair(s)"
        if r.classification.convertible:
            line += f"  convert {r.classification.convert_legs} leg(s)"
        print(line)

    if os.environ.get("EGGPLANT_LIVE_TRADE") != "1":
        print("\nEGGPLANT_LIVE_TRADE=1 (plus relayer keys) to settle everything")
        return

    signer = LocalSigner(os.environ[PRIVATE_KEY_VAR])
    relayer = RelayerClient(os.environ["RELAYER_API_KEY"], os.environ["RELAYER_API_KEY_ADDRESS"])
    summary = await sweep_all(signer, relayer, data, gamma, rpc_url, wallet, opts)
    print(
        f"done — {summary.held_events} held, {summary.actionable} actionable, "
        f"{summary.executed} settled, {summary.failed} failed"
    )


if __name__ == "__main__":
    asyncio.run(main())
