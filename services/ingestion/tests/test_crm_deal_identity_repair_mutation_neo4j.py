# ruff: noqa: E501 -- executable Cypher fixtures retain their graph shape.
"""Disposable-Neo4j acceptance coverage for the #309 atomic deal mutation."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier, Event
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
    build_inventory_binding_digest,
)
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_mutation import (
    CrmDealIdentityRepairMutationRepository,
    RepairMutationAuthorityError,
    RepairMutationDriftError,
)
from src.graph.crm_deal_identity_repair_mutation_authority import _authority_evidence
from src.graph.crm_deal_identity_repair_mutation_records import canonical_payload
from src.graph.queries.crm_deal_identity_repair_ledger import (
    CREATE_CRM_DEAL_REPAIR_LEDGER_SCHEMA,
)
from src.models import (
    EngineType,
    MatchDecision,
    MatchResult,
    RawIdentifier,
    RecordType,
    SourceRecordEnvelope,
    SourceRecordLifecycleStatus,
)
from src.pipeline_normalization import (
    normalize_envelope_addresses,
    normalize_envelope_attributes,
    normalize_envelope_identifiers,
)
from src.pipeline_writes import persist_source_record
from src.record_lifecycle import (
    DuplicateVersion,
    activate_staged_version,
    load_locked_source_state,
    plan_incoming_version,
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


def _deal_payload(
    deal_id: str,
    contact_id: str,
    *,
    full_name: str | None = None,
) -> dict[str, object]:
    contact = CrmContact(contact_id, full_name, phones=("+6591234567",), kind="contact")
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


def _seed_domain(
    driver: Driver,
    *,
    independent_support: bool,
    deal_full_name: str | None = None,
) -> None:
    deal = _deal_payload("1", "contact-1", full_name=deal_full_name)
    negative = _deal_payload("2", "contact-2")
    deal_attributes = deal["attributes"]
    deal_identifiers = deal["identifiers"]
    assert isinstance(deal_attributes, dict)
    assert isinstance(deal_identifiers, list)
    params = {
        "source_instance_id": _SOURCE,
        "control_instance_id": _CONTROL,
        "observed_at": _OBSERVED.isoformat(),
        "deal_raw": json.dumps(deal["raw_payload"], sort_keys=True, separators=(",", ":")),
        "deal_normalized": json.dumps(
            {"attributes": deal_attributes, "identifiers": deal_identifiers},
            sort_keys=True,
            separators=(",", ":"),
        ),
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
            CREATE (deal:SourceRecord {source_record_pk: 'deal-pk', source_record_id: 'bitrix-crm-deal-1', source_record_version: '1', source_version_key: 'deal-v1', source_instance_id: $source_instance_id, entity_key: 'tenant-a', record_type: 'crm_deal', source_entity_type: 'deal', source_entity_id: '1', identity_policy_version: 'crm_deal_identity_v2', lifecycle_status: 'active', is_latest: true, observed_at: datetime($observed_at), record_hash: $deal_hash, raw_payload: $deal_raw, normalized_payload: $deal_normalized})-[:FROM_SOURCE]->(source)
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
                CREATE (:StandaloneCrmCensus {census_id: 'census-a', generation: 1, source_key: 'bitrix_chat', source_instance_id: $source_instance_id})
                CREATE (:StandaloneCrmCensusFence {census_id: 'census-a', generation: 1, stream_kind: 'contacts', token: 'source-fence', owner_id: 'source-worker'})
                CREATE (:StandaloneCrmChildPublication {census_id: 'census-a', generation: 1, stream_kind: 'contacts', task_name: 'source-task', task_id: 'source-task-id', payload_digest: $support_digest, status: 'published'})
                CREATE (:StandaloneCrmHttpCallReservation {intent_id: 'call-intent', census_id: 'census-a', generation: 1, stream_kind: 'contacts', fence_token: 'source-fence', task_id: 'source-task-id', status: 'succeeded'})
                CREATE (:StandaloneCrmSourceFactPageReceipt {receipt_key: 'support-receipt', status: 'committed', census_id: 'census-a', generation: 1, stream_kind: 'contacts', fence_token: 'source-fence', fence_owner_id: 'source-worker', source_key: 'bitrix_chat', source_instance_id: $source_instance_id, control_instance_id: $control_instance_id, task_name: 'source-task', task_id: 'source-task-id', payload_digest: $support_digest, call_intent_id: 'call-intent', authorization_id: 'authorization', authorization_digest: $support_digest, available_at: datetime($observed_at), availability_contract_version: 'v1', frozen_upper_id: 10})
                CREATE (support:SourceRecord {source_record_pk: 'contact-support-pk', source_record_id: 'bitrix-crm-contact-contact-1', source_record_version: '1', source_version_key: 'contact-support-v1', source_instance_id: $source_instance_id, record_type: 'identity', source_entity_type: 'contact', source_entity_id: 'contact-1', identity_policy_version: 'crm_contact_identity_v1', lifecycle_status: 'active', is_latest: true, observed_at: datetime($observed_at), record_hash: 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', raw_payload: '{}', normalized_payload: '{}', standalone_crm_available_at: datetime($observed_at), standalone_crm_census_id: 'census-a', standalone_crm_stream_kind: 'contacts', standalone_crm_generation: 1, standalone_crm_fence_token: 'source-fence', standalone_crm_fence_owner_id: 'source-worker', standalone_crm_task_name: 'source-task', standalone_crm_task_id: 'source-task-id', standalone_crm_payload_digest: $support_digest, standalone_crm_call_intent_id: 'call-intent', standalone_crm_authorization_id: 'authorization', standalone_crm_authorization_digest: $support_digest, standalone_crm_availability_contract_version: 'v1', standalone_crm_frozen_upper_id: 10, standalone_crm_control_instance_id: $control_instance_id})-[:FROM_SOURCE]->(source)
                CREATE (support)-[:LINKED_TO {is_active: true, source_record_pk: 'contact-support-pk'}]->(person)
                CREATE (person)-[:IDENTIFIED_BY {is_active: true, source_record_pk: 'contact-support-pk'}]->(contact)
                """,
                **params,
                support_digest=_DIGEST,
            ).consume()


def _deactivate_child_contamination(driver: Driver) -> None:
    """Remove seeded self-support only when an automatic-authority test needs it."""
    with driver.session() as session:
        session.run(
            "MATCH (:SourceRecord {source_record_pk: 'child-pk'})-[link:LINKED_TO]->() "
            "SET link.is_active = false"
        ).consume()
        session.run(
            "MATCH (:Person)-[fact:HAS_FACT {source_record_pk: 'child-pk'}]->() "
            "SET fact.is_active = false"
        ).consume()


def _inventory(driver: Driver) -> tuple[RepairInventoryItem, RepairInventoryItem]:
    inventory = collect_repair_inventory(cast(Neo4jClient, _Client(driver)))
    by_pk = {item.source_record_pk: item for item in inventory.items}
    return by_pk["deal-pk"], by_pk["negative-pk"]


def _seed_authority(
    driver: Driver, item: RepairInventoryItem, *, run_id: str = "run-a"
) -> RepairMutationCommand:
    unit = RepairUnit(
        run_id,
        "unit-a",
        1,
        0,
        1,
        _DIGEST,
        item.graph_fingerprint,
        "allocated",
        item.inventory_key,
        item.source_record_pk,
        item.graph_fingerprint,
        item.stored_payload_fingerprint,
        build_inventory_binding_digest(item),
    )
    fence = RepairFence(
        run_id, "unit-a", "fence-a", 1, 0, 1, "worker-a", "token-a", _DIGEST, _DIGEST, "claimed"
    )
    command = RepairMutationCommand(unit, fence, item, _SOURCE, _CONTROL)
    with driver.session() as session:
        session.run(
            """
            CREATE (:CrmDealRepairRun {run_id: $run_id, boundary_digest: $boundary_digest, source_instance_id: $source_instance_id, control_instance_id: $control_instance_id, rollback_authority_reference: $rollback_authority_reference, rollback_authority_policy: $rollback_authority_policy, status: 'qualified', execution_allowed: false, source_record_pks_json: $source_record_pks_json})
            CREATE (:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id, generation: 1, sequence: 0, attempt: 1, boundary_digest: $boundary_digest, inventory_fingerprint: $unit_fingerprint, inventory_key: $inventory_key, source_record_pk: $source_record_pk, inventory_graph_fingerprint: $inventory_graph_fingerprint, inventory_stored_payload_fingerprint: $inventory_stored_payload_fingerprint, inventory_binding_digest: $inventory_binding_digest, state: 'allocated'})
            CREATE (:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id, fence_id: 'fence-a', generation: 1, sequence: 0, attempt: 1, owner_id: 'worker-a', token: 'token-a', boundary_digest: $boundary_digest, fence_fingerprint: $fence_fingerprint, state: 'claimed'})
            """,
            run_id=unit.run_id,
            unit_id=unit.unit_id,
            boundary_digest=_DIGEST,
            unit_fingerprint=item.graph_fingerprint,
            source_instance_id=_SOURCE,
            control_instance_id=_CONTROL,
            source_record_pks_json=json.dumps([item.source_record_pk], separators=(",", ":")),
            inventory_key=item.inventory_key,
            source_record_pk=item.source_record_pk,
            inventory_graph_fingerprint=item.graph_fingerprint,
            inventory_stored_payload_fingerprint=item.stored_payload_fingerprint,
            inventory_binding_digest=command.inventory_binding_digest,
            rollback_authority_reference="reviewed-312",
            rollback_authority_policy="reviewed_rollback_v1",
            fence_fingerprint=fence.fence_fingerprint,
        ).consume()
    return command


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


_DYNAMIC_TRANSACTION_KEYS = {
    "ingested_at",
    "activated_at",
    "review_staged_at",
    "created_at",
    "updated_at",
    "linked_at",
    "first_seen_at",
    "last_seen_at",
    "last_confirmed_at",
}


def _rollback_dynamic(value: object, key: str | None = None) -> object:
    if key in _DYNAMIC_TRANSACTION_KEYS:
        return {"dynamic": "transaction_datetime"}
    if isinstance(value, dict):
        return {item_key: _rollback_dynamic(item, item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_rollback_dynamic(item) for item in value]
    if hasattr(value, "iso_format"):
        formatted = value.iso_format()
        if isinstance(formatted, str):
            return _normalized_iso(formatted)
    if isinstance(value, str):
        return _normalized_iso(value)
    return value


def _normalized_iso(value: str) -> str:
    if "T" not in value:
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value


def _assert_structural_readback(
    driver: Driver,
    mutation_id: str,
    specifications: list[object],
    node_kinds: set[str],
    relationship_kinds: set[str],
) -> None:
    expected_nodes = [
        item
        for item in specifications
        if isinstance(item, dict) and item.get("object_kind") in node_kinds
    ]
    expected_relationships = [
        item
        for item in specifications
        if isinstance(item, dict) and item.get("object_kind") in relationship_kinds
    ]
    with driver.session() as session:
        row = session.run(
            """
            CALL {
              MATCH (node {repair_mutation_id: $mutation_id})
              WHERE node:SourceRecord OR node:MatchDecision OR node:ReviewCase
              RETURN collect({
                object_kind: CASE WHEN node:SourceRecord THEN 'SourceRecord'
                  WHEN node:MatchDecision THEN 'MatchDecision' ELSE 'ReviewCase' END,
                identity: CASE WHEN node:SourceRecord THEN {source_record_pk: node.source_record_pk}
                  WHEN node:MatchDecision THEN {match_decision_id: node.match_decision_id}
                  ELSE {review_case_id: node.review_case_id} END,
                properties: properties(node), preexisting: false, write_mode: 'created',
                multiplicity_ordinal: 0
              }) AS nodes
            }
            CALL {
              MATCH (left)-[relationship]->(right)
              WHERE relationship.repair_mutation_id = $mutation_id
                AND type(relationship) IN $relationship_kinds
              RETURN collect({
                object_kind: type(relationship), direction: 'outgoing',
                left_endpoint: CASE WHEN left:SourceRecord THEN {source_record_pk: left.source_record_pk}
                  WHEN left:MatchDecision THEN {match_decision_id: left.match_decision_id}
                  WHEN left:ReviewCase THEN {review_case_id: left.review_case_id}
                  WHEN left:Person THEN {person_id: left.person_id}
                  ELSE {entity_key: left.entity_key} END,
                right_endpoint: CASE WHEN right:SourceRecord THEN {source_record_pk: right.source_record_pk}
                  WHEN right:MatchDecision THEN {match_decision_id: right.match_decision_id}
                  WHEN right:ReviewCase THEN {review_case_id: right.review_case_id}
                  WHEN right:Person THEN {person_id: right.person_id}
                  WHEN right:SourceSystem THEN {source_key: right.source_key}
                  ELSE {entity_key: right.entity_key} END,
                properties: properties(relationship), preexisting: false, write_mode: 'created',
                multiplicity_ordinal: 0
              }) AS relationships
            }
            RETURN nodes, relationships
            """,
            mutation_id=mutation_id,
            relationship_kinds=sorted(relationship_kinds),
        ).single(strict=True)
    actual_nodes = [_rollback_dynamic(item) for item in row["nodes"]]
    actual_relationships = [_rollback_dynamic(item) for item in row["relationships"]]
    assert _canonical_rows(actual_nodes) == _canonical_rows(expected_nodes)
    assert _canonical_rows(actual_relationships) == _canonical_rows(expected_relationships)


def _canonical_rows(rows: list[object]) -> list[str]:
    return sorted(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows)


def _rollback_specification_key(item: dict[str, object]) -> str:
    object_kind = item.get("object_kind")
    if not isinstance(object_kind, str):
        raise AssertionError("rollback object kind is absent")
    if object_kind == "Identifier":
        identity = item.get("identity")
        return "Identifier:" + json.dumps(identity, sort_keys=True, separators=(",", ":"))
    if object_kind == "IDENTIFIED_BY":
        endpoint = item.get("right_endpoint")
        return "IDENTIFIED_BY:" + json.dumps(endpoint, sort_keys=True, separators=(",", ":"))
    if object_kind == "HAS_FACT":
        properties = item.get("properties")
        if not isinstance(properties, dict):
            raise AssertionError("rollback fact properties are absent")
        identity = {
            "left_endpoint": item.get("left_endpoint"),
            "right_endpoint": item.get("right_endpoint"),
            "attribute_name": properties.get("attribute_name"),
            "attribute_value": properties.get("attribute_value"),
        }
        return "HAS_FACT:" + json.dumps(identity, sort_keys=True, separators=(",", ":"))
    raise AssertionError("unexpected rollback object kind")


def _with_rollback_specification_metadata(
    actual: dict[str, object], expected_by_key: dict[str, list[dict[str, object]]]
) -> dict[str, object]:
    matching = expected_by_key.get(_rollback_specification_key(actual))
    if not matching:
        raise AssertionError("actual mutation object has no rollback specification")
    expected = matching.pop(0)
    return {
        **actual,
        "preexisting": expected["preexisting"],
        "write_mode": expected["write_mode"],
        "multiplicity_ordinal": expected["multiplicity_ordinal"],
    }


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
    _deactivate_child_contamination(neo4j_driver)
    item, negative = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    negative_before = _negative_state(neo4j_driver)
    repository = _repository(neo4j_driver)
    committed = repository.commit_atomic_mutation(command)
    replay = repository.commit_atomic_mutation(command)
    assert committed.decision == "committed"
    assert committed.mutation is not None and committed.mutation.outcome == "applied"
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


def test_exact_replay_rejects_repaired_desired_state_drift(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    committed = repository.commit_atomic_mutation(command)
    assert committed.mutation is not None and committed.mutation.outcome == "applied"
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (:SourceRecord {repair_mutation_id: $mutation_id})"
            "-[link:LINKED_TO]->(:Person) SET link.is_active = false",
            mutation_id=command.mutation_id,
        ).consume()
    with pytest.raises(RepairMutationDriftError, match="desired state changed"):
        repository.commit_atomic_mutation(command)


def test_exact_replay_rejects_retargeted_authoritative_link_with_same_cardinality(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    committed = repository.commit_atomic_mutation(command)
    assert committed.mutation is not None and committed.mutation.outcome == "applied"
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (new:SourceRecord {repair_mutation_id: $mutation_id})-[link:LINKED_TO]->()
            MATCH (other:Person {person_id: 'person-negative'})
            WITH new, link, other, properties(link) AS link_properties
            DELETE link
            CREATE (new)-[replacement:LINKED_TO]->(other)
            SET replacement = link_properties
            """,
            mutation_id=command.mutation_id,
        ).consume()
    with pytest.raises(RepairMutationDriftError, match="desired state changed"):
        repository.commit_atomic_mutation(command)


def test_exact_replay_rejects_replaced_identifier_projection_with_same_count(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    committed = repository.commit_atomic_mutation(command)
    assert committed.mutation is not None and committed.mutation.outcome == "applied"
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (person:Person {person_id: 'person-a'})
                  -[projection:IDENTIFIED_BY {repair_mutation_id: $mutation_id}]->()
            WITH person, projection, properties(projection) AS projection_properties
            LIMIT 1
            CREATE (replacement_identifier:Identifier {
              identifier_type: 'phone', identifier_scope: 'global',
              normalized_value: '+6599999999'
            })
            CREATE (person)-[replacement:IDENTIFIED_BY]->(replacement_identifier)
            SET replacement = projection_properties
            DELETE projection
            """,
            mutation_id=command.mutation_id,
        ).consume()
    with pytest.raises(RepairMutationDriftError, match="desired state changed"):
        repository.commit_atomic_mutation(command)


def test_exact_replay_rejects_removed_review_case_decision_chain(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=False)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    committed = repository.commit_atomic_mutation(command)
    assert committed.mutation is not None and committed.mutation.outcome == "review_required"
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (:ReviewCase {repair_mutation_id: $mutation_id})"
            "-[relationship:FOR_DECISION]->(:MatchDecision) DELETE relationship",
            mutation_id=command.mutation_id,
        ).consume()
    with pytest.raises(RepairMutationDriftError, match="desired state changed"):
        repository.commit_atomic_mutation(command)


def test_exact_replay_rejects_removed_previous_version_provenance(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    committed = repository.commit_atomic_mutation(command)
    assert committed.mutation is not None and committed.mutation.outcome == "applied"
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (:SourceRecord {source_record_pk: $old_source_record_pk})"
            "-[relationship:PREVIOUS_VERSION_OF]->"
            "(:SourceRecord {repair_mutation_id: $mutation_id}) DELETE relationship",
            old_source_record_pk=command.inventory.source_record_pk,
            mutation_id=command.mutation_id,
        ).consume()
    with pytest.raises(RepairMutationDriftError, match="desired state changed"):
        repository.commit_atomic_mutation(command)


def test_concurrent_exact_attempts_produce_one_bundle_and_one_replay(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repository.commit_atomic_mutation(command), range(2)))
    assert sorted(result.decision for result in results) == ["committed", "replayed"]
    assert all(
        result.mutation is not None and result.mutation.outcome == "applied" for result in results
    )
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


def test_malformed_qualified_payload_stages_safe_review_without_v2_fabrication(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (deal:SourceRecord {source_record_pk: 'deal-pk'}) "
            "SET deal.raw_payload = '{malformed'"
        ).consume()
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    result = _repository(neo4j_driver).commit_atomic_mutation(command)
    assert result.mutation is not None
    assert result.mutation.outcome == "review_required"
    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (new:SourceRecord {repair_mutation_id: $mutation_id})
            OPTIONAL MATCH (new)-[link:LINKED_TO]->(:Person)
            OPTIONAL MATCH (:Person)-[evidence:IDENTIFIED_BY|LIVES_AT|HAS_FACT]->()
            WHERE evidence.source_record_pk = new.source_record_pk
              AND coalesce(evidence.is_active, true)
            RETURN new.raw_payload AS raw_payload,
              new.repair_reconstruction_status AS reconstruction_status,
              count(CASE WHEN link IS NOT NULL AND coalesce(link.is_active, true) THEN 1 END)
                AS active_links,
              count(evidence) AS active_evidence
            """,
            mutation_id=command.mutation_id,
        ).single(strict=True)
    assert dict(row) == {
        "raw_payload": "{malformed",
        "reconstruction_status": "unreconstructable_review_only",
        "active_links": 0,
        "active_evidence": 0,
    }


def test_review_required_with_no_current_candidate_has_no_provisional_link(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=False)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (:SourceRecord {source_record_pk: 'deal-pk'})-[link:LINKED_TO]->() DELETE link"
        ).consume()
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    result = _repository(neo4j_driver).commit_atomic_mutation(command)
    assert result.mutation is not None
    assert result.mutation.outcome == "review_required"
    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (new:SourceRecord {repair_mutation_id: $mutation_id})
            OPTIONAL MATCH (new)-[link:LINKED_TO]->(:Person)
            RETURN count(CASE WHEN link IS NOT NULL AND coalesce(link.is_active, true) THEN 1 END)
                AS active_links,
              count(CASE WHEN link IS NOT NULL AND link.is_active = false
                AND link.provisional = true THEN 1 END) AS provisional_links
            """,
            mutation_id=command.mutation_id,
        ).single(strict=True)
    assert dict(row) == {"active_links": 0, "provisional_links": 0}


def test_control_lineage_mismatch_falls_to_review_while_complete_lineage_applies(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (support:SourceRecord {source_record_pk: 'contact-support-pk'}) "
            "SET support.standalone_crm_control_instance_id = 'wrong-control'"
        ).consume()
    item, _ = _inventory(neo4j_driver)
    result = _repository(neo4j_driver).commit_atomic_mutation(_seed_authority(neo4j_driver, item))
    assert result.mutation is not None and result.mutation.outcome == "review_required"

    _reset(neo4j_driver)
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    result = _repository(neo4j_driver).commit_atomic_mutation(_seed_authority(neo4j_driver, item))
    assert result.mutation is not None and result.mutation.outcome == "applied"


def test_deactivated_descendant_link_is_not_self_supporting_authority(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    with neo4j_driver.session() as session:
        evidence = session.execute_write(lambda tx: _authority_evidence(tx, command, ("person-a",)))
    assert {item.provenance_class for item in evidence} == {"independent_trusted"}


def test_review_required_replay_rejects_authority_drift(neo4j_driver: Driver) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    committed = repository.commit_atomic_mutation(command)
    assert committed.mutation is not None and committed.mutation.outcome == "review_required"
    replayed = repository.commit_atomic_mutation(command)
    assert replayed.decision == "replayed"
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (support:SourceRecord {source_record_pk: 'contact-support-pk'}) "
            "SET support.standalone_crm_authorization_digest = $digest",
            digest="sha256:" + "c" * 64,
        ).consume()
    with pytest.raises(RepairMutationDriftError, match="authority changed"):
        repository.commit_atomic_mutation(command)


@pytest.mark.parametrize("evidence_kind", ["historical", "self_supporting"])
def test_replay_rejects_unrelated_disqualifying_authority_evidence(
    neo4j_driver: Driver,
    evidence_kind: str,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    committed = repository.commit_atomic_mutation(command)
    assert committed.mutation is not None and committed.mutation.outcome == "applied"
    with neo4j_driver.session() as session:
        if evidence_kind == "historical":
            session.run(
                """
                MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
                      (person:Person {person_id: 'person-a'})
                CREATE (historical:SourceRecord {
                  source_record_pk: 'unrelated-historical-pk',
                  source_record_id: 'bitrix-crm-deal-unrelated', source_instance_id: $source,
                  record_type: 'crm_deal', lifecycle_status: 'active', is_latest: true
                })-[:FROM_SOURCE]->(source)
                CREATE (historical)-[:LINKED_TO {is_active: true}]->(person)
                """,
                source=_SOURCE,
            ).consume()
        else:
            session.run(
                """
                MATCH (replacement:SourceRecord {repair_mutation_id: $mutation_id}),
                      (source:SourceSystem {source_key: 'bitrix_chat'}),
                      (person:Person {person_id: 'person-a'})
                CREATE (child:SourceRecord {
                  source_record_pk: 'unrelated-self-pk', source_record_id: 'unrelated-history',
                  source_instance_id: $source, record_type: 'crm_history',
                  lifecycle_status: 'active', is_latest: true
                })-[:FROM_SOURCE]->(source)
                CREATE (child)-[:CHILD_OF]->(replacement)
                CREATE (child)-[:LINKED_TO {is_active: true}]->(person)
                """,
                mutation_id=command.mutation_id,
                source=_SOURCE,
            ).consume()
    with pytest.raises(RepairMutationDriftError, match="authority changed"):
        repository.commit_atomic_mutation(command)


def test_external_reviewed_v2_authority_applies_replays_and_detects_drift(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=False)
    _deactivate_child_contamination(neo4j_driver)
    reviewed_payload = _deal_payload("reviewed-v2", "contact-reviewed")
    reviewed_raw = reviewed_payload["raw_payload"]
    reviewed_attributes = reviewed_payload["attributes"]
    reviewed_identifiers = reviewed_payload["identifiers"]
    reviewed_hash = reviewed_payload["record_hash"]
    assert isinstance(reviewed_raw, dict)
    assert isinstance(reviewed_attributes, dict)
    assert isinstance(reviewed_identifiers, list)
    assert isinstance(reviewed_hash, str)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
                  (person:Person {person_id: 'person-a'})
            CREATE (reviewed:SourceRecord {
              source_record_pk: 'reviewed-v2-pk', source_record_id: 'bitrix-crm-deal-reviewed-v2',
              source_record_version: '1', source_version_key: 'reviewed-v2-key',
              source_instance_id: $source_instance_id, entity_key: 'tenant-a', record_type: 'crm_deal',
              source_entity_type: 'deal', source_entity_id: 'reviewed-v2',
              identity_policy_version: 'crm_deal_identity_v2',
              identity_link_key: 'bitrix:repair-test-source:deal:reviewed-v2',
              lifecycle_status: 'superseded', is_latest: false, record_hash: $record_hash,
              observed_at: datetime($observed_at), raw_payload: $raw_payload,
              normalized_payload: $normalized_payload
            })-[:FROM_SOURCE]->(source)
            CREATE (decision:MatchDecision {
              match_decision_id: 'reviewed-v2-decision', engine_type: 'deterministic',
              decision: 'merge', policy_version: 'crm_deal_identity_v2'
            })-[:ABOUT_LEFT {entity_type: 'source_record'}]->(reviewed)
            CREATE (decision)-[:ABOUT_RIGHT {entity_type: 'person'}]->(person)
            CREATE (review:ReviewCase {
              review_case_id: 'reviewed-v2-case', resolution: 'approved'
            })-[:FOR_DECISION]->(decision)
            """,
            source_instance_id=_SOURCE,
            observed_at=_OBSERVED.isoformat(),
            record_hash=reviewed_hash,
            raw_payload=json.dumps(reviewed_raw, sort_keys=True, separators=(",", ":")),
            normalized_payload=json.dumps(
                {"attributes": reviewed_attributes, "identifiers": reviewed_identifiers},
                sort_keys=True,
                separators=(",", ":"),
            ),
        ).consume()
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    committed = repository.commit_atomic_mutation(command)
    assert committed.mutation is not None and committed.mutation.outcome == "applied"
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (reviewed:SourceRecord {source_record_pk: 'reviewed-v2-pk'}) "
            "OPTIONAL MATCH (lock:SourceRecordIdentityLock {source_record_id: reviewed.source_record_id}) "
            "RETURN reviewed.lifecycle_status AS lifecycle_status, reviewed.is_latest AS is_latest, "
            "count(lock) AS lock_count"
        ).single(strict=True)
    assert dict(row) == {
        "lifecycle_status": "superseded",
        "is_latest": False,
        "lock_count": 1,
    }
    assert repository.commit_atomic_mutation(command).decision == "replayed"
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (review:ReviewCase {review_case_id: 'reviewed-v2-case'}) "
            "SET review.resolution = 'rejected'"
        ).consume()
    with pytest.raises(RepairMutationDriftError, match="authority changed"):
        repository.commit_atomic_mutation(command)


def test_active_no_match_lock_forces_review_required(neo4j_driver: Driver) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (person:Person {person_id: 'person-a'}), "
            "(other:Person {person_id: 'person-negative'}) "
            "CREATE (person)-[:NO_MATCH_LOCK {lock_id: 'active-lock'}]->(other)"
        ).consume()
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    result = repository.commit_atomic_mutation(command)
    assert result.mutation is not None and result.mutation.outcome == "review_required"
    assert repository.commit_atomic_mutation(command).decision == "replayed"
    with neo4j_driver.session() as session:
        session.run(
            "MATCH ()-[lock:NO_MATCH_LOCK {lock_id: 'active-lock'}]->() DELETE lock"
        ).consume()
    with pytest.raises(RepairMutationDriftError, match="authority changed"):
        repository.commit_atomic_mutation(command)


def test_lane_a_lineage_mismatch_forces_review_and_post_commit_drift_is_rejected(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (support:SourceRecord {source_record_pk: 'contact-support-pk'}) "
            "SET support.standalone_crm_task_id = 'mismatched-task'"
        ).consume()
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    rejected = _repository(neo4j_driver).commit_atomic_mutation(command)
    assert rejected.mutation is not None
    assert rejected.mutation.outcome == "review_required"

    _reset(neo4j_driver)
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    committed = repository.commit_atomic_mutation(command)
    assert committed.mutation is not None
    assert committed.mutation.outcome == "applied"
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (support:SourceRecord {source_record_pk: 'contact-support-pk'}) "
            "SET support.standalone_crm_authorization_digest = $digest",
            digest="sha256:" + "b" * 64,
        ).consume()
    with pytest.raises(RepairMutationDriftError, match="authority changed"):
        repository.commit_atomic_mutation(command)


def test_rollback_image_readback_tracks_identifier_preexistence_and_source_schema(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True, deal_full_name="Ada Lovelace")
    _deactivate_child_contamination(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (deal:SourceRecord {source_record_pk: 'deal-pk'}), "
            "(entity:Entity {entity_key: 'tenant-a'}) "
            "SET deal.link_status = 'linked' CREATE (deal)-[:OWNED_BY]->(entity)"
        ).consume()
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    result = _repository(neo4j_driver).commit_atomic_mutation(command)
    assert result.rollback_image is not None
    assert result.mutation is not None
    assert result.mutation.outcome == "applied"
    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (result:CrmDealRepairMutationResult {mutation_id: $mutation_id})
            MATCH (image:CrmDealRepairRollbackImage {rollback_image_id: result.rollback_image_id})
            MATCH (new:SourceRecord {source_record_pk: result.new_source_record_pk})
            OPTIONAL MATCH (new)-[:OWNED_BY]->(entity:Entity {entity_key: 'tenant-a'})
            RETURN image.payload_json AS payload_json, new.link_status AS link_status,
              toString(new.observed_at) AS observed_at, count(entity) AS owned_by_count
            """,
            mutation_id=command.mutation_id,
        ).single(strict=True)
    payload = canonical_payload(row["payload_json"])
    payload_body = payload["payload"]
    assert isinstance(payload_body, dict)
    pre_state = payload_body["pre_state"]
    rollback_operations = payload_body["rollback_operations"]
    assert isinstance(pre_state, dict)
    assert isinstance(rollback_operations, list)
    candidates = pre_state["created_identifier_candidates"]
    assert isinstance(candidates, list)
    assert all(
        isinstance(candidate, dict) and "preexisting" in candidate for candidate in candidates
    )
    assert json.dumps(payload, sort_keys=True, separators=(",", ":")) == row["payload_json"]
    delete_specifications = [
        operation
        for operation in rollback_operations
        if isinstance(operation, dict)
        and operation.get("operation") == "delete_created_nodes_and_identifiers"
        and operation.get("delete_identifier_only_when_preexisting_is_false") is True
    ]
    assert len(delete_specifications) == 1
    assert delete_specifications[0]["identifier_candidates"] == candidates
    specifications = payload_body["created_object_specifications"]
    assert isinstance(specifications, list)
    specification_kinds = {
        item.get("object_kind") for item in specifications if isinstance(item, dict)
    }
    assert {
        "SourceRecord",
        "MatchDecision",
        "FROM_SOURCE",
        "PREVIOUS_VERSION_OF",
        "ABOUT_LEFT",
        "ABOUT_RIGHT",
        "LINKED_TO",
        "OWNED_BY",
        "Identifier",
        "IDENTIFIED_BY",
        "HAS_FACT",
    } <= specification_kinds
    structural_specs = {
        item["object_kind"]: item
        for item in specifications
        if isinstance(item, dict)
        and item.get("object_kind") in {"SourceRecord", "MatchDecision", "LINKED_TO"}
    }
    assert structural_specs["SourceRecord"]["write_mode"] == "created"
    assert structural_specs["MatchDecision"]["properties"]["decision"] == "merge"
    assert structural_specs["LINKED_TO"]["properties"] == {
        "is_active": True,
        "provisional": False,
        "authoritative": True,
        "source_record_pk": structural_specs["SourceRecord"]["identity"]["source_record_pk"],
        "repair_mutation_id": command.mutation_id,
        "linked_at": {"dynamic": "transaction_datetime"},
    }
    _assert_structural_readback(
        neo4j_driver,
        command.mutation_id,
        specifications,
        {"SourceRecord", "MatchDecision"},
        {
            "FROM_SOURCE",
            "PREVIOUS_VERSION_OF",
            "ABOUT_LEFT",
            "ABOUT_RIGHT",
            "LINKED_TO",
            "OWNED_BY",
        },
    )
    with neo4j_driver.session() as session:
        actual = session.run(
            """
            MATCH (new:SourceRecord {repair_mutation_id: $mutation_id})
            CALL {
              WITH new
              MATCH (person)-[link:IDENTIFIED_BY {repair_mutation_id: $mutation_id}]->(identifier)
              RETURN collect({object_kind: 'IDENTIFIED_BY', direction: 'outgoing',
                left_endpoint: {person_id: person.person_id}, right_endpoint: {
                  identifier_type: identifier.identifier_type, identifier_scope: identifier.identifier_scope,
                  normalized_value: identifier.normalized_value}, properties: properties(link)}) AS links
            }
            CALL {
              WITH new
              MATCH (person)-[:IDENTIFIED_BY {repair_mutation_id: $mutation_id}]->(identifier)
              RETURN collect({object_kind: 'Identifier', identity: {
                identifier_type: identifier.identifier_type, identifier_scope: identifier.identifier_scope,
                normalized_value: identifier.normalized_value}, on_create_properties: CASE
                  WHEN identifier.repair_mutation_id = $mutation_id THEN {
                    source_instance_id: identifier.source_instance_id,
                    created_at: identifier.created_at,
                    repair_mutation_id: identifier.repair_mutation_id
                  }
                  ELSE {} END}) AS identifiers
            }
            CALL {
              WITH new
              MATCH (person)-[fact:HAS_FACT {repair_mutation_id: $mutation_id}]->(new)
              RETURN collect({object_kind: 'HAS_FACT', direction: 'outgoing',
                left_endpoint: {person_id: person.person_id}, right_endpoint: {
                  source_record_pk: new.source_record_pk}, properties: properties(fact)}) AS facts
            }
            RETURN identifiers + links + facts AS objects
            """,
            mutation_id=command.mutation_id,
        ).single(strict=True)["objects"]
    expected_by_key: dict[str, list[dict[str, object]]] = {}
    for item in specifications:
        if isinstance(item, dict) and item.get("object_kind") in {
            "Identifier",
            "IDENTIFIED_BY",
            "HAS_FACT",
        }:
            expected_by_key.setdefault(_rollback_specification_key(item), []).append(item)
    actual_with_specification_metadata = [
        _with_rollback_specification_metadata(item, expected_by_key)
        for item in actual
        if isinstance(item, dict)
    ]
    assert all(not remaining for remaining in expected_by_key.values())
    evidence_specifications = [
        item
        for item in specifications
        if isinstance(item, dict)
        and item.get("object_kind") in {"Identifier", "IDENTIFIED_BY", "HAS_FACT"}
    ]
    assert sorted(
        json.dumps(_rollback_dynamic(item), sort_keys=True, separators=(",", ":"))
        for item in actual_with_specification_metadata
    ) == sorted(
        json.dumps(item, sort_keys=True, separators=(",", ":")) for item in evidence_specifications
    )
    assert row["link_status"] == "linked"
    assert row["observed_at"].startswith("2026-08-01T12:00:00")
    assert row["owned_by_count"] == 1


def test_rollback_image_omits_fact_specification_when_deal_has_no_attributes(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    result = _repository(neo4j_driver).commit_atomic_mutation(command)
    assert result.rollback_image is not None
    with neo4j_driver.session() as session:
        payload_json = session.run(
            "MATCH (image:CrmDealRepairRollbackImage {rollback_image_id: $rollback_image_id}) "
            "RETURN image.payload_json AS payload_json",
            rollback_image_id=result.rollback_image.rollback_image_id,
        ).single(strict=True)["payload_json"]
    payload = canonical_payload(payload_json)
    body = payload["payload"]
    assert isinstance(body, dict)
    specifications = body["created_object_specifications"]
    assert isinstance(specifications, list)
    assert all(
        not isinstance(item, dict) or item.get("object_kind") != "HAS_FACT"
        for item in specifications
    )


def test_review_rollback_image_describes_review_chain_and_provisional_link(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=False)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    result = _repository(neo4j_driver).commit_atomic_mutation(command)
    assert result.mutation is not None and result.mutation.outcome == "review_required"
    assert result.rollback_image is not None
    with neo4j_driver.session() as session:
        payload_json = session.run(
            "MATCH (image:CrmDealRepairRollbackImage {rollback_image_id: $rollback_image_id}) "
            "RETURN image.payload_json AS payload_json",
            rollback_image_id=result.rollback_image.rollback_image_id,
        ).single(strict=True)["payload_json"]
    assert isinstance(payload_json, str)
    payload = canonical_payload(payload_json)
    payload_body = payload["payload"]
    assert isinstance(payload_body, dict)
    specifications = payload_body["created_object_specifications"]
    assert isinstance(specifications, list)
    kinds = {item.get("object_kind") for item in specifications if isinstance(item, dict)}
    assert {
        "SourceRecord",
        "MatchDecision",
        "ReviewCase",
        "FROM_SOURCE",
        "PREVIOUS_VERSION_OF",
        "ABOUT_LEFT",
        "ABOUT_RIGHT",
        "LINKED_TO",
        "FOR_DECISION",
    } <= kinds
    provisional = next(
        item
        for item in specifications
        if isinstance(item, dict) and item.get("object_kind") == "LINKED_TO"
    )
    assert provisional["properties"] == {
        "is_active": False,
        "provisional": True,
        "authoritative": False,
        "source_record_pk": next(
            item["identity"]["source_record_pk"]
            for item in specifications
            if isinstance(item, dict) and item.get("object_kind") == "SourceRecord"
        ),
        "repair_mutation_id": command.mutation_id,
        "linked_at": {"dynamic": "transaction_datetime"},
    }
    _assert_structural_readback(
        neo4j_driver,
        command.mutation_id,
        specifications,
        {"SourceRecord", "MatchDecision", "ReviewCase"},
        {
            "FROM_SOURCE",
            "PREVIOUS_VERSION_OF",
            "ABOUT_LEFT",
            "ABOUT_RIGHT",
            "LINKED_TO",
            "FOR_DECISION",
        },
    )


def test_barrier_concurrent_conflicting_commands_commit_once_and_reject_drift(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    conflicting_inventory = RepairInventoryItem(
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
    conflicting_unit = replace(
        command.unit,
        inventory_fingerprint=conflicting_inventory.graph_fingerprint,
        inventory_graph_fingerprint=conflicting_inventory.graph_fingerprint,
        inventory_binding_digest=build_inventory_binding_digest(conflicting_inventory),
    )
    conflicting_command = RepairMutationCommand(
        conflicting_unit,
        command.fence,
        conflicting_inventory,
        _SOURCE,
        _CONTROL,
    )
    barrier = Barrier(2)
    repository = _repository(neo4j_driver)

    def _commit(candidate: RepairMutationCommand) -> object:
        barrier.wait(timeout=10)
        try:
            return repository.commit_atomic_mutation(candidate)
        except RepairMutationDriftError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(_commit, (command, conflicting_command)))
    committed = [
        outcome for outcome in outcomes if getattr(outcome, "decision", None) == "committed"
    ]
    drifted = [outcome for outcome in outcomes if isinstance(outcome, RepairMutationDriftError)]
    assert len(committed) == 1
    assert len(drifted) == 1
    assert "bound" in str(drifted[0]) or "identity" in str(drifted[0])
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (result:CrmDealRepairMutationResult {run_id: 'run-a', unit_id: 'unit-a'}) "
            "OPTIONAL MATCH (image:CrmDealRepairRollbackImage {run_id: 'run-a', unit_id: 'unit-a'}) "
            "OPTIONAL MATCH (checkpoint:CrmDealRepairCheckpoint {run_id: 'run-a', unit_id: 'unit-a'}) "
            "OPTIONAL MATCH (outbox:CrmDealRepairOutbox {run_id: 'run-a', unit_id: 'unit-a'}) "
            "RETURN count(DISTINCT result) AS results, count(DISTINCT image) AS images, "
            "count(DISTINCT checkpoint) AS checkpoints, count(DISTINCT outbox) AS outboxes"
        ).single(strict=True)
    assert dict(row) == {"results": 1, "images": 1, "checkpoints": 1, "outboxes": 1}


def test_support_authority_writer_blocks_behind_serialized_repair_and_replay_drifts(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    serialized = Event()
    release_repair = Event()
    writer_started = Event()
    lifecycle_locked = Event()

    def _pause_after_serialization(stage: MutationFailureStage) -> None:
        if stage == "after_classification":
            serialized.set()
            assert release_repair.wait(timeout=10)

    repository = _repository(neo4j_driver, _pause_after_serialization)
    with ThreadPoolExecutor(max_workers=2) as pool:
        repair = pool.submit(repository.commit_atomic_mutation, command)
        assert serialized.wait(timeout=10)
        writer = pool.submit(
            _write_new_support_source_version,
            neo4j_driver,
            writer_started,
            lifecycle_locked,
        )
        assert writer_started.wait(timeout=10)
        assert not lifecycle_locked.wait(timeout=0.2)
        release_repair.set()
        committed = repair.result(timeout=10)
        writer.result(timeout=10)
    assert committed.decision == "committed"
    with pytest.raises(RepairMutationDriftError, match="authority changed"):
        _repository(neo4j_driver).commit_atomic_mutation(command)


def _write_new_support_source_version(
    driver: Driver,
    started: Event,
    lifecycle_locked: Event,
) -> None:
    """Use the ordinary lifecycle lock/plan/persist/activate path for a support revision."""
    with driver.session() as session:

        def _write(tx: ManagedTransaction) -> None:
            started.set()
            state = load_locked_source_state(
                tx,
                "bitrix_chat",
                "bitrix-crm-contact-contact-1",
                _SOURCE,
            )
            planned = plan_incoming_version(state, "e" * 64)
            if isinstance(planned, DuplicateVersion):
                raise AssertionError("support lifecycle writer unexpectedly deduplicated")
            lifecycle_locked.set()
            envelope = SourceRecordEnvelope(
                source_system="bitrix_chat",
                source_instance_id=_SOURCE,
                source_record_id="bitrix-crm-contact-contact-1",
                record_type=RecordType.IDENTITY,
                observed_at=_OBSERVED.isoformat(),
                record_hash="e" * 64,
                identifiers=[
                    RawIdentifier(
                        type="crm_contact_id",
                        value="contact-1",
                        source_instance_id=_SOURCE,
                    )
                ],
                raw_payload={"ID": "contact-1"},
                source_entity_type="contact",
                source_entity_id="contact-1",
                identity_policy_version="crm_contact_identity_v1",
                identity_link_key=f"bitrix:{_SOURCE}:contact:contact-1",
            )
            envelope.source_record_version = str(planned.version)
            source_record_pk = persist_source_record(
                tx,
                envelope=envelope,
                identifiers=normalize_envelope_identifiers(envelope),
                addresses=normalize_envelope_addresses(envelope),
                attributes=normalize_envelope_attributes(envelope),
                match_result=MatchResult(
                    decision=MatchDecision.MERGE,
                    confidence=1.0,
                    matched_person_id="person-a",
                    engine_type=EngineType.DETERMINISTIC,
                ),
                is_new_person=False,
                ingest_run_id=None,
                lifecycle_status=SourceRecordLifecycleStatus.PENDING_REVIEW,
                expected_active_source_record_pk=planned.active_source_record_pk,
                control_instance_id=_CONTROL,
            )
            activate_staged_version(
                tx,
                source_system="bitrix_chat",
                source_record_id=envelope.source_record_id,
                source_instance_id=_SOURCE,
                old_source_record_pk=planned.active_source_record_pk,
                new_source_record_pk=source_record_pk,
            )

        session.execute_write(_write)


def test_replay_rejects_tampered_immutable_rollback_bundle(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    committed = repository.commit_atomic_mutation(command)
    assert committed.mutation is not None and committed.mutation.outcome == "applied"
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (image:CrmDealRepairRollbackImage {rollback_image_id: $image_id}) "
            "SET image.payload_json = '{}'",
            image_id=command.rollback_image_id,
        ).consume()
    with pytest.raises(RepairMutationDriftError, match="bundle payload differs"):
        repository.commit_atomic_mutation(command)


def test_replay_rejects_committed_bundle_cardinality_drift(neo4j_driver: Driver) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    committed = repository.commit_atomic_mutation(command)
    assert committed.mutation is not None and committed.mutation.outcome == "applied"
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:SourceRecord {source_record_pk: 'duplicate-replay-source-pk', "
            "repair_mutation_id: $mutation_id})",
            mutation_id=command.mutation_id,
        ).consume()
    with pytest.raises(RepairMutationDriftError, match="bundle cardinality differs"):
        repository.commit_atomic_mutation(command)


@pytest.mark.parametrize(
    ("label", "cypher", "error_match"),
    [
        (
            "result_digest",
            "MATCH (result:CrmDealRepairMutationResult {mutation_id: $mutation_id}) "
            "SET result.result_digest = 'sha256:' + '0'",
            "result digest is malformed",
        ),
        (
            "checkpoint_scope",
            "MATCH (checkpoint:CrmDealRepairCheckpoint {run_id: 'run-a', unit_id: 'unit-a'}) "
            "SET checkpoint.generation = 99",
            "scope",
        ),
        (
            "outbox_fence",
            "MATCH (outbox:CrmDealRepairOutbox {run_id: 'run-a', unit_id: 'unit-a'}) "
            "SET outbox.delivery_token = 'tampered-token'",
            "fence",
        ),
    ],
)
def test_replay_rejects_root_and_child_scope_digest_tamper(
    neo4j_driver: Driver,
    label: str,
    cypher: str,
    error_match: str,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    repository = _repository(neo4j_driver)
    committed = repository.commit_atomic_mutation(command)
    assert committed.mutation is not None and committed.mutation.outcome == "applied"
    with neo4j_driver.session() as session:
        session.run(cypher, mutation_id=command.mutation_id).consume()
    with pytest.raises(RepairMutationDriftError, match=error_match):
        repository.commit_atomic_mutation(command)


def test_exact_reconstruction_preserves_non_null_primary_contact_full_name(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    _deactivate_child_contamination(neo4j_driver)
    named = _deal_payload("1", "contact-1", full_name="Ada Lovelace")
    raw_payload = named["raw_payload"]
    attributes = named["attributes"]
    identifiers = named["identifiers"]
    record_hash = named["record_hash"]
    assert isinstance(raw_payload, dict)
    assert isinstance(attributes, dict)
    assert isinstance(identifiers, list)
    assert isinstance(record_hash, str)
    normalized_payload = json.dumps(
        {"attributes": attributes, "identifiers": identifiers},
        sort_keys=True,
        separators=(",", ":"),
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (deal:SourceRecord {source_record_pk: 'deal-pk'}) "
            "SET deal.raw_payload = $raw_payload, deal.normalized_payload = $normalized_payload, "
            "deal.record_hash = $record_hash",
            raw_payload=json.dumps(raw_payload, sort_keys=True, separators=(",", ":")),
            normalized_payload=normalized_payload,
            record_hash=record_hash,
        ).consume()
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    result = _repository(neo4j_driver).commit_atomic_mutation(command)
    assert result.mutation is not None
    assert result.mutation.outcome == "applied"
    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (new:SourceRecord {repair_mutation_id: $mutation_id})
            OPTIONAL MATCH (:Person)-[fact:HAS_FACT {
              source_record_pk: new.source_record_pk, attribute_name: 'full_name',
              attribute_value: 'Ada Lovelace', is_active: true
            }]->(new)
            RETURN new.normalized_payload AS normalized_payload, count(fact) AS full_name_facts
            """,
            mutation_id=command.mutation_id,
        ).single(strict=True)
    normalized_payload = json.loads(row["normalized_payload"])
    assert normalized_payload["attributes"] == {"full_name": "Ada Lovelace"}
    assert row["full_name_facts"] == 1


def test_retirement_uses_frozen_inventory_descendants_and_preserves_unrelated_records(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=False)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'}),
              (child:SourceRecord {source_record_pk: 'child-pk'}),
              (person:Person {person_id: 'person-a'})
            CREATE (depth_two:SourceRecord {source_record_pk: 'depth-two-pk',
              source_record_id: 'history-2', source_record_version: '1',
              source_version_key: 'depth-two-v1', source_instance_id: $source_instance_id,
              record_type: 'crm_history', lifecycle_status: 'active', is_latest: true,
              record_hash: $record_hash, raw_payload: '{}', normalized_payload: '{}'})
              -[:FROM_SOURCE]->(source)
            CREATE (depth_two)-[:CHILD_OF]->(child)
            CREATE (depth_two)-[:LINKED_TO {is_active: true, source_record_pk: 'depth-two-pk'}]->(person)
            CREATE (unrelated:SourceRecord {source_record_pk: 'unrelated-pk',
              source_record_id: 'unrelated-history', source_record_version: '1',
              source_version_key: 'unrelated-v1', source_instance_id: $source_instance_id,
              record_type: 'crm_history', lifecycle_status: 'active', is_latest: true,
              record_hash: $record_hash, raw_payload: '{}', normalized_payload: '{}'})
              -[:FROM_SOURCE]->(source)
            CREATE (unrelated)-[:LINKED_TO {is_active: true, source_record_pk: 'unrelated-pk'}]->(person)
            """,
            source_instance_id=_SOURCE,
            record_hash="e" * 64,
        ).consume()
    item, _ = _inventory(neo4j_driver)
    descendants = item.payload["descendants"]
    assert isinstance(descendants, list)
    inventoried_pks = {
        row["source_record_pk"]
        for row in descendants
        if isinstance(row, dict) and isinstance(row.get("source_record_pk"), str)
    }
    assert inventoried_pks == {"child-pk", "depth-two-pk"}
    command = _seed_authority(neo4j_driver, item)
    _repository(neo4j_driver).commit_atomic_mutation(command)
    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (:SourceRecord {source_record_pk: 'child-pk'})-[child:LINKED_TO]->()
            MATCH (:SourceRecord {source_record_pk: 'depth-two-pk'})-[depth_two:LINKED_TO]->()
            MATCH (:SourceRecord {source_record_pk: 'unrelated-pk'})-[unrelated:LINKED_TO]->()
            RETURN child.is_active AS child_active, depth_two.is_active AS depth_two_active,
              unrelated.is_active AS unrelated_active
            """
        ).single(strict=True)
    assert dict(row) == {
        "child_active": False,
        "depth_two_active": False,
        "unrelated_active": True,
    }


def test_drift_stale_fence_and_unblocked_dispatch_leave_no_mutation(
    neo4j_driver: Driver,
) -> None:
    _seed_domain(neo4j_driver, independent_support=True)
    item, _ = _inventory(neo4j_driver)
    command = _seed_authority(neo4j_driver, item)
    original_hash = item.payload["record_hash"]
    assert isinstance(original_hash, str)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (deal:SourceRecord {source_record_pk: 'deal-pk'}) "
            "SET deal.record_hash = $record_hash",
            record_hash="f" * 64,
        ).consume()
    with pytest.raises(RepairMutationDriftError):
        _repository(neo4j_driver).commit_atomic_mutation(command)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (deal:SourceRecord {source_record_pk: 'deal-pk'}) "
            "SET deal.record_hash = $record_hash",
            record_hash=original_hash,
        ).consume()
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
