"""Relayer EIP-712 typehashes, the golden create-proxy hash, and quota
classification."""

from eth_utils import keccak

from eggplant_sdk.relayer import (
    BATCH_TYPEHASH,
    CALL_TYPEHASH,
    CREATE_DOMAIN_TYPEHASH,
    CREATE_PROXY_TYPEHASH,
    DOMAIN_SEPARATOR_TYPEHASH,
    DW_DOMAIN_TYPEHASH,
    DW_NAME_HASH,
    DW_VERSION_HASH,
    SAFE_FACTORY_NAME_HASH,
    SAFE_TX_TYPEHASH,
    DepositWalletCall,
    compute_create_proxy_hash,
    compute_deposit_wallet_batch_hash,
    compute_safe_tx_hash,
    is_quota_response,
    parse_quota_reset,
)


def test_quota_response_matches_429_or_body():
    # The clean signal.
    assert is_quota_response(429, "")
    assert is_quota_response(429, "whatever")
    # Non-429 statuses that name the quota still route to the retry path
    # (case-insensitive) — otherwise a retry cycle burns its rebuild attempts
    # hammering the same exhausted quota before failing.
    assert is_quota_response(403, "API quota exceeded")
    assert is_quota_response(400, "Monthly QUOTA reached")
    # Genuine non-quota failures stay hard errors.
    assert not is_quota_response(400, "bad request")
    assert not is_quota_response(500, "internal error")


def test_quota_reset_parses_from_body():
    assert parse_quota_reset("quota exceeded, resets in 3600 seconds") == 3600
    assert parse_quota_reset("no hint here") is None


def test_domain_separator_typehash_matches():
    assert (
        keccak(b"EIP712Domain(uint256 chainId,address verifyingContract)")
        == DOMAIN_SEPARATOR_TYPEHASH
    )


def test_safe_tx_typehash_matches():
    assert (
        keccak(
            b"SafeTx(address to,uint256 value,bytes data,uint8 operation,"
            b"uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,"
            b"address refundReceiver,uint256 nonce)"
        )
        == SAFE_TX_TYPEHASH
    )


def test_create_domain_typehash_matches():
    assert (
        keccak(b"EIP712Domain(string name,uint256 chainId,address verifyingContract)")
        == CREATE_DOMAIN_TYPEHASH
    )


def test_safe_factory_name_hash_matches():
    assert keccak(b"Polymarket Contract Proxy Factory") == SAFE_FACTORY_NAME_HASH


def test_create_proxy_typehash_matches():
    assert (
        keccak(b"CreateProxy(address paymentToken,uint256 payment,address paymentReceiver)")
        == CREATE_PROXY_TYPEHASH
    )


def test_create_proxy_hash_matches_reference_client():
    factory = "0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b"
    digest = compute_create_proxy_hash(factory, 137)
    assert digest == bytes.fromhex(
        "563ac315294c5be01ab1f3b04a5abdfa39e8317a9d90679d4e63caf760b126a4"
    )
    assert compute_create_proxy_hash(factory, 80002) != digest


def test_safe_tx_hash_deterministic_and_nonce_sensitive():
    safe = "0xd93b25Cb943D14d0d34FBAf01fc93a0F8b5f6e47"
    to = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
    data = bytes([0x09, 0x5E, 0xA7, 0xB3])

    h1 = compute_safe_tx_hash(safe, 137, to, data, 0)
    h2 = compute_safe_tx_hash(safe, 137, to, data, 0)
    assert h1 == h2
    assert compute_safe_tx_hash(safe, 137, to, data, 1) != h1


def test_dw_domain_typehash_matches():
    assert (
        keccak(
            b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
        )
        == DW_DOMAIN_TYPEHASH
    )


def test_dw_name_and_version_hashes_match():
    assert keccak(b"DepositWallet") == DW_NAME_HASH
    assert keccak(b"1") == DW_VERSION_HASH


def test_call_typehash_matches():
    assert keccak(b"Call(address target,uint256 value,bytes data)") == CALL_TYPEHASH


def test_batch_typehash_matches():
    assert (
        keccak(
            b"Batch(address wallet,uint256 nonce,uint256 deadline,Call[] calls)"
            b"Call(address target,uint256 value,bytes data)"
        )
        == BATCH_TYPEHASH
    )


def test_batch_hash_deterministic_and_input_sensitive():
    wallet = "0xd93b25Cb943D14d0d34FBAf01fc93a0F8b5f6e47"
    calls = [
        DepositWalletCall(
            target="0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296", data=bytes([0xDE, 0xAD])
        )
    ]
    h1 = compute_deposit_wallet_batch_hash(wallet, 137, 5, 1_000, calls)
    h2 = compute_deposit_wallet_batch_hash(wallet, 137, 5, 1_000, calls)
    assert h1 == h2
    assert compute_deposit_wallet_batch_hash(wallet, 137, 6, 1_000, calls) != h1
    assert compute_deposit_wallet_batch_hash(wallet, 137, 5, 1_001, calls) != h1
