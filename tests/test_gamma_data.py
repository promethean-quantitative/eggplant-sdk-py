"""Gamma and Data API model parsing."""

import json

from eggplant_sdk.data import Position
from eggplant_sdk.gamma import GammaEvent, GammaMarket, KeysetResponse, _url_encode


def test_url_encoding_is_rfc3986_unreserved():
    assert _url_encode("abc-DEF_1.2~") == "abc-DEF_1.2~"
    assert _url_encode("a b/c?d=e") == "a%20b%2Fc%3Fd%3De"


def test_parses_real_gamma_event_shape():
    # Guards the parsing (camelCase renames, the `tags` array, the
    # JSON-encoded `clobTokenIds` string, ignored extra fields) against a
    # payload shaped like Gamma's real responses.
    payload = json.loads(
        """{
        "id": "622565", "slug": "2026-travelers-championship-winner",
        "title": "PGA Tour: Travelers Championship Winner",
        "negRisk": true,
        "volume24hr": 12345.6,
        "startTime": "2026-06-25T00:00:00Z",
        "startDate": "2026-06-22T16:29:54.669905Z",
        "endDate": "2026-06-28T00:00:00Z",
        "tags": [
            {"id": "1", "label": "Sports", "slug": "sports", "forceHide": true},
            {"id": "100219", "label": "Golf", "slug": "golf"}
        ],
        "markets": [
            {
                "active": true,
                "groupItemTitle": "Sam Burns",
                "clobTokenIds": "[\\"100\\",\\"200\\"]",
                "orderPriceMinTickSize": 0.001,
                "questionID": "0xaa0edfa656a0e70bf8c63f09438cd70979fef8e31fcc62d80840b5a375a55401",
                "conditionId": "0xbb0edfa656a0e70bf8c63f09438cd70979fef8e31fcc62d80840b5a375a55401",
                "feeSchedule": {"rate": 0.05, "rebateRate": 0.2}
            },
            {"active": false, "groupItemTitle": "Withdrawn Player"}
        ]
    }"""
    )
    event = GammaEvent.from_dict(payload)
    assert event.neg_risk
    assert event.start_time == "2026-06-25T00:00:00Z"
    assert any(tag.slug == "sports" for tag in event.tags)

    market = event.markets[0]
    assert market.yes_token_id() == "100"
    assert market.no_token_id() == "200"
    assert market.tick_size == 0.001
    assert market.fee_schedule.rate == 0.05

    # The convert adapter carries the ids through.
    ids = market.market_ids()
    assert ids is not None
    assert ids.no_token_id == "200"
    assert ids.yes_token_id == "100"
    assert ids.question_id is not None and ids.condition_id is not None

    # A market with no token ids yields no MarketIds.
    assert event.markets[1].market_ids() is None


def test_keyset_response_parses_with_and_without_cursor():
    page = KeysetResponse.from_dict({"events": [], "next_cursor": "MTA="})
    assert page.next_cursor == "MTA="

    last = KeysetResponse.from_dict({"next_cursor": None})
    assert last.events == []
    assert last.next_cursor is None


def test_clob_token_ids_missing_or_null_is_none():
    assert GammaMarket.from_dict({"active": True}).clob_token_ids is None
    assert GammaMarket.from_dict({"active": True, "clobTokenIds": None}).clob_token_ids is None


def test_position_parses_venue_shape():
    position = Position.from_dict(
        json.loads(
            """{
        "asset": "71321045679252212594626385532706912750332728571942532289631379312455583992563",
        "size": 12.5,
        "conditionId": "0xabc",
        "eventSlug": "some-event",
        "title": "Some Event",
        "outcome": "Yes",
        "negativeRisk": true,
        "redeemable": true,
        "curPrice": 0.997,
        "somethingNew": {"ignored": true}
    }"""
        )
    )
    assert position.size == 12.5
    assert position.event_slug == "some-event"
    assert position.outcome == "Yes"
    assert position.negative_risk and position.redeemable


def test_position_is_lenient_about_missing_fields():
    position = Position.from_dict({"asset": "123", "size": 5.0})
    assert position.asset == "123"
    assert position.condition_id == ""
    assert not position.redeemable
