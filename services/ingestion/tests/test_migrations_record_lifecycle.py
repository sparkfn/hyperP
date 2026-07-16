"""Contract tests for the legacy SourceRecord lifecycle migration."""

from __future__ import annotations

from typing import cast

import pytest
from src.graph.client import Neo4jClient
from src.graph.migrations import apply_data_migrations, migrate_source_record_lifecycle
from src.source_version_keys import encode_source_version_key
from test_migrations_record_type import _Client


def _migrate(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Small state model mirroring the Cypher contract for outcome coverage."""

    def stable_pk(row: dict[str, object]) -> str:
        raw = row.get("source_record_pk")
        if raw:
            return str(raw)
        repair_id = row.get("legacy_repair_id")
        if repair_id is None:
            repair_id = f"repair-{row['element_id']}"
            row["legacy_repair_id"] = repair_id
        return str(repair_id)

    def source_for(row: dict[str, object]) -> str:
        if "source_systems" in row:
            sources = {str(value) for value in cast(list[object], row["source_systems"]) if value}
            if len(sources) == 1:
                return sources.pop()
            return f"legacy-orphan:{stable_pk(row)}"
        return str(row.get("source_system") or f"legacy-orphan:{stable_pk(row)}")

    ordered = sorted(
        rows,
        key=lambda row: (
            source_for(row),
            str(row.get("source_record_id") or f"legacy-pk:{stable_pk(row)}"),
            int(row["source_record_version"] or -1),
            str(row.get("ingested_at") or ""),
            stable_pk(row),
        ),
    )
    groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in ordered:
        source = source_for(row)
        record_id = str(row.get("source_record_id") or f"legacy-pk:{stable_pk(row)}")
        groups.setdefault((source, record_id), []).append(row)

    for (source, record_id), versions in groups.items():
        accepted = [
            row
            for row in versions
            if row.get("lifecycle_status") not in {"pending_review", "rejected", "link_failed"}
            and not (
                row.get("lifecycle_status") is None and row.get("link_status") == "pending_review"
            )
        ]
        anchors = [
            row
            for row in accepted
            if row.get("lifecycle_status") == "active" or row.get("is_latest") is True
        ]
        active = (anchors or accepted)[-1] if (anchors or accepted) else None
        duplicate_counts: dict[str, int] = {}
        for row in versions:
            status = row.get("lifecycle_status")
            if status not in {"pending_review", "rejected", "link_failed"}:
                if status is None and row.get("link_status") == "pending_review":
                    row["lifecycle_status"] = "pending_review"
                else:
                    row["lifecycle_status"] = "active" if row is active else "superseded"
            row["is_latest"] = row is active
            version = str(row.get("source_record_version") or f"legacy-pk:{stable_pk(row)}")
            index = duplicate_counts.get(version, 0)
            duplicate_counts[version] = index + 1
            discriminator = None if index == 0 else stable_pk(row)
            row["source_version_key"] = encode_source_version_key(
                source,
                record_id,
                version,
                duplicate_discriminator=discriminator,
            )
    return ordered


def test_lifecycle_migration_groups_and_orders_exact_source_identities() -> None:
    from src.graph import migrations

    query = migrations.MIGRATE_SOURCE_RECORD_LIFECYCLE
    assert "collect(DISTINCT ss.source_key)" in query
    assert "coalesce(version.source_record_id" in query
    assert "toInteger(version.source_record_version)" in query
    assert "version.ingested_at" in query
    assert "collect(version) AS versions" in query


def test_lifecycle_migration_preserves_review_and_terminal_states() -> None:
    from src.graph import migrations

    query = migrations.MIGRATE_SOURCE_RECORD_LIFECYCLE
    assert "'pending_review'" in query
    assert "'rejected'" in query
    assert "'link_failed'" in query
    assert "version.link_status = 'pending_review'" in query
    assert "version.lifecycle_status IN ['pending_review', 'rejected', 'link_failed']" in query


def test_legacy_null_status_predicate_is_explicitly_two_valued_in_cypher() -> None:
    from src.graph import migrations

    query = migrations.MIGRATE_SOURCE_RECORD_LIFECYCLE
    compact_query = " ".join(query.split())
    assert "coalesce(" in query
    assert (
        "coalesce( version.lifecycle_status IN "
        "['pending_review', 'rejected', 'link_failed'], false )"
    ) in compact_query
    assert (
        "coalesce( version.lifecycle_status IS NULL AND "
        "version.link_status = 'pending_review', false )"
    ) in compact_query
    assert "NOT (\n        version.lifecycle_status IN" not in query


def test_lifecycle_migration_marks_one_accepted_version_active() -> None:
    from src.graph import migrations

    query = migrations.MIGRATE_SOURCE_RECORD_LIFECYCLE
    assert "last(accepted_versions)" in query
    assert "THEN 'active'" in query
    assert "ELSE 'superseded'" in query


def test_lifecycle_migration_backfills_stable_unique_version_keys() -> None:
    from src.graph import migrations

    query = migrations.MIGRATE_SOURCE_RECORD_LIFECYCLE
    assert "source_version_key" in query
    assert query.index("version.source_version_key = NULL") < query.index(
        "version.source_version_key =\n"
    )
    assert "'sv1:'" in query
    assert "toString(size(source_system)) + ':' + source_system" in query
    assert "version.source_record_pk" in query
    assert "coalesce(\n         toString(version.source_record_version)" in query
    assert "duplicate_index" in query


def test_state_model_isolates_source_identity_and_honours_legacy_anchor() -> None:
    rows = [
        {
            "source_record_pk": "a2",
            "source_system": "a",
            "source_record_id": "1",
            "source_record_version": "2",
            "ingested_at": "2026-01-01",
            "is_latest": True,
        },
        {
            "source_record_pk": "a10",
            "source_system": "a",
            "source_record_id": "1",
            "source_record_version": "10",
            "ingested_at": "2026-02-01",
            "is_latest": False,
        },
        {
            "source_record_pk": "review",
            "source_system": "a",
            "source_record_id": "1",
            "source_record_version": "11",
            "ingested_at": "2026-03-01",
            "link_status": "pending_review",
        },
        {
            "source_record_pk": "other",
            "source_system": "b",
            "source_record_id": "1",
            "source_record_version": "10",
            "ingested_at": "2026-04-01",
            "is_latest": True,
        },
    ]

    migrated = _migrate(rows)
    by_pk = {str(row["source_record_pk"]): row for row in migrated}
    assert by_pk["a2"]["lifecycle_status"] == "active"
    assert by_pk["a10"]["lifecycle_status"] == "superseded"
    assert by_pk["review"]["lifecycle_status"] == "pending_review"
    assert by_pk["other"]["lifecycle_status"] == "active"


def test_state_model_preserves_terminal_states_and_is_idempotent() -> None:
    rows = [
        {
            "source_record_pk": "old",
            "source_system": "a",
            "source_record_id": "1",
            "source_record_version": "2",
            "ingested_at": "same",
            "is_latest": False,
        },
        {
            "source_record_pk": "new",
            "source_system": "a",
            "source_record_id": "1",
            "source_record_version": "10",
            "ingested_at": "same",
            "is_latest": True,
        },
        {
            "source_record_pk": "rejected",
            "source_system": "a",
            "source_record_id": "1",
            "source_record_version": "11",
            "ingested_at": "later",
            "lifecycle_status": "rejected",
        },
        {
            "source_record_pk": "failed",
            "source_system": "a",
            "source_record_id": "1",
            "source_record_version": "12",
            "ingested_at": "later",
            "lifecycle_status": "link_failed",
        },
    ]

    first = _migrate(rows)
    snapshot = [dict(row) for row in first]
    second = _migrate(first)
    assert second == snapshot
    assert sum(row["lifecycle_status"] == "active" for row in second) == 1
    assert {row["lifecycle_status"] for row in second} >= {"rejected", "link_failed"}


def test_state_model_multiple_or_missing_anchors_use_deterministic_last() -> None:
    rows = [
        {
            "source_record_pk": "anchor-2",
            "source_system": "a",
            "source_record_id": "1",
            "source_record_version": "2",
            "ingested_at": "1",
            "is_latest": True,
        },
        {
            "source_record_pk": "anchor-10",
            "source_system": "a",
            "source_record_id": "1",
            "source_record_version": "10",
            "ingested_at": "2",
            "is_latest": True,
        },
        {
            "source_record_pk": "fallback-2",
            "source_system": "b",
            "source_record_id": "1",
            "source_record_version": "2",
            "ingested_at": "1",
        },
        {
            "source_record_pk": "fallback-10",
            "source_system": "b",
            "source_record_id": "1",
            "source_record_version": "10",
            "ingested_at": "2",
        },
    ]

    migrated = _migrate(rows)
    active = {
        str(row["source_record_pk"]) for row in migrated if row["lifecycle_status"] == "active"
    }
    assert active == {"anchor-10", "fallback-10"}


def test_state_model_repairs_duplicates_missing_versions_and_orphans() -> None:
    rows = [
        {
            "source_record_pk": "dup-a",
            "source_system": "a",
            "source_record_id": "1",
            "source_record_version": "2",
            "ingested_at": "1",
            "source_version_key": "collision",
        },
        {
            "source_record_pk": "dup-b",
            "source_system": "a",
            "source_record_id": "1",
            "source_record_version": "2",
            "ingested_at": "2",
            "source_version_key": "collision",
        },
        {
            "source_record_pk": "missing",
            "source_system": "a",
            "source_record_id": "1",
            "source_record_version": None,
            "ingested_at": "0",
        },
        {
            "source_record_pk": "orphan",
            "source_system": None,
            "source_record_id": "1",
            "source_record_version": "1",
            "ingested_at": "0",
        },
    ]

    migrated = _migrate(rows)
    keys = [str(row["source_version_key"]) for row in migrated]
    assert len(keys) == len(set(keys))
    assert encode_source_version_key("a", "1", "2", duplicate_discriminator="dup-b") in keys
    assert any("legacy-pk:missing" in key for key in keys)
    assert encode_source_version_key("legacy-orphan:orphan", "1", "1") in keys
    by_pk = {str(row["source_record_pk"]): row for row in migrated}
    assert by_pk["dup-b"]["lifecycle_status"] == "active"
    assert by_pk["dup-a"]["lifecycle_status"] == "superseded"


def test_state_model_missing_primary_keys_persist_stable_repair_ids() -> None:
    rows = [
        {
            "source_record_pk": None,
            "element_id": "node-1",
            "source_system": None,
            "source_record_id": None,
            "source_record_version": None,
            "ingested_at": "1",
        },
        {
            "source_record_pk": None,
            "element_id": "node-2",
            "source_system": None,
            "source_record_id": None,
            "source_record_version": None,
            "ingested_at": "1",
        },
    ]

    first = _migrate(rows)
    keys = [str(row["source_version_key"]) for row in first]
    assert len(keys) == len(set(keys)) == 2
    rows[0]["element_id"] = "restored-node-9"
    rows[1]["element_id"] = "restored-node-10"
    assert [str(row["source_version_key"]) for row in _migrate(rows)] == keys
    assert keys == [
        encode_source_version_key(
            "legacy-orphan:repair-node-1",
            "legacy-pk:repair-node-1",
            "legacy-pk:repair-node-1",
        ),
        encode_source_version_key(
            "legacy-orphan:repair-node-2",
            "legacy-pk:repair-node-2",
            "legacy-pk:repair-node-2",
        ),
    ]


def test_state_model_multi_source_rows_use_per_record_malformed_namespace() -> None:
    rows = [
        {
            "source_record_pk": "multi-1",
            "source_systems": ["a", "b"],
            "source_record_id": "same",
            "source_record_version": "1",
            "ingested_at": "1",
        },
        {
            "source_record_pk": "multi-2",
            "source_systems": ["b", "a"],
            "source_record_id": "same",
            "source_record_version": "1",
            "ingested_at": "1",
        },
    ]

    keys = [str(row["source_version_key"]) for row in _migrate(rows)]
    assert keys == [
        encode_source_version_key("legacy-orphan:multi-1", "same", "1"),
        encode_source_version_key("legacy-orphan:multi-2", "same", "1"),
    ]


def test_query_contract_binds_authoritative_anchor_and_orphan_fallback() -> None:
    from src.graph import migrations

    query = migrations.MIGRATE_SOURCE_RECORD_LIFECYCLE
    assert "version.is_latest = true" in query
    assert "version.lifecycle_status = 'active'" in query
    assert "last(anchor_versions)" in query
    assert "legacy-orphan:" in query
    assert "OPTIONAL MATCH (version)-[:FROM_SOURCE]" in query
    assert "duplicate_discriminator" in query
    assert "legacy_repair_id" in query
    assert "randomUUID()" in query
    assert "elementId(" not in query


def test_migration_marker_serializes_and_gates_completed_reruns() -> None:
    from src.graph import migrations
    from src.graph.schema_init import BASE_LIFECYCLE_CONSTRAINTS

    query = migrations.MIGRATE_SOURCE_RECORD_LIFECYCLE
    assert "MERGE (migration:DataMigration" in query
    assert "migration.lock_version = coalesce(migration.lock_version, 0) + 1" in query
    assert "WHERE migration.completed_at IS NULL" in query
    assert query.index("SET migration.completed_at = datetime()") > query.index(
        "version.lifecycle_status = CASE"
    )
    assert "data_migration_key_unique" in "\n".join(BASE_LIFECYCLE_CONSTRAINTS)


def test_migration_runner_noops_after_marker_completion() -> None:
    class _NoopResult:
        def single(self) -> None:
            return None

    class _NoopTx:
        def run(self, _query: str) -> _NoopResult:
            return _NoopResult()

    class _NoopClient:
        def execute_write(self, work: object) -> object:
            return cast("object", work)(_NoopTx())  # type: ignore[operator]

    client = _NoopClient()

    assert migrate_source_record_lifecycle(cast(Neo4jClient, client)) == 0


def test_failed_migration_transaction_can_be_retried() -> None:
    class _FailingTx:
        def run(self, _query: str) -> None:
            raise RuntimeError("simulated migration failure")

    class _RetryClient:
        def __init__(self) -> None:
            self.attempts = 0

        def execute_write(self, work: object) -> object:
            self.attempts += 1
            if self.attempts == 1:
                return cast("object", work)(_FailingTx())  # type: ignore[operator]
            return cast("object", work)(_Client(updated=3).tx)  # type: ignore[operator]

    client = _RetryClient()

    with pytest.raises(RuntimeError, match="simulated migration failure"):
        migrate_source_record_lifecycle(cast(Neo4jClient, client))

    assert migrate_source_record_lifecycle(cast(Neo4jClient, client)) == 3
    assert client.attempts == 2


def test_completed_marker_is_followed_by_repeatable_late_record_reconciliation() -> None:
    from src.graph import migrations

    query = migrations.RECONCILE_SOURCE_RECORD_LIFECYCLE
    assert "WHERE migration.completed_at IS NULL" not in query
    assert "migration.lock_version = coalesce(migration.lock_version, 0) + 1" in query
    assert "version.source_version_key IS NULL" in query
    assert "version.lifecycle_status IS NULL" in query
    assert "complete.source_version_key IS NOT NULL" in query
    assert "complete.lifecycle_status IS NOT NULL" in query
    assert "duplicate_discriminator" in query
    assert "legacy_repair_id" in query


def test_late_record_reconciliation_runs_after_marker_migrations() -> None:
    from src.graph.migrations import apply_data_migrations

    client = _Client(updated=2)
    apply_data_migrations(cast(Neo4jClient, client))

    assert len(client.tx.queries) == 5
    assert "source_record_lifecycle_v1" in client.tx.queries[1]
    assert "projection_relationship_lifecycle_v1" in client.tx.queries[2]
    assert "WHERE migration.completed_at IS NULL" not in client.tx.queries[3]
    assert "WHERE migration.completed_at IS NULL" not in client.tx.queries[4]


def test_lifecycle_migration_runner_and_registration() -> None:
    client = _Client(updated=4)

    assert migrate_source_record_lifecycle(cast(Neo4jClient, client)) == 4
    assert len(client.tx.queries) == 1

    client = _Client(updated=2)
    apply_data_migrations(cast(Neo4jClient, client))
    assert len(client.tx.queries) == 5
