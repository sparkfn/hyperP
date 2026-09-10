"""Neo4j 5 execution coverage for the authenticated variable-depth person graph."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncSession, Driver, GraphDatabase
from starlette.routing import Mount

import src.repositories.neo4j.person as person_module
from src.app import build_app
from src.auth.deps import get_current_user_or_oauth_client, require_active_user
from src.auth.models import AuthUser
from src.graph.queries.graph import MAX_HOPS, MIN_HOPS, get_graph_query, get_node_graph_query
from src.repositories.deps import get_person_repo
from src.repositories.neo4j.person import Neo4jPersonRepository
from src.types import GraphEdge, PersonGraph


@dataclass(frozen=True)
class _TestGraph:
    driver: Driver
    uri: str
    user: str
    password: str
    run_id: str


@dataclass(frozen=True)
class _SeededGraph:
    person_id: str
    generic_element_id: str


@pytest.fixture
def neo4j_person_graph() -> Iterator[_TestGraph]:
    uri = os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_URI")
    if uri is None:
        pytest.skip("disposable person-list Neo4j test database is not configured")
    _validate_disposable_target(uri)
    password = os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_PASSWORD")
    if password is None:
        pytest.fail("HYPERP_NEO4J_PERSON_LIST_TEST_PASSWORD is required")
    user = os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_USER", "neo4j")
    driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=5)
    test_graph = _TestGraph(
        driver=driver,
        uri=uri,
        user=user,
        password=password,
        run_id=uuid4().hex,
    )
    try:
        driver.verify_connectivity()
        yield test_graph
    finally:
        _cleanup_test_graph(test_graph)
        driver.close()


def _validate_disposable_target(uri: str) -> None:
    parsed = urlparse(uri)
    service_host = os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_SERVICE_HOST")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if service_host:
        allowed_hosts.add(service_host)
    if parsed.scheme != "bolt":
        pytest.fail("person graph integration tests require a direct Bolt URI")
    if parsed.hostname not in allowed_hosts:
        pytest.fail("person graph integration tests only accept the disposable CI Neo4j host")
    if parsed.username is not None or parsed.password is not None:
        pytest.fail("person graph integration tests must not embed credentials in the URI")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        pytest.fail("person graph integration tests must not select a database or URI options")


def _cleanup_test_graph(test_graph: _TestGraph) -> None:
    with test_graph.driver.session() as session:
        session.run(
            "MATCH (node {_person_graph_test_run: $run_id}) DETACH DELETE node",
            run_id=test_graph.run_id,
        ).consume()
        remaining = session.run(
            "MATCH (node {_person_graph_test_run: $run_id}) RETURN count(node) AS total",
            run_id=test_graph.run_id,
        ).single(strict=True)
    assert int(remaining["total"]) == 0


def _seed_graph(test_graph: _TestGraph) -> _SeededGraph:
    person_id = f"person-graph-{test_graph.run_id}"
    with test_graph.driver.session() as session:
        record = session.run(
            """
            CREATE (root:Person {
              person_id: $person_id,
              fixture_role: 'root',
              _person_graph_test_run: $run_id
            })
            CREATE (active:Identifier {
              fixture_role: 'active',
              value: 'active-identifier',
              _person_graph_test_run: $run_id
            })
            CREATE (legacy:Address {
              fixture_role: 'legacy',
              value: 'legacy-address',
              _person_graph_test_run: $run_id
            })
            CREATE (retired:Person {
              person_id: $retired_person_id,
              fixture_role: 'retired_branch',
              _person_graph_test_run: $run_id
            })
            CREATE (control:GraphProbe {
              fixture_role: 'control',
              value: 'non-repairable-control',
              _person_graph_test_run: $run_id
            })
            CREATE (root)-[:IDENTIFIED_BY {
              is_active: true,
              fixture_role: 'active_link'
            }]->(active)
            CREATE (root)-[:LIVES_AT {
              fixture_role: 'legacy_link'
            }]->(legacy)
            CREATE (root)-[:LINKED_TO {
              is_active: false,
              fixture_role: 'retired_branch'
            }]->(retired)
            CREATE (root)-[:CHILD_OF {
              is_active: false,
              fixture_role: 'control_link'
            }]->(control)
            CREATE (active)-[:KNOWS {
              is_active: false,
              fixture_role: 'retired_final_edge'
            }]->(legacy)
            RETURN elementId(active) AS generic_element_id
            """,
            person_id=person_id,
            retired_person_id=f"retired-person-graph-{test_graph.run_id}",
            run_id=test_graph.run_id,
        ).single(strict=True)
    return _SeededGraph(
        person_id=person_id,
        generic_element_id=str(record["generic_element_id"]),
    )


def test_person_graph_builders_explain_on_neo4j_5(neo4j_person_graph: _TestGraph) -> None:
    with neo4j_person_graph.driver.session() as session:
        for max_hops in range(MIN_HOPS, MAX_HOPS + 1):
            session.run(
                "EXPLAIN\n" + get_graph_query(max_hops),
                person_id="person-graph-explain",
            ).consume()
            session.run(
                "EXPLAIN\n" + get_node_graph_query(max_hops),
                element_id="4:missing:0",
            ).consume()


@pytest.mark.anyio
async def test_person_graph_repository_and_mounted_routes_preserve_lifecycle_contract(
    neo4j_person_graph: _TestGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_graph(neo4j_person_graph)
    driver = AsyncGraphDatabase.driver(
        neo4j_person_graph.uri,
        auth=(neo4j_person_graph.user, neo4j_person_graph.password),
        connection_timeout=5,
    )
    try:
        monkeypatch.setattr(person_module, "get_session", _live_session_factory(driver))
        repository = Neo4jPersonRepository()

        person_graph = await repository.get_graph(seeded.person_id, max_hops=2)
        generic_graph = await repository.get_node_graph(seeded.generic_element_id, max_hops=2)

        assert person_graph is not None
        assert generic_graph is not None
        _assert_lifecycle_graph(person_graph)
        _assert_lifecycle_graph(generic_graph)

        app = build_app()
        frontend_app = _mounted_frontend_app(app)
        frontend_app.dependency_overrides[require_active_user] = _active_user
        frontend_app.dependency_overrides[get_current_user_or_oauth_client] = _active_user
        frontend_app.dependency_overrides[get_person_repo] = lambda: repository

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            person_response = await client.get(
                f"/app/v2/persons/{seeded.person_id}/graph",
                params={"max_hops": 2},
            )
            generic_response = await client.get(
                "/app/v2/persons/graph/node",
                params={"element_id": seeded.generic_element_id, "max_hops": 2},
            )

        _assert_enveloped_graph_response(person_response)
        _assert_enveloped_graph_response(generic_response)
    finally:
        await driver.close()


def _live_session_factory(driver: AsyncDriver) -> Callable[[bool], AsyncSession]:
    def get_live_session(write: bool = False) -> AsyncSession:
        access_mode = "WRITE" if write else "READ"
        return driver.session(default_access_mode=access_mode)

    return get_live_session


async def _active_user() -> AuthUser:
    return AuthUser(
        email="person-graph@example.com",
        google_sub="person-graph-user",
        role="employee",
        entity_key="eko",
    )


def _mounted_frontend_app(app: FastAPI) -> FastAPI:
    for route in app.routes:
        if isinstance(route, Mount) and route.path == "/app/v2":
            mounted = route.app
            assert isinstance(mounted, FastAPI)
            return mounted
    raise AssertionError("/app/v2 mount not found")


def _assert_lifecycle_graph(person_graph: PersonGraph) -> None:
    nodes_by_role = {
        str(node.properties["fixture_role"]): node
        for node in person_graph.nodes
        if "fixture_role" in node.properties
    }
    edges_by_role = {
        str(edge.properties["fixture_role"]): edge
        for edge in person_graph.edges
        if "fixture_role" in edge.properties
    }

    assert {"root", "active", "legacy", "control"} <= set(nodes_by_role)
    assert "retired_branch" not in nodes_by_role
    assert "retired_branch" not in edges_by_role
    assert "retired_final_edge" not in edges_by_role
    assert set(edges_by_role) == {"active_link", "legacy_link", "control_link"}
    assert nodes_by_role["root"].label == "Person"
    assert nodes_by_role["active"].label == "Identifier"
    assert nodes_by_role["legacy"].label == "Address"
    assert nodes_by_role["control"].label == "GraphProbe"
    assert nodes_by_role["active"].properties["value"] == "active-identifier"
    assert nodes_by_role["legacy"].properties["value"] == "legacy-address"
    assert nodes_by_role["control"].properties["value"] == "non-repairable-control"

    active_link = edges_by_role["active_link"]
    legacy_link = edges_by_role["legacy_link"]
    control_link = edges_by_role["control_link"]
    assert active_link.properties["is_active"] is True
    assert "is_active" not in legacy_link.properties
    assert control_link.properties["is_active"] is False

    _assert_edge(
        active_link,
        edge_type="IDENTIFIED_BY",
        source=nodes_by_role["root"].id,
        target=nodes_by_role["active"].id,
    )
    _assert_edge(
        legacy_link,
        edge_type="LIVES_AT",
        source=nodes_by_role["root"].id,
        target=nodes_by_role["legacy"].id,
    )
    _assert_edge(
        control_link,
        edge_type="CHILD_OF",
        source=nodes_by_role["root"].id,
        target=nodes_by_role["control"].id,
    )


def _assert_edge(edge: GraphEdge, *, edge_type: str, source: str, target: str) -> None:
    assert edge.type == edge_type
    assert edge.source == source
    assert edge.target == target


def _assert_enveloped_graph_response(response: Response) -> None:
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    assert isinstance(payload.get("meta"), dict)
    assert isinstance(payload["meta"].get("request_id"), str)
    assert isinstance(payload.get("data"), dict)
    assert set(payload["data"]) == {"nodes", "edges"}
    _assert_lifecycle_graph(PersonGraph.model_validate(payload["data"]))
