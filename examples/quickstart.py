"""Authenticate, inspect a market, sign an order — and only place it when
explicitly asked to.

    POLYMARKET_PRIVATE_KEY=0x… python examples/quickstart.py

Environment:
- ``POLYMARKET_PRIVATE_KEY`` (required) — the signing EOA.
- ``SIGNATURE_TYPE`` — ``eoa`` (default), ``proxy``, ``safe``, or
  ``poly1271``.
- ``FUNDER`` — the funding wallet, required for ``proxy``/``safe``/
  ``poly1271`` (for ``proxy``/``safe`` it can be derived; see
  ``chain.derive_*_wallet``).
- ``TOKEN_ID`` — a token to quote; without it the example stops after
  authentication.
- ``EGGPLANT_LIVE_TRADE=1`` — actually POST the order (then cancel it). Off
  by default: the example prints the signed wire body instead.
"""

import asyncio
import json
import os
import time

from eggplant_sdk import PRIVATE_KEY_VAR, ClobClient, LocalSigner
from eggplant_sdk.clob.poster import PostTimings
from eggplant_sdk.clob.signing import (
    ExchangeDomain,
    build_signable_order,
    generate_salt,
    to_fixed_usdc,
)
from eggplant_sdk.clob.tick import MIN_SIZE, TickEntry
from eggplant_sdk.clob.types import OrderType, SignatureType


async def main() -> None:
    signer = LocalSigner(os.environ[PRIVATE_KEY_VAR])

    # --- 1. Build + authenticate the client (create-or-derive an API key). ---
    builder = ClobClient.builder()
    signature_type = os.environ.get("SIGNATURE_TYPE", "eoa")
    if signature_type == "proxy":
        builder.signature_type(SignatureType.PROXY)
    elif signature_type == "safe":
        builder.signature_type(SignatureType.GNOSIS_SAFE)
    elif signature_type == "poly1271":
        builder.signature_type(SignatureType.POLY1271)
    if funder := os.environ.get("FUNDER"):
        builder.funder(funder)

    client = await builder.authenticate(signer)
    print(f"authenticated: api key {client.credentials.key}")
    print(f"maker/funder:  {client.identity.maker}")
    print(f"venue time:    {await client.server_time()}")

    token_id = os.environ.get("TOKEN_ID")
    if not token_id:
        print("\nset TOKEN_ID=<decimal token id> to quote a market")
        await client.aclose()
        return

    # --- 2. Read the market's grid and pick the signing domain. ---
    tick = await client.tick_size(token_id)
    neg_risk = await client.neg_risk(token_id)
    print(f"token {token_id}: tick {tick}, negRisk {neg_risk}")

    order_signer = client.order_signer(ExchangeDomain.ctf_v2(neg_risk))

    # --- 3. Sign a minimum-size BUY resting at the price floor (post-only
    #        GTC): essentially unfillable, ideal for validating a pipeline. ---
    entry = TickEntry.new(tick, int(token_id))
    size, price = MIN_SIZE, entry.min_price
    signable = build_signable_order(
        entry.token_id_int,
        to_fixed_usdc(size * price),  # maker: USDC in
        to_fixed_usdc(size),  # taker: shares out
        client.identity,
        time.time_ns() // 1_000_000,
        OrderType.GTC,
        generate_salt(),
        True,  # post-only
    )
    signed_order = order_signer.sign_order(signable, signer)
    print(f"\nsigned {size} @ {price} (BUY, GTC, post-only):")
    print(json.dumps(signed_order.to_wire(), indent=2))

    # --- 4. Only touch the venue when explicitly asked. ---
    if os.environ.get("EGGPLANT_LIVE_TRADE") != "1":
        print("\nEGGPLANT_LIVE_TRADE=1 to place (and immediately cancel) it")
        await client.aclose()
        return

    poster = client.poster()
    timings = PostTimings()
    posts = await poster.post_orders([signed_order], timings, int(time.time()))
    response = posts[0].response
    print(
        f"placed: accepted={response.is_accepted()} id={response.order_id} "
        f"status={response.status} ({posts[0].rtt_ms:.1f}ms)"
    )

    if response.order_id:
        cancelled = await poster.cancel_orders([response.order_id])
        print(f"cancelled: {cancelled.canceled}")

    await poster.aclose()
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
