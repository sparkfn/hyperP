"""Contract tests for #146 typed CRM-history authority boundaries."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest
from neo4j import ManagedTransaction
from src.crm_history_contract import activity_reader_predicate, generic_activity_properties
from src.graph.client import Neo4jClient
from src.graph.crm_history_authority import (
    AuthorityDecision,
    AuthorityDecisionConflictError,
    AuthorityWriteContext,
    append_authority_decision,
    append_authority_decision_in_transaction,
)
from src.graph.crm_history_projection_migration import (
    ACQUIRE_PROJECTION_MIGRATION,
    PROJECT_LEGACY_ACTIVITY_BATCH,
    RELEASE_PROJECTION_MIGRATION,
    ROLLBACK_LEGACY_ACTIVITY_BATCH,
    project_legacy_generic_activities,
    rollback_legacy_generic_activities,
)
from src.graph.queries.crm_history import (
    ACTIVATE_PENDING_CALLS_FOR_DEAL,
    CREATE_CALL_FROM_HISTORY,
    CREATE_CRM_HISTORY,
)
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
    for query in (CREATE_CALL_FROM_HISTORY, ACTIVATE_PENDING_CALLS_FOR_DEAL):
        assert "history.history_family IS NULL OR history.history_family = 'activity'" in query


def test_authority_ledger_requires_active_run_generation_and_head_cas() -> None:
    assert len(CREATE_CRM_HISTORY_AUTHORITY_CONSTRAINTS) == 4
    assert "[:ACTIVE_ATTEMPT]" in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert "logical.active_generation = $generation" in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert callable(append_authority_decision)
    assert callable(append_authority_decision_in_transaction)
    assert "BitrixIngestionStream" not in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert "resolved_head.head_version = $expected_head_version" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "current_authority_token = $expected_authority_token" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "$next_authority_token = $expected_authority_token + 1" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "coalesce(resolved_head.authority_token, resolved_head.fence_token, 0)" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "resolved_head.authority_token = decision.authority_token" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "resolved_head.fence_token = decision.authority_token" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "$decision_kind = 'accepted' AND $authority_state = 'effective'" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "$authority_state IN ['withheld_parent', 'rejected']" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "existing.available_at = datetime($available_at)" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "known_variant IS NULL OR known_variant.hash_version = $hash_version" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "head_version: $expected_head_version + 1" in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert "existing.run_id = attempt.ingest_run_id" not in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert "existing.run_generation = logical.active_generation" not in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "existing.prior_head_version = $expected_head_version" not in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "datetime($available_at) >= correction_target.available_at" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "datetime($available_at) >= current_decision.available_at" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "semantic_match AS returned_semantic_match" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "returned_semantic_match AS semantic_match" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "\n         semantic_match AS semantic_match\n" not in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "true AS replayed" in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert "coalesce(existing.authority_token, existing.fence_token)" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "CREATE (decision)-[:CORRECTS]->(correction_target)" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )
    assert "[:SELECTS_VARIANT]->(variant)" in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert "CREATE (decision:CrmHistoryAuthorityDecision" in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert "size(association_parents) = 1" in APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert "association_parent.record_type = 'crm_deal'" in (APPEND_CRM_HISTORY_AUTHORITY_DECISION)
    assert "association_parent.lifecycle_status = 'active'" in (
        APPEND_CRM_HISTORY_AUTHORITY_DECISION
    )


def test_legacy_projection_is_marked_and_rollback_refuses_native_overwrite() -> None:
    assert callable(rollback_legacy_generic_activities)
    assert "migration.lease_owner = $lease_owner" in PROJECT_LEGACY_ACTIVITY_BATCH
    assert "migration.lease_expires_at >= datetime()" in PROJECT_LEGACY_ACTIVITY_BATCH
    assert "SET migration.lease_expires_at = migration.lease_expires_at" in (
        PROJECT_LEGACY_ACTIVITY_BATCH
    )
    assert "lease_expires_at" in ACQUIRE_PROJECTION_MIGRATION
    assert "migration.lease_owner = $lease_owner" in RELEASE_PROJECTION_MIGRATION
    assert "record.history_family IS NULL" in PROJECT_LEGACY_ACTIVITY_BATCH
    assert "record.history_kind IS NULL" in PROJECT_LEGACY_ACTIVITY_BATCH
    assert "record.history_projected_at IS NULL" in PROJECT_LEGACY_ACTIVITY_BATCH
    assert "record.history_family = $history_family" in ROLLBACK_LEGACY_ACTIVITY_BATCH
    assert "record.event_at = record.observed_at" in PROJECT_LEGACY_ACTIVITY_BATCH
    assert "activated_at" not in PROJECT_LEGACY_ACTIVITY_BATCH
    assert "crm_history_projection_migration" in ROLLBACK_LEGACY_ACTIVITY_BATCH
    assert "record.history_source = $history_source" in ROLLBACK_LEGACY_ACTIVITY_BATCH
    assert "record.event_at IS NULL AND record.observed_at IS NULL" in (
        ROLLBACK_LEGACY_ACTIVITY_BATCH
    )
    assert "record.history_projected_at = record.crm_history_projection_migrated_at" in (
        ROLLBACK_LEGACY_ACTIVITY_BATCH
    )
    assert (
        "record.history_projection_version = $projection_version" in ROLLBACK_LEGACY_ACTIVITY_BATCH
    )


def test_authority_models_reject_invalid_fences_and_correction_shapes() -> None:
    with pytest.raises(ValueError, match="advance"):
        AuthorityWriteContext(
            logical_run_id="logical",
            ingest_run_id="attempt",
            generation=1,
            expected_head_version=2,
            expected_fence_token=4,
            next_fence_token=4,
        )
    context = AuthorityWriteContext(
        logical_run_id="logical",
        ingest_run_id="attempt",
        generation=1,
        expected_head_version=2,
        expected_fence_token=4,
        next_fence_token=5,
    )
    assert context.expected_authority_token == 4
    assert context.next_authority_token == 5
    assert context.expected_fence_token == 4
    with pytest.raises(ValueError, match="require"):
        AuthorityDecision(
            decision_id="decision",
            event_identity="event",
            canonical_hash="sha256:event",
            hash_version="bitrix-stage-history-v1",
            decision_kind="correction",
            available_at="2026-08-06T04:00:00Z",
            logical_parent_source_system="bitrix_chat",
            logical_parent_source_record_id="deal-1",
        )
    with pytest.raises(ValueError, match="kind/state"):
        AuthorityDecision(
            decision_id="decision",
            event_identity="event",
            canonical_hash="sha256:event",
            hash_version="bitrix-stage-history-v1",
            decision_kind="accepted",
            authority_state="withheld_conflict",
            available_at="2026-08-06T04:00:00Z",
            logical_parent_source_system="bitrix_chat",
            logical_parent_source_record_id="deal-1",
        )


class _AuthorityTx:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.query: str | None = None
        self.params: dict[str, object] = {}

    def run(self, query: str, **params: object) -> _Result:
        self.query = query
        self.params = params
        return _Result(self.row)


def _authority_context() -> AuthorityWriteContext:
    return AuthorityWriteContext(
        logical_run_id="logical",
        ingest_run_id="attempt-2",
        generation=2,
        expected_head_version=1,
        expected_authority_token=1,
        next_authority_token=2,
    )


def _authority_decision() -> AuthorityDecision:
    return AuthorityDecision(
        decision_id="decision",
        event_identity="event",
        canonical_hash="sha256:event",
        hash_version="bitrix-stage-history-v1",
        decision_kind="variant",
        authority_state="withheld_conflict",
        available_at="2026-08-14T04:00:00Z",
        logical_parent_source_system="bitrix_chat",
        logical_parent_source_record_id="deal-1",
    )


def test_in_transaction_authority_append_maps_replay_and_authority_token() -> None:
    tx = _AuthorityTx(
        {
            "decision_id": "decision",
            "head_version": 2,
            "authority_token": 2,
            "replayed": True,
            "semantic_match": True,
        }
    )

    result = append_authority_decision_in_transaction(
        cast(ManagedTransaction, tx),
        _authority_context(),
        _authority_decision(),
    )

    assert result is not None
    assert result.replayed is True
    assert result.authority_token == result.fence_token == 2
    assert tx.query == APPEND_CRM_HISTORY_AUTHORITY_DECISION
    assert tx.params["expected_authority_token"] == 1
    assert tx.params["authority_state"] == "withheld_conflict"
    assert "expected_fence_token" not in tx.params


def test_in_transaction_authority_append_rejects_same_id_different_semantics() -> None:
    tx = _AuthorityTx(
        {
            "decision_id": "decision",
            "head_version": 2,
            "authority_token": 2,
            "replayed": True,
            "semantic_match": False,
        }
    )

    with pytest.raises(AuthorityDecisionConflictError, match="different immutable semantics"):
        append_authority_decision_in_transaction(
            cast(ManagedTransaction, tx),
            _authority_context(),
            _authority_decision(),
        )


class _Result:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row


class _LeaseLossTx:
    def run(self, query: str, **_params: object) -> _Result:
        if query == ACQUIRE_PROJECTION_MIGRATION:
            return _Result({"acquired": True})
        if query == RELEASE_PROJECTION_MIGRATION:
            return _Result({"released": True})
        if query == PROJECT_LEGACY_ACTIVITY_BATCH:
            return _Result(None)
        raise AssertionError("unexpected migration query")


class _LeaseLossClient:
    def execute_write[ResultT](
        self, work: Callable[[ManagedTransaction], ResultT], **_kwargs: object
    ) -> ResultT:
        return work(cast(ManagedTransaction, _LeaseLossTx()))


def test_projection_reports_lease_loss_instead_of_false_completion() -> None:
    with pytest.raises(RuntimeError, match="lost its lease"):
        project_legacy_generic_activities(
            cast(Neo4jClient, _LeaseLossClient()), history_source="bitrix_chat"
        )


def test_bitrix_activity_alias_repair_is_separate_reversible_and_never_rewrites_source_data() -> (
    None
):
    from src.graph.crm_history_projection_migration import (
        BITRIX_ACTIVITY_ALIAS_MIGRATION_KEY,
        PROJECT_BITRIX_ACTIVITY_ALIAS_BATCH,
        ROLLBACK_BITRIX_ACTIVITY_ALIAS_BATCH,
    )

    assert BITRIX_ACTIVITY_ALIAS_MIGRATION_KEY != "crm_history_activity_projection_v1"
    assert "history_family: 'crm_activity'" in PROJECT_BITRIX_ACTIVITY_ALIAS_BATCH
    assert "record.history_source = 'bitrix_crm_activity'" in PROJECT_BITRIX_ACTIVITY_ALIAS_BATCH
    assert (
        "record.projection_source = 'bitrix_crm_activity_v1'" in PROJECT_BITRIX_ACTIVITY_ALIAS_BATCH
    )
    assert "SET record.history_family = 'activity'" in PROJECT_BITRIX_ACTIVITY_ALIAS_BATCH
    assert "record_hash" not in PROJECT_BITRIX_ACTIVITY_ALIAS_BATCH
    assert "raw_payload" not in PROJECT_BITRIX_ACTIVITY_ALIAS_BATCH
    assert "source_record_id" not in PROJECT_BITRIX_ACTIVITY_ALIAS_BATCH
    assert (
        "crm_history_projection_migration: $migration_key" in ROLLBACK_BITRIX_ACTIVITY_ALIAS_BATCH
    )
    assert "SET record.history_family = 'crm_activity'" in ROLLBACK_BITRIX_ACTIVITY_ALIAS_BATCH
