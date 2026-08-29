# ruff: noqa: E501 -- executable Cypher fixtures retain their graph shape.
"""Disposable-Neo4j acceptance coverage for the #309 atomic deal mutation."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.connectors.bitrix_openlines.connector import build_crm_deal_envelope
from src.connectors.bitrix_openlines.models import CrmContact, CrmDeal
from src.crm_deal_identity_repair.execution_models import RepairFence, RepairUnit
from src.crm_deal_identity_repair.inventory import collect_repair_inventory
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.crm_deal_identity_repair.mutation_models import (
    MutationFailureStage,
    RepairMutationCommand,
)
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_mutation import (
    CrmDealIdentityRepairMutationRepository,
    RepairMutationAuthorityError,
    RepairMutationDriftError,
)
from src.graph.crm_deal_identity_repair_mutation_records import canonical_payload
from src.graph.queries.crm_deal_identity_repair_ledger import (
    CREATE_CRM_DEAL_REPAIR_LEDGER_SCHEMA,
)

T = TypeVar("T")
_DIGEST = "sha256:" + "a" * 64
_SOURCE = "repair-test-source"
_CONTROL = "repair-test-control"
_OBSERVED = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
_FAILURE_STAGES: tuple[MutationFailureStage, ...] = (
    "after_guard",
    "after_source_lock",
    "after_classification",
    "after_rollback_image",
    "after_source_record",
    "after_retirement",
    "after_decision",
    "after_staging",
    "after_ledger",
    "after_checkpoint",
    "after_outbox",
    "after_postcondition",
)


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
        pytest.fail("CRM repair mutation tests require an explicitly disposable Neo4j host")
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
        session.run(
            "CREATE CONSTRAINT source_version_key_unique IF NOT EXISTS "
            "FOR (record:SourceRecord) REQUIRE record.source_version_key IS UNIQUE"
        ).consume()
        session.run(
            "CREATE CONSTRAINT source_identity_lock_unique IF NOT EXISTS "
            "FOR (lock:SourceRecordIdentityLock) REQUIRE "
            "(lock.source_system, lock.source_instance_id, lock.source_record_id) IS UNIQUE"
        ).consume()


def _deal_payload(deal_id: str, contact_id: str) -> dict[str, object]:
    contact = CrmContact(contact_id, None, phones=("+6591234567",), kind="contact")
    return cast(
        dict[str, object],
        build_crm_deal_envelope(
            CrmDeal(
                id=deal_id,
                title=f"Deal {deal_id}",
                category_id="1",
                stage_id="NEW",
                observed_at=_OBSERVED,
                primary_contact=contact,
                contacts=(contact,),
                contact_count=1,
                has_ambiguous_contacts=False,
                raw_payload={"ID": deal_id, "TITLE": f"Deal {deal_id}"},
            ),
            "tenant-a",
            source_instance_id=_SOURCE,
        ),
    )


def _seed_domain(driver: Driver, *, independent_support: bool) -> None:
    deal = _deal_payload("1", "contact-1")
    negative = _deal_payload("2", "contact-2")
    params = {
        "source_instance_id": _SOURCE,
        "control_instance_id": _CONTROL,
        "observed_at": _OBSERVED.isoformat(),
        "deal_raw": json.dumps(deal["raw_payload"], sort_keys=True, separators=(",", ":")),
        "deal_hash": deal["record_hash"],
        "negative_raw": json.dumps(negative["raw_payload"], sort_keys=True, separators=(",", ":")),
        "negative_hash": negative["record_hash"],
    }
    with driver.session() as session:
        session.run(
            """
            CREATE (source:SourceSystem {source_key: 'bitrix_chat', is_active: true})
            CREATE (source_instance:BitrixSourceInstance {source_key: 'bitrix_chat', source_instance_id: $source_instance_id, status: 'active'})-[:INSTANCE_OF]->(source)
            CREATE (:BitrixSourceInstance {source_key: 'bitrix_chat', source_instance_id: $control_instance_id, status: 'active'})-[:INSTANCE_OF]->(source)
            CREATE (binding:BitrixExecutionSourceBinding {source_key: 'bitrix_chat', source_instance_id: $source_instance_id, control_instance_id: $control_instance_id})
            CREATE (source_instance)-[:OWNS_BITRIX_CONTROL]->(binding)
            CREATE (:BitrixDispatchControl {source_key: 'bitrix_chat', control_instance_id: $control_instance_id, blocked: true})
            CREATE (:Entity {entity_key: 'tenant-a'})
            CREATE (person:Person {person_id: 'person-a', status: 'active'})
            CREATE (other:Person {person_id: 'person-negative', status: 'active'})
            CREATE (deal:SourceRecord {source_record_pk: 'deal-pk', source_record_id: 'bitrix-crm-deal-1', source_record_version: '1', source_version_key: 'deal-v1', source_instance_id: $source_instance_id, entity_key: 'tenant-a', record_type: 'crm_deal', source_entity_type: 'deal', source_entity_id: '1', identity_policy_version: 'crm_deal_identity_v2', lifecycle_status: 'active', is_latest: true, observed_at: datetime($observed_at), record_hash: $deal_hash, raw_payload: $deal_raw, normalized_payload: '{}'})-[:FROM_SOURCE]->(source)
            CREATE (deal)-[:LINKED_TO {is_active: true, source_record_pk: 'deal-pk'}]->(person)
            CREATE (contact:Identifier {identifier_type: 'crm_contact_id', identifier_scope: $source_instance_id, source_instance_id: $source_instance_id, normalized_value: 'contact-1'})
            CREATE (person)-[:IDENTIFIED_BY {is_active: true, source_record_pk: 'deal-pk'}]->(contact)
            CREATE (phone:Identifier {identifier_type: 'phone', identifier_scope: 'global', normalized_value: '+6591234567'})
            CREATE (person)-[:IDENTIFIED_BY {is_active: true, source_record_pk: 'deal-pk'}]->(phone)
            CREATE (child:SourceRecord {source_record_pk: 'child-pk', source_record_id: 'history-1', source_record_version: '1', source_version_key: 'child-v1', source_instance_id: $source_instance_id, record_type: 'crm_history', lifecycle_status: 'active', is_latest: true, record_hash: 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc', raw_payload: '{}', normalized_payload: '{}'})-[:FROM_SOURCE]->(source)
            CREATE (child)-[:CHILD_OF]->(deal)
            CREATE (child)-[:LINKED_TO {is_active: true, source_record_pk: 'child-pk'}]->(person)
            CREATE (person)-[:HAS_FACT {is_active: true, source_record_pk: 'child-pk', attribute_name: 'note', attribute_value: 'contaminated'}]->(child)
            CREATE (negative:SourceRecord {source_record_pk: 'negative-pk', source_record_id: 'bitrix-crm-deal-2', source_record_version: '1', source_version_key: 'negative-v1', source_instance_id: $source_instance_id, entity_key: 'tenant-a', record_type: 'crm_deal', source_entity_type: 'deal', source_entity_id: '2', identity_policy_version: 'crm_deal_identity_v2', lifecycle_status: 'active', is_latest: true, observed_at: datetime($observed_at), record_hash: $negative_hash, raw_payload: $negative_raw, normalized_payload: '{}'})-[:FROM_SOURCE]->(source)
            CREATE (negative)-[:LINKED_TO {is_active: true, source_record_pk: 'negative-pk'}]->(other)
            CREATE (negative_id:Identifier {identifier_type: 'crm_contact_id', identifier_scope: $source_instance_id, source_instance_id: $source_instance_id, normalized_value: 'contact-2'})
            CREATE (other)-[:IDENTIFIED_BY {is_active: true, source_record_pk: 'negative-pk'}]->(negative_id)
            """,
            **params,
        ).consume()
        if independent_support:
            session.run(
                """
                MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
                      (person:Person {person_id: 'person-a'}),
                      (contact:Identifier {identifier_type: 'crm_contact_id', normalized_value: 'contact-1'})
                CREATE (support:SourceRecord {source_record_pk: 'contact-support-pk', source_record_id: 'bitrix-crm-contact-contact-1', source_record_version: '1', source_version_key: 'contact-support-v1', source_instance_id: $source_instance_id, record_type: 'identity', source_entity_type: 'contact', source_entity_id: 'contact-1', identity_policy_version: 'crm_contact_identity_v1', lifecycle_status: 'active', is_latest: true, observed_at: datetime($observed_at), record_hash: 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', raw_payload: '{}', normalized_payload: '{}'})-[:FROM_SOURCE]->(source)
                CREATE (support)-[:LINKED_TO {is_active: true, source_record_pk: 'contact-support-pk'}]->(person)
                CREATE (person)-[:IDENTIFIED_BY {is_active: true, source_record_pk: 'contact-support-pk'}]->(contact)
                """,
                **params,
            ).consume()


def _inventory(driver: Driver) -> tuple[RepairInventoryItem, RepairInventoryItem]:
    inventory = collect_repair_inventory(cast(Neo4jClient, _Client(driver)))
    by_pk = {item.source_record_pk: item for item in inventory.items}
    return by_pk["deal-pk"], by_pk["negative-pk"]


def _seed_authority(driver: Driver, item: RepairInventoryItem) -> RepairMutationCommand:
    unit = RepairUnit("run-a", "unit-a", 1, 0, 1, _DIGEST, item.graph_fingerprint, "allocated")
    fence = RepairFence(
        "run-a", "unit-a", "fence-a", 1, 0, 1, "worker-a", "token-a", _DIGEST, _DIGEST, "claimed"
    )
    with driver.session() as session:
        session.run(
            """
            CREATE (:CrmDealRepairRun {run_id: $run_id, boundary_digest: $boundary_digest, source_instance_id: $source_instance_id, control_instance_id: $control_instance_id, status: 'qualified', execution_allowed: false})
            CREATE (:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id, generation: 1, sequence: 0, attempt: 1, boundary_digest: $boundary_digest, inventory_fingerprint: $unit_fingerprint, state: 'allocated'})
            CREATE (:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id, fence_id: 'fence-a', generation: 1, sequence: 0, attempt: 1, owner_id: 'worker-a', token: 'token-a', boundary_digest: $boundary_digest, state: 'claimed'})
            """,
            run_id=unit.run_id,
            unit_id=unit.unit_id,
            boundary_digest=_DIGEST,
            unit_fingerprint=item.graph_fingerprint,
            source_instance_id=_SOURCE,
            control_instance_id=_CONTROL,
        ).consume()
    return RepairMutationCommand(unit, fence, item, _SOURCE, _CONTROL)


def _repository(
    driver: Driver,
    failpoint: Callable[[MutationFailureStage], None] | None = None,
) -> CrmDealIdentityRepairMutationRepository:
    return CrmDealIdentityRepairMutationRepository(
        cast(Neo4jClient, _Client(driver)), failpoint=failpoint
    )


def _graph_state(driver: Driver) -> tuple[str, ...]:
    with driver.session() as session:
        nodes = session.run(
            "MATCH (node) RETURN labels(node) AS labels, properties(node) AS properties"
        ).data()
        relationships = session.run(
            "MATCH (left)-[relationship]->(right) RETURN labels(left) AS left_labels, properties(left) AS left_properties, type(relationship) AS relationship_type, properties(relationship) AS relationship_properties, labels(right) AS right_labels, properties(right) AS right_properties"
        ).data()
    return tuple(
        sorted(
            json.dumps(row, default=str, sort_keys=True, separators=(",", ":"))
            for row in [*nodes, *relationships]
        )
    )


def _negative_state(driver: Driver) -> tuple[str, ...]:
    with driver.session() as session:
        rows = session.run(
            """
            MATCH (negative:SourceRecord {source_record_pk: 'negative-pk'})
            OPTIONAL MATCH path=(negative)-[*0..1]-(neighbor)
            RETURN labels(neighbor) AS labels, properties(neighbor) AS properties,
                   [relationship IN relationships(path) | {type: type(relationship), properties: properties(relationship)}] AS relationships
            """
        ).data()
    return tuple(
        sorted(json.dumps(row, default=str, sort_keys=True, separators=(",", ":")) for row in rows)
    )


@pytest.mark.parametrize("stage", _FAILURE_STAGES)
def test_every_injected_stage_failure_rolls_back_domain_and_ledger(
    neo4j_driver: Driver,
    stage: MutationFailureStage,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    before = _graph_state(neo4j_driver)

    def _fail(observed: MutationFailureStage) -> None:
        if observed == stage:
            raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError, match="injected failure"):
        _repository(neo4j_driver, _fail).commit_atomic_mutation(command)
    assert _graph_state(neo4j_driver) == before


def test_deterministic_commit_exact_replay_and_negative_control_precision(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    item, negative = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    negative_before = _negative_state(neo4j_driver)
    repository = _repository(neo4j_driver)
    committed = repository.commit_atomic_mutation(command)
    replay = repository.commit_atomic_mutation(command)
    assert committed.decision == "committed"
    assert replay.decision == "replayed"
    assert replay.mutation == committed.mutation
    assert replay.rollback_image == committed.rollback_image
    assert replay.checkpoint == committed.checkpoint
    assert replay.outbox_event == committed.outbox_event
    assert _negative_state(neo4j_driver) == negative_before
    with pytest.raises(ValueError, match="negative-control"):
        RepairMutationCommand(command.unit, command.fence, negative, _SOURCE, _CONTROL)
    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (person:Person {person_id: 'person-a'})
            OPTIONAL MATCH (person)-[old:IDENTIFIED_BY {source_record_pk: 'deal-pk'}]->()
            OPTIONAL MATCH (person)-[independent:IDENTIFIED_BY {source_record_pk: 'contact-support-pk'}]->()
            OPTIONAL MATCH (person)-[child_fact:HAS_FACT {source_record_pk: 'child-pk'}]->()
            MATCH (result:CrmDealRepairMutationResult {run_id: 'run-a', unit_id: 'unit-a'})
            MATCH (image:CrmDealRepairRollbackImage {rollback_image_id: result.rollback_image_id})
            RETURN collect(DISTINCT old.is_active) AS old_states,
              collect(DISTINCT independent.is_active) AS independent_states,
              collect(DISTINCT child_fact.is_active) AS child_states,
              image.payload_json AS payload_json
            """
        ).single(strict=True)
    assert row["old_states"] == [False]
    assert row["independent_states"] == [True]
    assert row["child_states"] == [False]
    payload = canonical_payload(row["payload_json"])
    pre_state = payload["payload"]
    assert isinstance(pre_state, dict)
    assert pre_state["contract_version"] == "crm_deal_identity_repair_mutation_v1"


def test_concurrent_exact_attempts_produce_one_bundle_and_one_replay(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repository.commit_atomic_mutation(command), range(2)))
    assert sorted(result.decision for result in results) == ["committed", "replayed"]
    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (result:CrmDealRepairMutationResult {run_id: 'run-a', unit_id: 'unit-a'})
            OPTIONAL MATCH (image:CrmDealRepairRollbackImage {run_id: 'run-a', unit_id: 'unit-a'})
            OPTIONAL MATCH (checkpoint:CrmDealRepairCheckpoint {run_id: 'run-a', unit_id: 'unit-a'})
            OPTIONAL MATCH (outbox:CrmDealRepairOutbox {run_id: 'run-a', unit_id: 'unit-a'})
            RETURN count(DISTINCT result) AS results, count(DISTINCT image) AS images,
              count(DISTINCT checkpoint) AS checkpoints, count(DISTINCT outbox) AS outboxes
            """
        ).single(strict=True)
    assert dict(row) == {"results": 1, "images": 1, "checkpoints": 1, "outboxes": 1}


def test_review_required_has_no_active_evidence_and_one_explicit_provisional_link(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=False)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    result = _repository(neo4j_driver).commit_atomic_mutation(command)
    assert result.mutation is not None and result.mutation.outcome == "review_required"
    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (new:SourceRecord {repair_mutation_id: $mutation_id})
            OPTIONAL MATCH (new)-[link:LINKED_TO]->(:Person)
            OPTIONAL MATCH (:Person)-[evidence:IDENTIFIED_BY|LIVES_AT|HAS_FACT]->()
            WHERE evidence.source_record_pk = new.source_record_pk AND coalesce(evidence.is_active, true)
            RETURN new.lifecycle_status AS status,
              count(DISTINCT CASE WHEN coalesce(link.is_active, true) THEN link END) AS active_links,
              count(DISTINCT CASE WHEN link.is_active = false AND link.provisional = true THEN link END) AS provisional_links,
              count(DISTINCT evidence) AS active_evidence
            """,
            mutation_id=command.mutation_id,
        ).single(strict=True)
    assert dict(row) == {
        "status": "pending_review",
        "active_links": 0,
        "provisional_links": 1,
        "active_evidence": 0,
    }


def test_drift_stale_fence_and_unblocked_dispatch_leave_no_mutation(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    drifted = RepairInventoryItem(
        source_system=item.source_system,
        source_record_id=item.source_record_id,
        source_record_pk=item.source_record_pk,
        deal_id=item.deal_id,
        partition=item.partition,
        repair_conditions=item.repair_conditions,
        graph_fingerprint="sha256:" + "b" * 64,
        stored_payload_fingerprint=item.stored_payload_fingerprint,
        payload=item.payload,
    )
    with pytest.raises(RepairMutationDriftError):
        _repository(neo4j_driver).commit_atomic_mutation(
            RepairMutationCommand(command.unit, command.fence, drifted, _SOURCE, _CONTROL)
        )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (dispatch:BitrixDispatchControl {control_instance_id: $control}) "
            "SET dispatch.blocked = false",
            control=_CONTROL,
        ).consume()
    with pytest.raises(RepairMutationAuthorityError):
        _repository(neo4j_driver).commit_atomic_mutation(command)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (dispatch:BitrixDispatchControl {control_instance_id: $control}) "
            "SET dispatch.blocked = true",
            control=_CONTROL,
        ).consume()
        session.run(
            "MATCH (fence:CrmDealRepairFence {fence_id: 'fence-a'}) SET fence.state = 'lost'"
        ).consume()
    with pytest.raises(RepairMutationAuthorityError):
        _repository(neo4j_driver).commit_atomic_mutation(command)
    with neo4j_driver.session() as session:
        count = session.run(
            "MATCH (result:CrmDealRepairMutationResult) RETURN count(result) AS count"
        ).single(strict=True)["count"]
    assert count == 0
