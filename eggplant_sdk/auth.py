"""Authentication: API credentials, L1 (EIP-712 ``ClobAuth``) key derivation,
and L2 (HMAC) request signing.

Two layers, per the venue's model:

- **L1** proves control of a wallet by signing the ``ClobAuth`` EIP-712
  struct; the venue answers with :class:`Credentials` (create) or re-derives
  the existing ones (derive). See
  :class:`eggplant_sdk.clob.ClobClientBuilder`.
- **L2** signs every authenticated REST request:
  ``HMAC-SHA256(base64_url_decode(secret), "{ts}{METHOD}{path}{body}")``,
  sent as the ``POLY_*`` header set.

The implementation is pinned by golden vectors shared with the Rust sibling
SDK (``eggplant-sdk-rs``), which cross-checked them against the official
client.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import uuid

from eth_utils import keccak, to_canonical_address

from .errors import InvalidDataError
from .signer import LocalSigner

#: CLOB API key identifier type. The venue issues keys as UUIDs; the key rides
#: on every L2-authenticated request (``POLY_API_KEY``) and as the ``owner``
#: of posted orders.
ApiKey = uuid.UUID

#: L1/L2 header names. HTTP header lookup is case-insensitive; these render
#: lowercase on the wire.
POLY_ADDRESS = "POLY_ADDRESS"
POLY_API_KEY = "POLY_API_KEY"
POLY_NONCE = "POLY_NONCE"
POLY_PASSPHRASE = "POLY_PASSPHRASE"
POLY_SIGNATURE = "POLY_SIGNATURE"
POLY_TIMESTAMP = "POLY_TIMESTAMP"


class Credentials:
    """API credentials issued by the venue's L1 handshake.

    ``secret`` and ``passphrase`` are redacted from ``repr``/``str`` output —
    read them via :meth:`secret` / :meth:`passphrase` only where needed.
    """

    def __init__(self, key: ApiKey | str, secret: str, passphrase: str):
        self._key = key if isinstance(key, uuid.UUID) else uuid.UUID(key)
        self._secret = secret
        self._passphrase = passphrase

    @classmethod
    def from_dict(cls, data: dict) -> Credentials:
        """Parse the venue's ``{"apiKey": …, "secret": …, "passphrase": …}``
        response shape."""
        key = data.get("apiKey", data.get("key"))
        if key is None:
            raise InvalidDataError("credentials response has no apiKey")
        return cls(key, data["secret"], data["passphrase"])

    @property
    def key(self) -> ApiKey:
        """The API key (order ``owner``, ``POLY_API_KEY`` header)."""
        return self._key

    def secret(self) -> str:
        """The base64-URL-encoded HMAC secret."""
        return self._secret

    def passphrase(self) -> str:
        """The passphrase (``POLY_PASSPHRASE`` header)."""
        return self._passphrase

    def __repr__(self) -> str:
        return f"Credentials(key={self._key}, secret=***, passphrase=***)"


CLOB_AUTH_MESSAGE = "This message attests that I control the given wallet"

# The L1 attestation struct. Field order is load-bearing for the EIP-712
# typehash.
_CLOB_AUTH_TYPE_STRING = b"ClobAuth(address address,string timestamp,uint256 nonce,string message)"
# The ClobAuth domain has no verifying contract, so its domain type string
# carries only the three present fields.
_CLOB_AUTH_DOMAIN_TYPE = b"EIP712Domain(string name,string version,uint256 chainId)"


def _u256(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _addr32(address: str) -> bytes:
    return to_canonical_address(address).rjust(32, b"\x00")


def clob_auth_digest(address: str, chain_id: int, timestamp: int, nonce: int) -> bytes:
    """The EIP-712 signing digest of the L1 ``ClobAuth`` attestation."""
    domain_separator = keccak(
        keccak(_CLOB_AUTH_DOMAIN_TYPE) + keccak(b"ClobAuthDomain") + keccak(b"1") + _u256(chain_id)
    )
    struct_hash = keccak(
        keccak(_CLOB_AUTH_TYPE_STRING)
        + _addr32(address)
        + keccak(str(timestamp).encode())
        + _u256(nonce)
        + keccak(CLOB_AUTH_MESSAGE.encode())
    )
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def l1_headers(
    signer: LocalSigner,
    chain_id: int,
    timestamp: int,
    nonce: int | None = None,
) -> dict[str, str]:
    """The L1 headers (``POLY_ADDRESS``/``POLY_NONCE``/``POLY_SIGNATURE``/
    ``POLY_TIMESTAMP``) that authorize API-key creation and derivation.

    ``timestamp`` is Unix seconds; ``nonce`` defaults to ``0`` (each nonce
    maps to one API key — pass a different nonce to mint additional keys for
    the same wallet).
    """
    naive_nonce = nonce if nonce is not None else 0
    digest = clob_auth_digest(signer.address, chain_id, timestamp, naive_nonce)
    signature = signer.sign_hash(digest)
    return {
        POLY_ADDRESS: signer.address.lower(),
        POLY_NONCE: str(naive_nonce),
        POLY_SIGNATURE: signature.to_hex(),
        POLY_TIMESTAMP: str(timestamp),
    }


def l2_message(timestamp: int, method: str, path: str, body: str) -> str:
    """The exact string L2 signs: ``{timestamp}{METHOD}{path}{body}``.

    ``path`` is the URL path only — query strings are *not* part of the
    signed message. ``body`` is the exact text that will be sent (empty for
    GET).
    """
    return f"{timestamp}{method}{path}{body}"


def l2_hmac(secret: str, message: str | bytes) -> str:
    """HMAC-SHA256 of ``message`` under the base64-URL-decoded ``secret``,
    re-encoded base64-URL — the ``POLY_SIGNATURE`` value."""
    try:
        decoded_secret = base64.urlsafe_b64decode(secret)
    except (binascii.Error, ValueError) as e:
        raise InvalidDataError(f"credentials secret is not base64: {e}") from e
    if isinstance(message, str):
        message = message.encode()
    digest = hmac.new(decoded_secret, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode()


def l2_headers(
    address: str,
    credentials: Credentials,
    timestamp: int,
    method: str,
    path: str,
    body: str,
) -> dict[str, str]:
    """The full L2 header set for one request. ``address`` is the signer's
    EOA (sent checksummed); ``path``/``body`` per :func:`l2_message`."""
    signature = l2_hmac(credentials.secret(), l2_message(timestamp, method, path, body))
    return {
        POLY_ADDRESS: address,
        POLY_API_KEY: str(credentials.key),
        POLY_PASSPHRASE: credentials.passphrase(),
        POLY_SIGNATURE: signature,
        POLY_TIMESTAMP: str(timestamp),
    }
