"""Sweep classification tests (the pure, no-I/O layer)."""

from eggplant_sdk.convert import ConvertLeg
from eggplant_sdk.data import Position
from eggplant_sdk.sweep import classify_event, leg_sizes


def leg(n: int, yes: bool, cond: bool) -> ConvertLeg:
    """Test leg: NO token id ``2000 + n``, YES token id ``1000 + n`` when
    ``yes``, condition id tagged with ``n`` when ``cond``."""
    return ConvertLeg(
        question_id=bytes([n]) + bytes(31),
        condition_id=(bytes([0, n]) + bytes(30)) if cond else None,
        yes_token_id=(1000 + n) if yes else None,
        no_token_id=2000 + n,
    )


def pos(token: int, size: float) -> Position:
    return Position(asset=str(token), size=size, negative_risk=True)


def test_leg_sizes_maps_tokens_to_legs_and_sides():
    legs = [leg(1, True, True), leg(2, False, True)]
    # leg1: NO=2001 (held 10), YES=1001 (held 4); leg2: NO=2002 (held 7).
    positions = [pos(2001, 10.0), pos(1001, 4.0), pos(2002, 7.0), pos(9999, 5.0)]
    assert leg_sizes(legs, positions) == [(10.0, 4.0), (7.0, 0.0)]


def test_classify_flags_merge_and_convert():
    legs = [leg(1, True, True), leg(2, False, True)]
    # leg1 holds YES+NO (mergeable, min=4); both legs have leftover NO.
    c = classify_event(legs, [(10.0, 4.0), (7.0, 0.0)], 0.1)
    assert c.merge_pairs == 1
    assert c.merge_shares == 4.0
    # leftover NO: leg1 = 10-4 = 6, leg2 = 7 → 2 convert legs.
    assert c.convert_legs == 2
    assert c.mergeable and c.convertible and c.actionable()


def test_lone_convert_leg_below_floor_is_not_convertible():
    legs = [leg(1, False, True), leg(2, False, True)]
    # Only leg1 holds NO (0.05 shares) → lone convert leg below the floor.
    c = classify_event(legs, [(0.05, 0.0), (0.0, 0.0)], 0.1)
    assert c.convert_legs == 1
    assert not c.convertible
    assert not c.actionable()
    # The same lone leg above the floor is convertible.
    c2 = classify_event(legs, [(5.0, 0.0), (0.0, 0.0)], 0.1)
    assert c2.convertible and c2.actionable()


def test_merge_needs_a_condition_id():
    # Holds YES+NO but no condition id ⇒ can't merge; the YES offsets NO,
    # leaving nothing convertible either.
    legs = [leg(1, True, False)]
    c = classify_event(legs, [(5.0, 5.0)], 0.1)
    assert c.merge_pairs == 0
    assert c.convert_legs == 0
    assert not c.actionable()
