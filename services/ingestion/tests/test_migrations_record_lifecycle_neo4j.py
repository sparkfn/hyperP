"""Neo4j 5.26 integration coverage for the lifecycle migration.

Set ``HYPERP_NEO4J_MIGRATION_TEST_URI`` to a disposable localhost database to
enable these tests. The fixture intentionally refuses remote hosts because it
clears the graph between tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from neo4j import Record
from src.config import Settings
from src.graph import migrations
from src.graph.client import Neo4jClient
from src.graph.schema_init import apply_schema
from src.source_version_keys import encode_source_version_key


@pytest.fixture
def neo4j_client() -> Iterator[Neo4jClient]:
    uri = os.getenv("HYPERP_NEO4J_MIGRATION_TEST_URI")
    if uri is None:
        pytest.skip("disposable Neo4j migration test database is not configured")
    host = urlparse(uri).hostname
    if host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("migration integration tests only accept a localhost Neo4j URI")
    password = os.getenv("HYPERP_NEO4J_MIGRATION_TEST_PASSWORD")
    if password is None:
        pytest.fail("HYPERP_NEO4J_MIGRATION_TEST_PASSWORD is required")

    client = Neo4jClient(
        Settings(
            neo4j_uri=uri,
            neo4j_user=os.getenv("HYPERP_NEO4J_MIGRATION_TEST_USER", "neo4j"),
            neo4j_password=password,
        )
    )
    client.verify_connectivity()
    with client.session() as session:
        session.run("MATCH (node) DETACH DELETE node").consume()
    apply_schema(client)
    try:
        yield client
    finally:
        with client.session() as session:
            session.run("MATCH (node) DETACH DELETE node").consume()
        client.close()


def test_actual_queries_bound_large_identity_and_preserve_semantics(
    neo4j_client: Neo4jClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with neo4j_client.session() as session:
        session.run("CREATE (:SourceSystem {source_key: 'chat'})").consume()
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'chat'})
            UNWIND range(1, 7) AS source_version
            CREATE (:SourceRecord {
              source_record_pk: 'pk-' + toString(source_version),
              source_record_id: 'large-identity',
              source_record_version: toString(source_version),
              ingested_at: datetime() + duration({seconds: source_version}),
              lifecycle_status: CASE WHEN source_version = 6 THEN 'rejected' ELSE null END,
              is_latest: source_version = 5
            })-[:FROM_SOURCE]->(source)
            """
        ).consume()
        session.run(
            "CREATE (:SourceRecord {ingested_at: datetime(), link_status: 'pending_review'})"
        ).consume()
        session.run(
            """
            MATCH (record:SourceRecord {source_record_pk: 'pk-1'}),
                  (source:SourceSystem {source_key: 'chat'})
            CREATE (record)-[:FROM_SOURCE]->(source)
            """
        ).consume()

    monkeypatch.setattr(migrations, "SOURCE_RECORD_LIFECYCLE_BATCH_SIZE", 2)
    assert migrations.migrate_source_record_lifecycle(neo4j_client) == 8

    with neo4j_client.session() as session:
        active = list(
            session.run(
                """
                MATCH (record:SourceRecord {
                  source_record_id: 'large-identity', lifecycle_status: 'active'
                })
                RETURN record.source_record_pk AS source_record_pk,
                       record.is_latest AS is_latest
                """
            )
        )
        keys = [
            record["source_version_key"]
            for record in session.run(
                "MATCH (record:SourceRecord) RETURN record.source_version_key AS source_version_key"
            )
        ]
        rejected = session.run(
            """
            MATCH (record:SourceRecord {source_record_pk: 'pk-6'})
            RETURN record.lifecycle_status AS lifecycle_status
            """
        ).single(strict=True)
    assert [(record["source_record_pk"], record["is_latest"]) for record in active] == [
        ("pk-5", True)
    ]
    assert rejected["lifecycle_status"] == "rejected"
    assert len(keys) == len(set(keys)) == 8
    assert migrations.migrate_source_record_lifecycle(neo4j_client) == 0


def test_actual_queries_resume_committed_batch_and_reload_live_version(
    neo4j_client: Neo4jClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with neo4j_client.session() as session:
        session.run("CREATE (:SourceSystem {source_key: 'chat'})").consume()
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'chat'})
            UNWIND [{pk: 'a', version: '1'}, {pk: 'b', version: '2'}] AS row
            CREATE (:SourceRecord {
              source_record_pk: row.pk,
              source_record_id: 'retry',
              source_record_version: row.version,
              ingested_at: datetime(),
              is_latest: row.pk = 'b'
            })-[:FROM_SOURCE]->(source)
            """
        ).consume()

    monkeypatch.setattr(migrations, "SOURCE_RECORD_LIFECYCLE_BATCH_SIZE", 1)
    original_query = migrations._run_migration_query
    committed_batches = 0

    def fail_after_first_commit(
        client: Neo4jClient, query: str, **params: object
    ) -> Record | None:
        nonlocal committed_batches
        record = original_query(client, query, **params)
        if (
            query == migrations.MIGRATE_SOURCE_RECORD_LIFECYCLE_BATCH
            and record is not None
            and record["updated"] == 1
        ):
            committed_batches += 1
            if committed_batches == 1:
                raise RuntimeError("failure after committed batch")
        return record

    monkeypatch.setattr(migrations, "_run_migration_query", fail_after_first_commit)
    with pytest.raises(RuntimeError, match="failure after committed batch"):
        migrations.migrate_source_record_lifecycle(neo4j_client)
    monkeypatch.setattr(migrations, "_run_migration_query", original_query)

    with neo4j_client.session() as session:
        session.run(
            """
            MERGE (lock:SourceRecordIdentityLock {
              source_system: 'chat', source_record_id: 'retry'
            })
            SET lock.locked_at = datetime()
            WITH lock
            MATCH (old:SourceRecord {source_record_pk: 'b'})
            SET old.lifecycle_status = 'superseded', old.is_latest = false
            WITH old
            MATCH (source:SourceSystem {source_key: 'chat'})
            CREATE (:SourceRecord {
              source_record_pk: '0-live', source_record_id: 'retry',
              source_record_version: '2', ingested_at: datetime(),
              lifecycle_status: 'active', is_latest: true,
              source_version_key: $source_version_key
            })-[:FROM_SOURCE]->(source)
            """,
            source_version_key=encode_source_version_key("chat", "retry", "2"),
        ).consume()

    assert migrations.migrate_source_record_lifecycle(neo4j_client) == 1
    with neo4j_client.session() as session:
        active = list(
            session.run(
                """
                MATCH (record:SourceRecord {source_record_id: 'retry', lifecycle_status: 'active'})
                RETURN record.source_record_pk AS source_record_pk,
                       record.is_latest AS is_latest
                """
            )
        )
        marker = session.run(
            """
            MATCH (migration:DataMigration {migration_key: 'source_record_lifecycle_v1'})
            RETURN migration.updated_records AS updated_records,
                   migration.phase AS phase
            """
        ).single(strict=True)
        keys = [
            record["source_version_key"]
            for record in session.run(
                """
                MATCH (record:SourceRecord {source_record_id: 'retry'})
                RETURN record.source_version_key AS source_version_key
                """
            )
        ]
    assert [(record["source_record_pk"], record["is_latest"]) for record in active] == [
        ("0-live", True)
    ]
    assert len(keys) == len(set(keys)) == 3
    assert (marker["updated_records"], marker["phase"]) == (2, "complete")
