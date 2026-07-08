"""Redeem every resolved position a wallet holds.

    WALLET=0x… RPC_URL=https://… python examples/redeem.py            # dry run
    WALLET=0x… RPC_URL=https://… EGGPLANT_LIVE_TRADE=1 python examples/redeem.py

The dry run is read-only (Data API + one RPC balance read per pass) and
reports what the first page-set would redeem. Submission additionally needs
POLYMARKET_PRIVATE_KEY, RELAYER_API_KEY, RELAYER_API_KEY_ADDRESS, and the
wallet's approvals already bootstrapped.
"""

import asyncio
import os

from eggplant_sdk import PRIVATE_KEY_VAR, LocalSigner
from eggplant_sdk.convert import fmt_usdc
from eggplant_sdk.data import DataApiClient
from eggplant_sdk.redeem import RedeemOptions, plan_redeem, redeem_all
from eggplant_sdk.relayer import RelayerClient


async def main() -> None:
    wallet = os.environ.get("WALLET") or exit("set WALLET=<funder address>")
    rpc_url = os.environ.get("RPC_URL") or exit("set RPC_URL=<polygon rpc>")

    async with DataApiClient() as data:
        # Dry run: what the first page-set would redeem (submits nothing).
        redemptions, hit_cap = await plan_redeem(data, rpc_url, wallet)
        tail = " (more remain beyond the offset cap)" if hit_cap else ""
        print(f"{len(redemptions)} redeemable condition(s) in this page-set{tail}")
        for r in redemptions[:10]:
            amounts = f"yes={fmt_usdc(r.yes_amount)} no={fmt_usdc(r.no_amount)}"
            print(f"  0x{r.condition_id.hex()}  {amounts}")

        if os.environ.get("EGGPLANT_LIVE_TRADE") != "1":
            print("\nEGGPLANT_LIVE_TRADE=1 (plus relayer credentials) to redeem everything")
            return

        signer = LocalSigner(os.environ[PRIVATE_KEY_VAR])
        async with RelayerClient(
            os.environ["RELAYER_API_KEY"], os.environ["RELAYER_API_KEY_ADDRESS"]
        ) as relayer:
            summary = await redeem_all(signer, relayer, data, rpc_url, wallet, RedeemOptions())

    print(
        f"done — {summary.redeemed} redeemed, {summary.failed} failed "
        f"across {summary.passes} pass(es)"
    )


if __name__ == "__main__":
    asyncio.run(main())
