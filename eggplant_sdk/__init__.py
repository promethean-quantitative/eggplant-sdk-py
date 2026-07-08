"""eggplant-sdk — a Python SDK for Polymarket.

What's here:

- **CLOB trading** — client initialization with every signature type (EOA,
  proxy, Gnosis Safe, and ERC-1271 deposit wallets), precomputed EIP-712
  order signing, and a dedicated write-path poster with isolated connection
  pools.
- **Relayer operations** — gasless merge / split / convert / redeem for
  negRisk positions through Polymarket's relayer, including the
  ``DepositWallet`` batch path.
- **Market data** — lenient order-book fetching, Gamma API events, Data API
  positions, and WebSocket streams for both the market and user channels.

Financial math uses :class:`decimal.Decimal` on every order-affecting path.
Wire parsers are deliberately lenient: unknown enum values and missing fields
degrade gracefully instead of failing the whole response, because the venue
adds fields and tick sizes without notice.

Real money moves through this code. Read the docs of each module you use,
and prefer the venue's smallest sizes while validating an integration.
"""

from . import (
    approval,
    auth,
    book,
    chain,
    clob,
    convert,
    data,
    errors,
    fee,
    gamma,
    redeem,
    relayer,
    signer,
    ws,
)
from .auth import Credentials
from .chain import AMOY, POLYGON
from .clob import ClobClient, ClobClientBuilder
from .errors import (
    ApiError,
    EggplantError,
    InvalidDataError,
    RateLimitError,
    RelayerQuotaExhaustedError,
    WsError,
)
from .signer import LocalSigner, Signature

__version__ = "0.1.0"

#: Environment variable conventionally holding the signer's private key.
#: Nothing in the SDK reads it implicitly; it is a shared convention for
#: examples and downstream code.
PRIVATE_KEY_VAR = "POLYMARKET_PRIVATE_KEY"

__all__ = [
    "AMOY",
    "POLYGON",
    "PRIVATE_KEY_VAR",
    "ApiError",
    "ClobClient",
    "ClobClientBuilder",
    "Credentials",
    "EggplantError",
    "InvalidDataError",
    "LocalSigner",
    "RateLimitError",
    "RelayerQuotaExhaustedError",
    "Signature",
    "WsError",
    "approval",
    "auth",
    "book",
    "chain",
    "clob",
    "convert",
    "data",
    "errors",
    "fee",
    "gamma",
    "redeem",
    "relayer",
    "signer",
    "ws",
]
