"""WebSocket streams: the market channel (order books, price changes) and
the authenticated user channel (own trades and order lifecycle events).

- :mod:`~eggplant_sdk.ws.frames` — subscribe-frame builders and the text
  ``PING``/``PONG`` liveness protocol constants.
- :mod:`~eggplant_sdk.ws.market` / :mod:`~eggplant_sdk.ws.user` — typed
  messages plus thin single-connection streams
  (:class:`~eggplant_sdk.ws.market.MarketStream`,
  :class:`~eggplant_sdk.ws.user.UserStream`) that own the liveness protocol
  and hand back raw frames.
- :mod:`~eggplant_sdk.ws.util` — multi-connection plumbing: staggered recycle
  phasing, maker-side classification, bounded dedup.

Connection *policy* (how many connections, sharding, redundancy, backoff)
deliberately stays with the caller; these pieces are the mechanism.
"""

from . import frames, market, user, util

__all__ = ["frames", "market", "user", "util"]
