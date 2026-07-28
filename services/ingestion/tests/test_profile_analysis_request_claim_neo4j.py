"""Neo4j 5.26 coverage for on-demand profile-analysis request claiming.

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
from src.config import Settings
from src.graph.client import Neo4jClient
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
        Settings(
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
def test_request_claim_query_compiles_and_claims_each_analysis_type(
    neo4j_client: Neo4jClient,
    analysis_type: str,
) -> None:
    now = datetime(2026, 7, 28, 4, tzinfo=UTC)
    other_analysis_type = (
        "contact_tracing" if analysis_type == "sales" else "sales"
    )
    with neo4j_client.session() as session:
        session.run(
            """
            CREATE (person:Person {
              person_id: 'person-1', status: 'active', analysis_input_revision: 7
            })
            CREATE (request:ProfileAnalysisRequest {
              request_id: 'request-1', status: 'queued', analysis_type: $analysis_type
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

    repository.complete_request(request_id="request-1", status="succeeded")
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
