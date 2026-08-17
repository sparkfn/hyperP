"""Structural contracts for durable CRM stage-history Cypher primitives."""

from __future__ import annotations

from src.graph import queries
from src.graph.queries.source_records import LOCK_AND_GET_SOURCE_STATE
from src.graph.queries.stage_history_ingestion import (
    APPEND_STAGE_HISTORY_AUTHORITY_TRANSITION,
    APPEND_STAGE_HISTORY_INVALIDATION_INTENTS,
    APPEND_STAGE_HISTORY_PARENT_DECISION,
    CLAIM_STAGE_HISTORY_RETRY,
    CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW,
    CLAIM_STAGE_HISTORY_REVIEW_COMMAND,
    COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT,
    COMPLETE_STAGE_HISTORY_REVIEW_COMMAND,
    CREATE_STAGE_HISTORY_INGESTION_CONSTRAINTS,
    CREATE_STAGE_HISTORY_UNIT,
    GET_COMPLETED_STAGE_HISTORY_REVIEW_COMMAND,
    GET_STAGE_HISTORY_AUTHORITY_HEAD,
    GET_STAGE_HISTORY_RECONCILIATION,
    GET_STAGE_HISTORY_REVIEW_ASSOCIATION,
    GET_STAGE_HISTORY_REVIEW_COMMAND_CONTEXT,
    GET_STAGE_HISTORY_REVIEW_RESUME_CONTEXT,
    GET_STAGE_HISTORY_REVIEW_VARIANT_SET,
    GET_STAGE_HISTORY_STATUS,
    LOCK_STAGE_HISTORY_UNIT_FENCE,
    PERSIST_STAGE_HISTORY_REVIEW_COMMAND,
    PROJECT_STAGE_HISTORY_AUTHORITY_HEAD,
    PROJECT_STAGE_HISTORY_REVIEW_OUTCOME,
    RESOLVE_STAGE_HISTORY_PARENT_CANDIDATES,
    RESOLVE_STAGE_HISTORY_RETRY,
    RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW,
    STAGE_HISTORY_MUTATION_QUERIES,
    STAGE_HISTORY_REVIEW_MUTATION_QUERIES,
    UPSERT_STAGE_HISTORY_FAILED_OCCURRENCE,
    UPSERT_STAGE_HISTORY_OCCURRENCE,
    UPSERT_STAGE_HISTORY_RETRY,
    UPSERT_STAGE_HISTORY_UNIT_ACCOUNTING,
    UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD,
)

_PROHIBITED_RELATIONSHIPS = (
    "CHILD_OF",
    "OWNED_BY",
    "LINKED_TO",
    "DETAILS_HISTORY_ITEM",
    "REPRESENTS_HISTORY_ITEM",
)


def test_stage_history_schema_covers_immutable_and_claim_identities() -> None:
    schema = "\n".join(CREATE_STAGE_HISTORY_INGESTION_CONSTRAINTS)

    for constraint_name in (
        "stage_history_unit_identity_unique",
        "stage_history_occurrence_identity_unique",
        "stage_history_identity_lock_unique",
        "stage_history_parent_decision_id_unique",
        "stage_history_retry_identity_unique",
        "stage_history_review_command_id_unique",
        "stage_history_invalidation_intent_id_unique",
        "stage_history_unit_accounting_identity_unique",
    ):
        assert constraint_name in schema
    for index_name in (
        "stage_history_unit_run_sequence",
        "stage_history_unit_status",
        "stage_history_occurrence_run_disposition",
        "stage_history_occurrence_event_identity",
        "stage_history_parent_decision_event_state",
        "stage_history_retry_claim_scan",
        "stage_history_review_command_claim_scan",
        "stage_history_invalidation_claim_scan",
        "stage_history_source_record_family",
    ):
        assert index_name in schema
    assert "REQUIRE (retry.occurrence_id, retry.retry_sequence) IS UNIQUE" in schema
    assert "REQUIRE intent.intent_id IS UNIQUE" in schema
    assert "REQUIRE unit.status IS UNIQUE" not in schema
    assert "REQUIRE retry.status IS UNIQUE" not in schema


def test_every_stage_mutation_revalidates_the_complete_stage_fence() -> None:
    mutations = STAGE_HISTORY_MUTATION_QUERIES + STAGE_HISTORY_REVIEW_MUTATION_QUERIES
    assert mutations
    for query in mutations:
        assert "stream_key: 'crm_stage_history'" in query
        assert "logical_run_id: $logical_run_id" in query
        assert "ingest_run_id: $ingest_run_id" in query
        assert "attempt_generation: $attempt_generation" in query
        assert "stream_generation: $stream_generation" in query
        assert "fencing_token: $fencing_token" in query
        assert "status: 'active'" in query
        assert "[:ACTIVE_ATTEMPT]" in query
        assert "logical.active_generation = $attempt_generation" in query
        assert "attempt.generation = $attempt_generation" in query
        assert "logical.mode = $required_run_type" in query
    assert "SET stream.fence_lock_version" in LOCK_STAGE_HISTORY_UNIT_FENCE


def test_unit_creation_and_commit_use_exact_checkpoint_cas() -> None:
    for expected in (
        "checkpoint.cursor_json = $expected_cursor_json",
        "coalesce(checkpoint.revision, 0) = $expected_checkpoint_revision",
        "checkpoint.source_window_json = $source_window_json",
        "checkpoint.connector_version = $connector_version",
        "checkpoint.schema_version = $checkpoint_schema_version",
        "coalesce(checkpoint.committed_count, 0) = $expected_committed_count",
        "coalesce(checkpoint.duplicate_count, 0) = $expected_duplicate_count",
        "coalesce(checkpoint.excluded_count, 0) = $expected_excluded_count",
        "coalesce(checkpoint.retry_count, 0) = $expected_retry_count",
    ):
        assert expected in CREATE_STAGE_HISTORY_UNIT
        assert expected in COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT

    assert "unit.unit_digest = $unit_digest" in CREATE_STAGE_HISTORY_UNIT
    assert "unit.status = 'committed'" in COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT
    assert "checkpoint.revision = $expected_checkpoint_revision + 1" in (
        COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT
    )
    assert COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT.index(
        "occurrence_count = unit.fetched_count"
    ) < COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT.index(
        "checkpoint.cursor_json = $next_cursor_json"
    )


def test_occurrences_have_immutable_terminal_dispositions_without_identity_poisoning() -> None:
    for disposition in (
        "excluded_out_of_scope",
        "canonical_effective",
        "canonical_pending_parent",
        "parent_waiting",
        "parent_ambiguous",
        "same_hash_replay",
        "differing_hash_conflict",
    ):
        assert f"'{disposition}'" in UPSERT_STAGE_HISTORY_OCCURRENCE
    assert "ON CREATE SET occurrence.logical_run_id" in UPSERT_STAGE_HISTORY_OCCURRENCE
    assert "SET occurrence.terminal_disposition" not in UPSERT_STAGE_HISTORY_OCCURRENCE

    assert "'malformed_excluded'" in UPSERT_STAGE_HISTORY_FAILED_OCCURRENCE
    assert "'capture_rejected_valid'" in UPSERT_STAGE_HISTORY_FAILED_OCCURRENCE
    assert "occurrence.event_identity IS NULL" in UPSERT_STAGE_HISTORY_FAILED_OCCURRENCE
    assert "occurrence.canonical_hash IS NULL" in UPSERT_STAGE_HISTORY_FAILED_OCCURRENCE
    assert "raw_payload" not in UPSERT_STAGE_HISTORY_FAILED_OCCURRENCE


def test_stage_variants_use_numeric_versions_and_authority_only_lifecycle() -> None:
    query = UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD
    existing_variant_branch = query.split("UNION", maxsplit=1)[0]

    assert "StageHistoryIdentityLock" in query
    assert "max(toInteger(prior.source_record_version))" in query
    assert "source_record_version: toString(next_version)" in query
    assert "source_version_key: $source_version_key" in query
    assert "record_type: 'crm_history'" in query
    assert "history_family: 'stage'" in query
    assert "lifecycle_status: 'pending_review'" in query
    assert "is_latest: false" in query
    assert "link_status: 'stage_authority_only'" in query
    assert "CrmHistoryHashVariant" in query
    assert "[:EVIDENCED_BY]" in query
    assert "[:OBSERVED_STAGE_HISTORY_VARIANT]" in query
    assert "prior_different_variant_count" in query
    assert "created" in query
    assert "event_category_id: $event_category_id" in query
    assert "event_stage_id: $event_stage_id" in query
    assert "event_stage_semantic_id: $event_stage_semantic_id" in query
    assert "known_record.observed_at = datetime($source_observed_at)" not in existing_variant_branch
    assert (
        "valueType(known_record.observed_at) STARTS WITH 'ZONED DATETIME'"
        in existing_variant_branch
    )
    assert "observed_at: datetime($source_observed_at)" in query
    assert "category_id: $category_id" not in query


def test_parent_resolution_fails_closed_and_retry_claims_are_fenced() -> None:
    assert "size(active) = 1" in RESOLVE_STAGE_HISTORY_PARENT_CANDIDATES
    assert "size(active) > 1 THEN 'ambiguous'" in RESOLVE_STAGE_HISTORY_PARENT_CANDIDATES
    assert "size(pending) = 1" in RESOLVE_STAGE_HISTORY_PARENT_CANDIDATES
    assert "size(pending) > 1 THEN 'ambiguous'" in RESOLVE_STAGE_HISTORY_PARENT_CANDIDATES
    assert "ORDER BY" not in RESOLVE_STAGE_HISTORY_PARENT_CANDIDATES
    assert "LIMIT 1" not in RESOLVE_STAGE_HISTORY_PARENT_CANDIDATES
    assert "MERGE (parent_identity_lock:SourceRecordIdentityLock" in (
        RESOLVE_STAGE_HISTORY_PARENT_CANDIDATES
    )
    assert "source_system: $logical_parent_source_system" in (
        RESOLVE_STAGE_HISTORY_PARENT_CANDIDATES
    )
    assert "MERGE (lock:SourceRecordIdentityLock" in LOCK_AND_GET_SOURCE_STATE
    assert "source_system: $source_system" in LOCK_AND_GET_SOURCE_STATE
    assert "source_record_id: $source_record_id" in LOCK_AND_GET_SOURCE_STATE

    assert "CrmHistoryParentAssociationDecision" in APPEND_STAGE_HISTORY_PARENT_DECISION
    assert "SELECTS_STAGE_HISTORY_PARENT" in APPEND_STAGE_HISTORY_PARENT_DECISION
    assert "candidate.lifecycle_status = 'active'" in APPEND_STAGE_HISTORY_PARENT_DECISION
    assert "candidate.lifecycle_status = 'pending_review'" in (APPEND_STAGE_HISTORY_PARENT_DECISION)
    assert "$association_state = recounted_state" in APPEND_STAGE_HISTORY_PARENT_DECISION
    assert (
        "$association_state IN ['selected_active', 'selected_pending_review']"
        in APPEND_STAGE_HISTORY_PARENT_DECISION
    )
    assert "AND recounted_parent IS NOT NULL" in APPEND_STAGE_HISTORY_PARENT_DECISION
    assert "occurrence.current_association_decision_id = decision.decision_id" in (
        APPEND_STAGE_HISTORY_PARENT_DECISION
    )

    assert "retry.status = 'pending'" in UPSERT_STAGE_HISTORY_RETRY
    assert "retry.lease_expires_at < datetime()" in CLAIM_STAGE_HISTORY_RETRY
    assert "retry.lease_fencing_token = $fencing_token" in CLAIM_STAGE_HISTORY_RETRY
    assert "coalesce(retry.attempt_count, 0) < retry.max_attempts" in (CLAIM_STAGE_HISTORY_RETRY)
    assert "retry.next_attempt_at <= datetime()" in CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW
    assert "retry.attempt_count = coalesce(retry.attempt_count, 0) + 1" in (
        CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW
    )
    assert "lease_fencing_token: $fencing_token" in RESOLVE_STAGE_HISTORY_RETRY
    for lease_field in (
        "lease_attempt_id",
        "lease_attempt_generation",
        "lease_stream_generation",
        "lease_fencing_token",
        "claimed_at",
    ):
        assert f"retry.{lease_field} = NULL" in RESOLVE_STAGE_HISTORY_RETRY
    assert "$resolution IN ['resolved', 'rejected', 'quarantined']" in (RESOLVE_STAGE_HISTORY_RETRY)
    assert "$resolution IN ['pending', 'resolved', 'rejected', 'quarantined']" in (
        RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW
    )
    assert "datetime($next_attempt_at)" in RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW
    assert "occurrence.retry_state = retry.status" in CLAIM_STAGE_HISTORY_RETRY
    assert "occurrence.retry_state = retry.status" in RESOLVE_STAGE_HISTORY_RETRY


def test_current_projections_are_fenced_without_rewriting_source_unit_history() -> None:
    for query in (
        PROJECT_STAGE_HISTORY_AUTHORITY_HEAD,
        PROJECT_STAGE_HISTORY_REVIEW_OUTCOME,
        APPEND_STAGE_HISTORY_PARENT_DECISION,
        CLAIM_STAGE_HISTORY_RETRY,
        RESOLVE_STAGE_HISTORY_RETRY,
        CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW,
        RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW,
    ):
        assert "terminal_disposition =" not in query

    assert "occurrence.current_authority_decision_id = head.decision_id" in (
        PROJECT_STAGE_HISTORY_AUTHORITY_HEAD
    )
    assert "MATCH (command:StageHistoryReviewCommand" in PROJECT_STAGE_HISTORY_REVIEW_OUTCOME
    assert "command.lease_owner = $lease_owner" in PROJECT_STAGE_HISTORY_REVIEW_OUTCOME
    assert "event_occurrence.authority_state = head.authority_state" in (
        PROJECT_STAGE_HISTORY_REVIEW_OUTCOME
    )
    assert "target.association_state = $association_state" in (PROJECT_STAGE_HISTORY_REVIEW_OUTCOME)


def test_authority_head_query_uses_neo4j_not_in_predicate_syntax() -> None:
    assert "WHEN NOT head.authority_state IN" in GET_STAGE_HISTORY_AUTHORITY_HEAD
    assert "head.authority_state NOT IN" not in GET_STAGE_HISTORY_AUTHORITY_HEAD


def test_accounting_query_uses_neo4j_not_in_predicate_syntax() -> None:
    assert "AND NOT occurrence.terminal_disposition IN" in (UPSERT_STAGE_HISTORY_UNIT_ACCOUNTING)
    assert "occurrence.terminal_disposition NOT IN" not in (UPSERT_STAGE_HISTORY_UNIT_ACCOUNTING)


def test_checkpoint_commit_filters_subquery_aggregates_through_with() -> None:
    query = COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT
    aggregate_return = query.index("RETURN count(occurrence) AS occurrence_count")
    aggregate_filter = query.index("WHERE occurrence_count = unit.fetched_count")
    with_clause = query.index(
        "WITH checkpoint, logical, unit, accounting, occurrence_count,",
        aggregate_return,
    )

    assert aggregate_return < with_clause < aggregate_filter


def test_authority_replay_is_attempt_independent_and_head_cas_is_exact() -> None:
    query = APPEND_STAGE_HISTORY_AUTHORITY_TRANSITION

    assert "OPTIONAL MATCH (existing:CrmHistoryAuthorityDecision" in query
    assert "WHERE existing IS NOT NULL" in query
    assert "existing.originating_ingest_run_id = $ingest_run_id" not in query
    assert "resolved_head.head_version = $expected_head_version" in query
    assert "current_authority_token = $expected_authority_token" in query
    assert "$next_authority_token = $expected_authority_token + 1" in query
    assert "replayed" in query
    assert "'withheld_conflict'" in query
    assert "head.selected_variant_hash = CASE" in query
    assert "WHEN decision.authority_state IN ['effective', 'corrected']" in query
    assert "datetime($available_at) >= correction_target.available_at" in query


def test_invalidation_and_accounting_queries_encode_required_equations() -> None:
    assert "target_kind = 'crm_stage_timeline'" in APPEND_STAGE_HISTORY_INVALIDATION_INTENTS
    assert "UNWIND $intents AS item" in APPEND_STAGE_HISTORY_INVALIDATION_INTENTS
    assert "authority_decision_id = $authority_decision_id" in (
        APPEND_STAGE_HISTORY_INVALIDATION_INTENTS
    )
    assert "payload_json = item.payload_json" in APPEND_STAGE_HISTORY_INVALIDATION_INTENTS

    accounting = UPSERT_STAGE_HISTORY_UNIT_ACCOUNTING
    for dimension in (
        "malformed_excluded_count",
        "capture_rejected_valid_count",
        "excluded_out_of_scope_count",
        "canonical_effective_count",
        "canonical_pending_parent_count",
        "parent_waiting_count",
        "parent_ambiguous_count",
        "same_hash_replay_count",
        "differing_hash_conflict_count",
        "new_variant_count",
        "existing_same_hash_count",
        "new_conflict_variant_count",
        "selected_active_count",
        "selected_pending_review_count",
        "waiting_count",
        "ambiguous_count",
        "association_rejected_count",
        "effective_count",
        "withheld_parent_count",
        "withheld_conflict_count",
        "authority_rejected_count",
        "corrected_count",
        "retry_none_count",
        "retry_pending_count",
        "retry_claimed_count",
        "retry_resolved_count",
        "retry_rejected_count",
        "retry_quarantined_count",
    ):
        assert dimension in accounting
    assert "actual_fetched_count = $fetched_count" in accounting
    assert "actual_malformed_excluded_count = $malformed_excluded_count" in accounting
    assert "$new_variant_count + $existing_same_hash_count +" in accounting
    assert "$fetched_count - $excluded_out_of_scope_count" in accounting
    assert "units_balanced" in GET_STAGE_HISTORY_RECONCILIATION
    assert "variant_source_records_balanced" in GET_STAGE_HISTORY_RECONCILIATION
    assert "invalid_effective_head_count" in GET_STAGE_HISTORY_RECONCILIATION
    assert "association IS NULL" in GET_STAGE_HISTORY_RECONCILIATION
    assert "invalid_parent_association_count" in GET_STAGE_HISTORY_RECONCILIATION
    assert "parent_associations_balanced" in GET_STAGE_HISTORY_RECONCILIATION
    assert "ELSE size(selected_parents) <> 0" in GET_STAGE_HISTORY_RECONCILIATION
    assert "decision.selected_parent_source_record_pk IS NOT NULL" in (
        GET_STAGE_HISTORY_RECONCILIATION
    )
    assert "invalid_variant_evidence_count" in GET_STAGE_HISTORY_RECONCILIATION
    assert "checkpoint_last_unit_balanced" in GET_STAGE_HISTORY_RECONCILIATION
    assert "checkpoint_cursor_page_balanced" in GET_STAGE_HISTORY_RECONCILIATION

    unit_balance = GET_STAGE_HISTORY_RECONCILIATION.split(
        "balanced: accounting IS NOT NULL", maxsplit=1
    )[1].split("} END)", maxsplit=1)[0]
    assert "terminal_disposition" not in unit_balance
    assert "selected_active_count = accounting.selected_active_count" not in unit_balance
    assert "effective_count = accounting.effective_count" not in unit_balance
    assert "retry_resolved_count = accounting.retry_resolved_count" not in unit_balance
    assert "invalid_current_authority_projection_count" in GET_STAGE_HISTORY_RECONCILIATION
    assert "current_authority_partition_balanced" in GET_STAGE_HISTORY_RECONCILIATION
    assert "occurrence_variant_identity_count = variant_count" in (GET_STAGE_HISTORY_RECONCILIATION)
    assert "invalid_occurrence_variant_link_count = 0" in (GET_STAGE_HISTORY_RECONCILIATION)
    assert "nonterminal_unit_count" in GET_STAGE_HISTORY_RECONCILIATION
    for partition in (
        "total_new_variant_count",
        "total_selected_active_count",
        "total_effective_count",
        "total_retry_pending_count",
    ):
        assert partition in GET_STAGE_HISTORY_RECONCILIATION


def test_stage_queries_never_create_prohibited_relationships() -> None:
    combined = "\n".join(STAGE_HISTORY_MUTATION_QUERIES)
    for relationship in _PROHIBITED_RELATIONSHIPS:
        assert f"[:{relationship}]" not in combined
        assert f"[:{relationship} " not in combined


def test_review_commands_are_durable_fenced_and_lease_owned() -> None:
    assert "request_payload_digest = $request_payload_digest" in (
        PERSIST_STAGE_HISTORY_REVIEW_COMMAND
    )
    assert "authorization_reference = $authorization_reference" in (
        PERSIST_STAGE_HISTORY_REVIEW_COMMAND
    )
    assert "selected_variant_hash = $selected_variant_hash" in (
        PERSIST_STAGE_HISTORY_REVIEW_COMMAND
    )
    for expectation in (
        "expected_head_version = $expected_head_version",
        "expected_authority_token = $expected_authority_token",
        "expected_variant_set_digest = $expected_variant_set_digest",
    ):
        assert expectation in PERSIST_STAGE_HISTORY_REVIEW_COMMAND
        assert expectation in CLAIM_STAGE_HISTORY_REVIEW_COMMAND
    assert "retry_sequence = $retry_sequence" in PERSIST_STAGE_HISTORY_REVIEW_COMMAND
    assert "coalesce(command.retry_sequence, 0) = coalesce($retry_sequence, 0)" in (
        CLAIM_STAGE_HISTORY_REVIEW_COMMAND
    )
    assert "command.lease_fencing_token = $fencing_token" in (CLAIM_STAGE_HISTORY_REVIEW_COMMAND)
    assert "command.lease_fencing_token = $fencing_token" in (COMPLETE_STAGE_HISTORY_REVIEW_COMMAND)
    assert "command.lease_fencing_token = NULL" in (COMPLETE_STAGE_HISTORY_REVIEW_COMMAND)
    assert "result_authority_decision_id" in COMPLETE_STAGE_HISTORY_REVIEW_COMMAND


def test_status_reads_the_terminal_attempt_after_active_ownership_is_released() -> None:
    assert "(logical)-[:HAS_ATTEMPT]->(attempt:IngestRun)" in GET_STAGE_HISTORY_STATUS
    assert "attempt.generation = logical.active_generation" in GET_STAGE_HISTORY_STATUS
    assert "(logical)-[:ACTIVE_ATTEMPT]->(attempt:IngestRun)" not in (GET_STAGE_HISTORY_STATUS)


def test_operator_reads_fail_closed_to_stage_history_run_types() -> None:
    for query in (GET_STAGE_HISTORY_STATUS, GET_STAGE_HISTORY_RECONCILIATION):
        assert "logical.source_key = 'bitrix_chat'" in query
    for run_type in (
        "bounded_smoke_replay",
        "authoritative_backfill_replay",
        "authoritative_catch_up_replay",
        "capture_failure_accounting",
    ):
        assert f"'{run_type}'" in GET_STAGE_HISTORY_RECONCILIATION
    assert "logical.mode AS run_type" in GET_STAGE_HISTORY_RECONCILIATION
    assert "'parent_reconcile', 'conflict_review', 'correction_review'" in (
        GET_STAGE_HISTORY_STATUS
    )
    assert "status: 'completed'" in GET_COMPLETED_STAGE_HISTORY_REVIEW_COMMAND
    assert "ORDER BY variant.canonical_hash" in GET_STAGE_HISTORY_REVIEW_VARIANT_SET
    assert "size(selected_parents) = 1" in GET_STAGE_HISTORY_REVIEW_ASSOCIATION
    assert "selected_parent.lifecycle_status = 'active'" in (GET_STAGE_HISTORY_REVIEW_ASSOCIATION)
    assert "retry_sequence: $retry_sequence" in RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW
    assert "retry_sequence: $retry_sequence" in CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW
    assert "retry.review_command_id = $review_command_id" in (RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW)
    assert "selected_association_current" in GET_STAGE_HISTORY_AUTHORITY_HEAD
    assert "selected_parent.lifecycle_status = 'active'" in GET_STAGE_HISTORY_AUTHORITY_HEAD
    assert "[:FROM_SOURCE]" in GET_STAGE_HISTORY_AUTHORITY_HEAD
    assert "command.request_payload_digest AS request_payload_digest" in (
        GET_STAGE_HISTORY_REVIEW_RESUME_CONTEXT
    )
    assert "toString(command.available_at) AS available_at" in (
        GET_STAGE_HISTORY_REVIEW_RESUME_CONTEXT
    )
    assert "logical.status AS logical_status" in GET_STAGE_HISTORY_REVIEW_RESUME_CONTEXT
    for query in (
        GET_STAGE_HISTORY_REVIEW_COMMAND_CONTEXT,
        GET_STAGE_HISTORY_REVIEW_RESUME_CONTEXT,
    ):
        assert "logical.source_key = 'bitrix_chat'" in query
        assert "'parent_reconcile', 'conflict_review', 'correction_review'" in query
        assert "logical.configuration_fingerprint AS configuration_fingerprint" in query
        assert "command.request_payload_digest AS request_payload_digest" in query


def test_stage_query_primitives_are_reexported_on_compatibility_surface() -> None:
    assert queries.LOCK_STAGE_HISTORY_UNIT_FENCE == LOCK_STAGE_HISTORY_UNIT_FENCE
    assert queries.CREATE_STAGE_HISTORY_UNIT == CREATE_STAGE_HISTORY_UNIT
    assert queries.UPSERT_STAGE_HISTORY_OCCURRENCE == UPSERT_STAGE_HISTORY_OCCURRENCE
    assert queries.UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD == (
        UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD
    )
    assert queries.COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT == (
        COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT
    )
