"""Contract tests for projection relationship lifecycle migration."""

from __future__ import annotations

from typing import cast

import pytest
from src.graph.client import Neo4jClient
from src.graph.migrations import (
    apply_data_migrations,
    migrate_projection_relationship_lifecycle,
)
from test_migrations_record_type import _Client


def _migrate_relationships(
    relationships: list[dict[str, object]], records: list[dict[str, object]]
) -> list[dict[str, object]]:
    """State model of the marker-gated relationship backfill."""
    effective = {
        row.get("source_record_pk")
        for row in records
        if row.get("lifecycle_status") == "active"
        or (row.get("lifecycle_status") is None and row.get("is_latest") is True)
    }
    for relationship in relationships:
        if relationship.get("is_active") is not None:
            continue
        source_record_pk = relationship.get("source_record_pk")
        is_active = source_record_pk is None or source_record_pk in effective
        relationship["is_active"] = is_active
        timestamp_key = "activated_at" if is_active else "retired_at"
        relationship.setdefault(timestamp_key, "migration-time")
    return relationships


def _reconcile_relationships(
    relationships: list[dict[str, object]], records: list[dict[str, object]]
) -> list[dict[str, object]]:
    """State model of repeatable projection convergence."""
    effective = {
        row.get("source_record_pk")
        for row in records
        if row.get("lifecycle_status") == "active"
        or (row.get("lifecycle_status") is None and row.get("is_latest") is True)
    }
    for relationship in relationships:
        source_record_pk = relationship.get("source_record_pk")
        existing = relationship.get("is_active")
        expected = (
            (existing if existing is not None else True)
            if source_record_pk is None
            else (source_record_pk in effective)
        )
        if existing == expected:
            continue
        relationship["is_active"] = expected
        if expected:
            relationship.setdefault("activated_at", "reconciliation-time")
            relationship["retired_at"] = None
        else:
            relationship.setdefault("retired_at", "reconciliation-time")
    return relationships


def test_query_backfills_supported_relationships_from_effective_source_record() -> None:
    from src.graph import migrations

    query = migrations.MIGRATE_PROJECTION_RELATIONSHIP_LIFECYCLE
    assert "IDENTIFIED_BY|LIVES_AT|KNOWS|HAS_FACT" in query
    assert "relationship.is_active IS NULL" in query
    assert "source.source_record_pk = relationship.source_record_pk" in query
    assert "source.lifecycle_status = 'active'" in query
    assert "source.lifecycle_status IS NULL AND source.is_latest = true" in query
    assert "relationship.source_record_pk IS NULL" in query
    assert "relationship.is_active = is_active" in query
    assert "coalesce(relationship.activated_at" in query
    assert "coalesce(relationship.retired_at" in query


def test_state_model_covers_active_retired_unprovenanced_and_existing_states() -> None:
    relationships = [
        {"source_record_pk": "active"},
        {"source_record_pk": "legacy"},
        {"source_record_pk": "retired"},
        {},
        {"source_record_pk": "retired", "is_active": True, "activated_at": "original"},
    ]
    records = [
        {"source_record_pk": "active", "lifecycle_status": "active"},
        {"source_record_pk": "legacy", "lifecycle_status": None, "is_latest": True},
        {"source_record_pk": "retired", "lifecycle_status": "superseded"},
    ]

    migrated = _migrate_relationships(relationships, records)

    assert migrated[0] == {
        "source_record_pk": "active",
        "is_active": True,
        "activated_at": "migration-time",
    }
    assert migrated[1]["is_active"] is True
    assert migrated[2]["is_active"] is False
    assert migrated[2]["retired_at"] == "migration-time"
    assert migrated[3]["is_active"] is True
    assert migrated[4]["activated_at"] == "original"
    assert _migrate_relationships(migrated, records) == migrated


def test_projection_migration_has_distinct_serializing_completion_marker() -> None:
    from src.graph import migrations

    query = migrations.MIGRATE_PROJECTION_RELATIONSHIP_LIFECYCLE
    assert "projection_relationship_lifecycle_v1" in query
    assert "source_record_lifecycle_v1" not in query
    assert "migration.lock_version = coalesce(migration.lock_version, 0) + 1" in query
    assert "WHERE migration.completed_at IS NULL" in query
    assert query.index("SET migration.completed_at = datetime()") > query.index(
        "relationship.is_active = is_active"
    )


def test_projection_migration_noops_after_marker_completion() -> None:
    class _NoopResult:
        def single(self) -> None:
            return None

    class _NoopTx:
        def run(self, _query: str) -> _NoopResult:
            return _NoopResult()

    class _NoopClient:
        def execute_write(self, work: object) -> object:
            return cast("object", work)(_NoopTx())  # type: ignore[operator]

    assert migrate_projection_relationship_lifecycle(cast(Neo4jClient, _NoopClient())) == 0


def test_failed_projection_migration_can_be_retried() -> None:
    class _FailingTx:
        def run(self, _query: str) -> None:
            raise RuntimeError("simulated projection migration failure")

    class _RetryClient:
        def __init__(self) -> None:
            self.attempts = 0

        def execute_write(self, work: object) -> object:
            self.attempts += 1
            tx = _FailingTx() if self.attempts == 1 else _Client(updated=3).tx
            return cast("object", work)(tx)  # type: ignore[operator]

    client = _RetryClient()
    with pytest.raises(RuntimeError, match="simulated projection migration failure"):
        migrate_projection_relationship_lifecycle(cast(Neo4jClient, client))
    assert migrate_projection_relationship_lifecycle(cast(Neo4jClient, client)) == 3
    assert client.attempts == 2


def test_projection_migration_runs_after_source_record_lifecycle() -> None:
    client = _Client(updated=2)

    apply_data_migrations(cast(Neo4jClient, client))

    assert len(client.tx.queries) == 5
    assert "source_record_lifecycle_v1" in client.tx.queries[1]
    assert "projection_relationship_lifecycle_v1" in client.tx.queries[2]


def test_completed_marker_is_followed_by_late_relationship_reconciliation() -> None:
    from src.graph import migrations

    query = migrations.RECONCILE_PROJECTION_RELATIONSHIP_LIFECYCLE
    assert "WHERE migration.completed_at IS NULL" not in query
    assert "migration.lock_version = coalesce(migration.lock_version, 0) + 1" in query
    assert "relationship.is_active IS NULL" in query
    assert "relationship.is_active <> expected_is_active" in query
    assert "relationship.is_active = expected_is_active" in query
    assert "coalesce(relationship.activated_at" in query
    assert "coalesce(relationship.retired_at" in query

    records = [{"source_record_pk": "active", "lifecycle_status": "active"}]
    complete = {"source_record_pk": "active", "is_active": True, "activated_at": "original"}
    relationships = _migrate_relationships([complete.copy()], records)
    relationships.append({"source_record_pk": "active"})

    reconciled = _migrate_relationships(relationships, records)

    assert reconciled[0] == complete
    assert reconciled[1]["is_active"] is True


def test_reconciliation_converges_when_source_changes_between_beats() -> None:
    relationship = {
        "source_record_pk": "changing",
        "is_active": True,
        "activated_at": "original-activation",
    }
    first_records = [{"source_record_pk": "changing", "lifecycle_status": "active"}]

    assert _reconcile_relationships([relationship], first_records)[0] == relationship

    second_records = [{"source_record_pk": "changing", "lifecycle_status": "superseded"}]
    reconciled = _reconcile_relationships([relationship], second_records)[0]

    assert reconciled == {
        "source_record_pk": "changing",
        "is_active": False,
        "activated_at": "original-activation",
        "retired_at": "reconciliation-time",
    }


def test_reconciliation_preserves_unprovenanced_explicit_state_and_timestamps() -> None:
    relationships = [
        {},
        {"is_active": False, "retired_at": "original-retirement"},
        {"source_record_pk": "active", "is_active": False, "retired_at": "old"},
    ]
    records = [{"source_record_pk": "active", "lifecycle_status": "active"}]

    reconciled = _reconcile_relationships(relationships, records)

    assert reconciled[0] == {
        "is_active": True,
        "activated_at": "reconciliation-time",
        "retired_at": None,
    }
    assert reconciled[1] == {"is_active": False, "retired_at": "original-retirement"}
    assert reconciled[2] == {
        "source_record_pk": "active",
        "is_active": True,
        "retired_at": None,
        "activated_at": "reconciliation-time",
    }
