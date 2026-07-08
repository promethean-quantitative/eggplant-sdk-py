"""Redeem grouping and redemption-building tests."""

from eggplant_sdk.data import Position
from eggplant_sdk.redeem import Redemption, build_redemptions, group_redeemable


def cid_hex(tag: int) -> tuple[bytes, str]:
    raw = bytes(31) + bytes([tag])
    return raw, "0x" + raw.hex()


def pos(asset: str, condition_id: str, outcome: str, *, negative_risk: bool = True) -> Position:
    return Position(
        asset=asset,
        size=0.0,
        condition_id=condition_id,
        outcome=outcome,
        negative_risk=negative_risk,
        redeemable=True,
    )


def test_groups_both_market_types_and_tags_the_path():
    multi_cid, multi_s = cid_hex(1)
    binary_cid, binary_s = cid_hex(2)
    groups = group_redeemable(
        [
            pos("100", multi_s, "Yes"),
            pos("200", multi_s, "No"),
            pos("300", binary_s, "Yes", negative_risk=False),
        ]
    )
    assert len(groups) == 2
    # Multi-outcome condition (both sides held) → adapter path.
    assert groups[0].condition_id == multi_cid
    assert groups[0].neg_risk
    assert groups[0].yes_token == 100
    assert groups[0].no_token == 200
    # Binary condition → kept (no longer dropped), CTF path.
    assert groups[1].condition_id == binary_cid
    assert not groups[1].neg_risk
    assert groups[1].yes_token == 300


def test_drops_unparseable_rows():
    _, cid_s = cid_hex(1)
    groups = group_redeemable(
        [
            pos("xyz", cid_s, "No"),  # unparseable token id
            pos("300", "0xnothex", "Yes", negative_risk=False),  # unparseable condition id
        ]
    )
    assert groups == []


def test_redemptions_skip_zero_balance_and_dust():
    cid0, cid0s = cid_hex(1)
    cid1s = cid_hex(2)[1]
    cid2s = cid_hex(3)[1]
    groups = group_redeemable(
        [
            pos("10", cid0s, "No", negative_risk=False),  # binary, held above floor → kept
            pos("20", cid1s, "No"),  # zero on-chain → dropped
            pos("30", cid2s, "No"),  # below floor → dropped
        ]
    )
    balances = {10: 1_000_000, 20: 0, 30: 100}
    assert build_redemptions(groups, balances, min_raw=1_000) == [
        Redemption(condition_id=cid0, yes_amount=0, no_amount=1_000_000, neg_risk=False)
    ]
