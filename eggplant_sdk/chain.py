"""Chain-level constants: deployed contract addresses, wallet factories,
CREATE2 wallet derivations, and default service hosts.

Everything here is a public on-chain or venue constant.

Collateral: pUSD vs USDC.e
--------------------------

The current venue's exchange collateral (:attr:`ContractConfig.collateral`) is
**pUSD** (``0xC011…2DFB``), *not* USDC.e. Bridged USDC.e (:data:`USDC_E`) is
wrapped into pUSD via the :data:`COLLATERAL_ONRAMP`. Approving or funding the
wrong token is a fund-loss-adjacent mistake — double-check which one an
operation needs.

Addresses are checksummed ``str`` throughout the SDK.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_utils import keccak, to_canonical_address, to_checksum_address

#: Chain id for Polygon mainnet.
POLYGON = 137

#: Chain id for the Polygon Amoy testnet.
AMOY = 80002

# ---------------------------------------------------------------------------
# Default service hosts
# ---------------------------------------------------------------------------

#: CLOB REST API.
CLOB_HOST = "https://clob.polymarket.com"
#: Gamma API (event/market metadata).
GAMMA_HOST = "https://gamma-api.polymarket.com"
#: Data API (wallet positions).
DATA_API_HOST = "https://data-api.polymarket.com"
#: Relayer v2 (gasless Safe / deposit-wallet transaction submission).
RELAYER_HOST = "https://relayer-v2.polymarket.com"
#: Market-data WebSocket channel (order books, price changes).
WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
#: User WebSocket channel (own trades and order lifecycle events).
WS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

# ---------------------------------------------------------------------------
# Polygon contract addresses
# ---------------------------------------------------------------------------

#: Conditional Tokens Framework (ERC-1155 outcome tokens).
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

#: negRisk adapter: convert / merge / redeem / split for negRisk events.
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

#: negRisk CTF Exchange V2 — the EIP-712 verifying contract for negRisk order
#: signing (domain version "2").
NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"

#: CTF Exchange V2 for non-negRisk (regular binary) markets.
EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"

#: Combos exchange V3 (parlay / RFQ orders, domain version "3").
EXCHANGE_V3 = "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa"

#: Bridged USDC.e — the *input* to the collateral onramp, not the exchange
#: collateral itself (see the module docs on pUSD vs USDC.e).
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

#: Collateral onramp: wraps USDC.e into pUSD, the exchange collateral.
COLLATERAL_ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"

#: ``DepositWallet`` factory — the ``to`` target for relayer ``WALLET`` batch
#: submissions (ERC-1271 deposit wallets, signature type ``POLY1271``).
DEPOSIT_WALLET_FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"

#: Exchange collateral (pUSD) on both supported chains.
_COLLATERAL = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

# ---------------------------------------------------------------------------
# Per-chain configs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractConfig:
    """Deployed exchange-side contract addresses for one (chain, negRisk?) pair."""

    #: Legacy CTF Exchange (V1 orders).
    exchange: str
    #: CTF Exchange V2 (V2 orders — the current order flow).
    exchange_v2: str | None
    #: Exchange collateral token. On the current venue this is **pUSD**, not
    #: USDC.e — see the module docs.
    collateral: str
    #: Conditional Tokens Framework.
    conditional_tokens: str
    #: negRisk adapter; present only in negRisk configs. Must be approved for
    #: token transfers to trade negRisk markets.
    neg_risk_adapter: str | None


@dataclass(frozen=True)
class WalletConfig:
    """Wallet factory addresses for CREATE2 address derivation on one chain."""

    #: Factory for Polymarket proxy wallets (Magic/email users, signature
    #: type 1). Not deployed on every chain.
    proxy_factory: str | None
    #: Factory for 1-of-1 Gnosis Safe wallets (browser-wallet users,
    #: signature type 2).
    safe_factory: str


_CONTRACT_CONFIGS: dict[tuple[int, bool], ContractConfig] = {
    (POLYGON, False): ContractConfig(
        exchange="0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
        exchange_v2=EXCHANGE_V2,
        collateral=_COLLATERAL,
        conditional_tokens=CTF,
        neg_risk_adapter=None,
    ),
    (POLYGON, True): ContractConfig(
        exchange="0xC5d563A36AE78145C45a50134d48A1215220f80a",
        exchange_v2=NEG_RISK_EXCHANGE_V2,
        collateral=_COLLATERAL,
        conditional_tokens=CTF,
        neg_risk_adapter=NEG_RISK_ADAPTER,
    ),
    (AMOY, False): ContractConfig(
        exchange="0xdFE02Eb6733538f8Ea35D585af8DE5958AD99E40",
        exchange_v2=EXCHANGE_V2,
        collateral=_COLLATERAL,
        conditional_tokens="0x69308FB512518e39F9b16112fA8d994F4e2Bf8bB",
        neg_risk_adapter=None,
    ),
    (AMOY, True): ContractConfig(
        exchange="0xC5d563A36AE78145C45a50134d48A1215220f80a",
        exchange_v2=NEG_RISK_EXCHANGE_V2,
        collateral=_COLLATERAL,
        conditional_tokens="0x69308FB512518e39F9b16112fA8d994F4e2Bf8bB",
        neg_risk_adapter=NEG_RISK_ADAPTER,
    ),
}

_WALLET_CONFIGS: dict[int, WalletConfig] = {
    POLYGON: WalletConfig(
        proxy_factory="0xaB45c5A4B0c941a2F231C04C3f49182e1A254052",
        safe_factory="0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b",
    ),
    # Proxy factory unsupported on Amoy.
    AMOY: WalletConfig(
        proxy_factory=None,
        safe_factory="0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b",
    ),
}


def contract_config(chain_id: int, is_neg_risk: bool) -> ContractConfig | None:
    """The :class:`ContractConfig` for a chain, for either the regular or the
    negRisk exchange family. ``None`` for unsupported chains."""
    return _CONTRACT_CONFIGS.get((chain_id, is_neg_risk))


def wallet_config(chain_id: int) -> WalletConfig | None:
    """The :class:`WalletConfig` for a chain. ``None`` for unsupported chains."""
    return _WALLET_CONFIGS.get(chain_id)


# ---------------------------------------------------------------------------
# CREATE2 wallet derivation
# ---------------------------------------------------------------------------

#: Init code hash for Polymarket proxy wallets (EIP-1167 minimal proxy).
PROXY_INIT_CODE_HASH = bytes.fromhex(
    "d21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b"
)

#: Init code hash for Polymarket-deployed Gnosis Safe wallets.
SAFE_INIT_CODE_HASH = bytes.fromhex(
    "2bce2127ff07fb632d16c8347c4ebf501f4841168bed00d9e6ef715ddb6fcecf"
)


def _create2(factory: str, salt: bytes, init_code_hash: bytes) -> str:
    digest = keccak(b"\xff" + to_canonical_address(factory) + salt + init_code_hash)
    return to_checksum_address(digest[12:])


def derive_proxy_wallet(eoa: str, chain_id: int) -> str | None:
    """Derive the Polymarket proxy wallet address (signature type 1 funder)
    for an EOA via CREATE2. Salt is ``keccak256`` of the packed 20-byte
    address.

    ``None`` when the chain has no proxy factory.
    """
    config = wallet_config(chain_id)
    if config is None or config.proxy_factory is None:
        return None
    salt = keccak(to_canonical_address(eoa))
    return _create2(config.proxy_factory, salt, PROXY_INIT_CODE_HASH)


def derive_safe_wallet(eoa: str, chain_id: int) -> str | None:
    """Derive the 1-of-1 Gnosis Safe wallet address (signature type 2 funder)
    for an EOA via CREATE2. Salt is ``keccak256`` of the address ABI-padded to
    32 bytes.

    ``None`` when the chain is unsupported.
    """
    config = wallet_config(chain_id)
    if config is None:
        return None
    salt = keccak(to_canonical_address(eoa).rjust(32, b"\x00"))
    return _create2(config.safe_factory, salt, SAFE_INIT_CODE_HASH)
