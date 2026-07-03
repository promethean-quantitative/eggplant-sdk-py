"""Chain configs and the CREATE2 wallet-derivation vectors (shared with the
Rust sibling SDK)."""

from eggplant_sdk.chain import (
    AMOY,
    CTF,
    NEG_RISK_ADAPTER,
    NEG_RISK_EXCHANGE_V2,
    POLYGON,
    USDC_E,
    contract_config,
    derive_proxy_wallet,
    derive_safe_wallet,
    wallet_config,
)

# Well-known Foundry/Anvil test EOA.
TEST_EOA = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def test_polygon_neg_risk_config():
    cfg = contract_config(POLYGON, True)
    assert cfg is not None
    assert cfg.exchange_v2 == NEG_RISK_EXCHANGE_V2
    assert cfg.neg_risk_adapter == NEG_RISK_ADAPTER
    assert cfg.conditional_tokens == CTF
    assert cfg.exchange == "0xC5d563A36AE78145C45a50134d48A1215220f80a"


def test_polygon_regular_config():
    cfg = contract_config(POLYGON, False)
    assert cfg is not None
    assert cfg.neg_risk_adapter is None
    assert cfg.exchange == "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    # Collateral is pUSD, deliberately distinct from USDC.e.
    assert cfg.collateral != USDC_E


def test_unsupported_chain_is_none():
    assert contract_config(1, False) is None
    assert contract_config(1, True) is None
    assert wallet_config(1) is None


def test_derive_proxy_wallet_polygon_vector():
    # Compared lowercased (byte equality) — the shared vector's casing is
    # arbitrary; derive_proxy_wallet returns proper EIP-55.
    proxy = derive_proxy_wallet(TEST_EOA, POLYGON)
    assert proxy is not None
    assert proxy.lower() == "0x365f0ca36ae1f641e02fe3b7743673da42a13a70"


def test_derive_safe_wallet_polygon_vector():
    safe = derive_safe_wallet(TEST_EOA, POLYGON)
    assert safe is not None
    assert safe.lower() == "0xd93b25cb943d14d0d34fbaf01fc93a0f8b5f6e47"


def test_derive_proxy_wallet_amoy_unsupported():
    assert derive_proxy_wallet(TEST_EOA, AMOY) is None


def test_derive_safe_wallet_amoy_matches_polygon():
    # Same Safe factory on both networks ⇒ same derived address.
    assert derive_safe_wallet(TEST_EOA, AMOY) == derive_safe_wallet(TEST_EOA, POLYGON)


def test_derive_unsupported_chain():
    assert derive_proxy_wallet(TEST_EOA, 1) is None
    assert derive_safe_wallet(TEST_EOA, 1) is None
