"""Tests for private CRM parsing and allowlisted discovery aggregates."""

from __future__ import annotations

import json

from src.sales_prediction_discovery_mapping import (
    DiscoveryRow,
    aggregate_deals,
    aggregate_history_capability,
    aggregate_interactions,
)

_AS_OF = "2026-08-01T00:00:00Z"
_REPORT = "2026-08-05T00:00:00Z"
_ENTITIES = ("fundbox",)
_STAGE_CATALOG = {"fundbox": frozenset({"OPEN", "WON"})}


def _record(
    *,
    identifier: str = "private-deal-id",
    version: int = 1,
    payload: dict[str, object],
    observed_at: str = "2026-07-01T00:00:00Z",
    ingested_at: str = "2026-07-01T01:00:00Z",
    activated_at: str | None = "2026-07-01T02:00:00Z",
    superseded_at: str | None = None,
    rejected_at: str | None = None,
    link_failed_at: str | None = None,
) -> DiscoveryRow:
    return {
        "entity_key": "fundbox",
        "source_key": "bitrix_chat",
        "logical_record_id": identifier,
        "source_record_version": version,
        "observed_at": observed_at,
        "ingested_at": ingested_at,
        "activated_at": activated_at,
        "raw_payload": json.dumps(payload),
        "linked_person_count": 1,
        "superseded_at": superseded_at,
        "rejected_at": rejected_at,
        "link_failed_at": link_failed_at,
        "crm_history_child_count": 0,
    }


def test_amount_zero_is_distinct_from_missing_and_private_identifier_is_not_emitted() -> None:
    zero = _record(payload={"stage_id": "OPEN", "amount": "0", "currency": "SGD"})
    missing = _record(
        identifier="private-other-id", payload={"stage_id": "OPEN", "currency": "SGD"}
    )

    output = aggregate_deals([zero, missing], _AS_OF, _REPORT, _ENTITIES, _STAGE_CATALOG)

    assert {row["amount_state"] for row in output} == {"zero", "missing"}
    assert "private-deal-id" not in json.dumps(output)
    assert "private-other-id" not in json.dumps(output)


def test_late_arriving_evidence_is_not_historical_state() -> None:
    row = _record(
        payload={"stage_id": "OPEN"},
        ingested_at="2026-08-03T00:00:00Z",
        activated_at="2026-08-03T00:00:00Z",
    )

    output = aggregate_deals([row], _AS_OF, _REPORT, _ENTITIES, _STAGE_CATALOG)

    assert output[0]["historical_availability_status"] == "late_arriving_after_as_of"


def test_missing_activation_is_uncertain_not_inferred_from_current_lifecycle() -> None:
    row = _record(payload={"stage_id": "OPEN"}, activated_at=None)
    row["lifecycle_status"] = "active"

    output = aggregate_deals([row], _AS_OF, _REPORT, _ENTITIES, _STAGE_CATALOG)

    assert output[0]["historical_availability_status"] == (
        "historical_availability_unreconstructable"
    )


def test_rejected_link_failed_and_superseded_records_are_not_available_as_of() -> None:
    cases = (
        ("rejected_at", "rejected_by_as_of"),
        ("link_failed_at", "link_failed_by_as_of"),
        ("superseded_at", "superseded_by_as_of"),
    )
    for field, expected in cases:
        row = _record(payload={"stage_id": "OPEN"})
        row[field] = "2026-07-15T00:00:00Z"

        output = aggregate_deals([row], _AS_OF, _REPORT, _ENTITIES, _STAGE_CATALOG)

        assert output[0]["historical_availability_status"] == expected


def test_lifecycle_events_after_report_cutoff_do_not_change_historical_status() -> None:
    cases = ("rejected_at", "link_failed_at", "superseded_at")
    for field in cases:
        row = _record(payload={"stage_id": "OPEN"})
        row[field] = "2026-08-10T00:00:00Z"

        output = aggregate_deals([row], _AS_OF, _REPORT, _ENTITIES, _STAGE_CATALOG)

        assert output[0]["historical_availability_status"] == "available_as_of"


def test_snapshot_difference_is_not_an_authoritative_transition() -> None:
    first = _record(version=1, payload={"stage_id": "OPEN"})
    second = _record(version=2, payload={"stage_id": "WON"})

    output = aggregate_history_capability([first, second], _ENTITIES)

    assert output[0]["snapshot_detected_stage_difference"] == "yes"
    assert output[0]["authoritative_stage_transition_coverage"] == "unavailable"
    assert output[0]["first_won_reconstructability"] == "unreconstructable"


def test_history_capability_reports_entity_and_category_movement() -> None:
    first = _record(version=1, payload={"stage_id": "OPEN", "category_id": "1"})
    second = _record(version=2, payload={"stage_id": "OPEN", "category_id": "2"})
    second["entity_key"] = "eko"

    output = aggregate_history_capability([first, second], _ENTITIES)

    assert output[0]["entity_ownership_movement"] == "observed"
    assert output[0]["category_movement"] == "observed"


def test_optional_calls_do_not_supply_label_capability() -> None:
    call = _record(identifier="private-call-id", payload={"duration_seconds": 120})
    call["record_type"] = "call"

    output = aggregate_interactions([call], _AS_OF, _REPORT, _ENTITIES)

    assert output[0]["call_duration_status"] == "present"


def test_interaction_ids_are_scoped_by_record_type() -> None:
    call = _record(identifier="shared-private-id", payload={"duration_seconds": 120})
    call["record_type"] = "call"
    conversation = _record(identifier="shared-private-id", version=2, payload={"message_count": 1})
    conversation["record_type"] = "conversation"

    output = aggregate_interactions([call, conversation], _AS_OF, _REPORT, _ENTITIES)

    assert sum(int(row["record_count"]) for row in output) == 2
    assert {row["record_type"] for row in output} == {"call", "conversation"}


def test_non_finite_interaction_confidence_is_invalid() -> None:
    conversation = _record(payload={"message_count": 1})
    conversation["record_type"] = "conversation"
    conversation["extraction_confidence"] = float("nan")

    output = aggregate_interactions([conversation], _AS_OF, _REPORT, _ENTITIES)

    assert output[0]["confidence_status"] == "missing_or_invalid"


def test_out_of_range_interaction_confidence_is_invalid() -> None:
    conversation = _record(payload={"message_count": 1})
    conversation["record_type"] = "conversation"
    conversation["extraction_confidence"] = 1.01

    output = aggregate_interactions([conversation], _AS_OF, _REPORT, _ENTITIES)

    assert output[0]["confidence_status"] == "missing_or_invalid"


def test_unknown_record_type_value_is_not_emitted() -> None:
    interaction = _record(payload={"message_count": 1})
    interaction["record_type"] = "private-customer-reference"

    output = aggregate_interactions([interaction], _AS_OF, _REPORT, _ENTITIES)

    assert output[0]["record_type"] == "invalid_or_unknown"
    assert "private-customer-reference" not in json.dumps(output)


def test_deeply_nested_payload_is_counted_invalid_without_exposing_content() -> None:
    row = _record(payload={"stage_id": "OPEN"})
    row["raw_payload"] = "[" * 2000 + '"private-content"' + "]" * 2000

    output = aggregate_deals([row], _AS_OF, _REPORT, _ENTITIES, _STAGE_CATALOG)

    assert output[0]["raw_payload_status"] == "invalid"
    assert "private-content" not in json.dumps(output)


def test_unmapped_identifier_like_taxonomies_are_not_emitted() -> None:
    row = _record(
        payload={
            "stage_id": "CUSTOMER123456789",
            "category_id": "customer-secret",
            "currency": "AB12",
        }
    )

    output = aggregate_deals([row], _AS_OF, _REPORT, _ENTITIES, _STAGE_CATALOG)

    rendered = json.dumps(output)
    assert output[0]["stage_id"] == "unmapped_or_unknown"
    assert output[0]["category_status"] == "invalid_or_unknown"
    assert output[0]["currency_code"] == "invalid_or_unknown"
    assert "CUSTOMER123456789" not in rendered
    assert "customer-secret" not in rendered


def test_valid_but_unsupported_currency_is_distinct_from_invalid() -> None:
    row = _record(payload={"stage_id": "OPEN", "currency": "EUR"})

    output = aggregate_deals([row], _AS_OF, _REPORT, _ENTITIES, _STAGE_CATALOG)

    assert output[0]["currency_status"] == "present_valid_but_unsupported"
    assert output[0]["currency_code"] == "valid_but_unsupported"


def test_three_letter_non_iso_currency_is_invalid() -> None:
    row = _record(payload={"stage_id": "OPEN", "currency": "ZZZ"})

    output = aggregate_deals([row], _AS_OF, _REPORT, _ENTITIES, _STAGE_CATALOG)

    assert output[0]["currency_status"] == "invalid"
    assert output[0]["currency_code"] == "invalid_or_unknown"


def test_non_finite_amount_and_probability_values_are_invalid() -> None:
    for value in ("nan", "inf", "-inf"):
        row = _record(payload={"stage_id": "OPEN", "amount": value, "probability": value})

        output = aggregate_deals([row], _AS_OF, _REPORT, _ENTITIES, _STAGE_CATALOG)

        assert output[0]["amount_state"] == "invalid"
        assert output[0]["probability_state"] == "invalid"


def test_unknown_sources_with_same_logical_id_are_not_coalesced() -> None:
    first = _record(version=1, payload={"stage_id": "OPEN"})
    first["source_key"] = "unexpected_a"
    second = _record(version=2, payload={"stage_id": "WON"})
    second["source_key"] = "unexpected_b"

    output = aggregate_history_capability([first, second], _ENTITIES)

    assert sum(int(row["logical_deal_count"]) for row in output) == 2
    assert all(row["snapshot_detected_stage_difference"] == "no" for row in output)
