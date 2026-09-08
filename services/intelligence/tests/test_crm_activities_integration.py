"""Cross-module archive codec, boundary, and replay integration contracts."""

from __future__ import annotations

from intelligence.crm.activities.model_parsing import record_from_mapping
from intelligence.crm.activities.models import ArchiveRequest, BoundaryEntry, sha256_json
from intelligence.crm.activities.reconciliation import seal


def _row(link_status: str) -> dict[str, object]:
    return {
        "source_record_pk": "record-a",
        "source_record_id": "history-a",
        "source_record_version": "1",
        "source_version_key": "history-a-v1",
        "record_hash": "hash-a",
        "source_instance_id": "bitrix-primary",
        "source_key": "bitrix_chat",
        "record_type": "crm_history",
        "lifecycle_status": "active",
        "history_family": "activity",
        "history_kind": "call",
        "history_source": "bitrix_crm_activity",
        "projection_version": "2",
        "projection_source": "bitrix_crm_activity_v2",
        "event_at": "2026-09-08T01:00:00Z",
        "observed_at": "2026-09-08T02:00:00Z",
        "ingested_at": "2026-09-08T03:00:00Z",
        "available_at": "2026-09-08T04:00:00Z",
        "link_status": link_status,
        "stored_parent": {
            "source_record_pk": None,
            "source_instance_id": "bitrix-primary",
            "source_record_id": "deal-a",
            "record_type": "crm_deal",
            "source_system": "bitrix_chat",
        },
        "child_parents": [],
        "details_parents": [],
        "people": [
            {
                "person_id": "person-a",
                "status": "active",
                "revision": "3",
                "association_source_record_pk": "record-a",
            }
        ],
        "user_capabilities": [],
    }


def test_json_codec_round_trip_keeps_person_association_and_link_status() -> None:
    original = record_from_mapping(_row("linked"))
    round_trip = record_from_mapping(original.as_dict())
    assert round_trip == original
    assert round_trip.digest() == original.digest()
    assert round_trip.people[0].association_source_record_pk == "record-a"
    assert round_trip.link_status == "linked"


def test_boundary_changes_for_database_contract_and_link_status_drift() -> None:
    first = record_from_mapping(_row("linked"))
    second = record_from_mapping(_row("pending_review"))
    first_request = ArchiveRequest("checkpoint-a", "bitrix-primary", database_identity="db-one")
    second_request = ArchiveRequest("checkpoint-a", "bitrix-primary", database_identity="db-two")
    assert seal((first,), first_request).digest != seal((first,), second_request).digest
    assert BoundaryEntry.from_record(first) != BoundaryEntry.from_record(second)
    assert sha256_json(first.as_dict()) == first.digest()
