"""Neo4j coverage for Profile reads and the HAS_IDENTIFIER person-identifier migration.

Set ``HYPERP_NEO4J_PERSON_IDENTIFIERS_TEST_URI`` to a disposable localhost
database to enable this test. Every node carries the run tag so cleanup only
removes this test run's data.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from time import monotonic, sleep
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from neo4j import Driver, GraphDatabase
from neo4j.exceptions import ServiceUnavailable
from src.graph.mappers import map_person, map_person_identifier
from src.graph.mappers_profiles import map_profile, map_profile_detail, map_unlinked_profile
from src.graph.queries import (
    COUNT_PROFILES,
    COUNT_UNLINKED_PROFILES,
    GET_PROFILE,
    LIST_PROFILES,
    LIST_UNLINKED_PROFILES,
)
from src.graph.queries.persons import (
    COUNT_PERSON_IDENTIFIERS,
    FIND_PERSON_BY_IDENTIFIER,
    GET_PERSON_BY_ID,
    GET_PERSON_IDENTIFIERS,
)
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
        pytest.fail("profile integration tests only accept a localhost Neo4j URI")
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
                "MATCH (node {_person_identifiers_test_run: $test_run_id}) DETACH DELETE node",
                test_run_id=test_graph.run_id,
            ).consume()
        driver.close()


def _seed(graph: _TestGraph) -> None:
    run_id = graph.run_id
    nric_value = hashlib.sha256(run_id.encode()).hexdigest()
    with graph.driver.session() as session:
        session.run(
            """
            CREATE (person:Person {
              person_id: 'person-' + $run_id, status: 'active',
              preferred_full_name: 'Ada Example', updated_at: datetime(),
              _person_identifiers_test_run: $run_id
            })
            CREATE (source:SourceSystem {
              source_key: 'source-' + $run_id, _person_identifiers_test_run: $run_id
            })
            CREATE (phone:Identifier {
              identifier_type: 'phone', normalized_value: 'phone-' + $run_id,
              _person_identifiers_test_run: $run_id
            })
            CREATE (email:Identifier {
              identifier_type: 'email', normalized_value: 'email-' + $run_id + '@example.test',
              _person_identifiers_test_run: $run_id
            })
            CREATE (nric:Identifier {
              identifier_type: 'nric', normalized_value: $nric_value,
              nric_masked: 'S••••567A', _person_identifiers_test_run: $run_id
            })
            CREATE (legacy_only:Identifier {
              identifier_type: 'email', normalized_value: 'legacy-' + $run_id + '@example.test',
              _person_identifiers_test_run: $run_id
            })
            CREATE (inactive_only:Identifier {
              identifier_type: 'email',
              normalized_value: 'inactive-' + $run_id + '@example.test',
              _person_identifiers_test_run: $run_id
            })
            CREATE (record:SourceRecord {
              source_record_pk: 'sr-' + $run_id, source_record_id: 'sr-' + $run_id,
              record_type: 'identity', link_status: 'linked', lifecycle_status: 'active',
              observed_at: datetime('2026-01-01T00:00:00Z'),
              ingested_at: datetime('2026-01-01T00:00:00Z'),
              _person_identifiers_test_run: $run_id
            })-[:FROM_SOURCE]->(source)
            CREATE (record)-[:LINKED_TO {is_active: true}]->(person)
            CREATE (person)-[:HAS_IDENTIFIER {
              link_id: 'has-phone-' + $run_id, status: 'active', verified_count: 1,
              score: 0.0, golden_rank: null, computed_at: datetime()
            }]->(phone)
            CREATE (person)-[:HAS_IDENTIFIER {
              link_id: 'has-nric-' + $run_id, status: 'active', verified_count: 1,
              score: 0.0, golden_rank: null, computed_at: datetime()
            }]->(nric)
            CREATE (person)-[:IDENTIFIED_BY {
              is_active: true, is_verified: true, source_system_key: source.source_key,
              source_record_pk: record.source_record_pk
            }]->(phone)
            CREATE (person)-[:IDENTIFIED_BY {
              is_active: true, is_verified: false, source_system_key: source.source_key,
              source_record_pk: record.source_record_pk
            }]->(legacy_only)
            CREATE (person)-[:IDENTIFIED_BY {
              is_active: false, is_verified: false, source_system_key: source.source_key,
              source_record_pk: record.source_record_pk
            }]->(inactive_only)
            CREATE (record)-[:ASSERTS {
              assertion_id: 'assert-phone-' + $run_id, quality_flag: 'valid', is_verified: true
            }]->(phone)
            CREATE (open_profile:Profile {
              profile_id: 'profile-open-' + $run_id, status: 'open', evidence_version: 1,
              materialized_at: datetime('2026-01-02T00:00:00Z'),
              _person_identifiers_test_run: $run_id
            })
            CREATE (retired_profile:Profile {
              profile_id: 'profile-retired-' + $run_id, status: 'retired', evidence_version: 1,
              materialized_at: datetime('2026-01-01T00:00:00Z'),
              _person_identifiers_test_run: $run_id
            })
            CREATE (member:SourceRecord {
              source_record_pk: 'sr-member-' + $run_id, source_record_id: 'sr-member-' + $run_id,
              record_type: 'identity', link_status: 'profile', lifecycle_status: 'parked',
              observed_at: datetime('2026-01-02T00:00:00Z'),
              ingested_at: datetime('2026-01-02T00:00:00Z'),
              _person_identifiers_test_run: $run_id
            })-[:FROM_SOURCE]->(source)
            CREATE (member)-[:IN_PROFILE]->(open_profile)
            CREATE (member)-[:ASSERTS {
              assertion_id: 'assert-name-' + $run_id, quality_flag: 'valid', is_verified: true
            }]->(:ProfileFact {
              attribute_name: 'full_name', attribute_value: 'Ada Example',
              _person_identifiers_test_run: $run_id
            })
            CREATE (open_profile)-[:HAS_IDENTIFIER {
              link_id: 'profile-phone-' + $run_id, status: 'active', verified_count: 1,
              score: 0.0, computed_at: datetime()
            }]->(phone)
            CREATE (member)-[:ASSERTS {
              assertion_id: 'assert-profile-phone-' + $run_id,
              quality_flag: 'valid', is_verified: true
            }]->(phone)
            CREATE (promoted_profile:Profile {
              profile_id: 'profile-promoted-' + $run_id, status: 'promoted',
              evidence_version: 2, promoted_person_id: 'person-' + $run_id,
              materialized_at: datetime('2026-01-03T00:00:00Z'),
              _person_identifiers_test_run: $run_id
            })
            CREATE (promoted_member:SourceRecord {
              source_record_pk: 'sr-promoted-' + $run_id,
              source_record_id: 'sr-promoted-' + $run_id,
              record_type: 'identity', link_status: 'linked', lifecycle_status: 'active',
              observed_at: datetime('2026-01-03T00:00:00Z'),
              ingested_at: datetime('2026-01-03T00:00:00Z'),
              _person_identifiers_test_run: $run_id
            })-[:FROM_SOURCE]->(source)
            CREATE (promoted_member)-[:IN_PROFILE]->(promoted_profile)
            """,
            run_id=run_id,
            nric_value=nric_value,
        ).consume()


def _rows(graph: _TestGraph, query: str, **parameters: object) -> list[dict[str, object]]:
    with graph.driver.session() as session:
        result = session.run(query, **parameters)
        return [record_to_dict(r.keys(), list(r.values())) for r in result]


def test_person_identifiers_prefer_has_and_keep_legacy_fallback(
    neo4j_driver: _TestGraph,
) -> None:
    _seed(neo4j_driver)
    run_id = neo4j_driver.run_id
    person_id = f"person-{run_id}"

    rows = _rows(neo4j_driver, GET_PERSON_IDENTIFIERS, person_id=person_id, skip=0, limit=20)
    identifiers = [map_person_identifier(row) for row in rows]
    by_value = {item.normalized_value: item for item in identifiers}

    phone = by_value[f"phone-{run_id}"]
    assert phone.identifier_type == "phone"
    assert phone.score == 0.0
    assert phone.golden_rank is None
    assert phone.source_record_pks == [f"sr-{run_id}"]
    assert len([item for item in identifiers if item.normalized_value == f"phone-{run_id}"]) == 1

    assert phone.source_system_key == f"source-{run_id}"

    legacy_only = by_value[f"legacy-{run_id}@example.test"]
    assert legacy_only.score is None
    assert legacy_only.golden_rank is None
    assert legacy_only.source_system_key == f"source-{run_id}"

    inactive_only = by_value[f"inactive-{run_id}@example.test"]
    assert inactive_only.is_active is False
    assert inactive_only.score is None
    assert inactive_only.source_system_key == f"source-{run_id}"

    nric = by_value["S••••567A"]
    assert nric.identifier_type == "nric"
    assert nric.score == 0.0

    count = _rows(neo4j_driver, COUNT_PERSON_IDENTIFIERS, person_id=person_id)[0]["total"]
    assert count == 4


def test_profile_list_defaults_open_and_q_excludes_government_ids(
    neo4j_driver: _TestGraph,
) -> None:
    _seed(neo4j_driver)
    run_id = neo4j_driver.run_id

    open_rows = _rows(neo4j_driver, LIST_PROFILES, q=None, status="open", skip=0, limit=20)
    assert [map_profile(row).profile_id for row in open_rows] == [f"profile-open-{run_id}"]
    total = _rows(neo4j_driver, COUNT_PROFILES, q=None, status="open")[0]["total"]
    assert total == 1

    by_name = _rows(neo4j_driver, LIST_PROFILES, q="ada example", status="open", skip=0, limit=20)
    assert len(by_name) == 1

    by_identifier = _rows(
        neo4j_driver, LIST_PROFILES, q=f"phone-{run_id}", status="open", skip=0, limit=20
    )
    assert len(by_identifier) == 1

    by_nric = _rows(
        neo4j_driver,
        LIST_PROFILES,
        q=hashlib.sha256(run_id.encode()).hexdigest()[:16],
        status="open",
        skip=0,
        limit=20,
    )
    assert by_nric == []


def test_profile_detail_returns_members_and_masked_identifiers(
    neo4j_driver: _TestGraph,
) -> None:
    _seed(neo4j_driver)
    run_id = neo4j_driver.run_id

    rows = _rows(neo4j_driver, GET_PROFILE, profile_id=f"profile-open-{run_id}")
    detail = map_profile_detail(rows[0])

    assert detail.profile_id == f"profile-open-{run_id}"
    assert detail.status == "open"
    assert detail.record_count == 1
    assert detail.identifier_count == 1
    assert [record.source_record_pk for record in detail.source_records] == [f"sr-member-{run_id}"]
    assert detail.source_records[0].lifecycle_status == "parked"
    assert detail.identifiers[0].normalized_value == f"phone-{run_id}"
    assert detail.identifiers[0].score == 0.0

    promoted_rows = _rows(neo4j_driver, GET_PROFILE, profile_id=f"profile-promoted-{run_id}")
    promoted = map_profile_detail(promoted_rows[0])
    assert promoted.promoted_person_id == f"person-{run_id}"
    assert promoted.source_records[0].linked_person_id == f"person-{run_id}"


def test_person_lookup_and_detail_are_unchanged_with_profiles_present(
    neo4j_driver: _TestGraph,
) -> None:
    _seed(neo4j_driver)
    run_id = neo4j_driver.run_id
    person_id = f"person-{run_id}"

    found = _rows(
        neo4j_driver,
        FIND_PERSON_BY_IDENTIFIER,
        identifier_type="phone",
        values=[f"phone-{run_id}"],
    )
    assert [row["person"]["person_id"] for row in found] == [person_id]

    detail = _rows(neo4j_driver, GET_PERSON_BY_ID, person_id=person_id)[0]
    assert detail["person"]["person_id"] == person_id
    assert detail["source_record_count"] == 1


def _seed_shared_has_identifier(graph: _TestGraph) -> tuple[str, str, str]:
    run_id = graph.run_id
    left_id = f"has-left-{run_id}"
    right_id = f"has-right-{run_id}"
    value = f"shared-phone-{run_id}"
    with graph.driver.session() as session:
        session.run(
            """
            CREATE (left:Person {
              person_id: $left_id, status: 'active', _person_identifiers_test_run: $run_id
            })
            CREATE (right:Person {
              person_id: $right_id, status: 'active', _person_identifiers_test_run: $run_id
            })
            CREATE (identifier:Identifier {
              identifier_type: 'phone', identifier_scope: 'global',
              normalized_value: $value, _person_identifiers_test_run: $run_id
            })
            CREATE (left)-[:HAS_IDENTIFIER {
              link_id: 'has-left-' + $run_id, status: 'active', verified_count: 1,
              computed_at: datetime()
            }]->(identifier)
            CREATE (right)-[:HAS_IDENTIFIER {
              link_id: 'has-right-' + $run_id, status: 'active', verified_count: 1,
              computed_at: datetime()
            }]->(identifier)
            """,
            run_id=run_id,
            left_id=left_id,
            right_id=right_id,
            value=value,
        ).consume()
    return left_id, right_id, value


def test_find_person_by_identifier_counts_has_only_connections(
    neo4j_driver: _TestGraph,
) -> None:
    left_id, right_id, value = _seed_shared_has_identifier(neo4j_driver)

    rows = _rows(
        neo4j_driver,
        FIND_PERSON_BY_IDENTIFIER,
        identifier_type="phone",
        values=[value],
    )

    connection_counts = {row["person"]["person_id"]: row["connection_count"] for row in rows}
    assert connection_counts == {left_id: 1, right_id: 1}


def _seed_unlinked_worklist(graph: _TestGraph) -> None:
    run_id = graph.run_id
    with graph.driver.session() as session:
        session.run(
            """
            CREATE (rich:Profile {
              profile_id: 'profile-rich-' + $run_id, status: 'open', evidence_version: 1,
              materialized_at: datetime('2026-01-01T00:00:00Z'),
              _person_identifiers_test_run: $run_id
            })
            CREATE (plain:Profile {
              profile_id: 'profile-plain-' + $run_id, status: 'open', evidence_version: 1,
              materialized_at: datetime('2026-01-02T00:00:00Z'),
              _person_identifiers_test_run: $run_id
            })
            CREATE (closed:Profile {
              profile_id: 'profile-closed-' + $run_id, status: 'promoted', evidence_version: 1,
              materialized_at: datetime('2026-01-03T00:00:00Z'),
              _person_identifiers_test_run: $run_id
            })
            CREATE (deal:SourceRecord {
              source_record_pk: 'sr-deal-' + $run_id, record_type: 'crm_deal',
              link_status: 'profile', lifecycle_status: 'parked',
              _person_identifiers_test_run: $run_id
            })-[:IN_PROFILE]->(rich)
            CREATE (identity:SourceRecord {
              source_record_pk: 'sr-identity-' + $run_id, record_type: 'identity',
              link_status: 'profile', lifecycle_status: 'parked',
              _person_identifiers_test_run: $run_id
            })-[:IN_PROFILE]->(rich)
            CREATE (solo:SourceRecord {
              source_record_pk: 'sr-solo-' + $run_id, record_type: 'identity',
              link_status: 'profile', lifecycle_status: 'parked',
              _person_identifiers_test_run: $run_id
            })-[:IN_PROFILE]->(plain)
            CREATE (old_deal:SourceRecord {
              source_record_pk: 'sr-old-deal-' + $run_id, record_type: 'crm_deal',
              link_status: 'profile', lifecycle_status: 'parked',
              _person_identifiers_test_run: $run_id
            })-[:IN_PROFILE]->(closed)
            """,
            run_id=run_id,
        ).consume()


def test_list_unlinked_profiles_orders_by_value_score(neo4j_driver: _TestGraph) -> None:
    _seed_unlinked_worklist(neo4j_driver)
    run_id = neo4j_driver.run_id

    rows = _rows(neo4j_driver, LIST_UNLINKED_PROFILES, skip=0, limit=20)
    profiles = [map_unlinked_profile(row) for row in rows]

    assert [profile.profile_id for profile in profiles] == [
        f"profile-rich-{run_id}",
        f"profile-plain-{run_id}",
    ]
    assert [profile.value_score for profile in profiles] == [1, 0]
    assert [profile.record_count for profile in profiles] == [2, 1]
    total = _rows(neo4j_driver, COUNT_UNLINKED_PROFILES)[0]["total"]
    assert total == 2

    first_page = _rows(neo4j_driver, LIST_UNLINKED_PROFILES, skip=0, limit=1)
    assert [row["profile"]["profile_id"] for row in first_page] == [f"profile-rich-{run_id}"]


def _seed_golden_person(graph: _TestGraph, golden_json: str, golden_conflicts: str) -> str:
    run_id = graph.run_id
    person_id = f"person-golden-{run_id}"
    with graph.driver.session() as session:
        session.run(
            """
            CREATE (person:Person {
              person_id: $person_id, status: 'active',
              preferred_phone: '+6590000001', preferred_email: 'user@example.com',
              golden_json: $golden_json, golden_conflicts: $golden_conflicts,
              _person_identifiers_test_run: $run_id
            })
            """,
            person_id=person_id,
            golden_json=golden_json,
            golden_conflicts=golden_conflicts,
            run_id=run_id,
        ).consume()
    return person_id


def _golden_json() -> str:
    return json.dumps(
        {
            "email": [
                {
                    "value": "user@example.com",
                    "rank": 1,
                    "score": 0.9,
                    "verified": True,
                    "last_confirmed_at": "2026-02-01T00:00:00Z",
                    "source_count": 2,
                }
            ],
            "phone": [
                {
                    "value": "+6590000001",
                    "rank": 1,
                    "score": 0.95,
                    "verified": False,
                    "last_confirmed_at": None,
                    "source_count": 3,
                },
                {
                    "value": "+6590000002",
                    "rank": 2,
                    "score": 0.6,
                    "verified": True,
                    "last_confirmed_at": "2026-03-01T00:00:00Z",
                    "source_count": 1,
                },
            ],
        },
        sort_keys=True,
    )


def test_person_detail_golden_top_matches_rank_one_preferred_values(
    neo4j_driver: _TestGraph,
) -> None:
    person_id = _seed_golden_person(neo4j_driver, _golden_json(), "[]")

    rows = _rows(neo4j_driver, GET_PERSON_BY_ID, person_id=person_id)
    person = map_person(rows[0])

    assert person.golden_top is not None
    assert person.golden_top.phone[0].value == person.preferred_phone == "+6590000001"
    assert person.golden_top.phone[1].value == "+6590000002"
    assert person.golden_top.phone[1].rank == 2
    assert person.golden_top.email[0].value == person.preferred_email == "user@example.com"
    assert person.golden_top.conflicts == []


def test_person_detail_golden_top_is_none_when_json_is_malformed(
    neo4j_driver: _TestGraph,
) -> None:
    person_id = _seed_golden_person(neo4j_driver, "{not-json", "[]")

    rows = _rows(neo4j_driver, GET_PERSON_BY_ID, person_id=person_id)
    person = map_person(rows[0])

    assert person.golden_top is None
