"""Cutoff-aware privacy and provenance mapping contracts."""

from __future__ import annotations

import json

import pytest
from intelligence.crm_deal_refs.mapping import map_deal_reference, map_identity_revision
from intelligence.crm_deal_refs.models import IDENTITY_POLICY_VERSION

_AS_OF = "2026-02-01T00:00:00Z"


def _deal(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "source_record_id": "bitrix-crm-deal-42",
        "source_record_version": 1,
        "source_record_pk": "pk-1",
        "record_hash": "a" * 64,
        "source_entity_type": "deal",
        "source_entity_id": "42",
        "identity_policy_version": IDENTITY_POLICY_VERSION,
        "record_entity_key": "entity-a",
        "owned_entity_keys": [],
        "owned_entity_count": 0,
        "stage_id": "C1:NEW",
        "observed_at": "2026-01-01T00:00:00Z",
        "ingested_at": "2026-01-02T00:00:00Z",
        "lifecycle_status": "active",
        "link_status": "unresolved",
        "raw_payload": json.dumps(
            {
                "crm_deal_id": "42",
                "category_id": "1",
                "stage_id": "C1:NEW",
                "crm_deal_identity_policy_version": IDENTITY_POLICY_VERSION,
                "deal": {
                    "ID": "42",
                    "CATEGORY_ID": "1",
                    "STAGE_ID": "C1:NEW",
                    "STAGE_SEMANTIC_ID": "P",
                    "DATE_CREATE": "2026-01-01T00:00:00Z",
                    "DATE_MODIFY": "2026-01-02T00:00:00Z",
                    "CLOSEDATE": "2026-01-03T00:00:00Z",
                },
            }
        ),
    }
    row.update(changes)
    return row


def _identity(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "event_id": "event-1",
        "global_revision": 1,
        "source_instance_id": "instance-a",
        "source_entity_id": "42",
        "identity_policy_version": IDENTITY_POLICY_VERSION,
        "link_status": "unresolved",
        "hyperp_person_id": None,
        "person_status": None,
        "resolution_kind": "baseline",
        "resolution_revision": 1,
        "effective_at": "2026-01-01T00:00:00Z",
        "created_at": "2026-01-02T00:00:00Z",
    }
    row.update(changes)
    return row


def test_mapping_keeps_provenance_and_grounded_fields() -> None:
    record = map_deal_reference(_deal(), "instance-a", _AS_OF)
    assert record.record_hash == "a" * 64 and record.stage_semantic_id == "P"
    assert record.source_event_at == "2026-01-01T00:00:00Z"
    assert record.point_in_time_eligible and "raw_payload" not in record.__dict__
    prefixed = map_deal_reference(_deal(record_hash="sha256:" + "b" * 64), "instance-a", _AS_OF)
    assert prefixed.record_hash == "sha256:" + "b" * 64


@pytest.mark.parametrize("field", ("observed_at", "ingested_at"))
def test_future_or_unknown_deal_values_are_not_eligible(field: str) -> None:
    record = map_deal_reference(_deal(**{field: "2026-03-01T00:00:00Z"}), "instance-a", _AS_OF)
    assert not record.point_in_time_eligible
    unknown = map_deal_reference(_deal(ingested_at=None), "instance-a", _AS_OF)
    assert unknown.availability == "unknown" and not unknown.point_in_time_eligible


@pytest.mark.parametrize("field", ("DATE_CREATE", "DATE_MODIFY", "CLOSEDATE"))
def test_future_source_times_are_preserved_but_not_eligible(field: str) -> None:
    payload = json.loads(str(_deal()["raw_payload"]))
    payload["deal"][field] = "2026-03-01T00:00:00Z"
    record = map_deal_reference(_deal(raw_payload=json.dumps(payload)), "instance-a", _AS_OF)
    assert not record.point_in_time_eligible
    expected = "2026-03-01T00:00:00Z"
    assert expected in {
        record.source_event_at,
        record.source_effective_at,
        record.source_close_date,
    }


def test_equal_time_source_evidence_is_eligible() -> None:
    payload = json.loads(str(_deal()["raw_payload"]))
    for field in ("DATE_CREATE", "DATE_MODIFY", "CLOSEDATE"):
        payload["deal"][field] = _AS_OF
    record = map_deal_reference(
        _deal(observed_at=_AS_OF, ingested_at=_AS_OF, raw_payload=json.dumps(payload)),
        "instance-a",
        _AS_OF,
    )
    assert record.point_in_time_eligible
    assert record.available_at == record.first_known_at == _AS_OF


def test_empty_optional_close_and_stage_semantic_values_remain_missing() -> None:
    payload = json.loads(str(_deal()["raw_payload"]))
    payload["deal"]["CLOSEDATE"] = ""
    payload["deal"]["STAGE_SEMANTIC_ID"] = ""
    record = map_deal_reference(_deal(raw_payload=json.dumps(payload)), "instance-a", _AS_OF)
    assert record.source_close_date is None
    assert record.stage_semantic_id is None
    assert record.stage_id == "C1:NEW"


def test_policy_and_ownership_are_fail_closed_not_filtered() -> None:
    with pytest.raises(ValueError, match="policy"):
        map_deal_reference(_deal(identity_policy_version="old"), "instance-a", _AS_OF)
    with pytest.raises(ValueError, match="ambiguous"):
        map_deal_reference(
            _deal(
                record_entity_key=None,
                owned_entity_keys=["a", "b"],
                owned_entity_count=2,
            ),
            "instance-a",
            _AS_OF,
        )


@pytest.mark.parametrize(
    "status", ("unresolved", "pending_review", "blocked", "rejected", "retired")
)
def test_nonresolved_identity_never_exposes_person(status: str) -> None:
    with pytest.raises(ValueError, match="must not expose"):
        map_identity_revision(
            _identity(link_status=status, hyperp_person_id="00000000-0000-0000-0000-000000000001"),
            "instance-a",
            _AS_OF,
        )


def test_future_resolved_identity_does_not_expose_person() -> None:
    future = map_identity_revision(
        _identity(
            link_status="resolved",
            hyperp_person_id="00000000-0000-0000-0000-000000000001",
            person_status="active",
            created_at="2026-03-01T00:00:00Z",
        ),
        "instance-a",
        _AS_OF,
    )
    assert future.hyperp_person_id is None and future.person_status_observed is None
    assert not future.person_reference_eligible
    future_effective = map_identity_revision(
        _identity(
            link_status="resolved",
            hyperp_person_id="00000000-0000-0000-0000-000000000001",
            person_status="active",
            effective_at="2026-03-01T00:00:00Z",
        ),
        "instance-a",
        _AS_OF,
    )
    assert future_effective.hyperp_person_id is None
    assert not future_effective.person_reference_eligible
    missing_effective = map_identity_revision(
        _identity(
            link_status="resolved",
            hyperp_person_id="00000000-0000-0000-0000-000000000001",
            person_status="active",
            effective_at=None,
        ),
        "instance-a",
        _AS_OF,
    )
    assert missing_effective.hyperp_person_id is None
    assert not missing_effective.person_reference_eligible
    known = map_identity_revision(
        _identity(
            link_status="resolved",
            hyperp_person_id="00000000-0000-0000-0000-000000000001",
            person_status="active",
        ),
        "instance-a",
        _AS_OF,
    )
    assert known.hyperp_person_id is not None
    assert known.person_reference_eligible
