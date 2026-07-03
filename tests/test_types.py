"""Wire-shape and lenient-parse tests for the CLOB types."""

import json
import uuid
from decimal import Decimal

import pytest

from eggplant_sdk.clob.types import (
    CancelOrdersResponse,
    OpenOrder,
    OrderStatus,
    OrderType,
    OrderV2,
    PostOrderResponse,
    Side,
    SignatureType,
    SignedOrder,
    parse_order_type,
)
from eggplant_sdk.errors import InvalidDataError

NIL_KEY = uuid.UUID(int=0)


def test_side_wire_strings():
    assert Side.BUY.wire == "BUY"
    assert Side.SELL.wire == "SELL"
    assert Side.parse("sell") is Side.SELL
    # Unknown side strings degrade instead of failing the response.
    assert Side.parse("SHORT") is Side.UNKNOWN
    with pytest.raises(InvalidDataError):
        Side.from_u8(7)


def test_order_type_lenient_parse():
    assert parse_order_type("gtc") is OrderType.GTC
    assert parse_order_type("NEW_ORDER_TYPE") == "NEW_ORDER_TYPE"


def test_signature_type_wire_values():
    assert int(SignatureType.EOA) == 0
    assert int(SignatureType.PROXY) == 1
    assert int(SignatureType.GNOSIS_SAFE) == 2
    assert int(SignatureType.POLY1271) == 3
    assert SignatureType.from_u8(2) is SignatureType.GNOSIS_SAFE
    with pytest.raises(InvalidDataError):
        SignatureType.from_u8(9)


def test_signed_order_serialization_omits_post_only_when_none():
    signed = SignedOrder(order=OrderV2(), signature="0x00", order_type=OrderType.GTC, owner=NIL_KEY)
    wire = signed.to_wire()
    assert "postOnly" not in wire
    assert "deferExec" not in wire


def test_signed_order_serialization_includes_v2_fields_only():
    signed = SignedOrder(
        order=OrderV2(),
        signature="0x00",
        order_type=OrderType.GTC,
        owner=NIL_KEY,
        defer_exec=False,
    )
    wire = signed.to_wire()
    order = wire["order"]
    assert "timestamp" in order
    assert "metadata" in order
    assert "builder" in order
    assert "expiration" in order
    assert "taker" not in order
    assert "nonce" not in order
    assert "feeRateBps" not in order
    assert wire["deferExec"] is False


def test_signed_order_wire_shape_golden():
    order = OrderV2(
        salt=12_345,
        maker="0x" + "11" * 20,
        signer="0x" + "11" * 20,
        token_id=777,
        maker_amount=4_850_000,
        taker_amount=5_000_000,
        side=int(Side.BUY),
        signature_type=int(SignatureType.POLY1271),
        timestamp=1_700_000_000_000,
    )
    signed = SignedOrder(
        order=order,
        signature="0xdeadbeef",
        order_type=OrderType.GTC,
        owner=NIL_KEY,
        expiration=0,
        post_only=True,
    )
    wire = signed.to_wire()
    # Salt is a JSON *number*; ids/amounts/timestamps are decimal strings;
    # side is the UPPERCASE string; signatureType is a number.
    assert wire["order"]["salt"] == 12_345
    assert wire["order"]["tokenId"] == "777"
    assert wire["order"]["makerAmount"] == "4850000"
    assert wire["order"]["takerAmount"] == "5000000"
    assert wire["order"]["side"] == "BUY"
    assert wire["order"]["signatureType"] == 3
    assert wire["order"]["timestamp"] == "1700000000000"
    assert wire["order"]["expiration"] == "0"
    assert wire["order"]["metadata"] == "0x" + "00" * 32
    assert wire["order"]["signature"] == "0xdeadbeef"
    assert wire["orderType"] == "GTC"
    assert wire["postOnly"] is True
    assert wire["owner"] == "00000000-0000-0000-0000-000000000000"
    # The whole thing serializes as JSON (the poster HMACs these bytes).
    json.dumps(wire)


def test_signed_order_oversize_salt_errors():
    signed = SignedOrder(
        order=OrderV2(salt=1 << 64),
        signature="0x00",
        order_type=OrderType.GTC,
        owner=NIL_KEY,
    )
    with pytest.raises(InvalidDataError):
        signed.to_wire()


def test_post_order_response_venue_shape():
    # The exact shape the venue answers with (string decimals, orderID).
    response = PostOrderResponse.from_dict(
        {
            "errorMsg": "",
            "makingAmount": "1.0",
            "takingAmount": "2.0",
            "orderID": "0xabc",
            "status": "live",
            "success": True,
        }
    )
    assert response.is_accepted()
    assert response.making_amount == Decimal(1)
    assert response.order_id == "0xabc"
    assert response.status is OrderStatus.LIVE

    # Rejection with an unknown status string still parses.
    rejected = PostOrderResponse.from_dict(
        {
            "errorMsg": "not enough balance",
            "makingAmount": "",
            "takingAmount": "",
            "orderID": "",
            "status": "SOME_NEW_STATUS",
            "success": False,
        }
    )
    assert not rejected.is_accepted()
    assert rejected.making_amount == Decimal(0)
    assert rejected.status == "SOME_NEW_STATUS"


def test_cancel_orders_response_venue_shape():
    response = CancelOrdersResponse.from_dict(
        json.loads('{"canceled":["a"],"notCanceled":{"b":"Order already filled"}}')
    )
    assert response.canceled == ["a"]
    assert response.not_canceled["b"] == "Order already filled"

    # Nulls collapse to empty collections.
    nulls = CancelOrdersResponse.from_dict(json.loads('{"canceled":null,"notCanceled":null}'))
    assert nulls.canceled == [] and nulls.not_canceled == {}


def test_open_order_is_lenient():
    # Only `id` is required; everything else degrades to defaults.
    order = OpenOrder.from_dict({"id": "0xdead"})
    assert order.id == "0xdead"
    assert order.side is Side.UNKNOWN
    assert order.status == ""

    full = OpenOrder.from_dict(
        {
            "id": "0xbeef",
            "status": "LIVE",
            "side": "BUY",
            "asset_id": "777",
            "price": "0.42",
            "original_size": "100",
            "size_matched": "1.5",
            "created_at": 1_700_000_000,
        }
    )
    assert full.side is Side.BUY
    assert full.price == Decimal("0.42")
    assert full.created_at == 1_700_000_000
