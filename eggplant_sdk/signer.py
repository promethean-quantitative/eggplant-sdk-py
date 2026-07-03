"""Local key signing.

:class:`LocalSigner` wraps a raw secp256k1 private key and produces the
65-byte ``r ‖ s ‖ v`` signatures the venue and the relayer expect. ECDSA is
deterministic (RFC 6979), so signing the same digest twice yields
byte-identical output.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_keys import keys
from eth_utils import keccak, to_checksum_address

from .errors import InvalidDataError


@dataclass(frozen=True)
class Signature:
    """One ECDSA signature. ``y_parity`` is the raw recovery id (0/1); the
    wire forms add a ``v`` base (27 for standard ECDSA, 31 for the Safe
    ``eth_sign`` convention)."""

    r: int
    s: int
    y_parity: int

    def to_bytes(self, v_base: int = 27) -> bytes:
        """The 65-byte ``r ‖ s ‖ v`` form with ``v = y_parity + v_base``."""
        return (
            self.r.to_bytes(32, "big")
            + self.s.to_bytes(32, "big")
            + bytes([self.y_parity + v_base])
        )

    def to_hex(self, v_base: int = 27) -> str:
        """``0x``-prefixed hex of :meth:`to_bytes`."""
        return "0x" + self.to_bytes(v_base).hex()


class LocalSigner:
    """A wallet signer backed by a local private key.

    Accepts the key as ``0x``-prefixed (or bare) hex, or as 32 raw bytes.
    ``address`` is the checksummed EOA address.
    """

    def __init__(self, private_key: str | bytes):
        if isinstance(private_key, str):
            raw = private_key.removeprefix("0x")
            try:
                private_key = bytes.fromhex(raw)
            except ValueError as e:
                raise InvalidDataError(f"private key is not hex: {e}") from e
        if len(private_key) != 32:
            raise InvalidDataError("private key must be 32 bytes")
        self._key = keys.PrivateKey(private_key)
        self.address: str = to_checksum_address(self._key.public_key.to_address())

    def sign_hash(self, digest: bytes) -> Signature:
        """Sign a 32-byte digest directly (no prefixing)."""
        if len(digest) != 32:
            raise InvalidDataError("digest must be 32 bytes")
        sig = self._key.sign_msg_hash(digest)
        return Signature(r=sig.r, s=sig.s, y_parity=sig.v)

    def sign_message(self, message: bytes) -> Signature:
        """Sign ``message`` under the EIP-191 personal-message prefix
        (``"\\x19Ethereum Signed Message:\\n" + len``)."""
        prefixed = b"\x19Ethereum Signed Message:\n" + str(len(message)).encode() + message
        return self.sign_hash(keccak(prefixed))

    def __repr__(self) -> str:  # never expose the key
        return f"LocalSigner(address={self.address})"
