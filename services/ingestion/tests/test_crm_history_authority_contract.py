"""Contract tests for #146 typed CRM-history authority boundaries."""

from __future__ import annotations

from src.crm_history_contract import activity_reader_predicate, generic_activity_properties
from src.graph.crm_history_projection_migration import (
    ACQUIRE_PROJECTION_MIGRATION,
    PROJECT_LEGACY_ACTIVITY_BATCH,
    RELEASE_PROJECTION_MIGRATION,
    ROLLBACK_LEGACY_ACTIVITY_BATCH,
    rollback_legacy_generic_activities,
)
from src.graph.queries.crm_history import CREATE_CRM_HISTORY
from src.graph.queries.crm_history_authority import (
    APPEND_CRM_HISTORY_AUTHORITY_DECISION,
    CREATE_CRM_HISTORY_AUTHORITY_CONSTRAINTS,
)
from src.models import RecordType, SourceRecordEnvelope, SourceRecordParentRef


def _activity() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-history-42",
        record_type=RecordType.CRM_HISTORY,
        observed_at="2026-08-06T04:00:00Z",
        record_hash="sha256:activity",
        parent_ref=SourceRecordParentRef(
            parent_source_system="bitrix_chat",
            parent_source_record_id="bitrix-deal-7",
            parent_record_type=RecordType.CRM_DEAL,
        ),
    )


def test_generic_activities_are_typed_at_creation_and_reader_is_fail_closed() -> None:
    properties = generic_activity_properties(_activity())

    assert properties.history_family.value == "activity"
    assert properties.event_at == "2026-08-06T04:00:00Z"
    assert "history_family: $history_family" in CREATE_CRM_HISTORY
    assert "history_projected_at: datetime()" in CREATE_CRM_HISTORY
    assert activity_reader_predicate() == (
        "(history.history_family IS NULL OR history.history_family = 'activity')"
    )
    assert "<> 'stage'" not in activity_reader_predicate()


def test_authority_ledger_requires_active_run_generation_and_head_cas() -> None:
    assert len(CREATE_CRM_HISTORY_AUTHORITY_CONSTRAINTS) == 4
    assert "[:ACTIVE_ATTEMPT]" in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert "logical.active_generation = $generation" in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert APPEND_CRM_HISTORY_AUTHORITY_DECISION.index("OPTIONAL MATCH (head") < (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION.index("MERGE (group")
    )
    assert "head.head_version = $expected_head_version" in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert "head.fence_token = $expected_fence_token" in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert "$next_fence_token > $expected_fence_token" in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert "[:SELECTS_VARIANT]->(variant)" in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert "CREATE (decision:CrmHistoryAuthorityDecision" in APPEND_CRM_HISTORY_AUTHORITY_DECISION


def test_legacy_projection_is_marked_and_rollback_refuses_native_overwrite() -> None:
    assert callable(rollback_legacy_generic_activities)
    assert "migration.lease_owner = $lease_owner" in PROJECT_LEGACY_ACTIVITY_BATCH
    assert "migration.lease_expires_at >= datetime()" in PROJECT_LEGACY_ACTIVITY_BATCH
    assert "lease_expires_at" in ACQUIRE_PROJECTION_MIGRATION
    assert "migration.lease_owner = $lease_owner" in RELEASE_PROJECTION_MIGRATION
    assert "record.history_family IS NULL" in PROJECT_LEGACY_ACTIVITY_BATCH
    assert "record.history_family = $history_family" in ROLLBACK_LEGACY_ACTIVITY_BATCH
    assert "record.event_at = record.observed_at" in PROJECT_LEGACY_ACTIVITY_BATCH
    assert "activated_at" not in PROJECT_LEGACY_ACTIVITY_BATCH
    assert "crm_history_projection_migration" in ROLLBACK_LEGACY_ACTIVITY_BATCH
    assert "record.history_source = $history_source" in ROLLBACK_LEGACY_ACTIVITY_BATCH
    assert (
        "record.history_projection_version = $projection_version"
        in ROLLBACK_LEGACY_ACTIVITY_BATCH
    )
