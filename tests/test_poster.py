"""Poster helpers: exact cancel bytes, terminal-reason classification, and
cancel partitioning."""

import json

from eggplant_sdk.clob.poster import (
    CancelEndpoint,
    OrderEndpoint,
    cancel_order_body,
    cancel_reason_is_terminal,
    partition_cancels,
)
from eggplant_sdk.clob.types import CancelOrdersResponse


def test_cancel_order_body_is_exact_json():
    # The HMAC signs these exact bytes, so the serialization must be
    # byte-stable.
    assert cancel_order_body("0xabc") == b'{"orderID":"0xabc"}'


def test_terminal_cancel_reasons_are_recognized():
    # The venue's wording for "the order is gone".
    for reason in [
        "Order already filled",
        "order is filled",
        "Order not found",
        "ORDER ALREADY MATCHED",
        "order already executed",
        "order complete",
        "order already canceled",
        "order already cancelled",
        "order expired",
    ]:
        assert cancel_reason_is_terminal(reason), f"should be terminal: {reason!r}"


def test_transient_cancel_reasons_are_retried():
    # Anything not recognized as terminal is retried, never dropped — a
    # still-live order must never be abandoned.
    for reason in [
        "",
        "rate limited",
        "too many requests",
        "service unavailable",
        "request timeout",
        "connection reset",
        "internal error",
    ]:
        assert not cancel_reason_is_terminal(reason), f"should be retried: {reason!r}"


def test_partition_cancels_splits_done_from_retry():
    # Venue confirmed "a" canceled; "b" is gone (terminal reason → done);
    # "c" hit a non-terminal reason and "d" was omitted entirely → both
    # retried, never dropped.
    batch = [(0, 0, "a"), (0, 1, "b"), (0, 2, "c"), (0, 3, "d")]
    response = CancelOrdersResponse.from_dict(
        json.loads(
            '{"canceled":["a"],"notCanceled":{"b":"Order already filled","c":"rate limited"}}'
        )
    )
    done, retry = partition_cancels(batch, response, lambda leg: leg[2])
    assert done == [(0, 0, "a"), (0, 1, "b")], "canceled + terminal-reason legs are done"
    assert retry == [(0, 2, "c"), (0, 3, "d")], "non-terminal + omitted legs are retried"


def test_partition_cancels_retries_everything_on_transport_error():
    # A whole-POST failure has no per-id verdicts: every leg is presumed
    # still resting.
    batch = [(0, 0, "a"), (0, 1, "b")]
    done, retry = partition_cancels(batch, None, lambda leg: leg[2])
    assert done == []
    assert retry == batch


def test_cancel_endpoint_parse_and_flip():
    assert CancelEndpoint("orders") is CancelEndpoint.ORDERS
    assert CancelEndpoint("order") is CancelEndpoint.ORDER

    for endpoint in (CancelEndpoint.ORDERS, CancelEndpoint.ORDER):
        assert endpoint.flipped().flipped() is endpoint
        # The config token must parse back to the same variant, or a flip
        # would silently reset on a config round-trip.
        assert CancelEndpoint(endpoint.as_config_str()) is endpoint


def test_order_endpoint_parse():
    assert OrderEndpoint("orders") is OrderEndpoint.ORDERS
    assert OrderEndpoint("order") is OrderEndpoint.ORDER
