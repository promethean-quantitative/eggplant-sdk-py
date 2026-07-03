"""Watch your own fills on the user channel, with dedup, final-status
gating, and own-maker filtering.

    POLYMARKET_PRIVATE_KEY=0x… python examples/stream_user_fills.py

The channel delivers **maker** fills only; taker fills are credited from the
POST response instead. Handle messages in this order: gate on a final status
**before** deduping (a RETRYING first sighting must not swallow the
confirmation), dedup by trade id, filter ``maker_orders`` to your own key,
and derive your side — the trade's top-level ``side`` describes the *taker*.
"""

import asyncio
import os

from eggplant_sdk import PRIVATE_KEY_VAR, ClobClient, LocalSigner
from eggplant_sdk.ws.user import TradeMessage, UserStream, UserStreamConfig, trade_status_is_final
from eggplant_sdk.ws.util import SeenIds, our_maker_side


async def main() -> None:
    signer = LocalSigner(os.environ[PRIVATE_KEY_VAR])
    client = await ClobClient.builder().authenticate(signer)
    our_key = client.credentials.key
    print(f"watching fills for api key {our_key}")

    # For redundancy, open several identically-subscribed streams behind one
    # shared SeenIds — first delivery wins.
    stream = await UserStream.connect(UserStreamConfig(credentials=client.credentials))
    seen = SeenIds(1024)

    while (message := await stream.next_message()) is not None:
        if not isinstance(message, TradeMessage):
            continue
        if not trade_status_is_final(message.status) or not seen.insert(message.id):
            continue
        for maker in message.maker_orders:
            if maker.owner != our_key:
                continue
            side = our_maker_side(message.side, message.outcome, maker.outcome)
            print(f"filled {side} {maker.matched_amount} @ {maker.price}")


if __name__ == "__main__":
    asyncio.run(main())
