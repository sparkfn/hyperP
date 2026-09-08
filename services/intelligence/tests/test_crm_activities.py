"""Focused pure contracts for the bounded CRM activities archive."""

from __future__ import annotations

from pathlib import Path

import pytest
from intelligence.crm.activities import checkpoints
from intelligence.crm.activities.dispositions import assert_partition, classify
from intelligence.crm.activities.manifests import write_snapshot
from intelligence.crm.activities.models import (
    ArchiveRecord,
    ArchiveRequest,
    ParentReference,
    PersonReference,
)
from intelligence.crm.activities.reconciliation import capture, seal, verify_boundary
from intelligence.graph.queries.crm_activities import (
    ACTIVITY_COMPATIBILITY,
    PREFLIGHT_STRUCTURAL_INVALID,
    READ_SELECTED_PAGE,
)


def _record(
    identity: str,
    kind: str = "crm_history",
    *,
    lifecycle: str | None = "active",
    children: tuple[ParentReference, ...] = (),
    details: tuple[ParentReference, ...] = (),
) -> ArchiveRecord:
    stored_parent = ParentReference(
        None,
        "bitrix-primary",
        "deal-a",
        "crm_deal",
        "STORED_PARENT",
    )
    if kind == "call" and children:
        parent = children[0]
        stored_parent = ParentReference(
            None,
            parent.source_instance_id,
            parent.source_record_id,
            parent.record_type,
            "STORED_PARENT",
        )
    return ArchiveRecord(
        identity,
        f"record-{identity}",
        "1",
        f"version-{identity}",
        f"hash-{identity}",
        "bitrix-primary",
        "bitrix_chat",
        kind,
        lifecycle,
        "activity" if kind == "crm_history" else None,
        "call" if kind == "crm_history" else None,
        "bitrix_crm_activity" if kind == "crm_history" else None,
        "2" if kind == "crm_history" else None,
        "bitrix_crm_activity_v2" if kind == "crm_history" else None,
        None,
        "2026-09-01T00:00:00Z",
        None,
        stored_parent,
        children,
        details,
        (PersonReference("person-a", "active", None, identity),),
        (),
    )


def test_closed_query_contract_excludes_raw_payload_stage_and_conversations() -> None:
    lower = READ_SELECTED_PAGE.lower()
    assert "raw_payload" not in lower
    assert "conversation" not in lower
    assert "<> 'stage'" not in ACTIVITY_COMPATIBILITY
    assert "history_family = 'crm_activity'" in ACTIVITY_COMPATIBILITY
    assert "projection_source = 'bitrix_crm_activity_v1'" in ACTIVITY_COMPATIBILITY
    assert "IN ['1', '2']" in ACTIVITY_COMPATIBILITY
    assert "DETAILS_HISTORY_ITEM" in READ_SELECTED_PAGE
    assert "ingested_at" in READ_SELECTED_PAGE
    assert "source_system" in READ_SELECTED_PAGE


def test_closed_legacy_versions_are_explicit_and_preflight_is_read_only() -> None:
    assert "IN ['1', '2']" in ACTIVITY_COMPATIBILITY
    assert "<> 'stage'" not in ACTIVITY_COMPATIBILITY
    assert "CHILD_OF|DETAILS_HISTORY_ITEM" in PREFLIGHT_STRUCTURAL_INVALID
    lower = PREFLIGHT_STRUCTURAL_INVALID.lower()
    for forbidden in ("raw_payload", "create", "merge", "set", "delete"):
        assert forbidden not in lower


def test_parent_source_system_and_timestamps_are_preserved() -> None:
    record = ArchiveRecord(
        "activity-a",
        "record-a",
        "1",
        "version-a",
        "hash-a",
        "bitrix-primary",
        "bitrix_chat",
        "crm_history",
        "active",
        "activity",
        "call",
        "bitrix_crm_activity",
        "2",
        "bitrix_crm_activity_v2",
        "2026-09-08T01:00:00Z",
        "2026-09-08T02:00:00Z",
        "2026-09-08T04:00:00Z",
        ParentReference(
            None,
            "bitrix-primary",
            "deal-a",
            "crm_deal",
            "STORED_PARENT",
            "bitrix_chat",
        ),
        (),
        (),
        (),
        (),
        "2026-09-08T03:00:00Z",
    )
    assert record.stored_parent.source_system == "bitrix_chat"
    assert record.event_at == "2026-09-08T01:00:00Z"
    assert record.observed_at == "2026-09-08T02:00:00Z"
    assert record.ingested_at == "2026-09-08T03:00:00Z"
    assert record.available_at == "2026-09-08T04:00:00Z"


def test_dispositions_keep_missing_references_observable_but_quarantine_bad_calls() -> None:
    activity = _record("activity-a")
    parent = ParentReference(
        "activity-a", "bitrix-primary", "record-activity-a", "crm_history", "CHILD_OF"
    )
    details = ParentReference(
        "activity-a",
        "bitrix-primary",
        "record-activity-a",
        "crm_history",
        "DETAILS_HISTORY_ITEM",
    )
    valid_call = _record("call-a", "call", children=(parent,), details=(details,))
    bad_call = _record("call-b", "call", children=(parent,), details=())
    outcomes = classify((activity, valid_call, bad_call))
    assert [(item.source_record_pk, item.disposition, item.reason_code) for item in outcomes] == [
        ("activity-a", "accepted", None),
        ("call-a", "accepted", None),
        ("call-b", "quarantined", "ambiguous_companion_parent"),
    ]
    assert_partition((activity, valid_call, bad_call), outcomes)


def test_unknown_lifecycle_is_rejected_not_retried() -> None:
    outcomes = classify((_record("activity-a", lifecycle="future_state"),))
    assert outcomes[0].disposition == "rejected"
    assert outcomes[0].reason_code == "unknown_lifecycle"


def test_companion_edge_shapes_are_explicitly_quarantined() -> None:
    activity = _record("activity-a")
    child = ParentReference(
        "activity-a", "bitrix-primary", "record-activity-a", "crm_history", "CHILD_OF"
    )
    details = ParentReference(
        "activity-a",
        "bitrix-primary",
        "record-activity-a",
        "crm_history",
        "DETAILS_HISTORY_ITEM",
    )
    cross = ParentReference(
        "activity-a", "bitrix-secondary", "record-activity-a", "crm_history", "CHILD_OF"
    )
    conflict = ParentReference(
        "activity-b",
        "bitrix-primary",
        "record-activity-b",
        "crm_history",
        "DETAILS_HISTORY_ITEM",
    )
    records = (
        activity,
        _record("child-only", "call", children=(child,)),
        _record("details-only", "call", details=(details,)),
        _record("cross", "call", children=(cross,), details=(details,)),
        _record("conflict", "call", children=(child,), details=(conflict,)),
    )
    outcomes = {item.source_record_pk: item for item in classify(records)}
    assert outcomes["child-only"].disposition == "quarantined"
    assert outcomes["details-only"].disposition == "quarantined"
    assert outcomes["cross"].reason_code == "cross_instance_companion_parent"
    assert outcomes["conflict"].reason_code == "conflicting_companion_parent"


class _FakeRepository:
    def __init__(self, records: tuple[ArchiveRecord, ...]) -> None:
        self.records = records
        self.invalid_count = 0

    def page(
        self, request: ArchiveRequest, after_source_record_pk: str
    ) -> tuple[ArchiveRecord, ...]:
        return tuple(
            item for item in self.records if item.source_record_pk > after_source_record_pk
        )[: request.page_size]

    def by_identities(
        self, request: ArchiveRequest, identities: tuple[str, ...]
    ) -> tuple[ArchiveRecord, ...]:
        return tuple(item for item in self.records if item.source_record_pk in identities)

    def close(self) -> None:
        return None

    def structural_invalid_count(self, request: ArchiveRequest) -> int:
        del request
        return self.invalid_count


def test_structural_preflight_fails_closed_before_keyset_capture() -> None:
    repository = _FakeRepository((_record("activity-a"),))
    repository.invalid_count = 1
    with pytest.raises(RuntimeError, match="structural-invalid"):
        capture(repository, ArchiveRequest("snapshot-a", "bitrix-primary"), 10, 10)


def test_boundary_is_deterministic_across_source_pages_and_rejects_drift(
    tmp_path: Path,
) -> None:
    records = (_record("activity-a"), _record("activity-b"))
    repository = _FakeRepository(records)
    first = ArchiveRequest("snapshot-a", "bitrix-primary", page_size=1)
    second = ArchiveRequest("snapshot-a", "bitrix-primary", page_size=2)
    first_boundary = seal(capture(repository, first, 10, 10), first)
    second_boundary = seal(capture(repository, second, 10, 10), second)
    assert first_boundary.entries == second_boundary.entries
    assert first_boundary.digest == second_boundary.digest
    assert first_boundary.logical_snapshot_id == second_boundary.logical_snapshot_id
    first_staging = tmp_path / "staging" / "first"
    second_staging = tmp_path / "staging" / "second"
    first_staging.mkdir(parents=True)
    second_staging.mkdir(parents=True)
    first_manifest = write_snapshot(first_staging, first_boundary, records, classify(records))
    second_manifest = write_snapshot(second_staging, second_boundary, records, classify(records))
    assert first_manifest["snapshot_id"] == second_manifest["snapshot_id"]
    assert first_manifest["digest"] == second_manifest["digest"]
    first_page = next((first_staging / "snapshots" / "crm" / "activities").rglob("page-*.json"))
    second_page = next((second_staging / "snapshots" / "crm" / "activities").rglob("page-*.json"))
    assert first_page.read_bytes() == second_page.read_bytes()
    boundary = seal(capture(repository, first, 10, 10), first)
    repository.records = (_record("activity-a"), _record("activity-c"))
    with pytest.raises(RuntimeError, match="drift"):
        verify_boundary(repository, boundary)


def test_checkpoint_rejects_unsafe_links_and_conflicting_request(tmp_path: Path) -> None:
    run = tmp_path / "staging" / "run-a"
    run.mkdir(parents=True)
    root = checkpoints.checkpoint_root(run, "snapshot-a")
    request = ArchiveRequest("snapshot-a", "bitrix-primary")
    checkpoints.initialize(root, request)
    with pytest.raises(RuntimeError, match="conflicts"):
        checkpoints.initialize(root, ArchiveRequest("snapshot-a", "bitrix-secondary"))
    (root / "unsafe.json").symlink_to(root / "request.json")
    with pytest.raises(ValueError, match="unsafe"):
        checkpoints.bounded_usage(root, 100_000, 100)


def test_checkpoint_initialization_preserves_progress_and_hashed_colon_identity(
    tmp_path: Path,
) -> None:
    run = tmp_path / "staging" / "run-a"
    run.mkdir(parents=True)
    root = checkpoints.checkpoint_root(run, "checkpoint-a")
    request = ArchiveRequest("checkpoint-a", "bitrix-primary")
    checkpoints.initialize(root, request)
    boundary = seal((_record("record:with:colon"),), request)
    checkpoints.write_record(root, "record:with:colon", _record("record:with:colon").as_dict())
    checkpoints.write_boundary(root, boundary)
    checkpoints.advance(root, boundary.digest, 1)
    checkpoints.initialize(root, request)
    assert checkpoints.state(root)["phase"] == "paging"
    assert (
        checkpoints.read_record(root, "record:with:colon")["source_record_pk"]
        == "record:with:colon"
    )
    with pytest.raises(ValueError, match="Windows-safe"):
        checkpoints.checkpoint_root(run, "checkpoint:unsafe")
