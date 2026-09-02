"""Neo4j coverage for person CRM metric aggregation.

Set ``HYPERP_NEO4J_CRM_METRICS_TEST_URI`` to a disposable localhost Neo4j
instance to enable this test. It clears the graph before and after each test.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from neo4j import Driver, GraphDatabase
from src.graph.queries.crm import GET_PERSON_CRM_METRICS
from src.graph.queries.persons import COUNT_PERSON_TIMELINE, GET_PERSON_TIMELINE
from src.graph.queries.review import ACTIVATE_PENDING_REVIEW_RECORD


@dataclass(frozen=True)
class _TestGraph:
    driver: Driver
    run_id: str


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
        pytest.fail("CRM metrics integration tests only accept an explicitly configured Neo4j host")
    password = os.getenv("HYPERP_NEO4J_CRM_METRICS_TEST_PASSWORD")
    if password is None:
        pytest.fail("HYPERP_NEO4J_CRM_METRICS_TEST_PASSWORD is required")

    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_CRM_METRICS_TEST_USER", "neo4j"), password),
    )
    driver.verify_connectivity()
    test_graph = _TestGraph(driver=driver, run_id=uuid4().hex)
    try:
        yield test_graph
    finally:
        with driver.session() as session:
            session.run(
                """
                MATCH (node {_crm_metrics_test_run: $test_run_id})
                DETACH DELETE node
                """,
                test_run_id=test_graph.run_id,
            ).consume()
        driver.close()


def test_metrics_query_uses_projected_stage_with_persisted_json_payload(
    neo4j_driver: _TestGraph,
) -> None:
    """A JSON-string payload must not be dereferenced as a Cypher map."""
    raw_payload = json.dumps({"stage_id": "C2:WON"})
    with neo4j_driver.driver.session() as session:
        session.run(
            """
            CREATE (person:Person {
              person_id: 'person-1', _crm_metrics_test_run: $test_run_id
            })
            CREATE (source:SourceSystem {
              source_key: 'bitrix_chat', _crm_metrics_test_run: $test_run_id
            })
            CREATE (deal:SourceRecord {
              record_type: 'crm_deal', lifecycle_status: 'active', is_latest: true,
              observed_at: datetime('2026-07-21T00:00:00Z'),
              raw_payload: $raw_payload, crm_deal_stage_id: 'C2:WON',
              _crm_metrics_test_run: $test_run_id
            })
            CREATE (activity:SourceRecord {
              record_type: 'crm_history', lifecycle_status: 'active', is_latest: true,
              history_kind: 'email',
              event_at: datetime('2026-08-19T00:01:00Z'),
              observed_at: datetime('2026-08-19T00:00:00Z'),
              _crm_metrics_test_run: $test_run_id
            })
            CREATE (deal)-[:FROM_SOURCE]->(source)
            CREATE (activity)-[:FROM_SOURCE]->(source)
            CREATE (deal)-[:LINKED_TO {is_active: true}]->(person)
            CREATE (deal)-[:LINKED_TO {is_active: true}]->(person)
            CREATE (activity)-[:LINKED_TO {is_active: true}]->(person)
            CREATE (activity)-[:LINKED_TO {is_active: true}]->(person)
            """,
            raw_payload=raw_payload,
            test_run_id=neo4j_driver.run_id,
        ).consume()
        row = session.run(
            GET_PERSON_CRM_METRICS,
            person_id="person-1",
            as_of_at="2026-08-20T00:00:00+00:00",
        ).single(strict=True)

    assert row["deal_count"] == 1
    assert row["activity_count"] == 1
    assert row["recent_30d_deal_count"] == 1
    assert row["recent_30d_activity_count"] == 1
    assert row["deal_stage_breakdown"] == [{"stage_id": "C2:WON", "count": 1}]
    assert row["activity_kind_breakdown"] == [
        {
            "history_kind": "email",
            "count": 1,
            "last_event_at": row["last_activity_at"],
        }
    ]
    assert row["days_since_last_crm_touch"] == 0
    assert row["days_since_last_deal"] == 30
    assert row["days_since_last_activity"] == 0


def test_review_activation_does_not_materialize_knows_from_retired_declarer_link(
    neo4j_driver: _TestGraph,
) -> None:
    """Current review authority must ignore repair-retired declarer ownership."""
    with neo4j_driver.driver.session() as session:
        session.run(
            """
            CREATE (source:SourceSystem {
              source_key: 'bitrix_chat', _crm_metrics_test_run: $test_run_id
            })
            CREATE (approved:Person {
              person_id: 'approved-declarer-target', status: 'active',
              _crm_metrics_test_run: $test_run_id
            })
            CREATE (current_declarer:Person {
              person_id: 'current-declarer', status: 'active', _crm_metrics_test_run: $test_run_id
            })
            CREATE (retired_declarer:Person {
              person_id: 'retired-declarer', status: 'active', _crm_metrics_test_run: $test_run_id
            })
            CREATE (pending:SourceRecord {
              source_record_pk: 'pending-declarer', source_instance_id: 'bitrix-primary',
              source_record_id: 'deal-declarer', record_type: 'crm_deal',
              lifecycle_status: 'pending_review', is_latest: false,
              _crm_metrics_test_run: $test_run_id
            })-[:FROM_SOURCE]->(source)
            CREATE (declarer_source:SourceRecord {
              source_record_pk: 'declarer-source-pk', source_record_id: 'declarer-source',
              lifecycle_status: 'active', is_latest: true, _crm_metrics_test_run: $test_run_id
            })-[:FROM_SOURCE]->(source)
            CREATE (declarer_source)-[:LINKED_TO {is_active: true}]->(current_declarer)
            CREATE (declarer_source)-[:LINKED_TO {is_active: false, retired_at: datetime(),
              retired_by_repair_id: 'repair-310'}]->(retired_declarer)
            CREATE (decision:MatchDecision {_crm_metrics_test_run: $test_run_id})
            CREATE (review:ReviewCase {review_case_id: 'review-declarer',
              _crm_metrics_test_run: $test_run_id})-[:FOR_DECISION]->(decision)
            CREATE (decision)-[:ABOUT_LEFT]->(pending)
            CREATE (decision)-[:ABOUT_RIGHT]->(approved)
            """,
            test_run_id=neo4j_driver.run_id,
        ).consume()
        session.run(
            ACTIVATE_PENDING_REVIEW_RECORD,
            review_case_id="review-declarer",
            pending_source_record_pk="pending-declarer",
            source_system_key="bitrix_chat",
            expected_active_source_record_pk=None,
            approved_person_id="approved-declarer-target",
            observed_at="2026-09-02T00:00:00Z",
            identifiers=[],
            addresses=[],
            attributes=[],
            bankruptcy_cases=[],
            vehicle_mentions=[],
            knows_relationships=[
                {
                    "declarer_source_record_id": "declarer-source",
                    "declarer_source_system_key": "bitrix_chat",
                    "source_system_key": "bitrix_chat",
                    "relationship_label": "knows",
                    "relationship_category": "declared",
                    "status": "approved",
                    "approved_at": "2026-09-02T00:00:00Z",
                }
            ],
        ).single(strict=True)
        # Activation MERGEs this lock outside the explicitly seeded test graph.
        # Tag it immediately so the fixture's run-scoped cleanup removes every
        # node this test caused to exist without touching concurrent test data.
        session.run(
            """
            MATCH (lock:SourceRecordIdentityLock {
              source_system: 'bitrix_chat', source_instance_id: 'bitrix-primary',
              source_record_id: 'deal-declarer'
            })
            SET lock._crm_metrics_test_run = $test_run_id
            """,
            test_run_id=neo4j_driver.run_id,
        ).consume()
        rows = list(
            session.run(
                """
                MATCH (declarer:Person)-[knows:KNOWS {is_active: true}]->
                      (:Person {person_id: 'approved-declarer-target'})
                WHERE declarer.person_id IN ['current-declarer', 'retired-declarer']
                RETURN declarer.person_id AS person_id
                ORDER BY person_id
                """
            )
        )
    assert [row["person_id"] for row in rows] == ["current-declarer"]


def test_review_activation_returns_link_only_owner_of_superseded_crm_deal(
    neo4j_driver: _TestGraph,
) -> None:
    with neo4j_driver.driver.session() as session:
        session.run(
            """
            CREATE (source:SourceSystem {
              source_key: 'bitrix_chat', _crm_metrics_test_run: $test_run_id
            })
            CREATE (old_owner:Person {
              person_id: 'old-owner', status: 'active', _crm_metrics_test_run: $test_run_id
            })
            CREATE (approved:Person {
              person_id: 'approved', status: 'active', _crm_metrics_test_run: $test_run_id
            })
            CREATE (pending_prior_owner:Person {
              person_id: 'pending-prior-owner', status: 'active',
              _crm_metrics_test_run: $test_run_id
            })
            CREATE (call_prior_owner:Person {
              person_id: 'call-prior-owner', status: 'active',
              _crm_metrics_test_run: $test_run_id
            })
            CREATE (already_retired_owner:Person {
              person_id: 'already-retired-owner', status: 'active',
              _crm_metrics_test_run: $test_run_id
            })
            CREATE (old:SourceRecord {
              source_record_pk: 'old-deal', source_instance_id: 'bitrix-primary',
              source_record_id: 'deal-1',
              record_type: 'crm_deal', lifecycle_status: 'active', is_latest: true,
              _crm_metrics_test_run: $test_run_id
            })-[:FROM_SOURCE]->(source)
            CREATE (pending:SourceRecord {
              source_record_pk: 'pending-deal', source_instance_id: 'bitrix-primary',
              source_record_id: 'deal-1',
              record_type: 'crm_deal', lifecycle_status: 'pending_review',
              expected_active_source_record_pk: 'old-deal', is_latest: false,
              _crm_metrics_test_run: $test_run_id
            })-[:FROM_SOURCE]->(source)
            CREATE (foreign:SourceRecord {
              source_record_pk: 'foreign-deal', source_instance_id: 'bitrix-secondary',
              source_record_id: 'deal-1',
              record_type: 'crm_deal', lifecycle_status: 'active', is_latest: true,
              _crm_metrics_test_run: $test_run_id
            })-[:FROM_SOURCE]->(source)
            CREATE (old)-[:LINKED_TO]->(old_owner)
            CREATE (old)-[:LINKED_TO {
              is_active: false, retired_at: datetime('2026-08-01T00:00:00Z'),
              retired_by_review_case_id: 'already-retired'
            }]->(already_retired_owner)
            CREATE (pending)-[:LINKED_TO {is_active: true}]->(pending_prior_owner)
            CREATE (history:SourceRecord {
              source_record_pk: 'pending-history', record_type: 'crm_history',
              history_family: 'activity', _crm_metrics_test_run: $test_run_id
            })-[:CHILD_OF]->(pending)
            CREATE (call:SourceRecord {
              source_record_pk: 'pending-call', record_type: 'call',
              lifecycle_status: 'pending_review', is_latest: false,
              _crm_metrics_test_run: $test_run_id
            })-[:CHILD_OF]->(history)
            CREATE (call)-[:LINKED_TO {is_active: true}]->(call_prior_owner)
            CREATE (decision:MatchDecision {
              match_decision_id: 'decision-1', _crm_metrics_test_run: $test_run_id
            })
            CREATE (review:ReviewCase {
              review_case_id: 'review-1', _crm_metrics_test_run: $test_run_id
            })-[:FOR_DECISION]->(decision)
            CREATE (decision)-[:ABOUT_LEFT]->(pending)
            CREATE (decision)-[:ABOUT_RIGHT]->(approved)
            """,
            test_run_id=neo4j_driver.run_id,
        ).consume()

        row = session.run(
            ACTIVATE_PENDING_REVIEW_RECORD,
            review_case_id="review-1",
            pending_source_record_pk="pending-deal",
            source_system_key="bitrix_chat",
            expected_active_source_record_pk="old-deal",
            approved_person_id="approved",
            observed_at="2026-08-21T00:00:00Z",
            identifiers=[],
            addresses=[],
            attributes=[],
            bankruptcy_cases=[],
            vehicle_mentions=[],
            knows_relationships=[],
        ).single(strict=True)
        session.run(
            """
            MATCH (lock:SourceRecordIdentityLock {
              source_system: 'bitrix_chat', source_instance_id: 'bitrix-primary',
              source_record_id: 'deal-1'
            })
            SET lock._crm_metrics_test_run = $test_run_id
            """,
            test_run_id=neo4j_driver.run_id,
        ).consume()
        foreign = session.run(
            """
            MATCH (record:SourceRecord {source_record_pk: 'foreign-deal'})
            RETURN record.lifecycle_status AS lifecycle_status,
                   record.is_latest AS is_latest
            """
        ).single(strict=True)

        retired_links = {
            row["person_id"]: {
                "is_active": row["is_active"],
                "retired": row["retired"],
                "retired_by_review_case_id": row["retired_by_review_case_id"],
            }
            for row in session.run(
                """
                MATCH (:SourceRecord {source_record_pk: 'old-deal'})-[link:LINKED_TO]->
                      (owner:Person)
                RETURN owner.person_id AS person_id, link.is_active AS is_active,
                       link.retired_at IS NOT NULL AS retired,
                       link.retired_by_review_case_id AS retired_by_review_case_id
                """
            )
        }
        retired_pending_and_call_links = {
            row["source_record_pk"]: {
                "is_active": row["is_active"],
                "retired": row["retired"],
                "retired_by_review_case_id": row["retired_by_review_case_id"],
            }
            for row in session.run(
                """
                MATCH (record:SourceRecord)-[link:LINKED_TO]->(:Person)
                WHERE record.source_record_pk IN ['pending-deal', 'pending-call']
                  AND link.is_active = false
                RETURN record.source_record_pk AS source_record_pk, link.is_active AS is_active,
                       link.retired_at IS NOT NULL AS retired,
                       link.retired_by_review_case_id AS retired_by_review_case_id
                """
            )
        }
        old_timeline_count = session.run(COUNT_PERSON_TIMELINE, person_id="old-owner").single(
            strict=True
        )["total"]
        approved_timeline = list(
            session.run(GET_PERSON_TIMELINE, person_id="approved", skip=0, limit=10)
        )
        audit_link_count = session.run(
            """
            MATCH (:SourceRecord {source_record_pk: 'old-deal'})-[link:LINKED_TO]->
                  (:Person)
            RETURN count(link) AS total
            """
        ).single(strict=True)["total"]
        second = session.run(
            ACTIVATE_PENDING_REVIEW_RECORD,
            review_case_id="review-1",
            pending_source_record_pk="pending-deal",
            source_system_key="bitrix_chat",
            expected_active_source_record_pk="old-deal",
            approved_person_id="approved",
            observed_at="2026-08-21T00:00:00Z",
            identifiers=[],
            addresses=[],
            attributes=[],
            bankruptcy_cases=[],
            vehicle_mentions=[],
            knows_relationships=[],
        ).single()
        link_counts = session.run(
            """
            MATCH (record:SourceRecord)-[link:LINKED_TO]->(:Person)
            WHERE record.source_record_pk IN ['old-deal', 'pending-deal', 'pending-call']
            RETURN count(link) AS total, count(CASE WHEN link.is_active THEN 1 END) AS active_total
            """
        ).single(strict=True)

    assert set(row["affected_person_ids"]) == {
        "approved",
        "already-retired-owner",
        "old-owner",
        "pending-prior-owner",
    }
    assert row["old_source_record_pks"] == ["old-deal"]
    assert foreign == {"lifecycle_status": "active", "is_latest": True}
    assert retired_links == {
        "old-owner": {
            "is_active": False,
            "retired": True,
            "retired_by_review_case_id": "review-1",
        },
        "already-retired-owner": {
            "is_active": False,
            "retired": True,
            "retired_by_review_case_id": "already-retired",
        },
    }
    assert retired_pending_and_call_links == {
        "pending-deal": {
            "is_active": False,
            "retired": True,
            "retired_by_review_case_id": "review-1",
        },
        "pending-call": {
            "is_active": False,
            "retired": True,
            "retired_by_review_case_id": "review-1",
        },
    }
    assert old_timeline_count == 0
    assert all(
        item["source_record"]["source_record_pk"] != "old-deal" for item in approved_timeline
    )
    assert audit_link_count == 2
    assert second is None
    assert link_counts == {"total": 6, "active_total": 2}
