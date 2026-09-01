"""Disposable Neo4j state coverage for #311 verification evidence reads."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, BrokenBarrierError, Lock
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.crm_deal_identity_repair.inventory import collect_repair_inventory
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.crm_deal_identity_repair.verification_models import RepairVerificationCommand
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_verification import (
    CrmDealIdentityRepairVerificationRepository,
)
from src.graph.crm_deal_identity_repair_verification_run import (
    canonical_source_record_pks_json,
    classify_negative_controls,
    negative_control_query_items,
)
from src.graph.crm_deal_identity_repair_verification_secondary import (
    FrozenContextSubject,
    assert_current_context,
    expected_post_repair_context,
)
from src.graph.queries import crm_deal_identity_repair_verification as verification_queries
from src.graph.queries.crm_deal_identity_repair_ledger import (
    CREATE_CRM_DEAL_REPAIR_LEDGER_SCHEMA,
)
from test_crm_deal_identity_repair_mutation_neo4j import (
    _CONTROL as MUTATION_CONTROL,
)
from test_crm_deal_identity_repair_mutation_neo4j import (
    _SOURCE as MUTATION_SOURCE,
)
from test_crm_deal_identity_repair_mutation_neo4j import (
    _deactivate_child_contamination,
    _inventory,
    _seed_authority,
    _seed_domain,
)
from test_crm_deal_identity_repair_mutation_neo4j import (
    _repository as mutation_repository,
)

T = TypeVar("T")
_SOURCE = "verification-test-source"
_DIGEST = "sha256:" + "a" * 64


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

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        with self._driver.session() as session:
            return session.execute_write(work)


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    uri = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_URI")
    user = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_USER")
    password = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_PASSWORD")
    if uri is None or user is None or password is None:
        pytest.skip("disposable CRM repair Neo4j database is not configured")
    allowed = {"localhost", "127.0.0.1", "::1"}
    if os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_SERVICE_HOST") == "neo4j":
        allowed.add("neo4j")
    if urlparse(uri).hostname not in allowed:
        pytest.fail("verification tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    for _ in range(15):
        try:
            driver.verify_connectivity()
            break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    else:
        pytest.fail("disposable CRM repair Neo4j database did not become ready")
    try:
        _reset(driver)
        yield driver
    finally:
        _reset(driver)
        driver.close()


def _reset(driver: Driver) -> None:
    with driver.session() as session:
        session.run("MATCH (node) DETACH DELETE node").consume()
        for statement in CREATE_CRM_DEAL_REPAIR_LEDGER_SCHEMA:
            session.run(statement).consume()
        session.run(
            "CREATE CONSTRAINT source_record_pk_unique IF NOT EXISTS "
            "FOR (record:SourceRecord) REQUIRE record.source_record_pk IS UNIQUE"
        ).consume()


def _seed_negative_control(driver: Driver) -> RepairInventoryItem:
    raw_payload = json.dumps(
        {
            "ID": "2",
            "TITLE": "Clean",
            "crm_deal_identity_policy_version": "legacy",
        },
        separators=(",", ":"),
    )
    normalized_payload = json.dumps(
        {
            "attributes": {},
            "identifiers": [],
            "crm_deal_identity_policy_version": "legacy",
        },
        separators=(",", ":"),
    )
    with driver.session() as session:
        session.run(
            """
            CREATE (source:SourceSystem {source_key: 'bitrix_chat', is_active: true})
            CREATE (person:Person {person_id: 'person-negative', status: 'active'})
            CREATE (deal:SourceRecord {
              source_record_pk: 'negative-pk', source_record_id: 'bitrix-crm-deal-2',
              source_record_version: '1', source_instance_id: $source_instance_id,
              record_type: 'crm_deal', lifecycle_status: 'active', is_latest: true,
              record_hash: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
              observed_at: datetime('2026-08-01T12:00:00Z'), raw_payload: $raw_payload,
              normalized_payload: $normalized_payload
            })-[:FROM_SOURCE]->(source)
            CREATE (deal)-[:LINKED_TO {
              is_active: true, source_record_pk: 'negative-pk', proof: 'frozen'
            }]->(person)
            CREATE (identifier:Identifier {
              identifier_type: 'crm_contact_id', identifier_scope: $source_instance_id,
              source_instance_id: $source_instance_id, normalized_value: 'contact-2'
            })
            CREATE (person)-[:IDENTIFIED_BY {
              is_active: true, source_record_pk: 'negative-pk', source_system_key: 'bitrix_chat',
              proof: 'frozen'
            }]->(identifier)
            """,
            source_instance_id=_SOURCE,
            raw_payload=raw_payload,
            normalized_payload=normalized_payload,
        ).consume()
    inventory = collect_repair_inventory(cast(Neo4jClient, _Client(driver)))
    assert len(inventory.negative_controls) == 1
    assert inventory.negative_controls[0].repair_conditions == ("negative_control",)
    return inventory.negative_controls[0]


def _initialize_person_derived_state(driver: Driver, person_id: str) -> None:
    """Seed strict #311 derived-state baselines before the #309 mutation transaction."""
    with driver.session() as session:
        session.run(
            """
            MATCH (person:Person {person_id: $person_id})
            SET person.crm_deal_count = 1,
                person.analysis_input_revision = 0,
                person.profile_completeness_score = 0.0,
                person.golden_profile_version = 'v0.1.0'
            """,
            person_id=person_id,
        ).consume()


def _retirement_requirement(
    relationship_type: str,
    source_record_pk: str,
    left_source_record_pk: str,
    *,
    frozen_count: int = 1,
    frozen_active_count: int = 1,
) -> dict[str, str | int]:
    return {
        "relationship_type": relationship_type,
        "source_record_pk": source_record_pk,
        "left_source_record_pk": left_source_record_pk,
        "frozen_count": frozen_count,
        "frozen_active_count": frozen_active_count,
    }


def _classify(driver: Driver, item: RepairInventoryItem) -> tuple[str, ...]:
    with driver.session() as session:
        rows = list(
            session.run(
                verification_queries.READ_NEGATIVE_CONTROL_FULL_STATE,
                items=negative_control_query_items((item,)),
            )
        )
    return classify_negative_controls((item,), rows)


def _state(driver: Driver) -> tuple[str, ...]:
    with driver.session() as session:
        return tuple(
            sorted(
                json.dumps(row, default=str, sort_keys=True, separators=(",", ":"))
                for row in session.run(
                    "MATCH (left)-[relationship]->(right) "
                    "RETURN labels(left) AS left_labels, properties(left) AS left_properties, "
                    "type(relationship) AS relationship_type, "
                    "properties(relationship) AS relationship_properties, "
                    "labels(right) AS right_labels, properties(right) AS right_properties"
                ).data()
            )
        )


def test_negative_control_current_state_is_exact_and_read_only(neo4j_driver: Driver) -> None:
    item = _seed_negative_control(neo4j_driver)
    before = _state(neo4j_driver)
    assert _classify(neo4j_driver, item) == ("unchanged",)
    assert _state(neo4j_driver) == before


def test_run_verification_counts_accepts_300_canonical_source_pk_boundary(
    neo4j_driver: Driver,
) -> None:
    item = _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            CREATE (:CrmDealRepairRun {
              repair_id: 'repair-a', run_id: 'run-a', boundary_digest: $digest,
              inventory_digest: $digest, source_instance_id: $source_instance_id,
              control_instance_id: 'control-a', source_record_pks_json: $source_record_pks_json,
              status: 'qualified', execution_allowed: false, eligible_unit_count: 0
            })
            """,
            digest=_DIGEST,
            source_instance_id=_SOURCE,
            source_record_pks_json=canonical_source_record_pks_json((item,)),
        ).consume()
        row = session.run(
            verification_queries.READ_RUN_VERIFICATION_COUNTS,
            repair_id="repair-a",
            run_id="run-a",
            boundary_digest=_DIGEST,
            inventory_digest=_DIGEST,
            source_instance_id=_SOURCE,
            control_instance_id="control-a",
            source_record_pks_json=canonical_source_record_pks_json((item,)),
        ).single(strict=True)
    assert row is not None


@pytest.mark.parametrize(
    "mutation",
    (
        "MATCH (source:SourceRecord {source_record_pk: 'negative-pk'}) "
        'SET source.raw_payload = \'{\\"ID\\":\\"2\\",\\"TITLE\\":\\"Changed\\"}\'',
        "MATCH (source:SourceRecord {source_record_pk: 'negative-pk'}) "
        "SET source.normalized_payload = '{\\\"changed\\\":true}'",
        "MATCH (:SourceRecord {source_record_pk: 'negative-pk'})-[link:LINKED_TO]->() "
        "SET link.proof = 'changed'",
        "MATCH (deal:SourceRecord {source_record_pk: 'negative-pk'}), "
        "(person:Person {person_id: 'person-negative'}) "
        "CREATE (deal)-[:LINKED_TO {is_active: true, source_record_pk: 'negative-pk', "
        "proof: 'frozen'}]->(person)",
        "MATCH (:Person {person_id: 'person-negative'})-[projection:IDENTIFIED_BY]->() "
        "SET projection.proof = 'changed'",
        "MATCH (person:Person {person_id: 'person-negative'}), "
        "(identifier:Identifier {normalized_value: 'contact-2'}) "
        "CREATE (person)-[:IDENTIFIED_BY {is_active: true, source_record_pk: 'negative-pk', "
        "source_system_key: 'bitrix_chat', proof: 'frozen'}]->(identifier)",
    ),
)
def test_negative_control_detects_exact_payload_and_relationship_drift(
    neo4j_driver: Driver, mutation: str
) -> None:
    item = _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(mutation).consume()
    assert _classify(neo4j_driver, item) == ("drifted",)


def test_negative_control_detects_missing_source(neo4j_driver: Driver) -> None:
    item = _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (source:SourceRecord {source_record_pk: 'negative-pk'}) DETACH DELETE source"
        ).consume()
    assert _classify(neo4j_driver, item) == ("missing",)


@pytest.mark.parametrize(
    ("mutation", "expected_classification"),
    (
        (
            "MATCH (source:SourceRecord {source_record_pk: 'negative-pk'}) "
            "SET source.repair_mutation_id = 'forbidden-mutation'",
            "stamped",
        ),
        (
            "MATCH (:SourceRecord {source_record_pk: 'negative-pk'})-[link:LINKED_TO]->() "
            "SET link.retired_by_repair_mutation_id = 'forbidden-mutation'",
            "stamped",
        ),
        (
            "MATCH (source:SourceRecord {source_record_pk: 'negative-pk'}) "
            "SET source.repair_mutation_id = 'forbidden-mutation' "
            "CREATE (:CrmDealRepairMutationResult {mutation_id: 'forbidden-mutation'})",
            "stamped",
        ),
        (
            "CREATE (:CrmDealRepairVerification {source_record_pk: 'negative-pk'})",
            "unchanged",
        ),
        (
            "CREATE (:CrmDealRepairSecondaryDisposition {source_record_pk: 'negative-pk'})",
            "unchanged",
        ),
    ),
)
def test_negative_control_detects_graph_or_ledger_stamp(
    neo4j_driver: Driver, mutation: str, expected_classification: str
) -> None:
    item = _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(mutation).consume()
    assert _classify(neo4j_driver, item) == (expected_classification,)


def test_primary_query_counts_applied_review_retirement_and_forbidden_evidence(
    neo4j_driver: Driver,
) -> None:
    _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
                  (person:Person {person_id: 'person-negative'})
            CREATE (old:SourceRecord {source_record_pk: 'old-pk', record_type: 'crm_deal'})
              -[:FROM_SOURCE]->(source)
            CREATE (new:SourceRecord {source_record_pk: 'new-pk', record_type: 'crm_deal',
              repair_mutation_id: 'mutation-a', link_status: 'linked'})-[:FROM_SOURCE]->(source)
            CREATE (new)-[:LINKED_TO {is_active: true, authoritative: true,
              source_record_pk: 'new-pk', repair_mutation_id: 'mutation-a'}]->(person)
            CREATE (old)-[:LINKED_TO {is_active: false, source_record_pk: 'old-pk',
              retired_by_repair_mutation_id: 'mutation-a'}]->(person)
            CREATE (person)-[:IDENTIFIED_BY {is_active: false, source_record_pk: 'old-pk',
              retired_by_repair_mutation_id: 'mutation-a'}]->(:Identifier {identifier_type: 'phone',
              normalized_value: '+6500000000'})
            """
        ).consume()
        row = session.run(
            verification_queries.READ_PRIMARY_POSTCONDITIONS,
            new_source_record_pk="new-pk",
            mutation_id="mutation-a",
            retired_source_record_pks=["old-pk"],
            retirement_requirements=[
                _retirement_requirement("LINKED_TO", "old-pk", "old-pk"),
                _retirement_requirement("IDENTIFIED_BY", "old-pk", "old-pk"),
            ],
            closure_source_record_pks=["old-pk", "new-pk"],
        ).single(strict=True)
    assert row["active_links"] == 1
    assert row["active_any_links"] == 1
    assert row["retirement_stamp_failure_count"] == 0
    assert row["forbidden_projection_count"] == 0


@pytest.mark.parametrize(
    ("identifier_type", "normalized_value"),
    (
        ("phone", "+6500000000"),
        ("email", "person@example.test"),
        ("crm_contact_id", "bad-group@g.us"),
    ),
)
def test_primary_query_reports_forbidden_retired_projection(
    neo4j_driver: Driver,
    identifier_type: str,
    normalized_value: str,
) -> None:
    _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
                  (person:Person {person_id: 'person-negative'})
            CREATE (new:SourceRecord {source_record_pk: 'new-pk', record_type: 'crm_deal',
              repair_mutation_id: 'mutation-a'})-[:FROM_SOURCE]->(source)
            CREATE (new)-[:LINKED_TO {is_active: true, authoritative: true,
              source_record_pk: 'new-pk'}]->(person)
            CREATE (identifier:Identifier {identifier_type: $identifier_type,
              normalized_value: $normalized_value})
            CREATE (person)-[:IDENTIFIED_BY {is_active: true, source_record_pk: 'old-pk'}]
              ->(identifier)
            """,
            identifier_type=identifier_type,
            normalized_value=normalized_value,
        ).consume()
        row = session.run(
            verification_queries.READ_PRIMARY_POSTCONDITIONS,
            new_source_record_pk="new-pk",
            mutation_id="mutation-a",
            retired_source_record_pks=["old-pk"],
            retirement_requirements=[],
            closure_source_record_pks=["old-pk", "new-pk"],
        ).single(strict=True)
    assert row["forbidden_projection_count"] == 1


def test_primary_query_reports_forbidden_replacement_projection(neo4j_driver: Driver) -> None:
    _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
                  (person:Person {person_id: 'person-negative'})
            CREATE (new:SourceRecord {source_record_pk: 'new-pk', record_type: 'crm_deal',
              repair_mutation_id: 'mutation-a', link_status: 'linked'})-[:FROM_SOURCE]->(source)
            CREATE (new)-[:LINKED_TO {is_active: true, authoritative: true,
              source_record_pk: 'new-pk'}]->(person)
            CREATE (identifier:Identifier {identifier_type: 'crm_contact_id',
              normalized_value: 'forbidden@g.us'})
            CREATE (person)-[:IDENTIFIED_BY {is_active: true, source_record_pk: 'new-pk'}]
              ->(identifier)
            """
        ).consume()
        row = session.run(
            verification_queries.READ_PRIMARY_POSTCONDITIONS,
            new_source_record_pk="new-pk",
            mutation_id="mutation-a",
            retired_source_record_pks=[],
            retirement_requirements=[],
            closure_source_record_pks=["new-pk"],
        ).single(strict=True)
    assert row["forbidden_projection_count"] == 1


def test_run_graph_totals_report_unsupported_replacement_multilink(
    neo4j_driver: Driver,
) -> None:
    _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
                  (first:Person {person_id: 'person-negative'})
            CREATE (:Person {person_id: 'person-other'})
            CREATE (old:SourceRecord {source_record_pk: 'old-pk', record_type: 'crm_deal'})
              -[:FROM_SOURCE]->(source)
            CREATE (new:SourceRecord {source_record_pk: 'new-pk', record_type: 'crm_deal',
              repair_mutation_id: 'mutation-a'})-[:FROM_SOURCE]->(source)
            CREATE (old)-[:PREVIOUS_VERSION_OF]->(new)
            CREATE (new)-[:LINKED_TO {is_active: true, authoritative: true}]->(first)
            WITH new
            MATCH (second:Person {person_id: 'person-other'})
            CREATE (new)-[:LINKED_TO {is_active: true, authoritative: true}]->(second)
            """
        ).consume()
        row = session.run(
            verification_queries.READ_RUN_GRAPH_TOTALS,
            frozen_source_record_pks=["old-pk"],
        ).single(strict=True)
    assert row["active_links"] == 2
    assert row["unsupported_multi_links"] == 1


def test_primary_query_reports_review_required_invariant_failures(neo4j_driver: Driver) -> None:
    _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
                  (person:Person {person_id: 'person-negative'})
            CREATE (new:SourceRecord {source_record_pk: 'new-pk', record_type: 'crm_deal',
              repair_mutation_id: 'mutation-review', link_status: 'pending_review'})
              -[:FROM_SOURCE]->(source)
            CREATE (new)-[:LINKED_TO {is_active: true, authoritative: false,
              provisional: true, source_record_pk: 'new-pk'}]->(person)
            CREATE (person)-[:HAS_FACT {is_active: true, source_record_pk: 'new-pk'}]->(new)
            """
        ).consume()
        row = session.run(
            verification_queries.READ_PRIMARY_POSTCONDITIONS,
            new_source_record_pk="new-pk",
            mutation_id="mutation-review",
            retired_source_record_pks=[],
            retirement_requirements=[],
            closure_source_record_pks=["new-pk"],
        ).single(strict=True)
    assert row["active_links"] == 0
    assert row["active_any_links"] == 1
    assert row["active_new_evidence"] == 1
    assert row["repair_review_count"] == 0


@pytest.mark.parametrize("extra_provisional", (False, True))
def test_primary_query_exposes_extra_inactive_review_required_links(
    neo4j_driver: Driver,
    extra_provisional: bool,
) -> None:
    _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
                  (person:Person {person_id: 'person-negative'})
            CREATE (new:SourceRecord {source_record_pk: 'new-pk', record_type: 'crm_deal',
              repair_mutation_id: 'mutation-review', link_status: 'pending_review'})
              -[:FROM_SOURCE]->(source)
            CREATE (review:ReviewCase {review_case_id: 'review-review',
              repair_mutation_id: 'mutation-review'})
            CREATE (decision:MatchDecision {match_decision_id: 'decision-review',
              repair_mutation_id: 'mutation-review'})
            CREATE (review)-[:FOR_DECISION {repair_mutation_id: 'mutation-review'}]->(decision)
            CREATE (decision)-[:ABOUT_LEFT {entity_type: 'source_record',
              repair_mutation_id: 'mutation-review'}]->(new)
            CREATE (new)-[:LINKED_TO {is_active: false, provisional: true,
              source_record_pk: 'new-pk'}]->(person)
            """
        ).consume()
        session.run(
            """
            MATCH (new:SourceRecord {source_record_pk: 'new-pk'}),
                  (person:Person {person_id: 'person-negative'})
            CREATE (new)-[:LINKED_TO {is_active: false, provisional: $provisional,
              source_record_pk: 'new-pk'}]->(person)
            """,
            provisional=extra_provisional,
        ).consume()
        row = session.run(
            verification_queries.READ_PRIMARY_POSTCONDITIONS,
            new_source_record_pk="new-pk",
            mutation_id="mutation-review",
            retired_source_record_pks=[],
            retirement_requirements=[],
            closure_source_record_pks=["new-pk"],
        ).single(strict=True)
    assert row["all_links"] > row["provisional_links"] or row["provisional_links"] > 1


def test_primary_query_reports_missing_retirement_stamp_and_preserves_independent_evidence(
    neo4j_driver: Driver,
) -> None:
    _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
                  (person:Person {person_id: 'person-negative'})
            CREATE (new:SourceRecord {source_record_pk: 'new-pk', record_type: 'crm_deal',
              repair_mutation_id: 'mutation-a'})-[:FROM_SOURCE]->(source)
            CREATE (new)-[:LINKED_TO {is_active: true, authoritative: true,
              source_record_pk: 'new-pk'}]->(person)
            CREATE (old:SourceRecord {source_record_pk: 'old-pk', record_type: 'crm_deal'})
              -[:FROM_SOURCE]->(source)
            CREATE (old)-[:LINKED_TO {is_active: false, source_record_pk: 'old-pk'}]->(person)
            CREATE (identifier:Identifier {
              identifier_type: 'crm_contact_id', normalized_value: 'same'
            })
            CREATE (person)-[:IDENTIFIED_BY {is_active: false, source_record_pk: 'old-pk'}]
              ->(identifier)
            CREATE (person)-[:IDENTIFIED_BY {is_active: true, source_record_pk: 'independent-pk',
              proof: 'independent'}]->(identifier)
            """
        ).consume()
        row = session.run(
            verification_queries.READ_PRIMARY_POSTCONDITIONS,
            new_source_record_pk="new-pk",
            mutation_id="mutation-a",
            retired_source_record_pks=["old-pk"],
            retirement_requirements=[
                _retirement_requirement("LINKED_TO", "old-pk", "old-pk"),
                _retirement_requirement("IDENTIFIED_BY", "old-pk", "old-pk"),
            ],
            closure_source_record_pks=["old-pk", "new-pk"],
        ).single(strict=True)
        independent = session.run(
            """
            MATCH (:Person {person_id: 'person-negative'})-[projection:IDENTIFIED_BY {
              source_record_pk: 'independent-pk'}]->(:Identifier {normalized_value: 'same'})
            RETURN count(projection) AS count, properties(projection) AS properties
            """
        ).single(strict=True)
    assert row["retirement_stamp_failure_count"] == 2
    assert independent["count"] == 1
    assert independent["properties"] == {
        "is_active": True,
        "source_record_pk": "independent-pk",
        "proof": "independent",
    }


def test_primary_query_counts_missing_stamp_on_inactive_relationship(neo4j_driver: Driver) -> None:
    _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
                  (person:Person {person_id: 'person-negative'})
            CREATE (new:SourceRecord {source_record_pk: 'new-pk', record_type: 'crm_deal',
              repair_mutation_id: 'mutation-a', link_status: 'linked'})-[:FROM_SOURCE]->(source)
            CREATE (new)-[:LINKED_TO {is_active: true, authoritative: true,
              source_record_pk: 'new-pk'}]->(person)
            CREATE (old:SourceRecord {source_record_pk: 'old-pk', record_type: 'crm_deal'})
              -[:FROM_SOURCE]->(source)
            CREATE (old)-[:DESCRIBES_ADDRESS {is_active: false, source_record_pk: 'old-pk'}]
              ->(:Address {address_id: 'old-address'})
            """
        ).consume()
        row = session.run(
            verification_queries.READ_PRIMARY_POSTCONDITIONS,
            new_source_record_pk="new-pk",
            mutation_id="mutation-a",
            retired_source_record_pks=["old-pk"],
            retirement_requirements=[
                _retirement_requirement(
                    "DESCRIBES_ADDRESS",
                    "old-pk",
                    "old-pk",
                )
            ],
            closure_source_record_pks=["old-pk", "new-pk"],
        ).single(strict=True)
    assert row["retirement_stamp_failure_count"] == 1


def test_primary_query_accepts_frozen_inactive_relationship_with_prior_stamp(
    neo4j_driver: Driver,
) -> None:
    _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
                  (person:Person {person_id: 'person-negative'})
            CREATE (new:SourceRecord {source_record_pk: 'new-pk', record_type: 'crm_deal',
              repair_mutation_id: 'mutation-a', link_status: 'linked'})-[:FROM_SOURCE]->(source)
            CREATE (new)-[:LINKED_TO {is_active: true, authoritative: true,
              source_record_pk: 'new-pk'}]->(person)
            CREATE (old:SourceRecord {source_record_pk: 'old-pk', record_type: 'crm_deal'})
              -[:FROM_SOURCE]->(source)
            CREATE (old)-[:LINKED_TO {is_active: false, source_record_pk: 'old-pk',
              retired_by_repair_mutation_id: 'prior-mutation'}]->(person)
            """
        ).consume()
        row = session.run(
            verification_queries.READ_PRIMARY_POSTCONDITIONS,
            new_source_record_pk="new-pk",
            mutation_id="mutation-a",
            retired_source_record_pks=["old-pk"],
            retirement_requirements=[
                _retirement_requirement(
                    "LINKED_TO",
                    "old-pk",
                    "old-pk",
                    frozen_active_count=0,
                )
            ],
            closure_source_record_pks=["old-pk", "new-pk"],
        ).single(strict=True)
    assert row["retirement_stamp_failure_count"] == 0


def test_primary_query_accepts_exact_review_required_shape(neo4j_driver: Driver) -> None:
    _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
                  (person:Person {person_id: 'person-negative'})
            CREATE (new:SourceRecord {source_record_pk: 'new-pk', record_type: 'crm_deal',
              repair_mutation_id: 'mutation-review', link_status: 'pending_review'})
              -[:FROM_SOURCE]->(source)
            CREATE (decision:MatchDecision {match_decision_id: 'decision-review',
              repair_mutation_id: 'mutation-review'})
            CREATE (decision)-[:ABOUT_LEFT {entity_type: 'source_record',
              repair_mutation_id: 'mutation-review'}]->(new)
            CREATE (review:ReviewCase {review_case_id: 'review-review',
              repair_mutation_id: 'mutation-review'})
              -[:FOR_DECISION {repair_mutation_id: 'mutation-review'}]->(decision)
            CREATE (new)-[:LINKED_TO {is_active: false, provisional: true,
              authoritative: false, source_record_pk: 'new-pk'}]->(person)
            """
        ).consume()
        row = session.run(
            verification_queries.READ_PRIMARY_POSTCONDITIONS,
            new_source_record_pk="new-pk",
            mutation_id="mutation-review",
            retired_source_record_pks=[],
            retirement_requirements=[],
            closure_source_record_pks=["new-pk"],
        ).single(strict=True)
    assert row["active_any_links"] == 0
    assert row["active_new_evidence"] == 0
    assert row["repair_review_count"] == 1
    assert row["repair_decision_count"] == 1
    assert row["provisional_links"] == 1


def test_negative_control_ignores_unrelated_repaired_source_sharing_owner(
    neo4j_driver: Driver,
) -> None:
    item = _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
                  (person:Person {person_id: 'person-negative'})
            CREATE (other:SourceRecord {source_record_pk: 'unrelated-pk', record_type: 'crm_deal',
              repair_mutation_id: 'other-mutation'})-[:FROM_SOURCE]->(source)
            CREATE (other)-[:LINKED_TO {source_record_pk: 'unrelated-pk',
              retired_by_repair_mutation_id: 'other-mutation'}]->(person)
            """
        ).consume()
    assert _classify(neo4j_driver, item) == ("unchanged",)


def test_negative_control_detects_real_ledger_binding_via_unit_source_pk(
    neo4j_driver: Driver,
) -> None:
    item = _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            CREATE (run:CrmDealRepairRun {run_id: 'bound-run'})
            CREATE (unit:CrmDealRepairUnit {run_id: 'bound-run', unit_id: 'bound-unit',
              source_record_pk: 'negative-pk'})
            CREATE (result:CrmDealRepairMutationResult {run_id: 'bound-run', unit_id: 'bound-unit'})
            CREATE (verification:CrmDealRepairVerification {
              run_id: 'bound-run', unit_id: 'bound-unit'
            })
            CREATE (disposition:CrmDealRepairSecondaryDisposition {run_id: 'bound-run',
              unit_id: 'bound-unit'})
            CREATE (run)-[:HAS_REPAIR_MUTATION]->(result)
            """
        ).consume()
    assert _classify(neo4j_driver, item) == ("stamped",)


def test_negative_control_detects_stamp_in_authenticated_descendant_closure(
    neo4j_driver: Driver,
) -> None:
    item = _seed_negative_control(neo4j_driver)
    descendant_item = RepairInventoryItem(
        source_system=item.source_system,
        source_record_id=item.source_record_id,
        source_record_pk=item.source_record_pk,
        deal_id=item.deal_id,
        partition=item.partition,
        graph_fingerprint=item.graph_fingerprint,
        stored_payload_fingerprint=item.stored_payload_fingerprint,
        payload={**item.payload, "descendants": [{"source_record_pk": "negative-child"}]},
    )
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (root:SourceRecord {source_record_pk: 'negative-pk'})
            CREATE (child:SourceRecord {source_record_pk: 'negative-child',
              repair_mutation_id: 'child-mutation'})-[:CHILD_OF]->(root)
            """
        ).consume()
    assert _classify(neo4j_driver, descendant_item) == ("stamped",)


def test_secondary_context_accepts_exact_retired_depth_one_and_two_descendants(
    neo4j_driver: Driver,
) -> None:
    _seed_negative_control(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (root:SourceRecord {source_record_pk: 'negative-pk'}),
                  (owner:Person {person_id: 'person-negative'})
            CREATE (first:SourceRecord {source_record_pk: 'child-one', source_record_id: 'one',
              record_type: 'crm_history', lifecycle_status: 'active'})-[:CHILD_OF]->(root)
            CREATE (second:SourceRecord {source_record_pk: 'child-two', source_record_id: 'two',
              record_type: 'crm_history', lifecycle_status: 'active'})-[:CHILD_OF]->(first)
            CREATE (first)-[:LINKED_TO {is_active: false,
              retired_by_repair_mutation_id: 'mutation-a'}]->(owner)
            CREATE (second)-[:LINKED_TO {is_active: false,
              retired_by_repair_mutation_id: 'mutation-a'}]->(owner)
            """
        ).consume()
        rows = tuple(
            session.run(
                verification_queries.READ_SECONDARY_CONTEXT,
                source_record_pks=["negative-pk"],
            )
        )
    current = tuple(
        FrozenContextSubject(row["kind"], row["stable_id"], dict(row["evidence"])) for row in rows
    )
    frozen = (
        FrozenContextSubject(
            "descendant",
            "child-one",
            {
                "record_type": "crm_history",
                "source_record_pk": "child-one",
                "source_record_id": "one",
                "lifecycle_status": "active",
                "relationship_type": "LINKED_TO",
                "relationship_is_active": True,
                "owner_person_id": "person-negative",
            },
        ),
        FrozenContextSubject(
            "descendant",
            "child-two",
            {
                "record_type": "crm_history",
                "source_record_pk": "child-two",
                "source_record_id": "two",
                "lifecycle_status": "active",
                "relationship_type": "LINKED_TO",
                "relationship_is_active": True,
                "owner_person_id": "person-negative",
            },
        ),
    )
    assert_current_context(expected_post_repair_context(frozen, "mutation-a"), current)


def test_concurrent_exact_verification_commits_once_then_replays_read_only(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _initialize_person_derived_state(neo4j_driver, "person-a")
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    mutation_command = _seed_authority(neo4j_driver, item)
    mutation = mutation_repository(neo4j_driver).commit_atomic_mutation(mutation_command)
    assert mutation.decision == "committed"
    command = RepairVerificationCommand(
        mutation_command.unit,
        mutation_command.fence,
        item,
        MUTATION_SOURCE,
        MUTATION_CONTROL,
        "worker-a",
        "verification-claim",
    )
    barrier = Barrier(2)
    arrival_lock = Lock()
    arrivals = 0

    def _force_pending_claim_race(stage: str) -> None:
        nonlocal arrivals
        if stage != "after_bundle":
            return
        with arrival_lock:
            arrivals += 1
            if arrivals > 2:
                raise AssertionError("Neo4j retried the forced pending-claim transaction")
        try:
            barrier.wait(timeout=10)
        except BrokenBarrierError as exc:
            raise AssertionError(
                "forced pending-claim barrier did not receive both deliveries"
            ) from exc

    repository = CrmDealIdentityRepairVerificationRepository(
        cast(Neo4jClient, _Client(neo4j_driver)), failpoint=_force_pending_claim_race
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repository.verify_and_reconcile_unit(command), range(2)))
    assert arrivals == 2
    assert sorted(result.decision for result in results) == ["committed", "replayed"]
    before = _state(neo4j_driver)
    replay = repository.verify_and_reconcile_unit(command)
    assert replay.decision == "replayed"
    assert _state(neo4j_driver) == before


def test_mutation_and_verification_retire_active_depth_one_and_two_descendants(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _initialize_person_derived_state(neo4j_driver, "person-a")
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (graph_source:SourceSystem {source_key: 'bitrix_chat'}),
                  (child:SourceRecord {source_record_pk: 'child-pk'}),
                  (person:Person {person_id: 'person-a'})
            CREATE (depth_two:SourceRecord {
              source_record_pk: 'depth-two-pk', source_record_id: 'history-2',
              source_record_version: '1', source_version_key: 'depth-two-v1',
              source_instance_id: $source_instance_id, record_type: 'crm_history',
              lifecycle_status: 'active', is_latest: true,
              record_hash: 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
              raw_payload: '{}', normalized_payload: '{}'
            })-[:FROM_SOURCE]->(graph_source)
            CREATE (depth_two)-[:CHILD_OF]->(child)
            CREATE (depth_two)-[:LINKED_TO {is_active: true}]->(person)
            """,
            source_instance_id=MUTATION_SOURCE,
        ).consume()
    item, _ = _inventory(neo4j_driver)
    mutation_command = _seed_authority(neo4j_driver, item)
    mutation = mutation_repository(neo4j_driver).commit_atomic_mutation(mutation_command)
    assert mutation.decision == "committed"
    command = RepairVerificationCommand(
        mutation_command.unit,
        mutation_command.fence,
        item,
        MUTATION_SOURCE,
        MUTATION_CONTROL,
        "worker-a",
        "descendant-verification-claim",
    )
    verification = CrmDealIdentityRepairVerificationRepository(
        cast(Neo4jClient, _Client(neo4j_driver))
    ).verify_and_reconcile_unit(command)
    assert verification.decision == "committed"
    with neo4j_driver.session() as session:
        rows = tuple(
            session.run(
                """
                MATCH (source:SourceRecord)-[link:LINKED_TO]->()
                WHERE source.source_record_pk IN ['child-pk', 'depth-two-pk']
                RETURN source.source_record_pk AS source_record_pk, link.is_active AS is_active,
                  link.retired_by_repair_mutation_id AS retired_by_repair_mutation_id
                ORDER BY source_record_pk
                """
            )
        )
    assert [
        (row["source_record_pk"], row["is_active"], row["retired_by_repair_mutation_id"])
        for row in rows
    ] == [
        ("child-pk", False, mutation_command.mutation_id),
        ("depth-two-pk", False, mutation_command.mutation_id),
    ]
