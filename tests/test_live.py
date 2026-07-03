"""Live smoke tests against production Polymarket. Read-only.

Deliberately skipped unless ``EGGPLANT_LIVE=1`` so a bare ``pytest`` run
never touches the network by accident::

    EGGPLANT_LIVE=1 pytest tests/test_live.py -v

``test_derive_key_and_read_clob`` additionally needs
``POLYMARKET_PRIVATE_KEY``; ``test_wallet_positions`` needs ``WALLET`` (a
funder address). Both skip silently when their variable is absent.
"""

import os

import pytest

from eggplant_sdk import ClobClient, LocalSigner
from eggplant_sdk.data import DataApiClient
from eggplant_sdk.gamma import GammaClient

live = pytest.mark.skipif(
    os.environ.get("EGGPLANT_LIVE") != "1",
    reason="live venue: EGGPLANT_LIVE=1 pytest tests/test_live.py",
)


@live
async def test_gamma_universe_and_books():
    async with GammaClient() as gamma:
        page = await gamma.fetch_keyset_page(None, 10, None)
    assert page.events, "open-event universe is never empty"

    token_ids = []
    for event in page.events:
        for market in event.markets or []:
            token_ids.extend(market.clob_token_ids or [])
    token_ids = token_ids[:20]
    if not token_ids:
        pytest.skip("no token ids on the first page")

    client = ClobClient.builder().with_credentials(
        "0x0000000000000000000000000000000000000001",
        __import__("eggplant_sdk").Credentials("00000000-0000-0000-0000-000000000000", "AAAA", "x"),
    )
    try:
        books = await client.order_books(token_ids)
        assert books, "public books endpoint returned nothing"
    finally:
        await client.aclose()


@live
async def test_derive_key_and_read_clob():
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        pytest.skip("POLYMARKET_PRIVATE_KEY not set")
    signer = LocalSigner(private_key)
    client = await ClobClient.builder().authenticate(signer)
    try:
        assert await client.server_time() > 0
    finally:
        await client.aclose()


@live
async def test_wallet_positions():
    wallet = os.environ.get("WALLET")
    if not wallet:
        pytest.skip("WALLET not set")
    async with DataApiClient() as data:
        positions = await data.all_positions(wallet, 1.0)
    assert isinstance(positions, list)
