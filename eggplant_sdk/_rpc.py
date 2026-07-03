"""Minimal JSON-RPC ``eth_call`` helpers for the convert engine and the
approvals bootstrap. Read-only — nothing here signs or submits transactions.
"""

from __future__ import annotations

from typing import Any

import httpx
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak

from .errors import InvalidDataError


def selector(signature: str) -> bytes:
    """The 4-byte function selector of a Solidity signature."""
    return keccak(signature.encode())[:4]


def encode_call(signature: str, types: list[str], args: list[Any]) -> bytes:
    """``selector ‖ abi.encode(args)`` calldata for one function call."""
    return selector(signature) + abi_encode(types, args)


async def eth_call(rpc_url: str, to: str, data: bytes) -> bytes:
    """One ``eth_call`` against ``latest``. Errors are surfaced, never masked
    as empty results — a masked failed read can silently look like "nothing
    to do"."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": to, "data": "0x" + data.hex()}, "latest"],
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as http:
            response = await http.post(rpc_url, json=payload)
    except httpx.HTTPError as e:
        raise InvalidDataError(f"RPC connect failed: {e}") from e
    if response.status_code >= 400:
        raise InvalidDataError(f"RPC error {response.status_code}: {response.text[:300]}")
    body = response.json()
    if "error" in body:
        raise InvalidDataError(f"RPC error: {body['error']}")
    result = body.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise InvalidDataError(f"RPC returned no result: {body}")
    return bytes.fromhex(result[2:])


async def erc20_balance_of(rpc_url: str, token: str, owner: str) -> int:
    data = encode_call("balanceOf(address)", ["address"], [owner])
    result = await eth_call(rpc_url, token, data)
    return _decode_single(result, "uint256", "balanceOf")


async def erc20_allowance(rpc_url: str, token: str, owner: str, spender: str) -> int:
    data = encode_call("allowance(address,address)", ["address", "address"], [owner, spender])
    result = await eth_call(rpc_url, token, data)
    return _decode_single(result, "uint256", "allowance")


async def erc1155_balance_of_batch(
    rpc_url: str, token: str, accounts: list[str], ids: list[int]
) -> list[int]:
    data = encode_call(
        "balanceOfBatch(address[],uint256[])", ["address[]", "uint256[]"], [accounts, ids]
    )
    result = await eth_call(rpc_url, token, data)
    try:
        (balances,) = abi_decode(["uint256[]"], result)
    except Exception as e:  # eth_abi raises several decode error types
        raise InvalidDataError(f"balanceOfBatch failed: {e}") from e
    return list(balances)


async def erc1155_is_approved_for_all(
    rpc_url: str, token: str, account: str, operator: str
) -> bool:
    data = encode_call(
        "isApprovedForAll(address,address)", ["address", "address"], [account, operator]
    )
    result = await eth_call(rpc_url, token, data)
    return bool(_decode_single(result, "bool", "isApprovedForAll"))


async def contract_nonce(rpc_url: str, contract: str) -> int:
    """A Gnosis Safe's ``nonce()``. Raises when the contract is not deployed
    (the call returns no data)."""
    result = await eth_call(rpc_url, contract, selector("nonce()"))
    return _decode_single(result, "uint256", "nonce")


def _decode_single(result: bytes, abi_type: str, label: str) -> Any:
    try:
        (value,) = abi_decode([abi_type], result)
    except Exception as e:
        raise InvalidDataError(f"{label} failed to decode: {e}") from e
    return value
