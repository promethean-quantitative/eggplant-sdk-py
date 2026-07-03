"""List a wallet's open and redeemable positions (no credentials needed).

WALLET=0x… python examples/positions.py
"""

import asyncio
import os

from eggplant_sdk.data import DataApiClient


async def main() -> None:
    wallet = os.environ.get("WALLET")
    if not wallet:
        raise SystemExit("set WALLET=<funder address>")

    async with DataApiClient() as data:
        positions = await data.all_positions(wallet, 1.0)
        print(f"{len(positions)} positions ≥ 1 share:")
        for p in positions[:20]:
            kind = "negRisk" if p.negative_risk else "binary"
            print(f"  {p.size:>12.2f}  {p.outcome:<4} {p.title} ({kind})")
        if len(positions) > 20:
            print(f"  … and {len(positions) - 20} more")

        redeemable, hit_cap = await data.all_redeemable_positions(wallet)
        note = " (offset cap hit — a tail may remain; redeem and re-fetch)" if hit_cap else ""
        print(f"\n{len(redeemable)} redeemable position(s){note}")


if __name__ == "__main__":
    asyncio.run(main())
