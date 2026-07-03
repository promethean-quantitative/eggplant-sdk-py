"""Subscribe frames and the liveness protocol for Polymarket's WS channels.

Liveness is text-frame based, not WebSocket ping opcodes: send the literal
text :data:`PING` every :data:`PING_INTERVAL`; the venue answers with the
literal text :data:`PONG`. A socket that hasn't ponged within
:data:`PONG_TIMEOUT` is half-open (NAT drop, server stall) and must be
reconnected — waiting for a TCP-level failure can take minutes.
"""

from __future__ import annotations

import json

from ..auth import Credentials

#: Liveness ping text frame.
PING = "PING"
#: Expected liveness answer.
PONG = "PONG"

#: Cadence of :data:`PING` frames, seconds.
PING_INTERVAL = 10.0

#: No :data:`PONG` for this long ⇒ treat the socket as half-open and
#: reconnect. Three ping intervals tolerates the odd dropped frame.
PONG_TIMEOUT = 30.0


def market_subscribe_frame(token_ids: list[str], custom_features: bool) -> str:
    """The market-channel subscribe frame: ``{"assets_ids": […], "type":
    "market"}``. ``custom_features`` additionally requests the
    ``best_bid_ask``/``new_market``/``market_resolved`` event kinds."""
    frame: dict = {"assets_ids": token_ids, "type": "market"}
    if custom_features:
        frame["custom_feature_enabled"] = True
    return json.dumps(frame)


def user_subscribe_frame(credentials: Credentials, markets: list[str]) -> str:
    """The user-channel subscribe frame. Carries the raw credentials in-band
    — that is the venue's protocol — so send it only over the TLS socket.

    Empty ``markets`` subscribes to every fill on the authenticated account.
    """
    return json.dumps(
        {
            "type": "user",
            "markets": markets,
            "auth": {
                "apiKey": str(credentials.key),
                "secret": credentials.secret(),
                "passphrase": credentials.passphrase(),
            },
        }
    )
