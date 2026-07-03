"""Order-signing tests: the differential proof against eth-account's generic
EIP-712 implementation, the maker/signer identity table, and the Poly1271
wrapped envelope."""

import uuid
from decimal import Decimal

import pytest
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from eggplant_sdk.chain import POLYGON
from eggplant_sdk.clob.signing import (
    JS_MAX_SAFE_INT,
    ORDER_TYPE_STRING,
    ExchangeDomain,
    OrderIdentity,
    OrderSigner,
    build_signable_order,
    build_signable_order_side,
    generate_salt,
    to_fixed_usdc,
)
from eggplant_sdk.clob.types import (
    OrderType,
    OrderV1,
    Side,
    SignableOrder,
    SignatureType,
)
from eggplant_sdk.errors import InvalidDataError
from eggplant_sdk.signer import LocalSigner

# Fixed throwaway key (the well-known anvil dev key) → hermetic tests.
PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_FUNDER = "0x0000000000000000000000000000000000000001"
NIL_KEY = uuid.UUID(int=0)


def make_signer():
    return LocalSigner(PRIVATE_KEY)


def poly1271_signer() -> OrderSigner:
    return OrderSigner(
        POLYGON,
        ExchangeDomain.ctf_v2(True),
        OrderIdentity.poly1271(TEST_FUNDER),
        NIL_KEY,
    )


def signable(identity: OrderIdentity, salt: int) -> SignableOrder:
    return build_signable_order(
        111,
        4_850_000,
        5_000_000,
        identity,
        1_700_000_000_000,
        OrderType.GTC,
        salt,
        False,
    )


def _eth_account_typed_data(domain: ExchangeDomain, order) -> "encode_typed_data":
    """The same order encoded through eth-account's generic EIP-712
    implementation — the independent reference the fast path must equal."""
    return encode_typed_data(
        full_message={
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Order": [
                    {"name": "salt", "type": "uint256"},
                    {"name": "maker", "type": "address"},
                    {"name": "signer", "type": "address"},
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "makerAmount", "type": "uint256"},
                    {"name": "takerAmount", "type": "uint256"},
                    {"name": "side", "type": "uint8"},
                    {"name": "signatureType", "type": "uint8"},
                    {"name": "timestamp", "type": "uint256"},
                    {"name": "metadata", "type": "bytes32"},
                    {"name": "builder", "type": "bytes32"},
                ],
            },
            "primaryType": "Order",
            "domain": {
                "name": domain.name,
                "version": domain.version,
                "chainId": POLYGON,
                "verifyingContract": domain.verifying_contract,
            },
            "message": {
                "salt": order.salt,
                "maker": order.maker,
                "signer": order.signer,
                "tokenId": order.token_id,
                "makerAmount": order.maker_amount,
                "takerAmount": order.taker_amount,
                "side": order.side,
                "signatureType": order.signature_type,
                "timestamp": order.timestamp,
                "metadata": order.metadata,
                "builder": order.builder,
            },
        }
    )


def test_build_signable_order_sets_amounts():
    identity = OrderIdentity.poly1271(TEST_FUNDER)
    order = signable(identity, generate_salt()).order
    assert order.maker_amount == 4_850_000
    assert order.taker_amount == 5_000_000
    assert order.token_id == 111
    assert order.side == int(Side.BUY)
    assert order.signature_type == int(SignatureType.POLY1271)
    assert order.maker == TEST_FUNDER
    assert order.signer == TEST_FUNDER


def test_identity_maker_signer_table():
    """The venue's maker/signer table, per signature type."""
    eoa = make_signer().address
    wallet = "0x" + "22" * 20

    identity = OrderIdentity.eoa(eoa)
    assert (identity.maker, identity.signer) == (eoa, eoa)
    assert int(identity.signature_type) == 0

    identity = OrderIdentity.proxy(eoa, wallet)
    assert (identity.maker, identity.signer) == (wallet, eoa)
    assert int(identity.signature_type) == 1

    identity = OrderIdentity.gnosis_safe(eoa, wallet)
    assert (identity.maker, identity.signer) == (wallet, eoa)
    assert int(identity.signature_type) == 2

    identity = OrderIdentity.poly1271(wallet)
    assert (identity.maker, identity.signer) == (wallet, wallet)
    assert int(identity.signature_type) == 3

    # And the order builder threads the identity through the struct.
    order = signable(OrderIdentity.proxy(eoa, wallet), 7).order
    assert order.maker == wallet
    assert order.signer == eoa
    assert order.signature_type == 1


def test_sign_order_is_deterministic_for_identical_input():
    """Pins the premise behind sign-once-per-leg fan-out: ECDSA over the same
    digest is deterministic (RFC 6979), so one signature serves every
    copy."""
    signer = make_signer()
    fast = poly1271_signer()
    identity = OrderIdentity.poly1271(TEST_FUNDER)
    a = fast.sign_order(signable(identity, 7), signer)
    b = fast.sign_order(signable(identity, 7), signer)
    assert a == b


def test_domain_separator_matches_eth_account_for_all_deployments():
    """The precomputed domain separator must equal eth-account's generic
    EIP-712 implementation for every deployment we know."""
    for domain, label in [
        (ExchangeDomain.ctf_v2(True), "negRisk v2"),
        (ExchangeDomain.ctf_v2(False), "regular v2"),
        (ExchangeDomain.combos_v3(), "combos v3"),
    ]:
        ours = OrderSigner(
            POLYGON, domain, OrderIdentity.eoa(TEST_FUNDER), NIL_KEY
        ).domain_separator
        reference = _eth_account_typed_data(
            domain, signable(OrderIdentity.eoa(TEST_FUNDER), 7).order
        )
        assert ours == reference.header, label


def test_eoa_digest_matches_eth_account_signing_hash():
    """For plain-ECDSA types the digest we sign must equal the generic
    EIP-712 signing hash — proving the whole fast path (not just the domain)
    is equivalent to the generic implementation."""
    signer = make_signer()
    eoa = signer.address
    domain = ExchangeDomain.ctf_v2(True)
    order_signer = OrderSigner(POLYGON, domain, OrderIdentity.eoa(eoa), NIL_KEY)

    order = signable(OrderIdentity.eoa(eoa), 7)

    reference = _eth_account_typed_data(domain, order.order)
    expected_digest = keccak(b"\x19\x01" + reference.header + reference.body)
    expected_sig = signer.sign_hash(expected_digest).to_hex()

    signed = order_signer.sign_order(order, signer)
    assert signed.signature == expected_sig


def test_ecdsa_signature_wire_format():
    """ECDSA signatures for types 0/1/2 render as 0x + 65-byte hex with
    v ∈ {27, 28} — the venue's expected format."""
    signer = make_signer()
    eoa = signer.address
    for identity in [
        OrderIdentity.eoa(eoa),
        OrderIdentity.proxy(eoa, "0x" + "22" * 20),
        OrderIdentity.gnosis_safe(eoa, "0x" + "33" * 20),
    ]:
        order_signer = OrderSigner(POLYGON, ExchangeDomain.ctf_v2(True), identity, NIL_KEY)
        signed = order_signer.sign_order(signable(identity, 7), signer)
        assert len(signed.signature) == 2 + 130, identity
        v = int(signed.signature[-2:], 16)
        assert v in (27, 28), identity


def test_poly1271_wrapped_envelope_shape():
    """The Poly1271 wrapped envelope: sig ‖ domain separator ‖ contents hash
    ‖ hex(type string) ‖ big-endian length."""
    signer = make_signer()
    fast = poly1271_signer()
    identity = OrderIdentity.poly1271(TEST_FUNDER)
    signed = fast.sign_order(signable(identity, 7), signer)

    wrapped = signed.signature
    assert wrapped.startswith("0x")
    type_suffix_len = len(ORDER_TYPE_STRING) * 2 + 4
    assert len(wrapped) == 2 + 130 + 64 + 64 + type_suffix_len
    # The domain separator rides right after the 65-byte signature.
    assert wrapped[2 + 130 : 2 + 130 + 64] == fast.domain_separator.hex()
    # The suffix ends with the big-endian u16 length of the type string.
    assert int(wrapped[-4:], 16) == len(ORDER_TYPE_STRING)


def test_signing_a_v1_payload_is_an_error():
    fast = poly1271_signer()
    v1 = SignableOrder(order=OrderV1(), order_type=OrderType.GTC)
    with pytest.raises(InvalidDataError):
        fast.sign_order(v1, make_signer())


def test_build_signable_order_is_gtc_with_zero_expiration():
    order = signable(OrderIdentity.poly1271(TEST_FUNDER), 7)
    assert order.order_type is OrderType.GTC
    assert order.expiration == 0


def test_build_signable_order_salt_is_js_safe():
    salt = generate_salt()
    order = build_signable_order(
        111,
        4_850_000,
        5_000_000,
        OrderIdentity.poly1271(TEST_FUNDER),
        1_700_000_000_000,
        OrderType.GTC,
        salt,
        False,
    )
    assert 0 < order.order.salt <= JS_MAX_SAFE_INT


def test_marketable_sell_builds_fak_taker_no_post_only():
    # A marketable SELL is a FAK taker with `post_only` off — the builder
    # emits no `postOnly` when off (the venue rejects post-only on FAK).
    # Amounts are the SELL swap: maker = `size` shares, taker = `size ×
    # price` USDC.
    size = Decimal(50)
    price = Decimal("0.002")
    order = build_signable_order_side(
        333,
        to_fixed_usdc(size),
        to_fixed_usdc(size * price),
        OrderIdentity.poly1271(TEST_FUNDER),
        1_700_000_000_000,
        OrderType.FAK,
        generate_salt(),
        False,
        Side.SELL,
    )
    assert order.order_type is OrderType.FAK
    assert order.post_only is None, "FAK must not carry post_only"


def test_to_fixed_usdc_truncates_to_six_decimals():
    assert to_fixed_usdc(Decimal("4.85")) == 4_850_000
    assert to_fixed_usdc(Decimal("0.0000019")) == 1
    assert to_fixed_usdc(Decimal(0)) == 0
    with pytest.raises(InvalidDataError):
        to_fixed_usdc(Decimal(-1))
