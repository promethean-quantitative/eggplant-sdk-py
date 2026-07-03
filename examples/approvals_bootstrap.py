"""Safe-path (signature type 2) approvals bootstrap: deploy the Safe if
missing and grant pUSD + CTF approvals, idempotently.

    POLYMARKET_PRIVATE_KEY=0x… RELAYER_API_KEY=… RELAYER_API_KEY_ADDRESS=… \
        RPC_URL=https://… python examples/approvals_bootstrap.py

Deposit wallets (type 3) approve differently — batch the same
``approve``/``setApprovalForAll`` calldata through
``RelayerClient.submit_deposit_wallet_batch`` instead.
"""

import asyncio
import logging
import os

from eggplant_sdk import PRIVATE_KEY_VAR, LocalSigner
from eggplant_sdk.approval import ensure_approvals
from eggplant_sdk.relayer import RelayerClient


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    signer = LocalSigner(os.environ[PRIVATE_KEY_VAR])
    rpc_url = os.environ.get("RPC_URL") or exit("set RPC_URL=<polygon rpc>")

    async with RelayerClient(
        os.environ["RELAYER_API_KEY"], os.environ["RELAYER_API_KEY_ADDRESS"]
    ) as relayer:
        await ensure_approvals(signer, relayer, rpc_url)


if __name__ == "__main__":
    asyncio.run(main())
