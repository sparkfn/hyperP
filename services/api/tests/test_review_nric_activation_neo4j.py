"""Disposable-Neo4j coverage for keyed pending-review NRIC activation."""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from neo4j import Driver, GraphDatabase, Session
from src.graph.queries.review import ACTIVATE_PENDING_REVIEW_RECORD
from src.repositories.neo4j.review import _pending_projection_params
from test_review_nric_activation import _canonical, _independent_digest, _key, _payload

_RUN_TAG = "_review_nric_activation_test_run"


@dataclass(frozen=True)
class _TestGraph:
    driver: Driver
    run_id: str


@dataclass(frozen=True)
class _ActivationWorld:
    source_key: str
    review_case_id: str
    pending_record_pk: str
    source_record_id: str
    approved_person_id: str


@pytest.fixture
def neo4j_driver() -> Iterator[_TestGraph]:
    uri = os.getenv("HYPERP_NEO4J_CRM_METRICS_TEST_URI")
    if uri is None:
        pytest.skip("disposable CRM metrics Neo4j test database is not configured")
    host = urlparse(uri).hostname
    service_host = os.getenv("HYPERP_NEO4J_CRM_METRICS_TEST_SERVICE_HOST")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if service_host is not None:
        allowed_hosts.add(service_host)
    if host not in allowed_hosts:
        pytest.fail("review NRIC activation tests only accept an explicitly configured Neo4j host")

    password = os.getenv("HYPERP_NEO4J_CRM_METRICS_TEST_PASSWORD")
    if password is None:
        pytest.fail("HYPERP_NEO4J_CRM_METRICS_TEST_PASSWORD is required")
    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_CRM_METRICS_TEST_USER", "neo4j"), password),
    )
    driver.verify_connectivity()
    graph = _TestGraph(driver=driver, run_id=uuid4().hex)
    try:
        yield graph
    finally:
        with driver.session() as session:
            session.run(
                f"MATCH (node {{{_RUN_TAG}: $run_id}}) DETACH DELETE node",
                run_id=graph.run_id,
            ).consume()
        driver.close()


def _seed_world(
    graph: _TestGraph,
    payload: str,
) -> _ActivationWorld:
    suffix = uuid4().hex
    world = _ActivationWorld(
        source_key=f"source-{suffix}",
        review_case_id=f"review-{suffix}",
        pending_record_pk=f"pending-{suffix}",
        source_record_id=f"record-{suffix}",
        approved_person_id=f"person-{suffix}",
    )
    with graph.driver.session() as session:
        session.run(
            f"""
            CREATE (source:SourceSystem {{source_key: $source_key, {_RUN_TAG}: $run_id}})
            CREATE (approved:Person {{person_id: $approved_person_id, status: 'active',
              {_RUN_TAG}: $run_id}})
            CREATE (pending:SourceRecord {{source_record_pk: $pending_record_pk,
              source_instance_id: $source_instance_id, source_record_id: $source_record_id,
              record_type: 'identity', lifecycle_status: 'pending_review', is_latest: false,
              normalized_payload: $payload,
              {_RUN_TAG}: $run_id}})-[:FROM_SOURCE]->(source)
            CREATE (decision:MatchDecision {{match_decision_id: $decision_id,
              {_RUN_TAG}: $run_id}})-[:ABOUT_LEFT]->(pending)
            CREATE (review:ReviewCase {{review_case_id: $review_case_id,
              {_RUN_TAG}: $run_id}})-[:FOR_DECISION]->(decision)
            CREATE (decision)-[:ABOUT_RIGHT]->(approved)
            """,
            source_key=world.source_key,
            approved_person_id=world.approved_person_id,
            pending_record_pk=world.pending_record_pk,
            source_instance_id=f"instance-{suffix}",
            source_record_id=world.source_record_id,
            payload=payload,
            decision_id=f"decision-{suffix}",
            review_case_id=world.review_case_id,
            run_id=graph.run_id,
        ).consume()
    return world


def _projected_identifiers(
    world: _ActivationWorld, payload: str, key: str
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    with patch("src.nric.config.nric_hmac_key", key):
        return _pending_projection_params(
            payload,
            source_system_key=world.source_key,
            source_record_id=world.source_record_id,
            source_record_pk=world.pending_record_pk,
        )


def _seed_identifier(
    session: Session,
    graph: _TestGraph,
    identifier_type: str,
    normalized_value: str,
) -> None:
    session.run(
        f"""
        CREATE (:Identifier {{identifier_id: $identifier_id, identifier_type: $identifier_type,
          identifier_scope: 'global', normalized_value: $normalized_value, {_RUN_TAG}: $run_id}})
        """,
        identifier_id=uuid4().hex,
        identifier_type=identifier_type,
        normalized_value=normalized_value,
        run_id=graph.run_id,
    ).consume()


def _activate(
    graph: _TestGraph,
    world: _ActivationWorld,
    payload: str,
    key: str,
) -> None:
    (
        identifiers,
        addresses,
        attributes,
        bankruptcy_cases,
        vehicle_mentions,
        knows_relationships,
    ) = _projected_identifiers(world, payload, key)
    with graph.driver.session() as session:
        session.run(
            ACTIVATE_PENDING_REVIEW_RECORD,
            review_case_id=world.review_case_id,
            pending_source_record_pk=world.pending_record_pk,
            source_system_key=world.source_key,
            expected_active_source_record_pk=None,
            approved_person_id=world.approved_person_id,
            observed_at="2026-11-20T00:00:00Z",
            identifiers=identifiers,
            addresses=addresses,
            attributes=attributes,
            bankruptcy_cases=bankruptcy_cases,
            vehicle_mentions=vehicle_mentions,
            knows_relationships=knows_relationships,
        ).single(strict=True)
        session.run(
            f"""
            MATCH (lock:SourceRecordIdentityLock {{source_system: $source_key,
              source_record_id: $source_record_id}})
            SET lock.{_RUN_TAG} = $run_id
            """,
            source_key=world.source_key,
            source_record_id=world.source_record_id,
            run_id=graph.run_id,
        ).consume()
        for item in identifiers:
            identifier_type = item.get("identifier_type")
            normalized_value = item.get("normalized_value")
            if not isinstance(identifier_type, str) or not isinstance(normalized_value, str):
                raise AssertionError("projected identifier was malformed")
            _tag_identifier(session, graph, identifier_type, normalized_value)


def _tag_identifier(
    session: Session,
    graph: _TestGraph,
    identifier_type: str,
    normalized_value: str,
) -> None:
    session.run(
        f"""
        MATCH (id:Identifier {{identifier_type: $identifier_type,
          normalized_value: $normalized_value}})
        SET id.{_RUN_TAG} = $run_id
        """,
        identifier_type=identifier_type,
        normalized_value=normalized_value,
        run_id=graph.run_id,
    ).consume()


def _identifier_counts(graph: _TestGraph, identifier_type: str) -> dict[str, int]:
    with graph.driver.session() as session:
        row = session.run(
            f"""
            MATCH (id:Identifier {{{_RUN_TAG}: $run_id, identifier_type: $identifier_type}})
            RETURN count(id) AS total,
              count(CASE WHEN id.normalized_value =~ '^[0-9a-f]{{64}}$' THEN 1 END) AS digests,
              count(CASE WHEN id.identifier_scope IS NULL THEN 1 END) AS scope_less
            """,
            run_id=graph.run_id,
            identifier_type=identifier_type,
        ).single(strict=True)
    total: object = row["total"]
    digests: object = row["digests"]
    scope_less: object = row["scope_less"]
    if (
        not isinstance(total, int)
        or not isinstance(digests, int)
        or not isinstance(scope_less, int)
    ):
        raise AssertionError("identifier count query returned malformed values")
    return {"total": total, "digests": digests, "scope_less": scope_less}


def test_rehashed_world_reuses_keyed_identifier_and_links_pending_record(
    neo4j_driver: _TestGraph,
) -> None:
    canonical = _canonical()
    key = _key()
    digest = _independent_digest(canonical, key)
    payload = _payload(canonical)
    world = _seed_world(neo4j_driver, payload)
    with neo4j_driver.driver.session() as session:
        _seed_identifier(session, neo4j_driver, "nric", digest)

    _activate(neo4j_driver, world, payload, key)

    counts = _identifier_counts(neo4j_driver, "nric")
    assert counts == {"total": 1, "digests": 1, "scope_less": 0}
    with neo4j_driver.driver.session() as session:
        row = session.run(
            f"""
            MATCH (:Person {{person_id: $person_id}})-[link:IDENTIFIED_BY
              {{source_record_pk: $source_record_pk}}]->
              (:Identifier {{{_RUN_TAG}: $run_id, identifier_type: 'nric',
                normalized_value: $normalized_value}})
            RETURN count(link) AS count
            """,
            person_id=world.approved_person_id,
            source_record_pk=world.pending_record_pk,
            run_id=neo4j_driver.run_id,
            normalized_value=digest,
        ).single(strict=True)
    edge_count: object = row["count"]
    if not isinstance(edge_count, int):
        raise AssertionError("activation edge count was malformed")
    assert edge_count == 1


def test_digest_absent_activation_creates_scoped_identifier(neo4j_driver: _TestGraph) -> None:
    canonical = _canonical()
    key = _key()
    digest = _independent_digest(canonical, key)
    payload = _payload(digest)
    world = _seed_world(neo4j_driver, payload)

    _activate(neo4j_driver, world, payload, key)

    assert _identifier_counts(neo4j_driver, "nric") == {
        "total": 1,
        "digests": 1,
        "scope_less": 0,
    }


def test_phone_identifier_keeps_scope_less_creation_behavior(neo4j_driver: _TestGraph) -> None:
    phone = f"phone-{secrets.token_hex(8)}"
    payload = _payload(phone, "phone")
    world = _seed_world(neo4j_driver, payload)

    _activate(neo4j_driver, world, payload, "")

    counts = _identifier_counts(neo4j_driver, "phone")
    assert counts == {"total": 1, "digests": 0, "scope_less": 1}
