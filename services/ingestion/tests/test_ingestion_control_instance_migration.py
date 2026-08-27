"""Structural tests for #272's restart-safe control migration."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from src.graph.queries.ingestion_control_instance_migration import (
    ACQUIRE_MIGRATION_LEASE,
    AFFECTED_LABELS,
    BLOCK_LEGACY_DISPATCH,
    COUNT_CONTROL_RELATIONSHIP_MISMATCHES,
    COUNT_INVALID_CONTROL_ROWS,
    COUNT_SOURCE_AMBIGUITIES,
    LEGACY_CONSTRAINT_SPECS,
    MARK_COMPLETE,
    NEW_CONSTRAINT_SPECS,
    PHASES,
    PROSPECTIVE_COLLISIONS,
    backfill_label_query,
)


def test_migration_has_all_durable_fail_closed_phases() -> None:
    assert PHASES == (
        "block_dispatch",
        "inventory",
        "backfill_ingest_runs",
        "backfill_logical_runs",
        "backfill_checkpoints",
        "backfill_bitrix_streams_and_fences",
        "backfill_dispatch_generations_publications",
        "validate_rows_and_future_identities",
        "drop_verified_legacy_constraints",
        "create_instance_constraints",
        "postvalidate",
        "complete",
    )
    assert "lease_owner" in ACQUIRE_MIGRATION_LEASE
    assert "lease_until" in ACQUIRE_MIGRATION_LEASE
    assert "migration.completed_at IS NULL" in ACQUIRE_MIGRATION_LEASE
    source = (
        Path(__file__).parents[1] / "src/graph/ingestion_control_instance_migration.py"
    ).read_text()
    assert "def _is_fresh_database" in source
    assert "def _complete_fresh_database" in source
    assert "_replace_verified_constraints(client, drop_legacy=False, create_new=True)" in source


def test_backfills_are_bounded_static_label_keysets() -> None:
    assert "IngestRun" in AFFECTED_LABELS
    assert "IngestionCheckpoint" in AFFECTED_LABELS
    assert "BitrixBackfillDispatchOutbox" in AFFECTED_LABELS
    assert "BitrixActivityOwnerRetry" in AFFECTED_LABELS
    assert {
        "StageHistoryUnit",
        "StageHistoryOccurrence",
        "StageHistoryRetry",
        "StageHistoryReviewCommand",
        "StageHistoryUnitAccounting",
    }.issubset(AFFECTED_LABELS)
    query = backfill_label_query("IngestRun")
    assert "MATCH (node:IngestRun)" in query
    assert "elementId(node) > $cursor" in query
    assert "LIMIT $batch_size" in query
    assert "migration.lease_until >= datetime()" in query
    assert "control_instance_id = 'legacy-default'" in query
    assert "OPTIONAL MATCH (node:IngestRun)" in query
    assert "FOREACH (node IN nodes" in query
    assert "RETURN nodes" in query


def test_constraint_specs_are_exact_and_do_not_overconstrain_registry() -> None:
    assert (
        "ingest_run_source_idempotency_unique",
        "IngestRun",
        ("source_key", "idempotency_key"),
    ) in LEGACY_CONSTRAINT_SPECS
    assert (
        "ingestion_checkpoint_key_unique",
        "IngestionCheckpoint",
        ("checkpoint_key",),
    ) in LEGACY_CONSTRAINT_SPECS
    assert (
        "ingest_run_source_control_idempotency_unique",
        "IngestRun",
        ("source_key", "control_instance_id", "idempotency_key"),
    ) in NEW_CONSTRAINT_SPECS
    assert all(
        "source_instance_id" not in properties for _name, _label, properties in NEW_CONSTRAINT_SPECS
    )


def test_validation_covers_rows_relationships_source_ambiguity_and_collisions() -> None:
    assert "HAS_ATTEMPT|ACTIVE_ATTEMPT" in COUNT_CONTROL_RELATIONSHIP_MISMATCHES
    assert (
        "HAS_STAGE_HISTORY_UNIT|HAS_STAGE_HISTORY_REVIEW_COMMAND"
        in COUNT_CONTROL_RELATIONSHIP_MISMATCHES
    )
    assert (
        "CONTAINS_STAGE_HISTORY_OCCURRENCE|HAS_STAGE_HISTORY_ACCOUNTING"
        in COUNT_CONTROL_RELATIONSHIP_MISMATCHES
    )
    assert "StageHistoryUnitAccounting" in COUNT_INVALID_CONTROL_ROWS
    assert "CHECKPOINT_FOR" in COUNT_CONTROL_RELATIONSHIP_MISMATCHES
    assert "FROM_SOURCE" in COUNT_SOURCE_AMBIGUITIES
    assert "HAS_ATTEMPT|ACTIVE_ATTEMPT" in COUNT_SOURCE_AMBIGUITIES
    assert (
        "checkpoint.control_instance_id <> logical.control_instance_id" in COUNT_SOURCE_AMBIGUITIES
    )
    assert "checkpoint.control_instance_id <> run.control_instance_id" in COUNT_SOURCE_AMBIGUITIES
    assert "checkpoint.source_key IS NULL AND size(source_keys) = 0" in COUNT_SOURCE_AMBIGUITIES
    assert (
        "checkpoint.logical_run_id IS NOT NULL AND checkpoint.checkpoint_key IS NOT NULL"
        in COUNT_SOURCE_AMBIGUITIES
    )
    assert "IngestionCheckpoint" in PROSPECTIVE_COLLISIONS
    assert "BitrixIngestionStream" in PROSPECTIVE_COLLISIONS
    assert "BitrixKnownOwnerRefreshMember" in PROSPECTIVE_COLLISIONS
    assert "BitrixBackfillDispatchOutbox" in PROSPECTIVE_COLLISIONS


def test_completion_only_unblocks_the_migration_owned_block() -> None:
    assert "migration_owned_block: true" in MARK_COMPLETE
    assert "control.blocked = false" in MARK_COMPLETE
    assert (
        "coalesce(control.migration_owned_block, false) OR NOT was_blocked"
        in __import__(
            "src.graph.queries.ingestion_control_instance_migration",
            fromlist=["BLOCK_LEGACY_DISPATCH"],
        ).BLOCK_LEGACY_DISPATCH
    )


def test_implementation_inspects_full_constraint_definition_and_rejects_aliases() -> None:
    source = (
        Path(__file__).parents[1] / "src/graph/ingestion_control_instance_schema.py"
    ).read_text()
    assert "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties" in source
    assert "definition.properties != properties" in source
    assert "unrecognized constraint" in source


class _Result:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self._row = row

    def __iter__(self) -> object:
        return iter(())

    def single(self) -> dict[str, object] | None:
        return self._row


class _FreshTransaction:
    def run(self, query: str, **_params: object) -> _Result:
        if query.startswith("SHOW CONSTRAINTS"):
            return _Result()
        assert "OPTIONAL MATCH (node)" in query
        assert "WITH markers, count(node) AS affected" in query
        return _Result({"fresh": True})


class _FreshClient:
    def execute_read(self, work: Callable[[_FreshTransaction], bool]) -> bool:
        return work(_FreshTransaction())


def test_zero_affected_rows_without_marker_selects_fresh_path() -> None:
    from src.graph.ingestion_control_instance_migration import _is_fresh_database

    assert _is_fresh_database(_FreshClient()) is True  # type: ignore[arg-type]


def test_dispatch_block_stamps_the_legacy_source_only_control_before_new_ddl() -> None:
    assert "OPTIONAL MATCH (existing:BitrixDispatchControl {source_key: 'bitrix_chat'})" in (
        BLOCK_LEGACY_DISPATCH
    )
    assert "CREATE (:BitrixDispatchControl" in BLOCK_LEGACY_DISPATCH
    assert "MATCH (control:BitrixDispatchControl {source_key: 'bitrix_chat'})" in (
        BLOCK_LEGACY_DISPATCH
    )
    assert "control.control_instance_id = 'legacy-default'" in BLOCK_LEGACY_DISPATCH
    assert (
        "MERGE (control:BitrixDispatchControl {source_key: 'bitrix_chat', control_instance_id"
        not in (BLOCK_LEGACY_DISPATCH)
    )


def test_required_parent_validation_rejects_orphaned_or_conflicting_control_children() -> None:
    for label, relation in (
        ("BitrixKnownOwnerRefreshSet", "HAS_KNOWN_OWNER_SET"),
        ("BitrixKnownOwnerRefreshMember", "HAS_MEMBER"),
        ("BitrixBackfillCoverage", "HAS_COVERAGE"),
        ("BitrixActivityOwnerRetry", "HAS_OWNER_RETRY"),
        ("BitrixBackfillDispatchOutbox", "HAS_SUCCESSOR"),
        ("BitrixIngestionStream", "logical_run_id"),
    ):
        assert label in COUNT_SOURCE_AMBIGUITIES
        assert relation in COUNT_SOURCE_AMBIGUITIES
    assert "size(parents) <> 1" in COUNT_SOURCE_AMBIGUITIES
    assert "size(successors) <> 1 OR size(correctives) <> 1" in COUNT_SOURCE_AMBIGUITIES


def test_block_preserves_operator_reason_and_rejects_conflicting_identity() -> None:
    assert "control.control_instance_id IS NULL" in BLOCK_LEGACY_DISPATCH
    assert "WHEN was_blocked THEN control.block_reason" in BLOCK_LEGACY_DISPATCH
    assert (
        "migration_owned_block: true, blocked: true, block_reason: 'control_instance_migration'"
        in MARK_COMPLETE
    )


def test_registry_constraint_is_required_by_control_readiness_postconditions() -> None:
    source = (
        Path(__file__).parents[1] / "src/graph/ingestion_control_instance_migration.py"
    ).read_text()
    assert "bitrix_source_instance_identity_unique" in source
    assert "BitrixSourceInstance" in source
    assert "source_instance_id" in source


def test_logical_checkpoint_validation_accepts_historical_producers_and_checks_all_owners() -> None:
    assert "size(attempts) = 0" in COUNT_SOURCE_AMBIGUITIES
    assert "any(attempt IN attempts WHERE" in COUNT_SOURCE_AMBIGUITIES
    assert "attempt.logical_run_id <> checkpoint.logical_run_id" in COUNT_SOURCE_AMBIGUITIES
    assert "attempt.source_key <> logicals[0].source_key" in COUNT_SOURCE_AMBIGUITIES
    checkpoint_section = COUNT_SOURCE_AMBIGUITIES.split(
        "MATCH (stream:BitrixIngestionStream)", maxsplit=1
    )[0]
    assert "size(attempts) <> 1" not in checkpoint_section


def test_control_instance_validation_uses_the_canonical_slug_boundaries() -> None:
    match = re.search(r"=~ '([^']+)'", COUNT_INVALID_CONTROL_ROWS)
    assert match is not None
    pattern = match.group(1)

    assert re.fullmatch(pattern, "portal-") is None
    assert re.fullmatch(pattern, "a" * 64) is not None


class _ReservedRegistrationTransaction:
    def run(self, query: str, **_params: object) -> _Result:
        assert "OPTIONAL MATCH (:BitrixSourceInstance" in query
        assert "count(relationship) AS relationship_count" in query
        assert "size(instances) = 1 AND relationship_count = 1" in query
        assert "size(targets) = 1" in query
        assert "targets[0].source_key = 'bitrix_chat'" in query
        assert "targets[0].is_active = true" in query
        assert "[(instances[0])-[:INSTANCE_OF]" not in query
        return _Result({"ready": True})


class _ReservedRegistrationClient:
    def execute_read(self, work: Callable[[_ReservedRegistrationTransaction], bool]) -> bool:
        return work(_ReservedRegistrationTransaction())


def test_reserved_legacy_registration_uses_valid_cardinality_query() -> None:
    from src.graph.ingestion_control_instance_migration import (
        _validate_reserved_legacy_registration,
    )

    _validate_reserved_legacy_registration(_ReservedRegistrationClient())  # type: ignore[arg-type]
