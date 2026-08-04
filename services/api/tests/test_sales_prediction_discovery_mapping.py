"""Tests for privacy-safe discovery aggregation and historical version selection."""

from __future__ import annotations

import json

from src.sales_prediction_discovery_mapping import (
    DiscoveryRow,
    aggregate_deals,
    aggregate_interactions,
    aggregate_sales,
)

_CUTOFF = "2026-08-01T00:00:00Z"


def _record(
    *,
    logical_record_id: str,
    version: int,
    lifecycle_status: str,
    raw_payload: str,
    activated_at: str | None = "2026-07-01T00:00:00Z",
    superseded_at: str | None = None,
    record_type: str | None = None,
) -> DiscoveryRow:
    return {
        "entity_key": "fundbox",
        "source_key": "bitrix_chat",
        "record_type": record_type,
        "logical_record_id": logical_record_id,
        "source_record_version": version,
        "lifecycle_status": lifecycle_status,
        "observed_at": "2026-07-01T00:00:00Z",
        "ingested_at": "2026-07-01T01:00:00Z",
        "activated_at": activated_at,
        "superseded_at": superseded_at,
        "rejected_at": None,
        "link_failed_at": None,
        "raw_payload": raw_payload,
        "linked_person_count": 1,
        "extraction_confidence": None,
    }


def test_pending_newer_deal_version_does_not_mask_accepted_version() -> None:
    old_payload = json.dumps(
        {
            "stage_id": "OPEN",
            "contact_count": 1,
            "crm_contact_resolution_required": False,
        }
    )
    new_payload = json.dumps(
        {
            "stage_id": "WON",
            "contact_count": 1,
            "crm_contact_resolution_required": False,
        }
    )
    rows = [
        _record(
            logical_record_id="private-deal-id",
            version=1,
            lifecycle_status="active",
            raw_payload=old_payload,
        ),
        _record(
            logical_record_id="private-deal-id",
            version=2,
            lifecycle_status="pending_review",
            raw_payload=new_payload,
            activated_at=None,
        ),
    ]

    output = aggregate_deals(rows, _CUTOFF)

    assert output == [
        {
            "entity_key": "fundbox",
            "stage_id": "OPEN",
            "historical_state": "accepted_at_cutoff",
            "linked_person_count": 1,
            "contact_count": 1,
            "ambiguous_contacts": False,
            "person_linkage_basis": "current_graph_projection",
            "missing_observed_at": False,
            "missing_stage_id": False,
            "invalid_stage_id": False,
            "invalid_contact_count": False,
            "invalid_ambiguous_contacts": False,
            "invalid_raw_payload": False,
            "deal_count": 1,
        }
    ]
    assert "private-deal-id" not in json.dumps(output)


def test_deal_aggregation_marks_invalid_payload_without_exposing_it() -> None:
    rows = [
        _record(
            logical_record_id="private-deal-id",
            version=1,
            lifecycle_status="active",
            raw_payload="{secret invalid payload",
        )
    ]

    output = aggregate_deals(rows, _CUTOFF)

    assert output[0]["invalid_raw_payload"] is True
    assert "secret" not in json.dumps(output)


def test_deal_aggregation_reports_multiple_versions_accepted_at_cutoff() -> None:
    payload = json.dumps(
        {
            "stage_id": "OPEN",
            "contact_count": 1,
            "crm_contact_resolution_required": False,
        }
    )
    rows = [
        _record(
            logical_record_id="private-deal-id",
            version=version,
            lifecycle_status="active",
            raw_payload=payload,
        )
        for version in (1, 2)
    ]

    output = aggregate_deals(rows, _CUTOFF)

    assert output[0]["historical_state"] == "multiple_accepted_versions_at_cutoff"


def test_deal_aggregation_rejects_naive_lifecycle_timestamp() -> None:
    payload = json.dumps(
        {
            "stage_id": "OPEN",
            "contact_count": 1,
            "crm_contact_resolution_required": False,
        }
    )
    row = _record(
        logical_record_id="private-deal-id",
        version=1,
        lifecycle_status="active",
        raw_payload=payload,
        activated_at="2026-07-01T00:00:00",
    )

    output = aggregate_deals([row], _CUTOFF)

    assert output[0]["historical_state"] == "invalid_lifecycle_timestamp"


def test_interaction_aggregation_rejects_boolean_confidence_and_reads_call_duration() -> None:
    call_payload = json.dumps({"duration_seconds": 120, "subject": "private"})
    row = _record(
        logical_record_id="private-call-id",
        version=1,
        lifecycle_status="active",
        raw_payload=call_payload,
        record_type="call",
    )
    row["extraction_confidence"] = True

    output = aggregate_interactions([row], _CUTOFF)

    assert output[0]["confidence_present_count"] == 0
    assert output[0]["duration_present_count"] == 1
    assert "private" not in json.dumps(output)


def test_sales_aggregation_reports_invalid_timestamp_and_link_cardinality() -> None:
    payload = json.dumps(
        {
            "order": {
                "status": "completed",
                "ordered_at": "not-a-date",
                "currency": "SGD",
                "source_order_id": "private-order-id",
            }
        }
    )
    row = _record(
        logical_record_id="private-sales-id",
        version=1,
        lifecycle_status="active",
        raw_payload=payload,
    )
    row["source_key"] = "fundbox:sales"
    row["linked_person_count"] = 2

    output = aggregate_sales([row], _CUTOFF)

    assert output[0]["order_status"] == "completed"
    assert output[0]["linked_person_count"] == 2
    assert output[0]["invalid_ordered_at"] is True
    assert "private-order-id" not in json.dumps(output)
