"""SDK-wide exception types.

One small hierarchy end to end, mirroring the venue's failure modes. The
variants worth branching on:

- :class:`RateLimitError` — HTTP 429 anywhere.
- :class:`RelayerQuotaExhaustedError` / :meth:`EggplantError.is_wallet_busy` —
  relayer submission flow control.
- :class:`ApiError` — non-2xx from a read endpoint, body attached.
- :class:`InvalidDataError` — unparseable input/response, and the poster's
  transport failures (timeout included).
"""

from __future__ import annotations

#: Relayer ``/submit`` response substring shown when another action is already
#: in flight for the wallet. Matched as a string because the relayer returns
#: it in the HTTP error body, which surfaces as :class:`InvalidDataError`.
WALLET_BUSY_MARKER = "wallet busy: active action exists"


class EggplantError(Exception):
    """Base class for every SDK error."""

    def is_wallet_busy(self) -> bool:
        """True when this is the transient relayer "wallet busy" condition.

        It clears once the in-flight action settles, so callers can wait and
        retry.
        """
        return False


class ApiError(EggplantError):
    """Non-2xx response from a Polymarket HTTP API (CLOB, Gamma, Data, relayer)."""

    def __init__(self, status: int, body: str):
        super().__init__(f"API error ({status}): {body}")
        self.status = status
        self.body = body


class RateLimitError(EggplantError):
    """HTTP 429. ``retry_after`` carries the raw ``Retry-After`` header when
    the venue sent one; it is advisory only."""

    def __init__(self, retry_after: str | None = None):
        super().__init__("rate limited")
        self.retry_after = retry_after


class InvalidDataError(EggplantError):
    """A response or input that could not be interpreted. The relayer's
    "wallet busy" condition also surfaces here — see
    :meth:`EggplantError.is_wallet_busy`."""

    def is_wallet_busy(self) -> bool:
        return WALLET_BUSY_MARKER in str(self)


class RelayerQuotaExhaustedError(EggplantError):
    """The relayer rejected a submission because the API key's quota is spent.

    ``resets_in_secs`` is scraped from the response body and is known to be
    unreliable (the real reset is usually much sooner) — treat it as
    logging-only and retry on your own fixed cadence.
    """

    def __init__(self, resets_in_secs: int):
        super().__init__(f"relayer quota exhausted, resets in {resets_in_secs}s")
        self.resets_in_secs = resets_in_secs


class WsError(EggplantError):
    """WebSocket transport failure or liveness (PONG deadline) breach."""
