"""WS frame builders, event/message parsing, and the multi-connection
utilities."""

import json
import uuid
from decimal import Decimal

from eggplant_sdk.auth import Credentials
from eggplant_sdk.book import Book, BookSide
from eggplant_sdk.clob.types import OrderStatus, Side
from eggplant_sdk.ws.frames import (
    PING_INTERVAL,
    PONG_TIMEOUT,
    market_subscribe_frame,
    user_subscribe_frame,
)
from eggplant_sdk.ws.market import (
    MarketBook,
    MarketLastTradePrice,
    MarketPriceChange,
    MarketTickSizeChange,
    MarketUnknown,
    parse_market_event,
)
from eggplant_sdk.ws.user import (
    OrderEventType,
    OrderMessage,
    TradeMessage,
    TradeStatus,
    UnknownMessage,
    parse_trade_status,
    parse_user_message,
    trade_status_is_final,
)
from eggplant_sdk.ws.util import SeenIds, market_recycle_offset, our_maker_side, recycle_offset

d = Decimal


def test_market_frame_shape():
    frame = json.loads(market_subscribe_frame(["111", "222"], True))
    assert frame["type"] == "market"
    assert frame["assets_ids"] == ["111", "222"]
    assert frame["custom_feature_enabled"] is True

    plain = json.loads(market_subscribe_frame([], False))
    assert "custom_feature_enabled" not in plain


def test_user_frame_shape():
    creds = Credentials(uuid.UUID(int=0), "c2VjcmV0", "pass")
    frame = json.loads(user_subscribe_frame(creds, []))
    assert frame["type"] == "user"
    assert frame["markets"] == []
    assert frame["auth"]["apiKey"] == str(uuid.UUID(int=0))
    assert frame["auth"]["secret"] == "c2VjcmV0"
    assert frame["auth"]["passphrase"] == "pass"


def test_pong_timeout_is_three_ping_intervals():
    assert PONG_TIMEOUT == PING_INTERVAL * 3


def test_parses_book_snapshot():
    text = """{
        "event_type": "book",
        "asset_id": "111",
        "market": "0xabc",
        "bids": [{"price": "0.48", "size": "30"}],
        "asks": [{"price": "0.52", "size": "10.5"}],
        "timestamp": "1751300000123",
        "hash": "deadbeef"
    }"""
    event = parse_market_event(text)
    assert isinstance(event, MarketBook)
    assert event.asset_id == "111"
    assert event.bids[0].price == d("0.48")
    assert event.asks[0].price == d("0.52")
    assert event.timestamp == 1_751_300_000_123


def test_parses_price_change_batch_and_maps_sides():
    text = """{
        "event_type": "price_change",
        "market": "0xabc",
        "price_changes": [
            {"asset_id": "111", "price": "0.49", "size": "25", "side": "BUY",
             "best_bid": "0.49", "best_ask": "0.52"},
            {"asset_id": "111", "price": "0.53", "size": "0", "side": "SELL",
             "best_bid": "0.49", "best_ask": ""}
        ],
        "timestamp": "1751300000123"
    }"""
    event = parse_market_event(text)
    assert isinstance(event, MarketPriceChange)
    assert len(event.price_changes) == 2
    assert event.price_changes[0].book_side() is BookSide.BID
    assert event.price_changes[1].book_side() is BookSide.ASK
    # A "0" size delta means level removal; empty best_ask degrades to None.
    assert event.price_changes[1].size == d(0)
    assert event.price_changes[1].best_ask is None
    assert event.price_changes[0].best_bid == d("0.49")


def test_parses_quarter_cent_tick_change():
    text = """{"event_type": "tick_size_change", "asset_id": "111",
               "old_tick_size": "0.01", "new_tick_size": "0.0025"}"""
    event = parse_market_event(text)
    assert event == MarketTickSizeChange(asset_id="111", new_tick_size=d("0.0025"))


def test_parses_last_trade_price_with_optional_fields_missing():
    event = parse_market_event(
        '{"event_type": "last_trade_price", "asset_id": "111", "price": "0.57"}'
    )
    assert isinstance(event, MarketLastTradePrice)
    assert event.price == d("0.57")
    assert event.size is None and event.side is None


def test_unknown_event_kind_degrades_not_fails():
    event = parse_market_event('{"event_type": "brand_new_thing", "payload": {"x": 1}}')
    assert isinstance(event, MarketUnknown)


def test_book_events_feed_the_book_state():
    # End-to-end shape check: WS frames drive eggplant_sdk.book.Book.
    book = Book()
    snapshot = parse_market_event(
        """{
        "event_type": "book", "asset_id": "111",
        "bids": [{"price": "0.48", "size": "30"}],
        "asks": [{"price": "0.52", "size": "10"}]
    }"""
    )
    assert isinstance(snapshot, MarketBook)
    book.apply_snapshot(
        ((lv.price, lv.size) for lv in snapshot.bids),
        ((lv.price, lv.size) for lv in snapshot.asks),
    )
    assert len(book.asks) == 1

    delta = parse_market_event(
        """{
        "event_type": "price_change", "market": "0xabc",
        "price_changes": [{"asset_id": "111", "price": "0.52", "size": "0",
                           "side": "SELL", "best_bid": "0.48", "best_ask": ""}]
    }"""
    )
    assert isinstance(delta, MarketPriceChange)
    for entry in delta.price_changes:
        book.apply_delta(entry.book_side(), entry.price, entry.size)
    assert not book.asks, "the SELL delta removed the ask level"


# Venue-shaped trade frame (the venue's published sample payload).
SAMPLE_TRADE = """{
    "asset_id": "52114319501245915516055106046884209969926127482827954674443846427813813222426",
    "event_type": "trade",
    "id": "28c4d2eb-bbea-40e7-a9f0-b2fdb56b2c2e",
    "last_update": "1672290701",
    "maker_orders": [
        {
            "asset_id": "52114319501245915516055106046884209969926127482827954674443846427813813222426",
            "matched_amount": "10",
            "order_id": "0xff354cd7ca7539dfa9c28d90943ab5779a4eac34b9b37a757d7b32bdfb11790b",
            "outcome": "YES",
            "owner": "9180014b-33c8-9240-a14b-bdca11c0a465",
            "price": "0.57"
        }
    ],
    "market": "0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af",
    "matchtime": "1672290701",
    "outcome": "YES",
    "owner": "9180014b-33c8-9240-a14b-bdca11c0a465",
    "price": "0.57",
    "side": "BUY",
    "size": "10",
    "status": "MATCHED",
    "taker_order_id": "0x06bc63e346ed4ceddce9efd6b3af37c8f8f440c92fe7da6b2d0f9e4ccbc50c42",
    "timestamp": "1672290701",
    "trade_owner": "9180014b-33c8-9240-a14b-bdca11c0a465",
    "type": "TRADE"
}"""


def test_parses_sample_trade_event():
    message = parse_user_message(SAMPLE_TRADE)
    assert isinstance(message, TradeMessage)
    assert message.id == "28c4d2eb-bbea-40e7-a9f0-b2fdb56b2c2e"
    assert message.side is Side.BUY
    assert message.price == d("0.57")
    assert message.size == d(10)
    assert message.status is TradeStatus.MATCHED
    assert message.matchtime == 1_672_290_701
    assert len(message.maker_orders) == 1
    assert message.maker_orders[0].outcome == "YES"
    assert str(message.maker_orders[0].owner) == "9180014b-33c8-9240-a14b-bdca11c0a465"


def test_parses_order_event():
    message = parse_user_message(
        """{
        "event_type": "order",
        "id": "0xff354cd7",
        "market": "0xbd31dc8a",
        "asset_id": "521143195",
        "side": "SELL",
        "price": "0.57",
        "type": "PLACEMENT",
        "outcome": "YES",
        "original_size": "100",
        "size_matched": "0",
        "timestamp": "1672290701",
        "status": "LIVE"
    }"""
    )
    assert isinstance(message, OrderMessage)
    assert message.msg_type is OrderEventType.PLACEMENT
    assert message.side is Side.SELL
    assert message.status is OrderStatus.LIVE
    assert message.original_size == d(100)


def test_unknown_event_kind_and_statuses_degrade():
    assert isinstance(parse_user_message('{"event_type": "brand_new", "x": 1}'), UnknownMessage)

    status = parse_trade_status("SOME_NEW_STATUS")
    assert status == "SOME_NEW_STATUS"
    assert not trade_status_is_final(status)
    assert trade_status_is_final(TradeStatus.CONFIRMED)
    assert trade_status_is_final(TradeStatus.FAILED)
    assert not trade_status_is_final(TradeStatus.RETRYING)


def test_recycle_off_when_disabled_or_single_connection():
    # interval 0 ⇒ off.
    assert recycle_offset(0, 2, 0) is None
    # a lone connection has no peer to cover the gap ⇒ off (relies on PONG).
    assert recycle_offset(0, 1, 300) is None


def test_recycle_offsets_evenly_phased_and_never_at_boot():
    # Four connections over a 300s period recycle at 75/150/225/300s: evenly
    # spaced by period/N, all in (0, period], so two never refresh at once
    # and none fires at startup (phase 0).
    offsets = [recycle_offset(k, 4, 300) for k in range(4)]
    assert offsets == [75.0, 150.0, 225.0, 300.0]
    assert all(offset > 0 for offset in offsets)


def test_market_recycle_off_without_same_shard_peer():
    # A shard with a single copy has no peer to cover the refresh gap ⇒ off.
    assert market_recycle_offset(0, 0, 4, 1, 300) is None
    # interval 0 ⇒ off too (delegated to recycle_offset).
    assert market_recycle_offset(0, 0, 4, 2, 0) is None


def test_market_recycle_copies_of_a_shard_are_period_over_redundancy_apart():
    # 3 shards × 2 copies over 300s: a shard's two copies must sit
    # period/redundancy = 150s apart so one is fully reconnected before its
    # peer recycles.
    for shard in range(3):
        c0 = market_recycle_offset(shard, 0, 3, 2, 300)
        c1 = market_recycle_offset(shard, 1, 3, 2, 300)
        assert c1 - c0 == 150.0


def test_market_recycle_phases_distinct_and_evenly_spread():
    # Every connection lands on a distinct phase in (0, period], evenly
    # spaced by period/total, so no two recycle together and none fires at
    # boot.
    num_shards, redundancy, period = 3, 2, 300
    total = num_shards * redundancy
    offsets = sorted(
        market_recycle_offset(shard, copy, num_shards, redundancy, period)
        for copy in range(redundancy)
        for shard in range(num_shards)
    )
    assert len(set(offsets)) == total, "all phases distinct"
    assert all(offset > 0 for offset in offsets), "none fires at boot"
    step = period / total
    assert offsets == [step * (i + 1) for i in range(total)], "evenly spaced by period/total"


def test_our_maker_side_truth_table():
    # Resting BUY NO — both taker fill mechanics (mint / direct) ⇒ BUY.
    assert our_maker_side(Side.BUY, "YES", "NO") is Side.BUY
    assert our_maker_side(Side.SELL, "NO", "NO") is Side.BUY

    # Resting SELL NO ⇒ SELL, in both complementary representations.
    assert our_maker_side(Side.SELL, "YES", "NO") is Side.SELL
    assert our_maker_side(Side.BUY, "NO", "NO") is Side.SELL

    # Resting SELL YES ⇒ SELL, in both complementary representations.
    assert our_maker_side(Side.BUY, "YES", "YES") is Side.SELL
    assert our_maker_side(Side.SELL, "NO", "YES") is Side.SELL

    # Indeterminate ⇒ None (callers decide their conservative default).
    assert our_maker_side(Side.UNKNOWN, "YES", "NO") is None
    assert our_maker_side(Side.BUY, None, "NO") is None

    # Outcome comparison is case-insensitive (guards an inversion hazard).
    assert our_maker_side(Side.BUY, "No", "no") is Side.SELL
    assert our_maker_side(Side.BUY, "Yes", "No") is Side.BUY


def test_dedup_drops_repeats_and_evicts_oldest():
    seen = SeenIds(2)
    assert seen.insert("a"), "first sighting is new"
    assert not seen.insert("a"), "repeat is a duplicate"
    assert seen.insert("b")
    # Inserting a third distinct id evicts the oldest ("a").
    assert seen.insert("c")
    assert not seen.contains("a")
    assert seen.insert("a"), "evicted id is treated as new again"
