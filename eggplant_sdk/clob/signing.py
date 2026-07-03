"""EIP-712 order signing for every Polymarket signature type.

:class:`OrderSigner` precomputes the exchange domain separator (and, for
deposit wallets, the Solady ``TypedDataSign`` template) once at construction;
per-order work is one struct hash, one or two keccaks, and one ECDSA
signature.

Signature-type dispatch:

- **Types 0/1/2** (EOA, proxy, Safe): the plain EIP-712 digest
  ``keccak256(0x1901 ‖ domainSeparator ‖ hashStruct(order))``, signed by the
  EOA. The venue validates proxy/Safe orders against the owning EOA.
- **Type 3** (``POLY1271`` deposit wallet): the digest is re-wrapped through
  the wallet's own ``DepositWallet`` (version "1") domain via Solady's
  ``TypedDataSign``, and the wire signature is the wrapped hex envelope
  ``sig ‖ exchangeDomainSeparator ‖ contentsHash ‖ contentsType ‖ len``.

**Real money.** Every constant here is verified against the deployed
exchanges; the test suite proves the precomputed fast path equals a generic
EIP-712 implementation, and the shared golden vectors pin it to the Rust
sibling SDK.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, localcontext

from eth_utils import keccak, to_canonical_address

from ..auth import ApiKey
from ..chain import EXCHANGE_V2, EXCHANGE_V3, NEG_RISK_EXCHANGE_V2
from ..errors import InvalidDataError
from ..signer import LocalSigner
from .types import OrderType, OrderV2, Side, SignableOrder, SignatureType, SignedOrder

#: USDC / conditional-token raw decimals (1 share = 10^6 raw units).
USDC_DECIMALS = 6

#: Largest integer JavaScript can hold exactly (2^53 − 1). Salts are masked
#: to this so the venue's JS-side tooling round-trips them losslessly.
JS_MAX_SAFE_INT = (1 << 53) - 1

_DOMAIN_TYPE_STRING = (
    b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)

#: EIP-712 domain name shared by every Polymarket exchange deployment.
ORDER_DOMAIN_NAME = "Polymarket CTF Exchange"

#: The V2 ``Order`` EIP-712 type string. Mirrors the deployed contract's
#: typehash — a unit test pins it.
ORDER_TYPE_STRING = (
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,"
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
    "uint256 timestamp,bytes32 metadata,bytes32 builder)"
)

#: The V1 ``Order`` EIP-712 type string (legacy exchange).
ORDER_V1_TYPE_STRING = (
    "Order(uint256 salt,address maker,address signer,address taker,uint256 tokenId,"
    "uint256 makerAmount,uint256 takerAmount,uint256 expiration,uint256 nonce,"
    "uint256 feeRateBps,uint8 side,uint8 signatureType)"
)

_SOLADY_TYPE_STRING = (
    "TypedDataSign(Order contents,string name,string version,uint256 chainId,"
    "address verifyingContract,bytes32 salt)" + ORDER_TYPE_STRING
)
_DEPOSIT_WALLET_NAME = b"DepositWallet"
_DEPOSIT_WALLET_VERSION = b"1"


def _u256(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _addr32(address: str) -> bytes:
    return to_canonical_address(address).rjust(32, b"\x00")


@dataclass(frozen=True)
class ExchangeDomain:
    """The EIP-712 domain of one exchange deployment: which contract verifies
    the order and under which protocol version string."""

    #: Domain ``name`` — :data:`ORDER_DOMAIN_NAME` on every known deployment.
    name: str
    #: Domain ``version``: ``"2"`` for the CTF Exchange V2 family, ``"3"``
    #: for the combos exchange, ``"1"`` for the legacy V1 exchange.
    version: str
    verifying_contract: str

    @classmethod
    def ctf_v2(cls, neg_risk: bool) -> ExchangeDomain:
        """The CTF Exchange V2 domain — the current order flow. ``neg_risk``
        selects between the negRisk and regular exchange deployments."""
        return cls(
            name=ORDER_DOMAIN_NAME,
            version="2",
            verifying_contract=NEG_RISK_EXCHANGE_V2 if neg_risk else EXCHANGE_V2,
        )

    @classmethod
    def combos_v3(cls) -> ExchangeDomain:
        """The combos (parlay/RFQ) exchange V3 domain."""
        return cls(name=ORDER_DOMAIN_NAME, version="3", verifying_contract=EXCHANGE_V3)

    @classmethod
    def custom(cls, version: str, verifying_contract: str) -> ExchangeDomain:
        """Escape hatch for a deployment this SDK doesn't know about yet."""
        return cls(name=ORDER_DOMAIN_NAME, version=version, verifying_contract=verifying_contract)


@dataclass(frozen=True)
class OrderIdentity:
    """Which addresses an order carries as ``maker``/``signer``, and how the
    venue validates its signature.

    Build via the per-type constructors — they encode the venue's
    maker/signer table so callers can't mis-wire it.
    """

    #: The wallet whose funds move (``maker`` field, and the funder the venue
    #: debits).
    maker: str
    #: The address the signature is validated against.
    signer: str
    signature_type: SignatureType

    @classmethod
    def eoa(cls, address: str) -> OrderIdentity:
        """Signature type 0: a plain EOA is both maker and signer."""
        return cls(maker=address, signer=address, signature_type=SignatureType.EOA)

    @classmethod
    def proxy(cls, eoa: str, proxy_wallet: str) -> OrderIdentity:
        """Signature type 1: a Polymarket proxy wallet (Magic/email login)
        holds the funds; the owning EOA signs. Derive the proxy address with
        :func:`eggplant_sdk.chain.derive_proxy_wallet`."""
        return cls(maker=proxy_wallet, signer=eoa, signature_type=SignatureType.PROXY)

    @classmethod
    def gnosis_safe(cls, eoa: str, safe_wallet: str) -> OrderIdentity:
        """Signature type 2: a 1-of-1 Gnosis Safe (browser wallet) holds the
        funds; the owning EOA signs. Derive the Safe address with
        :func:`eggplant_sdk.chain.derive_safe_wallet`."""
        return cls(maker=safe_wallet, signer=eoa, signature_type=SignatureType.GNOSIS_SAFE)

    @classmethod
    def poly1271(cls, deposit_wallet: str) -> OrderIdentity:
        """Signature type 3: an ERC-1271 deposit wallet is both maker and
        signer; the wallet-owning EOA produces the wrapped signature."""
        return cls(
            maker=deposit_wallet, signer=deposit_wallet, signature_type=SignatureType.POLY1271
        )


def order_v2_struct_hash(order: OrderV2) -> bytes:
    """EIP-712 ``hashStruct`` of a V2 order."""
    return keccak(
        keccak(ORDER_TYPE_STRING.encode())
        + _u256(order.salt)
        + _addr32(order.maker)
        + _addr32(order.signer)
        + _u256(order.token_id)
        + _u256(order.maker_amount)
        + _u256(order.taker_amount)
        + _u256(order.side)
        + _u256(order.signature_type)
        + _u256(order.timestamp)
        + order.metadata
        + order.builder
    )


class OrderSigner:
    """Precomputed EIP-712 order signer for one (exchange domain, identity)
    pair.

    Construction hashes the domain once; :meth:`sign_order` then does the
    minimum per-order work.
    """

    def __init__(
        self,
        chain_id: int,
        domain: ExchangeDomain,
        identity: OrderIdentity,
        owner: ApiKey,
    ):
        self._identity = identity
        self._owner = owner

        self._domain_separator = keccak(
            keccak(_DOMAIN_TYPE_STRING)
            + keccak(domain.name.encode())
            + keccak(domain.version.encode())
            + _u256(chain_id)
            + _addr32(domain.verifying_contract)
        )

        # The TypedDataSign wrapper hashes over the *deposit wallet's* own
        # domain: name "DepositWallet", version "1", this chain, and the
        # wallet address (= the order's signer for POLY1271) as verifying
        # contract, salt zero. Everything but the per-order contents hash
        # (slot 1) is prefilled.
        self._typed_data_prefix = (
            keccak(_SOLADY_TYPE_STRING.encode())
            # slot 1 (contents hash) spliced in per order
        )
        self._typed_data_suffix = (
            keccak(_DEPOSIT_WALLET_NAME)
            + keccak(_DEPOSIT_WALLET_VERSION)
            + _u256(chain_id)
            + _addr32(identity.signer)
            + b"\x00" * 32  # zero salt
        )

        type_bytes = ORDER_TYPE_STRING.encode()
        self._order_type_suffix = type_bytes.hex() + len(type_bytes).to_bytes(2, "big").hex()
        self._domain_separator_hex = self._domain_separator.hex()

    @property
    def identity(self) -> OrderIdentity:
        """The identity orders signed here carry."""
        return self._identity

    @property
    def domain_separator(self) -> bytes:
        """The exchange domain separator (useful for debugging signatures)."""
        return self._domain_separator

    def sign_order(self, signable: SignableOrder, signer: LocalSigner) -> SignedOrder:
        """Sign a V2 order. ``signer`` must hold the EOA key matching
        :attr:`OrderIdentity.signer` (for POLY1271: the EOA that owns the
        deposit wallet).

        ECDSA over a fixed digest is deterministic (RFC 6979), so signing the
        same signable twice yields byte-identical output — callers may
        duplicate a signed order freely.
        """
        order = signable.order
        if not isinstance(order, OrderV2):
            raise InvalidDataError("expected V2 order")
        if to_canonical_address(order.maker) != to_canonical_address(self._identity.maker):
            raise InvalidDataError("order maker must match the signer's identity")
        if order.signature_type != int(self._identity.signature_type):
            raise InvalidDataError("order signatureType must match the signer's identity")

        contents_hash = order_v2_struct_hash(order)

        if self._identity.signature_type is SignatureType.POLY1271:
            # Splice the contents hash into the precomputed Solady
            # TypedDataSign layout and sign the wrapped digest.
            struct_hash = keccak(self._typed_data_prefix + contents_hash + self._typed_data_suffix)
        else:
            # Types 0/1/2: sign the exchange digest directly; the venue
            # recovers the EOA from the plain 65-byte signature.
            struct_hash = contents_hash

        digest = keccak(b"\x19\x01" + self._domain_separator + struct_hash)
        sig = signer.sign_hash(digest)

        if self._identity.signature_type is SignatureType.POLY1271:
            signature = (
                "0x"
                + sig.to_bytes().hex()
                + self._domain_separator_hex
                + contents_hash.hex()
                + self._order_type_suffix
            )
        else:
            # 65 bytes r ‖ s ‖ v with v ∈ {27, 28} — the venue's expected
            # ECDSA wire form.
            signature = sig.to_hex()

        return SignedOrder(
            order=order,
            signature=signature,
            order_type=signable.order_type,
            owner=self._owner,
            expiration=signable.expiration,
            post_only=signable.post_only,
            defer_exec=signable.defer_exec,
        )


def generate_salt() -> int:
    """A time-derived salt masked to :data:`JS_MAX_SAFE_INT`."""
    return time.time_ns() & JS_MAX_SAFE_INT


def to_fixed_usdc(amount: Decimal) -> int:
    """Convert a non-negative decimal amount to raw 6-decimal units.

    Truncates past the 6th decimal. Errors on negative amounts or values out
    of range — order amounts from user input must never wrap.
    """
    if amount < 0:
        raise InvalidDataError(f"amount out of range for order: {amount}")
    with localcontext() as ctx:
        ctx.prec = 60
        raw = int(amount.quantize(Decimal("0.000001"), rounding=ROUND_DOWN) * 1_000_000)
    if raw >= (1 << 128):
        raise InvalidDataError(f"amount out of range for order: {amount}")
    return raw


def build_signable_order_side(
    token_id: int,
    maker_amount: int,
    taker_amount: int,
    identity: OrderIdentity,
    timestamp: int,
    order_type: OrderType | str,
    salt: int,
    post_only: bool,
    side: Side,
) -> SignableOrder:
    """Build a signable V2 order for either side.

    Amounts are raw 6-decimal units (see :func:`to_fixed_usdc`). For a BUY
    the maker pays ``size × price`` USDC (``maker_amount``) for ``size``
    shares (``taker_amount``); a SELL swaps them: the maker gives ``size``
    shares for ``size × price`` USDC. ``timestamp`` is free-form on the wire
    (milliseconds are conventional); ``expiration`` is always zero here —
    GTC/FOK/FAK orders don't expire, and GTD callers can set ``expiration``
    on the result.

    ``post_only`` is only emitted when ``True``: the venue rejects
    ``postOnly`` on non-GTC/GTD order types, and taker orders must stay able
    to take.
    """
    order = OrderV2(
        salt=salt,
        maker=identity.maker,
        signer=identity.signer,
        token_id=token_id,
        maker_amount=maker_amount,
        taker_amount=taker_amount,
        side=int(side),
        signature_type=int(identity.signature_type),
        timestamp=timestamp,
    )
    return SignableOrder(
        order=order,
        order_type=order_type,
        expiration=0,
        post_only=True if post_only else None,
    )


def build_signable_order(
    token_id: int,
    maker_amount: int,
    taker_amount: int,
    identity: OrderIdentity,
    timestamp: int,
    order_type: OrderType | str,
    salt: int,
    post_only: bool,
) -> SignableOrder:
    """BUY convenience wrapper over :func:`build_signable_order_side`."""
    return build_signable_order_side(
        token_id,
        maker_amount,
        taker_amount,
        identity,
        timestamp,
        order_type,
        salt,
        post_only,
        Side.BUY,
    )
