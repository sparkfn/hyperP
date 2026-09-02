"""Neo4j 5.26 coverage for person identifier enrichment.

Set ``HYPERP_NEO4J_PERSON_IDENTIFIERS_TEST_URI`` to a disposable localhost
database to enable this test. The fixture only removes nodes from its unique
test run and refuses remote hosts.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from time import monotonic, sleep
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from neo4j import Driver, GraphDatabase
from neo4j.exceptions import ServiceUnavailable
from src.graph.mappers import map_person_identifier
from src.graph.queries.persons import COUNT_PERSON_IDENTIFIERS, GET_PERSON_IDENTIFIERS
from src.repositories.neo4j._utils import record_to_dict


@dataclass(frozen=True)
class _TestGraph:
    driver: Driver
    run_id: str


def _verify_connectivity(driver: Driver) -> None:
    deadline = monotonic() + 60
    while True:
        try:
            driver.verify_connectivity()
            return
        except ServiceUnavailable:
            if monotonic() >= deadline:
                raise
            sleep(1)


@pytest.fixture
def neo4j_driver() -> Iterator[_TestGraph]:
    uri = os.getenv("HYPERP_NEO4J_PERSON_IDENTIFIERS_TEST_URI")
    if uri is None:
        pytest.skip("disposable person identifiers Neo4j test database is not configured")
    host = urlparse(uri).hostname
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    service_host = os.getenv("HYPERP_NEO4J_PERSON_IDENTIFIERS_TEST_SERVICE_HOST")
    if service_host is not None:
        allowed_hosts.add(service_host)
    if host not in allowed_hosts:
        pytest.fail("person identifiers integration tests only accept a localhost Neo4j URI")
    password = os.getenv("HYPERP_NEO4J_PERSON_IDENTIFIERS_TEST_PASSWORD")
    if password is None:
        pytest.fail("HYPERP_NEO4J_PERSON_IDENTIFIERS_TEST_PASSWORD is required")

    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_PERSON_IDENTIFIERS_TEST_USER", "neo4j"), password),
        connection_timeout=5,
    )
    _verify_connectivity(driver)
    test_graph = _TestGraph(driver=driver, run_id=uuid4().hex)
    try:
        yield test_graph
    finally:
        with driver.session() as session:
            session.run(
                """
                MATCH (node {_person_identifiers_test_run: $test_run_id})
                DETACH DELETE node
                """,
                test_run_id=test_graph.run_id,
            ).consume()
        driver.close()


def test_identifiers_query_executes_and_preserves_provenance_variants(
    neo4j_driver: _TestGraph,
) -> None:
    """The batched query must compile and retain valid, missing, and dangling provenance."""
    run_id = neo4j_driver.run_id
    person_id = f"person-{run_id}"
    source_key = f"source-{run_id}"
    record_entity_key = f"record-entity-{run_id}"
    source_entity_key = f"source-entity-{run_id}"
    phone_one_pk = f"phone-one-{run_id}"
    phone_two_pk = f"phone-two-{run_id}"
    email_pk = f"email-{run_id}"
    dangling_pk = f"dangling-{run_id}"
    email_value = f"a-{run_id}@example.test"
    phone_value = f"phone-{run_id}-1"
    dangling_value = f"phone-{run_id}-2"
    legacy_value = f"nric-{run_id}"

    with neo4j_driver.driver.session() as session:
        session.run(
            """
            CREATE (person:Person {
              person_id: $person_id,
              _person_identifiers_test_run: $test_run_id
            })
            CREATE (source:SourceSystem {
              source_key: $source_key,
              _person_identifiers_test_run: $test_run_id
            })
            CREATE (record_entity:Entity {
              entity_key: $record_entity_key,
              display_name: 'Record entity',
              entity_type: 'company',
              country_code: 'SG',
              is_active: true,
              _person_identifiers_test_run: $test_run_id
            })
            CREATE (source_entity:Entity {
              entity_key: $source_entity_key,
              display_name: 'Source entity',
              entity_type: 'company',
              country_code: 'SG',
              is_active: true,
              _person_identifiers_test_run: $test_run_id
            })
            CREATE (source)-[:OPERATED_BY]->(source_entity)
            CREATE (phone_one:SourceRecord {
              source_record_pk: $phone_one_pk,
              source_record_id: $phone_one_pk,
              record_type: 'identity',
              link_status: 'linked',
              lifecycle_status: 'active',
              observed_at: datetime('2026-08-20T00:00:00Z'),
              ingested_at: datetime('2026-08-20T00:00:00Z'),
              _person_identifiers_test_run: $test_run_id
            })-[:FROM_SOURCE]->(source)
            CREATE (phone_two:SourceRecord {
              source_record_pk: $phone_two_pk,
              source_record_id: $phone_two_pk,
              record_type: 'identity',
              link_status: 'linked',
              lifecycle_status: 'active',
              observed_at: datetime('2026-08-20T00:01:00Z'),
              ingested_at: datetime('2026-08-20T00:01:00Z'),
              _person_identifiers_test_run: $test_run_id
            })-[:FROM_SOURCE]->(source)
            CREATE (email:SourceRecord {
              source_record_pk: $email_pk,
              source_record_id: $email_pk,
              record_type: 'identity',
              link_status: 'linked',
              lifecycle_status: 'active',
              observed_at: datetime('2026-08-20T00:02:00Z'),
              ingested_at: datetime('2026-08-20T00:02:00Z'),
              _person_identifiers_test_run: $test_run_id
            })-[:FROM_SOURCE]->(source)
            CREATE (phone_one)-[:OWNED_BY]->(record_entity)
            CREATE (phone_two)-[:OWNED_BY]->(record_entity)
            CREATE (phone_identifier:Identifier {
              identifier_type: 'phone', normalized_value: $phone_value,
              _person_identifiers_test_run: $test_run_id
            })
            CREATE (email_identifier:Identifier {
              identifier_type: 'email', normalized_value: $email_value,
              _person_identifiers_test_run: $test_run_id
            })
            CREATE (legacy_identifier:Identifier {
              identifier_type: 'nric', normalized_value: $legacy_value,
              _person_identifiers_test_run: $test_run_id
            })
            CREATE (dangling_identifier:Identifier {
              identifier_type: 'phone', normalized_value: $dangling_value,
              _person_identifiers_test_run: $test_run_id
            })
            CREATE (person)-[:IDENTIFIED_BY {
              is_active: true, is_verified: true, source_system_key: $source_key,
              source_record_pk: $phone_one_pk
            }]->(phone_identifier)
            CREATE (person)-[:IDENTIFIED_BY {
              is_active: true, is_verified: true, source_system_key: $source_key,
              source_record_pk: $phone_two_pk
            }]->(phone_identifier)
            CREATE (person)-[:IDENTIFIED_BY {
              is_active: true, is_verified: false, source_system_key: $source_key,
              source_record_pk: $email_pk
            }]->(email_identifier)
            CREATE (person)-[:IDENTIFIED_BY {
              is_active: false, is_verified: false, source_system_key: $source_key
            }]->(legacy_identifier)
            CREATE (person)-[:IDENTIFIED_BY {
              is_active: true, is_verified: false, source_system_key: $source_key,
              source_record_pk: $dangling_pk
            }]->(dangling_identifier)
            """,
            person_id=person_id,
            source_key=source_key,
            record_entity_key=record_entity_key,
            source_entity_key=source_entity_key,
            phone_one_pk=phone_one_pk,
            phone_two_pk=phone_two_pk,
            email_pk=email_pk,
            dangling_pk=dangling_pk,
            email_value=email_value,
            phone_value=phone_value,
            dangling_value=dangling_value,
            legacy_value=legacy_value,
            test_run_id=run_id,
        ).consume()
        result = session.run(GET_PERSON_IDENTIFIERS, person_id=person_id, skip=0, limit=200)
        identifiers = [
            map_person_identifier(record_to_dict(record.keys(), list(record.values())))
            for record in result
        ]
        count_record = session.run(COUNT_PERSON_IDENTIFIERS, person_id=person_id).single(
            strict=True
        )
        total = count_record["total"]
        first_page = list(session.run(GET_PERSON_IDENTIFIERS, person_id=person_id, skip=0, limit=2))
        empty_page = list(session.run(GET_PERSON_IDENTIFIERS, person_id=person_id, skip=4, limit=2))

    assert total == 4
    assert [(item.identifier_type, item.normalized_value) for item in identifiers] == [
        ("phone", phone_value),
        ("email", email_value),
        ("phone", dangling_value),
        ("nric", legacy_value),
    ]
    assert [item.is_verified for item in identifiers] == [True, False, False, False]
    assert [(record["identifier_type"], record["normalized_value"]) for record in first_page] == [
        ("phone", phone_value),
        ("email", email_value),
    ]
    assert empty_page == []

    phone_identifier, email_identifier, dangling_identifier, legacy_identifier = identifiers
    assert email_identifier.entities[0].entity_key == source_entity_key
    assert email_identifier.entities[0].source_record_count == 1
    assert set(phone_identifier.source_record_pks) == {phone_one_pk, phone_two_pk}
    assert {item.source_record_pk for item in phone_identifier.source_records} == {
        phone_one_pk,
        phone_two_pk,
    }
    assert phone_identifier.entities[0].entity_key == record_entity_key
    assert phone_identifier.entities[0].source_record_count == 2
    assert dangling_identifier.source_record_pks == [dangling_pk]
    assert dangling_identifier.source_record_ids == []
    assert dangling_identifier.source_records == []
    assert dangling_identifier.entities == []
    assert legacy_identifier.source_record_pks == []
    assert legacy_identifier.source_record_ids == []
    assert legacy_identifier.source_records == []
    assert legacy_identifier.entities == []
