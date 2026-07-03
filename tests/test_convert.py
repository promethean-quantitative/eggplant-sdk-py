"""Convert calldata, planner, and chunking tests."""

import pytest
from eth_utils import keccak

from eggplant_sdk.chain import COLLATERAL_ONRAMP, NEG_RISK_ADAPTER, USDC_E
from eggplant_sdk.convert import (
    MERGE_GAS,
    ConvertLeg,
    EventPlan,
    MarketIds,
    NoHolding,
    PlannedCall,
    _batch_token_ids,
    _token_count,
    _unpack_balances,
    build_convert_calldata,
    build_merge_calldata,
    build_merge_calldata_ctf,
    build_redeem_calldata,
    build_split_calldata,
    build_split_calldata_ctf,
    classify_event,
    convert_gas,
    convert_legs,
    fmt_usdc,
    gas_chunks,
    index_set,
    market_id_from_question_id,
    merge_calls,
    plan_calls,
    plan_event,
    plan_tiers,
    redeem_calls,
    split_calls,
    wrap_calls,
)
from eggplant_sdk.errors import InvalidDataError
from eggplant_sdk.relayer import DepositWalletCall


def qid_hex(suffix: str) -> bytes:
    return bytes.fromhex(f"aa0edfa656a0e70bf8c63f09438cd70979fef8e31fcc62d80840b5a375a554{suffix}")


def test_market_id_zeros_last_byte():
    qid = qid_hex("03")
    mid = market_id_from_question_id(qid)
    assert mid[31] == 0
    assert mid[:31] == qid[:31]


def test_market_id_already_zero_is_noop():
    qid = qid_hex("00")
    assert market_id_from_question_id(qid) == qid


def test_index_set_single_question():
    assert index_set([qid_hex("03")]) == 1 << 3


def test_index_set_multiple_questions():
    # bit 0 | bit 1 | bit 5 = 1 + 2 + 32 = 35
    assert index_set([qid_hex("00"), qid_hex("01"), qid_hex("05")]) == 35


def test_index_set_empty_is_zero():
    assert index_set([]) == 0


def test_build_calldata_empty_question_ids_errors():
    with pytest.raises(InvalidDataError):
        build_convert_calldata([], 5_000_000)


def test_build_calldata_produces_valid_encoding():
    data = build_convert_calldata([qid_hex("00"), qid_hex("01")], 5_000_000)
    # 4-byte selector + 3 * 32-byte args = 100 bytes
    assert len(data) == 100
    assert data[:4] == keccak(b"convertPositions(bytes32,uint256,uint256)")[:4]


def test_merge_calldata_selector():
    data = build_merge_calldata(b"\x11" * 32, 1)
    assert len(data) == 68
    assert data[:4] == keccak(b"mergePositions(bytes32,uint256)")[:4]


def test_split_calldata_selector_mirrors_merge():
    # The 2-arg adapter split is the exact structural mirror of the heavily
    # exercised 2-arg merge.
    cid = b"\x22" * 32
    data = build_split_calldata(cid, 5_000_000)
    assert len(data) == 68
    assert data[:4] == keccak(b"splitPosition(bytes32,uint256)")[:4]
    # Args ride verbatim: conditionId word then amount word.
    assert data[4:36] == cid
    assert int.from_bytes(data[36:68], "big") == 5_000_000

    calls = split_calls([(cid, 5_000_000)])
    assert len(calls) == 1
    assert calls[0].target == NEG_RISK_ADAPTER
    assert calls[0].data == data


def test_ctf_split_and_merge_calldata_layout():
    cid = b"\x33" * 32
    split = build_split_calldata_ctf(USDC_E, cid, 7)
    # selector(4) + address + parent + conditionId + array offset + amount
    # (5 words) + array len + 2 elements (3 words) = 4 + 8*32.
    assert len(split) == 4 + 8 * 32
    assert split[:4] == keccak(b"splitPosition(address,bytes32,bytes32,uint256[],uint256)")[:4]

    merge = build_merge_calldata_ctf(USDC_E, cid, 7)
    assert len(merge) == 4 + 8 * 32
    assert merge[:4] == keccak(b"mergePositions(address,bytes32,bytes32,uint256[],uint256)")[:4]
    # Same arguments, different selector.
    assert split[4:] == merge[4:]


def test_redeem_calldata_selector_layout_and_target():
    cid = qid_hex("03")
    data = build_redeem_calldata(cid, 523_000_000, 0)
    # selector(4) + conditionId(32) + array offset(32) + len(32) + 2 elems = 164
    assert len(data) == 164
    assert data[:4] == keccak(b"redeemPositions(bytes32,uint256[])")[:4]

    # redeem_calls wraps the same calldata against the shared adapter.
    calls = redeem_calls([(cid, 1, 2)])
    assert len(calls) == 1
    assert calls[0].target == NEG_RISK_ADAPTER
    assert calls[0].data == build_redeem_calldata(cid, 1, 2)


def test_convert_legs_parses_and_skips():
    markets = [
        MarketIds(
            question_id="0xaa0edfa656a0e70bf8c63f09438cd70979fef8e31fcc62d80840b5a375a55401",
            condition_id="bb0edfa656a0e70bf8c63f09438cd70979fef8e31fcc62d80840b5a375a55401",
            yes_token_id="1001",
            no_token_id="2001",
        ),
        # Missing question id → skipped.
        MarketIds(question_id=None, condition_id=None, yes_token_id=None, no_token_id="2002"),
        # Garbage NO token id → skipped.
        MarketIds(
            question_id="0xaa0edfa656a0e70bf8c63f09438cd70979fef8e31fcc62d80840b5a375a55403",
            condition_id=None,
            yes_token_id=None,
            no_token_id="not-a-number",
        ),
    ]
    legs = convert_legs(markets)
    assert len(legs) == 1
    assert legs[0].no_token_id == 2001
    assert legs[0].yes_token_id == 1001
    assert legs[0].condition_id is not None


def test_fmt_usdc_renders_six_decimals():
    assert fmt_usdc(12_345_678) == "12.345678"
    assert fmt_usdc(0) == "0.000000"
    assert fmt_usdc(42) == "0.000042"


def holding(n: int, cond: bool, remaining: int) -> NoHolding:
    """Test holding: question id with position index `n` (last byte, so
    index-set bits stay distinct), condition id tagged with `n` when
    `cond`."""
    qid = bytes(31) + bytes([n])
    cid = bytes([0, n]) + bytes(30)
    return NoHolding(question_id=qid, condition_id=cid if cond else None, remaining_no=remaining)


def leg(n: int, yes: bool, cond: bool) -> ConvertLeg:
    """Test leg: question id tagged with `n`, NO token id `2000 + n`, YES
    token id `1000 + n` when `yes`, condition id tagged with `n` when
    `cond`."""
    qid = bytes([n]) + bytes(31)
    cid = bytes([0, n]) + bytes(30)
    return ConvertLeg(
        question_id=qid,
        condition_id=cid if cond else None,
        yes_token_id=1000 + n if yes else None,
        no_token_id=2000 + n,
    )


def test_classify_merges_pair_and_nets_remainder():
    # Leg 1: YES 7 / NO 5 → merge 5, nothing left over.
    # Leg 2: YES 3 / NO 9 → merge 3, 6 NO left over.
    legs = [leg(1, True, True), leg(2, True, True)]
    balances = [(5, 7), (9, 3)]
    merges, holdings = classify_event(legs, balances)
    assert merges == [(legs[0].condition_id, 5), (legs[1].condition_id, 3)]
    assert holdings == [
        NoHolding(
            question_id=legs[1].question_id,
            condition_id=legs[1].condition_id,
            remaining_no=6,
        )
    ]


def test_classify_missing_condition_id_skips_merge():
    legs = [leg(1, True, False)]
    merges, holdings = classify_event(legs, [(5, 5)])
    # The pair can't merge without a condition id, and the NO side is fully
    # offset by YES, so nothing remains to convert either.
    assert merges == []
    assert holdings == []


def test_classify_keeps_leg_order():
    legs = [leg(1, False, True), leg(2, False, True)]
    merges, holdings = classify_event(legs, [(6, 0), (4, 0)])
    assert merges == []
    assert [h.question_id for h in holdings] == [legs[0].question_id, legs[1].question_id]
    assert [h.remaining_no for h in holdings] == [6, 4]


def test_classify_nothing_held_is_empty():
    legs = [leg(1, True, True), leg(2, False, True)]
    merges, holdings = classify_event(legs, [(0, 0), (0, 0)])
    assert merges == [] and holdings == []


def test_tiers_uniform_levels_single_tier():
    hs = [holding(1, True, 7), holding(2, True, 7), holding(3, True, 7)]
    tiers = plan_tiers(hs, 0, 20)
    assert len(tiers) == 1
    assert tiers[0].amount == 7
    assert tiers[0].question_ids == [h.question_id for h in hs]
    assert tiers[0].post_merges == []


def test_tiers_uneven_levels_decompose():
    # [3,5,5,9] → convert 3 across all 4, then 2 across the ≥5 legs, then
    # the lone 9-leg's last 4.
    hs = [holding(1, True, 3), holding(2, True, 5), holding(3, True, 5), holding(4, True, 9)]
    tiers = plan_tiers(hs, 0, 20)
    assert len(tiers) == 3
    assert tiers[0].amount == 3 and len(tiers[0].question_ids) == 4
    assert tiers[1].amount == 2
    assert tiers[1].question_ids == [hs[1].question_id, hs[2].question_id, hs[3].question_id]
    assert tiers[2].amount == 4
    assert tiers[2].question_ids == [hs[3].question_id]
    assert all(not t.post_merges for t in tiers)


def test_tiers_empty_holdings_no_tiers():
    assert plan_tiers([], 100, 20) == []


def test_tiers_single_leg_dust_floor():
    # Below the floor: skipped. At the floor: converted (strict <).
    hs = [holding(1, True, 5)]
    assert plan_tiers(hs, 6, 20) == []
    tiers = plan_tiers(hs, 5, 20)
    assert len(tiers) == 1 and tiers[0].amount == 5


def test_tiers_final_single_leg_dropped_earlier_kept():
    # Floor 5 drops only the final lone-leg tier (amount 4); the earlier
    # multi-leg tiers convert regardless of amount.
    hs = [holding(1, True, 3), holding(2, True, 5), holding(3, True, 5), holding(4, True, 9)]
    tiers = plan_tiers(hs, 5, 20)
    assert [t.amount for t in tiers] == [3, 2]


def test_tiers_truncation_emits_post_merges():
    # 5 uniform legs, cap 3: convert the first 3; the truncated-out legs get
    # compensating merges — except the cid-less one, which can't merge.
    hs = [
        holding(1, True, 7),
        holding(2, True, 7),
        holding(3, True, 7),
        holding(4, True, 7),
        holding(5, False, 7),
    ]
    tiers = plan_tiers(hs, 0, 3)
    assert len(tiers) == 1
    assert tiers[0].amount == 7
    assert tiers[0].question_ids == [h.question_id for h in hs[:3]]
    assert tiers[0].post_merges == [(hs[3].condition_id, 7)]


def test_tiers_multi_tier_truncation_tracks_running_balances():
    # [5,5,5,5,8], cap 3: tier 1 converts 5 on the first three and
    # post-merges the truncated two; tier 2 is the deep leg's remaining 3
    # (8 − 5 burned via its tier-1 post-merge).
    hs = [
        holding(1, True, 5),
        holding(2, True, 5),
        holding(3, True, 5),
        holding(4, True, 5),
        holding(5, True, 8),
    ]
    tiers = plan_tiers(hs, 0, 3)
    assert len(tiers) == 2
    assert tiers[0].amount == 5 and len(tiers[0].question_ids) == 3
    assert tiers[0].post_merges == [(hs[3].condition_id, 5), (hs[4].condition_id, 5)]
    assert tiers[1].amount == 3
    assert tiers[1].question_ids == [hs[4].question_id]
    assert tiers[1].post_merges == []


def test_plan_event_sums_exact_proceeds():
    # merges 5; tiers (k4,a3) → 9, (k3,a2) → 4, (k1,a4) → 0. Total 18.
    hs = [holding(1, True, 3), holding(2, True, 5), holding(3, True, 5), holding(4, True, 9)]
    tiers = plan_tiers(hs, 0, 20)
    plan = plan_event([(hs[0].condition_id, 5)], tiers, 4)
    assert plan.proceeds == 18
    assert plan.n_legs == 4


def test_plan_event_truncation_preserves_proceeds():
    # 5 legs @7 capped at 3: (3−1)·7 from the convert + 7+7 from the
    # post-merges == the untruncated (5−1)·7.
    hs = [holding(n, True, 7) for n in range(1, 6)]
    tiers = plan_tiers(hs, 0, 3)
    plan = plan_event([], tiers, 5)
    assert plan.proceeds == 28


def test_plan_calls_orders_merges_converts_post_merges():
    hs = [holding(1, True, 7), holding(2, True, 7), holding(3, True, 7), holding(4, True, 7)]
    tiers = plan_tiers(hs, 0, 3)
    plan = plan_event([(hs[0].condition_id, 5)], tiers, 4)
    calls = plan_calls([plan])

    # merge, convert, post-merge — the wrap is NOT planned here (it goes out
    # post-batch by balance read; an in-batch proceeds-sized wrap is rejected
    # by the relayer's simulation).
    assert len(calls) == 3
    merge_selector = keccak(b"mergePositions(bytes32,uint256)")[:4]
    convert_selector = keccak(b"convertPositions(bytes32,uint256,uint256)")[:4]
    assert calls[0].call.target == NEG_RISK_ADAPTER
    assert calls[0].call.data[:4] == merge_selector
    assert calls[0].gas == MERGE_GAS
    assert calls[1].call.data[:4] == convert_selector
    assert calls[1].gas == convert_gas(4)
    assert calls[2].call.data[:4] == merge_selector
    assert calls[2].gas == MERGE_GAS


def test_plan_calls_empty_when_nothing_to_do():
    assert plan_calls([]) == []
    assert plan_calls([EventPlan()]) == []


def test_plan_calls_large_plan_emits_every_call():
    # 25 merge-only legs → 25 calls; the 20-call submission cap alone splits
    # them into 2 sequential submissions (merges are cheap, so the 8M gas
    # budget fits 20 of them).
    merges = [(bytes([0, n]) + bytes(30), 1_000_000) for n in range(1, 26)]
    plan = plan_event(merges, [], 25)
    calls = plan_calls([plan])
    assert len(calls) == 25
    ranges = gas_chunks(calls, 20, 8_000_000)
    assert ranges == [range(0, 20), range(20, 25)]


def pc(gas: int) -> PlannedCall:
    """Shorthand for a PlannedCall with the given gas weight (calldata is
    irrelevant to chunking)."""
    return PlannedCall(call=DepositWalletCall(target=NEG_RISK_ADAPTER, data=b""), gas=gas)


def test_gas_chunks_empty_and_single_chunk():
    assert gas_chunks([], 20, 8_000_000) == []
    planned = [pc(1_000_000), pc(2_000_000), pc(3_000_000)]
    assert gas_chunks(planned, 20, 8_000_000) == [range(0, 3)]


def test_gas_chunks_splits_on_gas_budget():
    # One cheap merge + six ~7M converts must NOT share submissions under an
    # 8M budget.
    planned = [pc(400_000)] + [pc(7_000_000)] * 6
    assert gas_chunks(planned, 20, 8_000_000) == [
        range(0, 2),
        range(2, 3),
        range(3, 4),
        range(4, 5),
        range(5, 6),
        range(6, 7),
    ]


def test_gas_chunks_oversize_call_gets_own_chunk():
    # A single call over the budget is atomic and still submits alone,
    # without starving its neighbours.
    planned = [pc(400_000), pc(12_000_000), pc(400_000)]
    assert gas_chunks(planned, 20, 8_000_000) == [range(0, 1), range(1, 2), range(2, 3)]


def test_gas_chunks_count_cap_still_applies():
    planned = [pc(1)] * 5
    assert gas_chunks(planned, 2, 8_000_000) == [range(0, 2), range(2, 4), range(4, 5)]
    # Degenerate cap clamps to 1.
    assert gas_chunks(planned[:2], 0, 8_000_000) == [range(0, 1), range(1, 2)]


def test_wrap_calls_shapes():
    wallet = NEG_RISK_ADAPTER  # any address works for the shape test
    no_approve = wrap_calls(wallet, 5, False)
    assert len(no_approve) == 1
    assert no_approve[0].target == COLLATERAL_ONRAMP
    assert no_approve[0].data[:4] == keccak(b"wrap(address,address,uint256)")[:4]

    with_approve = wrap_calls(wallet, 5, True)
    assert len(with_approve) == 2
    assert with_approve[0].target == USDC_E
    assert with_approve[0].data[:4] == keccak(b"approve(address,uint256)")[:4]


def test_batch_ids_layout_no_then_yes_when_present():
    legs = [leg(1, True, True), leg(2, False, True), leg(3, True, False)]
    assert _batch_token_ids(legs) == [2001, 1001, 2002, 2003, 1003]
    assert _token_count(legs) == 5


def test_unpack_round_trips_layout():
    legs = [leg(1, True, True), leg(2, False, True), leg(3, True, False)]
    pairs = _unpack_balances(legs, [10, 11, 20, 30, 31])
    assert pairs == [(10, 11), (20, 0), (30, 31)]


def test_unpack_short_result_errors():
    legs = [leg(1, True, True), leg(2, False, True)]
    with pytest.raises(InvalidDataError):
        _unpack_balances(legs, [10, 11])


def test_unpack_long_result_errors():
    legs = [leg(1, False, True)]
    with pytest.raises(InvalidDataError):
        _unpack_balances(legs, [10, 11])


def test_merge_calls_target_adapter():
    calls = merge_calls([(b"\x01" * 32, 5)])
    assert calls[0].target == NEG_RISK_ADAPTER
    assert calls[0].data == build_merge_calldata(b"\x01" * 32, 5)
