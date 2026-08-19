"""Evidence row parser tests for the #125 sales prediction package.

The parsing rules are the #149 Gate 1 contract ported to the ingestion
service: naive timestamps are rejected, an invalid stage row censors its whole
parent, and amount/currency follow the same state machines — with the v1
addition of payload facts (amount value, currency, assignment, contacts).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from src.sales_prediction.evidence import (
    parse_deal_rows,
    parse_payload_facts,
    parse_stage_rows,
    parse_timestamp,
)

_EVENT_AT = "2026-01-10T08:00:00Z"
_AVAILABLE_AT = "2026-01-10T08:05:00Z"


def _stage_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "event_identity": "evt-1",
        "parent_source_system": "bitrix_chat",
        "parent_source_record_id": "deal-1",
        "mapped_state": "open",
        "event_at": _EVENT_AT,
        "available_at": _AVAILABLE_AT,
        "authority_head_version": 1,
        "category_id": "5",
        "stage_id": "C5:NEW",
        "source_semantic": "S",
    }
    row.update(overrides)
    return row


def _deal_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "parent_source_system": "bitrix_chat",
        "parent_source_record_id": "deal-1",
        "version_key": "4:abc:1",
        "source_record_version": 3,
        "entity_key": "eko",
        "observed_at": "2026-01-05T00:00:00Z",
        "ingested_at": "2026-01-05T00:01:00Z",
        "activated_at": "2026-01-05T00:01:00Z",
        "raw_payload": json.dumps(
            {
                "amount": "1200.50",
                "currency": "SGD",
                "ASSIGNED_BY_ID": "7",
                "CONTACT_IDS": ["1", "2"],
            }
        ),
        "lifecycle_status": "active",
        "linked_person_count": 1,
        "active_person_count": 1,
        "latest_linked_at": "2026-01-06T00:00:00Z",
        "linked_person_ids": ["p1"],
        "active_person_ids": ["p1"],
    }
    row.update(overrides)
    return row


def test_parse_timestamp_converts_to_utc_and_rejects_naive() -> None:
    assert parse_timestamp("2026-01-10T08:00:00Z") == datetime(2026, 1, 10, 8, 0, tzinfo=UTC)
    assert parse_timestamp("2026-01-10T16:00:00+08:00") == datetime(2026, 1, 10, 8, 0, tzinfo=UTC)
    assert parse_timestamp("2026-01-10T08:00:00") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None


def test_parse_stage_rows_keeps_category_and_stage_identity() -> None:
    events, invalid = parse_stage_rows([_stage_row()])
    assert invalid == frozenset()
    assert len(events) == 1
    event = events[0]
    assert event.category_id == "5"
    assert event.stage_id == "C5:NEW"
    assert event.source_semantic == "S"
    assert event.parent_key == ("bitrix_chat", "deal-1")


def test_parse_stage_rows_censors_parent_of_invalid_row() -> None:
    rows = [
        _stage_row(event_identity="evt-1"),
        _stage_row(event_identity="evt-2", event_at="not-a-date"),
        _stage_row(event_identity="evt-3", parent_source_record_id="deal-2"),
    ]
    events, invalid = parse_stage_rows(rows)
    assert invalid == frozenset({("bitrix_chat", "deal-1")})
    assert [event.event_identity for event in events] == ["evt-3"]


def test_parse_stage_rows_rejects_availability_before_event() -> None:
    events, invalid = parse_stage_rows([_stage_row(available_at="2026-01-10T07:00:00Z")])
    assert events == []
    assert invalid == frozenset({("bitrix_chat", "deal-1")})


def test_parse_deal_rows_extracts_payload_facts() -> None:
    versions = parse_deal_rows([_deal_row()])
    assert len(versions) == 1
    version = versions[0]
    assert version.amount_state == "known"
    assert version.amount_value == 1200.50
    assert version.currency_status == "supported"
    assert version.currency == "SGD"
    assert version.assigned_known is True
    assert version.contact_count == 2
    assert version.timestamps_valid is True


def test_parse_deal_rows_reads_nested_deal_payload() -> None:
    payload = json.dumps({"deal": {"opportunity": "99.9", "CURRENCY_ID": "USD"}})
    versions = parse_deal_rows([_deal_row(raw_payload=payload)])
    version = versions[0]
    assert version.amount_state == "known"
    assert version.amount_value == 99.9
    assert version.currency_status == "supported"
    assert version.currency == "USD"
    assert version.assigned_known is False
    assert version.contact_count == 0


def test_parse_deal_rows_marks_invalid_timestamps() -> None:
    versions = parse_deal_rows([_deal_row(superseded_at="oops")])
    assert versions[0].timestamps_valid is False


def test_parse_deal_rows_defaults_lifecycle_unknown() -> None:
    versions = parse_deal_rows([_deal_row(lifecycle_status=None)])
    assert versions[0].lifecycle_status == "unknown"


def test_parse_payload_facts_state_machine_matches_gate() -> None:
    assert parse_payload_facts(None).amount_state == "missing"
    assert parse_payload_facts("not-json").amount_state == "invalid"
    assert parse_payload_facts(json.dumps({"amount": "-5"})).amount_state == "invalid"
    assert parse_payload_facts(json.dumps({"amount": "0"})).amount_state == "zero"
    assert parse_payload_facts(json.dumps({"amount": True})).amount_state == "invalid"
    facts = parse_payload_facts(json.dumps({"amount": "10"}))
    assert facts.amount_value == 10.0
    unsupported = parse_payload_facts(json.dumps({"amount": "10", "currency": "EUR"}))
    assert unsupported.currency_status == "unsupported"
    assert unsupported.currency == "EUR"
    invalid_currency = parse_payload_facts(json.dumps({"amount": "10", "currency": "XX"}))
    assert invalid_currency.currency_status == "invalid"
