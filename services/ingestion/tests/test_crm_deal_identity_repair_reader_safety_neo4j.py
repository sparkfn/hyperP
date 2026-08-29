"""Opt-in disposable Neo4j proof for #310 active-reader/audit-reader separation."""

from __future__ import annotations

import ast
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.graph.queries.crm_deal_identity_repair import INVENTORY_ACTIVE_CRM_DEALS
from src.graph.queries.knows import RESOLVE_KNOWS_ENDPOINTS
from src.graph.queries.matching import FIND_CANDIDATES_BY_IDENTIFIER

_MARKER = "issue-310-reader-safety"
_SOURCE_KEY = "reader-safety-bitrix"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
T = TypeVar("T")


def _api_query_constant(relative_path: str, query_name: str) -> str:
    query_file = _REPOSITORY_ROOT / relative_path
    module = ast.parse(query_file.read_text(encoding="utf-8-sig"))
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not isinstance(statement.value, ast.Constant):
            continue
        if not isinstance(statement.value.value, str):
            continue
        for target in statement.targets:
            if isinstance(target, ast.Name) and target.id == query_name:
                return statement.value.value
    raise AssertionError(f"API query constant {query_name} was not found")


class _Client:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._driver.session() as session:
            yield session

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        with self._driver.session() as session:
            return session.execute_read(work)


def _clear(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (node) WHERE node.reader_safety_test = $marker DETACH DELETE node",
            marker=_MARKER,
        ).consume()


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    uri = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_URI")
    user = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_USER")
    password = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_PASSWORD")
    if uri is None or user is None or password is None:
        pytest.skip("disposable #310 reader-safety Neo4j database is not configured")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_SERVICE_HOST") == "neo4j":
        allowed_hosts.add("neo4j")
    if urlparse(uri).hostname not in allowed_hosts:
        pytest.fail("#310 reader-safety test requires an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
        _clear(driver)
        yield driver
    finally:
        _clear(driver)
        driver.close()


def _seed(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            """
            CREATE (source:SourceSystem {source_key: $source_key, reader_safety_test: $marker})
            CREATE (active:Person {person_id: 'reader-safety-active', status: 'active',
                                   reader_safety_test: $marker})
            CREATE (retired:Person {person_id: 'reader-safety-retired', status: 'active',
                                    reader_safety_test: $marker})
            CREATE (contact:Person {person_id: 'reader-safety-contact', status: 'active',
                                    reader_safety_test: $marker})
            CREATE (deal:SourceRecord {source_record_pk: 'reader-safety-deal',
                                       source_record_id: 'reader-safety-deal',
                                       record_type: 'crm_deal', reader_safety_test: $marker})
            CREATE (declarer:SourceRecord {source_record_pk: 'reader-safety-declarer',
                                           reader_safety_test: $marker})
            CREATE (contact_record:SourceRecord {source_record_pk: 'reader-safety-contact-record',
                                                 reader_safety_test: $marker})
            CREATE (identifier:Identifier {identifier_type: 'phone', identifier_scope: 'global',
                                           normalized_value: '+15550000001',
                                           reader_safety_test: $marker})
            CREATE (active_order:Order {order_id: 'reader-safety-active-order',
                                        reader_safety_test: $marker})
            CREATE (retired_order:Order {order_id: 'reader-safety-retired-order',
                                         reader_safety_test: $marker})
            CREATE (active_entity:Entity {entity_key: 'reader-safety-active-entity',
                                          reader_safety_test: $marker})
            CREATE (retired_entity:Entity {entity_key: 'reader-safety-retired-entity',
                                           reader_safety_test: $marker})
            CREATE (auth_active:SourceRecord {source_record_pk: 'reader-safety-auth-active',
                                              reader_safety_test: $marker})
            CREATE (auth_retired:SourceRecord {source_record_pk: 'reader-safety-auth-retired',
                                               reader_safety_test: $marker})
            CREATE (decision:MatchDecision {match_decision_id: 'reader-safety-decision',
                                            reader_safety_test: $marker})
            CREATE (review:ReviewCase {review_case_id: 'reader-safety-review',
                                       reader_safety_test: $marker})
            CREATE (deal)-[:FROM_SOURCE]->(source)
            CREATE (deal)-[:LINKED_TO {is_active: true, reader_safety_test: $marker}]->(active)
            CREATE (deal)-[:LINKED_TO {
              is_active: false, retired_at: datetime(),
              retirement_reason: 'crm_deal_identity_repair', repair_run_id: 'reader-safety-run',
              reader_safety_test: $marker
            }]->(retired)
            CREATE (auth_active)-[:FROM_SOURCE]->(source)
            CREATE (auth_active)-[:OWNED_BY]->(active_entity)
            CREATE (auth_active)-[:LINKED_TO {is_active: true, reader_safety_test: $marker}]
                   ->(active)
            CREATE (auth_retired)-[:FROM_SOURCE]->(source)
            CREATE (auth_retired)-[:OWNED_BY]->(retired_entity)
            CREATE (auth_retired)-[:LINKED_TO {
              is_active: false, retired_at: datetime(),
              retirement_reason: 'crm_deal_identity_repair', reader_safety_test: $marker
            }]->(retired)
            CREATE (review)-[:FOR_DECISION]->(decision)
            CREATE (decision)-[:ABOUT_LEFT]->(deal)
            CREATE (declarer)-[:LINKED_TO {is_active: true, reader_safety_test: $marker}]->(active)
            CREATE (declarer)-[:LINKED_TO {
              is_active: false, retired_at: datetime(), repair_run_id: 'reader-safety-run',
              reader_safety_test: $marker
            }]->(retired)
            CREATE (contact_record)-[:LINKED_TO {
              is_active: true, reader_safety_test: $marker
            }]->(contact)
            CREATE (active)-[:IDENTIFIED_BY {is_active: true, quality_flag: 'valid',
                                             reader_safety_test: $marker}]->(identifier)
            CREATE (retired)-[:IDENTIFIED_BY {
              is_active: false, quality_flag: 'valid', retired_at: datetime(),
              retirement_reason: 'crm_deal_identity_repair', reader_safety_test: $marker
            }]->(identifier)
            CREATE (active)-[:PURCHASED {is_active: true, reader_safety_test: $marker}]
                   ->(active_order)
            CREATE (active)-[:PURCHASED {
              is_active: false, retired_at: datetime(),
              retirement_reason: 'crm_deal_identity_repair', reader_safety_test: $marker
            }]->(retired_order)
            """,
            source_key=_SOURCE_KEY,
            marker=_MARKER,
        ).consume()


def test_current_readers_exclude_retired_links_while_audit_inventory_retains_them(
    neo4j_driver: Driver,
) -> None:
    _seed(neo4j_driver)
    client = _Client(neo4j_driver)

    def resolve_endpoints(tx: ManagedTransaction) -> list[dict[str, object]]:
        return [
            dict(record)
            for record in tx.run(
                RESOLVE_KNOWS_ENDPOINTS,
                declarer_source_record_pk="reader-safety-declarer",
                contact_source_record_pk="reader-safety-contact-record",
            )
        ]

    def matching_candidates(tx: ManagedTransaction) -> list[str]:
        return [
            str(record["person_id"])
            for record in tx.run(
                FIND_CANDIDATES_BY_IDENTIFIER,
                identifier_type="phone",
                identifier_scope="global",
                normalized_value="+15550000001",
            )
        ]

    def inventory(tx: ManagedTransaction) -> list[dict[str, object]]:
        return [
            dict(record)
            for record in tx.run(INVENTORY_ACTIVE_CRM_DEALS, source_system=_SOURCE_KEY)
        ]

    def api_sales(tx: ManagedTransaction) -> list[str]:
        return [
            str(record["order_id"])
            for record in tx.run(
                _api_query_constant(
                    "services/api/src/graph/queries/sales.py",
                    "GET_PERSON_SALES",
                ),
                person_id="reader-safety-active",
                skip=0,
                limit=10,
            )
        ]

    def review_entity_keys(tx: ManagedTransaction) -> list[str]:
        record = tx.run(
            _api_query_constant(
                "services/api/src/graph/queries/users.py",
                "GET_ENTITIES_FOR_REVIEW_CASE",
            ),
            review_case_id="reader-safety-review",
        ).single()
        if record is None:
            return []
        return [str(entity_key) for entity_key in record["entity_keys"]]

    endpoints = client.execute_read(resolve_endpoints)
    candidates = client.execute_read(matching_candidates)
    inventories = client.execute_read(inventory)
    sales = client.execute_read(api_sales)
    authorized_entities = client.execute_read(review_entity_keys)

    assert endpoints == [
        {
            "declarer_person_id": "reader-safety-active",
            "contact_person_id": "reader-safety-contact",
        }
    ]
    assert candidates == ["reader-safety-active"]
    assert sales == ["reader-safety-active-order"]
    assert authorized_entities == ["reader-safety-active-entity"]
    assert len(inventories) == 1
    links = inventories[0]["linked_people"]
    assert isinstance(links, list)
    assert {str(link["person_id"]) for link in links if isinstance(link, dict)} == {
        "reader-safety-active", "reader-safety-retired"
    }
    retired = next(
        link for link in links
        if isinstance(link, dict) and link["person_id"] == "reader-safety-retired"
    )
    assert retired["is_active"] is False
    properties = retired["relationship_properties"]
    assert isinstance(properties, dict)
    assert properties["retirement_reason"] == "crm_deal_identity_repair"
    assert properties["repair_run_id"] == "reader-safety-run"

    with neo4j_driver.session() as session:
        remaining = session.run(
            "MATCH (node) WHERE node.reader_safety_test = $marker RETURN count(node) AS count",
            marker=_MARKER,
        ).single()
    assert remaining is not None
    assert int(remaining["count"]) == 16
