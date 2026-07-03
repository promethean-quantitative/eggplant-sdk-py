"""Polymarket relayer-v2 client: gasless transaction submission for Safe
wallets and ERC-1271 deposit wallets.

Three submission paths, all ``POST {host}/submit``:

- :meth:`RelayerClient.submit` — a Gnosis ``SafeTx`` (type ``SAFE``), signed
  with the Safe ``eth_sign`` convention (``v = parity + 31``, EIP-191
  re-hash). Used for approvals and arbitrary calls from a Safe wallet.
- :meth:`RelayerClient.deploy` — Safe wallet creation (type ``SAFE-CREATE``).
- :meth:`RelayerClient.submit_deposit_wallet_batch` — the ``DepositWallet``
  batch EIP-712 (type ``WALLET``): N calls executed atomically from a deposit
  wallet. The merge/convert/redeem/split engine rides this path.

The EIP-712 layouts here are pinned by typehash tests and a golden vector
shared with the Rust sibling SDK. Requires relayer API credentials
(Polymarket builder program).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from eth_utils import keccak, to_canonical_address

from .chain import DEPOSIT_WALLET_FACTORY, POLYGON, RELAYER_HOST
from .errors import ApiError, InvalidDataError, RelayerQuotaExhaustedError
from .signer import LocalSigner

logger = logging.getLogger(__name__)

# keccak256("EIP712Domain(uint256 chainId,address verifyingContract)")
DOMAIN_SEPARATOR_TYPEHASH = bytes.fromhex(
    "47e79534a245952e8b16893a336b85a3d9ea9fa8c573f3d803afb92a79469218"
)

# keccak256("SafeTx(address to,uint256 value,bytes data,uint8 operation,
#   uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,
#   address refundReceiver,uint256 nonce)")
SAFE_TX_TYPEHASH = bytes.fromhex("bb8310d486368db6bd6f849402fdd73ad53d316b5a4b2644ad6efe0f941286d8")

# keccak256("EIP712Domain(string name,uint256 chainId,address verifyingContract)")
CREATE_DOMAIN_TYPEHASH = bytes.fromhex(
    "8cad95687ba82c2ce50e74f7b754645e5117c3a5bec8151c0726d5857980a866"
)

# keccak256("Polymarket Contract Proxy Factory")
SAFE_FACTORY_NAME_HASH = bytes.fromhex(
    "0e50835e49a5f2de690010a802604667466241e3a0473df3748c77850723de32"
)

# keccak256("CreateProxy(address paymentToken,uint256 payment,address paymentReceiver)")
CREATE_PROXY_TYPEHASH = bytes.fromhex(
    "dee5f5588156b735c3bff14a54c9acefc845807cec91b7fd0809fa3deccab363"
)

# ── Deposit wallet (signature type 3) ───────────────────────────────

# keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
DW_DOMAIN_TYPEHASH = bytes.fromhex(
    "8b73c3c69bb8fe3d512ecc4cf759cc79239f7b179b0ffacaa9a75d522b39400f"
)

# keccak256("DepositWallet")
DW_NAME_HASH = bytes.fromhex("d682b529a17cda19aa275f3a050608f9e9401fadd1b0d233d81519972295828b")

# keccak256("1")
DW_VERSION_HASH = bytes.fromhex("c89efdaa54c0f20c7adf612882df0950f5a951637e0307cdcb4c672f298b8bc6")

# keccak256("Call(address target,uint256 value,bytes data)")
CALL_TYPEHASH = bytes.fromhex("84fa2cf05cd88e992eae77e851af68a4ee278dcff6ef504e487a55b3baadfbe5")

# keccak256("Batch(address wallet,uint256 nonce,uint256 deadline,Call[] calls)
#   Call(address target,uint256 value,bytes data)")  (one string, no break)
BATCH_TYPEHASH = bytes.fromhex("712ef66e8362c387e862cabf0923c209db0fa24cfc97d25eccba7c86f3ee1dd3")

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
_DEPOSIT_WALLET_DEADLINE_SECS = 600


def _u256(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _addr32(address: str) -> bytes:
    return to_canonical_address(address).rjust(32, b"\x00")


def _to_hex(data: bytes) -> str:
    return "0x" + data.hex()


@dataclass
class SubmitResponse:
    """Relayer acknowledgement of a submission."""

    transaction_id: str
    transaction_hash: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubmitResponse:
        return cls(
            transaction_id=data["transactionID"],
            transaction_hash=data.get("transactionHash"),
        )


@dataclass
class DepositWalletCall:
    """One call of a ``DepositWallet`` batch (``value`` is always zero on
    this path)."""

    target: str
    data: bytes


def is_quota_response(status: int, body: str) -> bool:
    """Classify a failed relayer response as quota exhaustion. It usually
    arrives as HTTP 429, but not always — a non-429 body that mentions
    "quota" is treated the same so it routes to the caller's retry path
    instead of being misread as a hard error."""
    return status == 429 or "quota" in body.lower()


def parse_quota_reset(body: str) -> int | None:
    marker = "resets in "
    idx = body.find(marker)
    if idx < 0:
        return None
    rest = body[idx + len(marker) :]
    digits = ""
    for ch in rest:
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits else None


class RelayerClient:
    """Relayer client. Requires a relayer API key pair (builder program)."""

    def __init__(
        self,
        api_key: str,
        api_key_address: str,
        host: str = RELAYER_HOST,
        chain_id: int = POLYGON,
    ):
        """A client against ``host`` (no trailing slash; defaults to the
        production relayer) on ``chain_id`` (used for :meth:`submit`'s
        ``SafeTx`` domain)."""
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self._host = host
        self._chain_id = chain_id
        self._api_key = api_key
        self._api_key_address = api_key_address

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> RelayerClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "RELAYER_API_KEY": self._api_key,
            "RELAYER_API_KEY_ADDRESS": self._api_key_address,
        }

    async def submit(
        self, signer: LocalSigner, safe: str, to: str, data: bytes, nonce: int
    ) -> SubmitResponse:
        """Submit one call from a Safe wallet (type ``SAFE``): sign the
        ``SafeTx`` EIP-712 hash with the Safe ``eth_sign`` convention and
        relay it."""
        tx_hash = compute_safe_tx_hash(safe, self._chain_id, to, data, nonce)

        # Safe eth_sign: v = parity + 31 (tells the Safe to verify with the
        # EIP-191 prefix).
        signature = signer.sign_message(tx_hash).to_hex(v_base=31)

        request = {
            "from": signer.address,
            "to": to,
            "proxyWallet": safe,
            "data": _to_hex(data),
            "nonce": str(nonce),
            "signature": signature,
            "signatureParams": {
                "gasPrice": "0",
                "operation": "0",
                "safeTxnGas": "0",
                "baseGas": "0",
                "gasToken": _ZERO_ADDRESS,
                "refundReceiver": _ZERO_ADDRESS,
            },
            "type": "SAFE",
        }
        return await self._post_submit(request)

    async def deploy(
        self, signer: LocalSigner, safe_factory: str, safe_address: str, chain_id: int
    ) -> SubmitResponse:
        """Deploy the signer's Safe wallet (type ``SAFE-CREATE``)."""
        logger.info("deploying Safe wallet %s via relayer", safe_address)

        eip712_hash = compute_create_proxy_hash(safe_factory, chain_id)
        signature = signer.sign_hash(eip712_hash).to_hex(v_base=27)

        request = {
            "from": signer.address,
            "to": safe_factory,
            "proxyWallet": safe_address,
            "data": "0x",
            "signature": signature,
            "signatureParams": {
                "paymentToken": _ZERO_ADDRESS,
                "payment": "0",
                "paymentReceiver": _ZERO_ADDRESS,
            },
            "type": "SAFE-CREATE",
        }

        response = await self._http.post(
            f"{self._host}/submit", headers=self._headers, json=request
        )
        if response.status_code >= 400:
            raise InvalidDataError(
                f"relayer deploy error ({response.status_code}): {response.text}"
            )
        result = SubmitResponse.from_dict(response.json())
        logger.info("Safe deploy submitted (tx_id=%s)", result.transaction_id)
        return result

    async def get_wallet_nonce(self, eoa: str) -> int:
        """The EOA's next ``WALLET``-type nonce (``GET /nonce``). Tolerates
        the relayer answering with a number or a numeric string."""
        response = await self._http.get(
            f"{self._host}/nonce",
            params={"address": eoa, "type": "WALLET"},
            headers=self._headers,
        )
        if response.status_code >= 400:
            raise InvalidDataError(f"nonce request failed: {response.text}")
        body = response.json()
        logger.debug("relayer nonce response: %s", body)
        nonce = body.get("nonce")
        try:
            return int(nonce)
        except (TypeError, ValueError):
            raise InvalidDataError(f"no nonce in response: {body}") from None

    async def submit_deposit_wallet_batch(
        self,
        signer: LocalSigner,
        wallet: str,
        chain_id: int,
        calls: list[DepositWalletCall],
    ) -> SubmitResponse:
        """Submit an atomic batch of calls from an ERC-1271 deposit wallet
        (type ``WALLET``): fetch the EOA's nonce, sign the ``Batch`` EIP-712
        hash, and relay against the deposit-wallet factory with a 10-minute
        deadline."""
        nonce = await self.get_wallet_nonce(signer.address)
        deadline = int(time.time()) + _DEPOSIT_WALLET_DEADLINE_SECS

        batch_hash = compute_deposit_wallet_batch_hash(wallet, chain_id, nonce, deadline, calls)
        signature = signer.sign_hash(batch_hash).to_hex(v_base=27)

        request = {
            "type": "WALLET",
            "from": signer.address,
            "to": DEPOSIT_WALLET_FACTORY,
            "nonce": str(nonce),
            "signature": signature,
            "depositWalletParams": {
                "depositWallet": wallet,
                "deadline": str(deadline),
                "calls": [
                    {"target": call.target, "value": "0", "data": _to_hex(call.data)}
                    for call in calls
                ],
            },
        }
        return await self._post_submit(request)

    async def _post_submit(self, request: dict[str, Any]) -> SubmitResponse:
        response = await self._http.post(
            f"{self._host}/submit", headers=self._headers, json=request
        )
        if response.status_code >= 400:
            body = response.text
            # Quota exhaustion isn't always a clean 429 — match the body too,
            # so a non-429 "quota exceeded" isn't misrouted to InvalidDataError
            # (where a retry cycle would burn its rebuild attempts hammering
            # the same dead quota). `resets_in_secs` is for the log only; pick
            # your own retry cadence, the relayer's hint is unreliable (it
            # reports ~3600s while the quota actually frees in well under a
            # minute).
            if is_quota_response(response.status_code, body):
                raise RelayerQuotaExhaustedError(parse_quota_reset(body) or 3600)
            raise InvalidDataError(f"relayer error ({response.status_code}): {body}")
        try:
            return SubmitResponse.from_dict(response.json())
        except (KeyError, ValueError) as e:
            raise ApiError(response.status_code, response.text[:300]) from e


def compute_deposit_wallet_batch_hash(
    wallet: str,
    chain_id: int,
    nonce: int,
    deadline: int,
    calls: list[DepositWalletCall],
) -> bytes:
    """The EIP-712 hash a ``WALLET``-type batch submission signs."""
    domain_separator = keccak(
        DW_DOMAIN_TYPEHASH + DW_NAME_HASH + DW_VERSION_HASH + _u256(chain_id) + _addr32(wallet)
    )

    # Hash each call: keccak256(CALL_TYPEHASH || target || value || keccak256(data))
    calls_concat = b""
    for call in calls:
        call_hash = keccak(CALL_TYPEHASH + _addr32(call.target) + _u256(0) + keccak(call.data))
        calls_concat += call_hash
    calls_hash = keccak(calls_concat)

    struct_hash = keccak(
        BATCH_TYPEHASH + _addr32(wallet) + _u256(nonce) + _u256(deadline) + calls_hash
    )
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def compute_create_proxy_hash(safe_factory: str, chain_id: int) -> bytes:
    """The EIP-712 hash a ``SAFE-CREATE`` deploy signs."""
    # Domain: EIP712Domain(string name, uint256 chainId, address verifyingContract)
    domain_separator = keccak(
        CREATE_DOMAIN_TYPEHASH + SAFE_FACTORY_NAME_HASH + _u256(chain_id) + _addr32(safe_factory)
    )
    # Struct: CreateProxy(address paymentToken, uint256 payment, address
    # paymentReceiver) — all values zero.
    struct_hash = keccak(CREATE_PROXY_TYPEHASH + _u256(0) + _u256(0) + _u256(0))
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def compute_safe_tx_hash(safe: str, chain_id: int, to: str, data: bytes, nonce: int) -> bytes:
    """The ``SafeTx`` EIP-712 hash a ``SAFE``-type submission signs (all gas
    params zero)."""
    # Domain separator: abi.encode(typehash, chainId, verifyingContract)
    domain_separator = keccak(DOMAIN_SEPARATOR_TYPEHASH + _u256(chain_id) + _addr32(safe))

    # Struct hash: 11 words (typehash + to + value + dataHash + operation..nonce).
    # All gas params are zero; only to, dataHash, and nonce are non-zero.
    struct_hash = keccak(
        SAFE_TX_TYPEHASH
        + _addr32(to)
        + _u256(0)  # value
        + keccak(data)
        + _u256(0)  # operation
        + _u256(0)  # safeTxGas
        + _u256(0)  # baseGas
        + _u256(0)  # gasPrice
        + _u256(0)  # gasToken
        + _u256(0)  # refundReceiver
        + _u256(nonce)
    )
    return keccak(b"\x19\x01" + domain_separator + struct_hash)
