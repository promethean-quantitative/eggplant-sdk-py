"""Approvals bootstrap for the **Safe wallet** (signature type 2) path.

Deploys the Safe if needed, then grants the exchange contracts their
collateral (pUSD) and CTF approvals via relayer ``SAFE`` submissions.

ERC-1271 deposit wallets (signature type 3) approve differently — batch the
``approve``/``setApprovalForAll`` calldata through
:meth:`~eggplant_sdk.relayer.RelayerClient.submit_deposit_wallet_batch`
instead.
"""

from __future__ import annotations

import asyncio
import logging

from . import _rpc
from .chain import POLYGON, contract_config, derive_safe_wallet, wallet_config
from .errors import EggplantError, InvalidDataError
from .relayer import RelayerClient
from .signer import LocalSigner

logger = logging.getLogger(__name__)

_UINT256_MAX = (1 << 256) - 1


async def ensure_approvals(signer: LocalSigner, relayer: RelayerClient, rpc_url: str) -> None:
    """Ensure the signer's Safe wallet exists and holds the approvals negRisk
    trading needs (pUSD + CTF, for the V2 exchange and the negRisk adapter).

    Idempotent: existing approvals are detected and skipped. A missing Safe
    is deployed through the relayer first.
    """
    eoa = signer.address
    safe = derive_safe_wallet(eoa, POLYGON)
    if safe is None:
        raise InvalidDataError("failed to derive Safe wallet address")

    logger.info("derived Safe wallet %s for EOA %s", safe, eoa)

    neg_risk_config = contract_config(POLYGON, True)
    if neg_risk_config is None:
        raise InvalidDataError("no neg risk contract config for Polygon")
    exchange_v2 = neg_risk_config.exchange_v2
    if exchange_v2 is None:
        raise InvalidDataError("no V2 exchange for Polygon")

    targets: list[tuple[str, str]] = [("Neg Risk CTF Exchange V2", exchange_v2)]
    if neg_risk_config.neg_risk_adapter is not None:
        targets.append(("Neg Risk Adapter", neg_risk_config.neg_risk_adapter))

    try:
        nonce = await _rpc.contract_nonce(rpc_url, safe)
        logger.info("read Safe nonce %d", nonce)
    except EggplantError:
        logger.warning("Safe not deployed at %s, deploying via relayer", safe)
        wallet_cfg = wallet_config(POLYGON)
        if wallet_cfg is None:
            raise InvalidDataError("no wallet config for Polygon") from None
        await relayer.deploy(signer, wallet_cfg.safe_factory, safe, POLYGON)
        logger.info("Safe deploy submitted, waiting for confirmation")
        await asyncio.sleep(10)
        nonce = 0

    for name, target in targets:
        allowance = await _rpc.erc20_allowance(rpc_url, neg_risk_config.collateral, safe, target)
        if allowance == 0:
            logger.info("approving pUSD for %s via relayer", name)
            data = _rpc.encode_call(
                "approve(address,uint256)", ["address", "uint256"], [target, _UINT256_MAX]
            )
            response = await relayer.submit(signer, safe, neg_risk_config.collateral, data, nonce)
            logger.info("pUSD approval for %s submitted (tx_id=%s)", name, response.transaction_id)
            nonce += 1
        else:
            logger.info("pUSD already approved for %s", name)

        ctf_approved = await _rpc.erc1155_is_approved_for_all(
            rpc_url, neg_risk_config.conditional_tokens, safe, target
        )
        if ctf_approved:
            logger.info("CTF already approved for %s", name)
        else:
            logger.info("approving CTF for %s via relayer", name)
            data = _rpc.encode_call(
                "setApprovalForAll(address,bool)", ["address", "bool"], [target, True]
            )
            response = await relayer.submit(
                signer, safe, neg_risk_config.conditional_tokens, data, nonce
            )
            logger.info("CTF approval for %s submitted (tx_id=%s)", name, response.transaction_id)
            nonce += 1

    logger.info("all approvals verified")
