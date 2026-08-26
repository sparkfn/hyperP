"""Neo4j regression coverage for stage-history replay after source-instance migration."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase
from src.graph.queries.source_instance_migrations import (
    MIGRATE_SOURCE_RECORD_SOURCE_INSTANCES_BATCH,
)
from src.graph.queries.stage_history_ingestion import (
    UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD,
)
from src.source_instances import LEGACY_DEFAULT_SOURCE_INSTANCE_ID


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    uri = os.getenv("HYPERP_NEO4J_CRM_DEAL_COUNT_TEST_URI")
    if uri is None:
        pytest.skip("disposable Neo4j migration test database is not configured")
    host = urlparse(uri).hostname
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    service_host = os.getenv("HYPERP_NEO4J_CRM_DEAL_COUNT_TEST_SERVICE_HOST")
    if service_host is not None:
        allowed_hosts.add(service_host)
    if host not in allowed_hosts:
        pytest.fail("source-instance migration test requires an explicitly configured host")
    password = os.getenv("HYPERP_NEO4J_CRM_DEAL_COUNT_TEST_PASSWORD")
    if password is None:
        pytest.fail("HYPERP_NEO4J_CRM_DEAL_COUNT_TEST_PASSWORD is required")
    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_CRM_DEAL_COUNT_TEST_USER", "neo4j"), password),
    )
    try:
        for _ in range(15):
            try:
                driver.verify_connectivity()
                break
            except Exception:  # noqa: BLE001 - disposable service readiness retry
                time.sleep(1)
        else:
            pytest.fail("disposable source-instance Neo4j database did not become ready")
        with driver.session() as session:
            existing = session.run("MATCH (node) RETURN count(node) AS total").single(strict=True)
            if existing["total"] != 0:
                pytest.fail("source-instance migration test requires an empty database")
        yield driver
    finally:
        with driver.session() as session:
            session.run("MATCH (node) DETACH DELETE node").consume()
        driver.close()


def test_stage_history_variant_replays_after_source_instance_migration(
    neo4j_driver: Driver,
) -> None:
    params: dict[str, object] = {
        "source_key": "bitrix_chat",
        "source_instance_id": LEGACY_DEFAULT_SOURCE_INSTANCE_ID,
        "logical_run_id": "logical-1",
        "ingest_run_id": "attempt-1",
        "attempt_generation": 1,
        "stream_generation": 1,
        "fencing_token": 1,
        "required_run_type": "backfill",
        "unit_id": "unit-1",
        "unit_digest": "unit-digest",
        "occurrence_id": "occurrence-1",
        "event_identity": "event-1",
        "canonical_hash": "hash-1",
        "hash_version": "bitrix-stage-history-v1",
        "source_record_pk": "record-1",
        "source_version_key": "stage-source-version-key",
        "history_kind": "stage_change",
        "history_source": "history",
        "history_projection_version": "v1",
        "history_projection_source": "bitrix",
        "event_category_id": "category-1",
        "event_stage_id": "stage-1",
        "event_stage_semantic_id": "semantic-1",
        "event_at": "2026-08-20T00:00:00Z",
        "source_observed_at": "2026-08-20T00:00:01Z",
        "raw_payload": '{"ID":"event-1"}',
    }
    with neo4j_driver.session() as session:
        session.run(
            """
            CREATE (source:SourceSystem {source_key: $source_key})
            CREATE (logical:IngestionLogicalRun {
              logical_run_id: $logical_run_id, active_generation: $attempt_generation,
              mode: $required_run_type, status: 'running'
            })
            CREATE (attempt:IngestRun {
              ingest_run_id: $ingest_run_id, generation: $attempt_generation
            })
            CREATE (logical)-[:ACTIVE_ATTEMPT]->(attempt)
            CREATE (:BitrixIngestionStream {
              source_key: $source_key, stream_key: 'crm_stage_history',
              logical_run_id: $logical_run_id, ingest_run_id: $ingest_run_id,
              attempt_generation: $attempt_generation,
              stream_generation: $stream_generation, fencing_token: $fencing_token,
              status: 'active'
            })
            CREATE (unit:StageHistoryUnit {
              unit_id: $unit_id, logical_run_id: $logical_run_id,
              unit_digest: $unit_digest, status: 'persisting'
            })
            CREATE (occurrence:StageHistoryOccurrence {
              occurrence_id: $occurrence_id, event_identity: $event_identity,
              canonical_hash: $canonical_hash
            })
            CREATE (unit)-[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(occurrence)
            CREATE (record:SourceRecord {
              source_record_pk: $source_record_pk, source_system: $source_key,
              source_record_id: $event_identity, source_record_version: '1',
              source_version_key: $source_version_key, record_type: 'crm_history',
              history_family: 'stage', history_kind: $history_kind,
              history_source: $history_source,
              history_projection_version: $history_projection_version,
              history_projection_source: $history_projection_source,
              event_category_id: $event_category_id, event_stage_id: $event_stage_id,
              event_stage_semantic_id: $event_stage_semantic_id,
              event_at: datetime($event_at), observed_at: datetime($source_observed_at),
              record_hash: $canonical_hash, raw_payload: $raw_payload,
              lifecycle_status: 'pending_review', is_latest: false,
              link_status: 'stage_authority_only'
            })-[:FROM_SOURCE]->(source)
            CREATE (variant:CrmHistoryHashVariant {
              event_identity: $event_identity, canonical_hash: $canonical_hash,
              hash_version: $hash_version
            })-[:EVIDENCED_BY]->(record)
            """,
            **params,  # type: ignore[arg-type]
        ).consume()
        migrated = session.run(
            MIGRATE_SOURCE_RECORD_SOURCE_INSTANCES_BATCH,
            legacy_source_instance_id=LEGACY_DEFAULT_SOURCE_INSTANCE_ID,
            migration_key="source-instance-test",
            batch_size=100,
        ).single(strict=True)
        replay = session.run(
            UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD,
            **params,  # type: ignore[arg-type]
        ).single(strict=True)
        stored = session.run(
            """
            MATCH (record:SourceRecord {source_record_pk: $source_record_pk})
            RETURN record.source_instance_id AS source_instance_id,
                   record.source_version_key AS source_version_key
            """,
            source_record_pk=params["source_record_pk"],
        ).single(strict=True)

    assert migrated["updated"] == 1
    assert replay["created"] is False
    assert replay["source_record_pk"] == params["source_record_pk"]
    assert dict(stored) == {
        "source_instance_id": LEGACY_DEFAULT_SOURCE_INSTANCE_ID,
        "source_version_key": params["source_version_key"],
    }
