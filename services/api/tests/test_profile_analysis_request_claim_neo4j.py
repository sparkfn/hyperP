"""Neo4j 5.26 coverage for profile-analysis queries and repository mapping.

Set ``HYPERP_NEO4J_PROFILE_ANALYSIS_TEST_URI`` to a disposable localhost
Neo4j database to enable these tests. The fixture intentionally refuses remote
hosts because it clears the graph between tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
from src.config import AppConfig
from src.profile_analysis_client import Neo4jClient
from src.profile_analysis_mapping import ProfileAnalysisTemporalMappingError
from src.profile_analysis_repository import Neo4jProfileAnalysisRepository


@pytest.fixture
def neo4j_client() -> Iterator[Neo4jClient]:
    uri = os.getenv("HYPERP_NEO4J_PROFILE_ANALYSIS_TEST_URI")
    if uri is None:
        pytest.skip("disposable profile-analysis Neo4j test database is not configured")
    host = urlparse(uri).hostname
    if host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("profile-analysis integration tests only accept a localhost Neo4j URI")
    password = os.getenv("HYPERP_NEO4J_PROFILE_ANALYSIS_TEST_PASSWORD")
    if password is None:
        pytest.fail("HYPERP_NEO4J_PROFILE_ANALYSIS_TEST_PASSWORD is required")

    client = Neo4jClient(
        AppConfig(
            neo4j_uri=uri,
            neo4j_user=os.getenv("HYPERP_NEO4J_PROFILE_ANALYSIS_TEST_USER", "neo4j"),
            neo4j_password=password,
        )
    )
    client.verify_connectivity()
    with client.session() as session:
        session.run("MATCH (node) DETACH DELETE node").consume()
    try:
        yield client
    finally:
        with client.session() as session:
            session.run("MATCH (node) DETACH DELETE node").consume()
        client.close()


@pytest.mark.parametrize("analysis_type", ("sales", "contact_tracing"))
@pytest.mark.parametrize("request_status", ("queued", "running"))
def test_request_claim_query_compiles_and_claims_queued_or_expired_running_request(
    neo4j_client: Neo4jClient,
    analysis_type: str,
    request_status: str,
) -> None:
    now = datetime(2026, 7, 28, 4, tzinfo=UTC)
    other_analysis_type = "contact_tracing" if analysis_type == "sales" else "sales"
    with neo4j_client.session() as session:
        session.run(
            """
            CREATE (person:Person {
              person_id: 'person-1', status: 'active', analysis_input_revision: 7
            })
            CREATE (request:ProfileAnalysisRequest {
              request_id: 'request-1', status: $request_status, analysis_type: $analysis_type
            })
            CREATE (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request)
            CREATE (person)-[:HAS_PROFILE_ANALYSIS]->(:ProfileAnalysis {
              analysis_type: $analysis_type, input_revision: 7
            })
            CREATE (person)-[:HAS_PROFILE_ANALYSIS]->(:ProfileAnalysis {
              analysis_type: $other_analysis_type, input_revision: 7
            })
            """,
            analysis_type=analysis_type,
            other_analysis_type=other_analysis_type,
            request_status=request_status,
        ).consume()

    repository = Neo4jProfileAnalysisRepository(neo4j_client)
    claimed = repository.claim_request(
        request_id="request-1",
        claim_token="claim-token",
        now=now,
        claim_until=now + timedelta(minutes=15),
    )
    with neo4j_client.session() as session:
        state = session.run(
            """
            MATCH (person:Person {person_id: 'person-1'})
                  -[:HAS_PROFILE_ANALYSIS_REQUEST]->(request:ProfileAnalysisRequest {
                    request_id: 'request-1'
                  })
            RETURN request.status AS request_status,
                   request.input_revision AS input_revision,
                   person.analysis_claim_token AS claim_token
            """
        ).single(strict=True)

    repository.complete_request(
        request_id="request-1",
        claim_token="claim-token",
        status="succeeded",
    )
    with neo4j_client.session() as session:
        terminal_state = session.run(
            """
            MATCH (:Person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request:ProfileAnalysisRequest {
              request_id: 'request-1'
            })
            RETURN request.status AS request_status
            """
        ).single(strict=True)

    assert claimed is not None
    assert claimed.person_id == "person-1"
    assert claimed.input_revision == 7
    assert [(due.analysis_type.value, due.attempt_number) for due in claimed.due] == [
        (analysis_type, 2)
    ]
    assert state["request_status"] == "running"
    assert state["input_revision"] == 7
    assert state["claim_token"] == "claim-token"
    assert terminal_state["request_status"] == "succeeded"


@pytest.mark.parametrize("storage_type", ("string", "native_utc", "native_named_zone"))
def test_snapshot_fetch_normalizes_string_and_native_order_timestamps(
    neo4j_client: Neo4jClient,
    storage_type: str,
) -> None:
    ordered_at = "2022-10-02T18:00:07Z"
    with neo4j_client.session() as session:
        session.run(
            """
            CREATE (person:Person {person_id: 'person-1', status: 'active'})
            CREATE (order:Order {
              order_id: 'order-1', ordered_at: $ordered_at, total_amount: 12.5, currency: 'SGD'
            })
            CREATE (person)-[:PURCHASED]->(order)
            """,
            ordered_at=ordered_at,
        ).consume()
        if storage_type == "native_utc":
            session.run(
                """
                MATCH (order:Order {order_id: 'order-1'})
                SET order.ordered_at = datetime($ordered_at)
                """,
                ordered_at=ordered_at,
            ).consume()
        elif storage_type == "native_named_zone":
            session.run(
                """
                MATCH (order:Order {order_id: 'order-1'})
                SET order.ordered_at = datetime({
                  year: 2022, month: 10, day: 2, hour: 18, timezone: 'Asia/Manila'
                })
                """
            ).consume()

    snapshot = Neo4jProfileAnalysisRepository(neo4j_client).fetch_snapshot("person-1").snapshot

    assert len(snapshot.orders) == 1
    assert snapshot.orders[0].order_date is not None
    assert snapshot.orders[0].order_date.value == "2022-10-02"


def test_snapshot_fetch_maps_unsupported_order_timestamp_type_to_a_safe_error(
    neo4j_client: Neo4jClient,
) -> None:
    with neo4j_client.session() as session:
        session.run(
            """
            CREATE (person:Person {person_id: 'person-1', status: 'active'})
            CREATE (order:Order {order_id: 'order-1', ordered_at: 42})
            CREATE (person)-[:PURCHASED]->(order)
            """
        ).consume()

    repository = Neo4jProfileAnalysisRepository(neo4j_client)

    with pytest.raises(ProfileAnalysisTemporalMappingError) as raised:
        repository.fetch_snapshot("person-1")

    assert str(raised.value) == "invalid safe profile analysis snapshot data"
