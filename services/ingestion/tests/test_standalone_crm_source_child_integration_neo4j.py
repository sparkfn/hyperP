# ruff: noqa: E501 -- Cypher fixture literals retain executable graph shape.
"""Real-Neo4j #306 source-child recovery coverage without Bitrix I/O.

The control-plane suite owns parent publication, cancellation, pause and
occurrence convergence.  This suite exercises the child-specific atomic handoff:
the deferred #302 receipt remains at the old contact cursor, #303's completed
binding position can be closed exactly once, and replay/stale authority cannot
write a matching topology.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import replace
from typing import cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase
from neo4j.exceptions import ServiceUnavailable
from src.crm_company_contracts import (
    CrmCompanyMembershipHead,
    CrmCompanyMembershipHeadCompareAndSet,
    CrmCompanyMembershipSnapshotRecord,
)
from src.crm_company_membership_writer import (
    CrmCompanyMembershipMutation,
    build_company_membership_commit,
)
from src.crm_identity_associations import normalize_company_membership_snapshot
from src.graph.client import Neo4jClient
from src.graph.crm_company_membership import CrmCompanyMembershipRepository
from src.graph.queries.standalone_crm_census import (
    CLAIM_PUBLISHED_CHILD,
    CLOSE_CONTACT_BINDING_POSITION,
    PRECONFIRM_PUBLISHED_CHILD,
)
from src.graph.queries.standalone_crm_source_facts import (
    CLAIM_PAGE,
    FINALIZE_PAGE,
    READ_PENDING_CONTACT_RECEIPT,
    READ_PENDING_LEAD_RECEIPT,
)
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.graph.standalone_crm_census_records import authority_context, authority_revision
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_census_models import StandaloneCrmChildEnvelope, StandaloneCrmPublication
from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
    canonical_request_payload,
)
from src.standalone_crm_census_types import StandaloneCrmStreamKind
from src.standalone_crm_child_contracts import (
    ContactBindingSubposition,
    ContactSourceChildEnvelope,
    StandaloneCrmSourceAvailability,
    StandaloneCrmSourceChildBudgetAuthorization,
    StandaloneCrmSourceChildScope,
    StandaloneCrmSourceChildUnitAuthority,
)
from src.standalone_crm_unit_repository import StandaloneCrmUnitAccountingDelta
from tests.standalone_crm_source_fact_neo4j_support import (
    DriverClient,
    reset_disposable_data,
)


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    """Use only the existing explicitly-disposable Lane A Neo4j 5.26 service."""
    uri = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_URI")
    password = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_PASSWORD")
    if uri is None or password is None:
        pytest.skip("disposable standalone CRM Lane A Neo4j database is not configured")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_SERVICE_HOST") == "neo4j":
        allowed_hosts.add("neo4j")
    if urlparse(uri).hostname not in allowed_hosts:
        pytest.fail("#306 source-child tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_USER", "neo4j"), password),
    )
    ready = False
    try:
        for _ in range(15):
            try:
                driver.verify_connectivity()
                ready = True
                break
            except ServiceUnavailable:
                time.sleep(1)
        if not ready:
            pytest.fail("disposable standalone CRM Lane A Neo4j database did not become ready")
        reset_disposable_data(driver)
        yield driver
    finally:
        if ready:
            reset_disposable_data(driver)
        driver.close()


def _parameters() -> dict[str, object]:
    return {
        "census_id": "source-child-contact",
        "request_json": "{}",
        "generation": 1,
        "fence_token": 2,
        "fence_owner_id": "contact-task",
        "source_key": "bitrix_chat",
        "source_instance_id": "portal-a",
        "control_instance_id": "control-a",
        "stream_kind": "contact",
        "frozen_upper_id": 10,
        "task_name": "src.standalone_crm_census_tasks.run_standalone_crm_census_unit",
        "task_id": "contact-task",
        "parent_task_id": "parent-contact-task",
        "payload_digest": "sha256:" + "a" * 64,
        "payload_json": "{}",
        "payload_version": "standalone-crm-child-v1",
        "queue": "ingestion",
        "available_at": "2026-08-29T00:00:00Z",
        "availability_contract_version": "standalone-crm-source-availability-v1",
        "attempt_deadline": "2099-01-01T00:00:00Z",
        "occurrence_deadline": "2099-01-02T00:00:00Z",
        "call_intent_id": "contact-page-intent",
        "receipt_key": "source-child-contact:1:contact:contact-page-intent",
        "content_digest": "sha256:" + "b" * 64,
        "checkpoint_absent": False,
        "expected_cursor": 5,
        "expected_processed": 0,
        "expected_skipped": 0,
        "proposed_cursor": 5,
        "proposed_processed": 1,
        "proposed_skipped": 0,
        "proposed_binding_subject": 6,
        "proposed_binding_offset": 0,
        "processed_delta": 1,
        "skipped_delta": 0,
        "failed_delta": 0,
        "attempt_call_limit": 2,
        "occurrence_call_limit": 4,
        "attempt_row_limit": 10,
        "occurrence_row_limit": 20,
        "authorization_id": "contact-authority",
        "authorization_digest": "sha256:" + "c" * 64,
        "source_receipts_json": json.dumps(
            [
                {
                    "row_id": 6,
                    "source_record_pk": "contact-source-pk",
                    "source_record_version": 1,
                    "record_hash": "sha256:" + "d" * 64,
                    "observed_at": "2026-08-28T00:00:00Z",
                }
            ],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "authority_revision": "source-sync:authority",
        "authority_json": '{"authority":"source-sync"}',
        "binding_count": 0,
        "binding_subject_id": 6,
        "owner_id": "contact-task",
        "contact_id": 6,
        "last_committed_id": 5,
    }


def _seed_contact(driver: Driver, parameters: dict[str, object]) -> None:
    with driver.session() as session:
        session.run(
            """
            CREATE (:SourceSystem {source_key: $source_key, is_active: true})<-[:INSTANCE_OF]-
              (:BitrixSourceInstance {source_key: $source_key, source_instance_id: $source_instance_id, status: 'active'})
            CREATE (:BitrixExecutionSourceBinding {source_key: $source_key, source_instance_id: $source_instance_id, control_instance_id: $control_instance_id})
            CREATE (:StandaloneCrmCensus {census_id: $census_id, generation: $generation, source_key: $source_key,
              source_instance_id: $source_instance_id, control_instance_id: $control_instance_id, census_kind: 'source_sync',
              request_json: $request_json, authority_revision: $authority_revision, authority_json: $authority_json,
              status: 'running', cancel_requested: false, created_at: datetime($available_at), occurrence_rows: 0})
            CREATE (:StandaloneCrmCensusAttempt {census_id: $census_id, generation: $generation, fence_token: $fence_token,
              status: 'running', task_id: $parent_task_id, attempt_deadline: datetime($attempt_deadline), row_count: 0})
            CREATE (:StandaloneCrmCensusUnit {census_id: $census_id, generation: $generation, stream_kind: $stream_kind,
              state: 'running', frozen_upper_id: $frozen_upper_id})
            CREATE (:StandaloneCrmCensusFence {census_id: $census_id, generation: $generation, stream_kind: $stream_kind,
              token: $fence_token, owner_id: $fence_owner_id, status: 'active', lease_until: datetime($attempt_deadline)})
            CREATE (:StandaloneCrmChildPublication {census_id: $census_id, generation: $generation, stream_kind: $stream_kind,
              task_name: $task_name, task_id: $task_id, payload_digest: $payload_digest,
              payload_json: $payload_json, payload_version: $payload_version, queue: $queue, status: 'published'})
            CREATE (:StandaloneCrmHttpCallReservation {intent_id: $call_intent_id, census_id: $census_id, generation: $generation,
              fence_token: $fence_token, stream_kind: $stream_kind, call_kind: 'page', cursor: $expected_cursor,
              task_id: $task_id, status: 'succeeded'})
            CREATE (:StandaloneCrmCensusCheckpoint {census_id: $census_id, stream_kind: $stream_kind,
              last_committed_id: $expected_cursor, processed_rows: $expected_processed, skipped_rows: $expected_skipped,
              generation: $generation, fence_token: $fence_token, frozen_upper_id: $frozen_upper_id, revision_id: null})
            """,
            **parameters,
        ).consume()


def _contact_envelope(parameters: dict[str, object]) -> ContactSourceChildEnvelope:
    scope = StandaloneCrmSourceChildScope("bitrix_chat", "portal-a", "control-a")
    unit = StandaloneCrmSourceChildUnitAuthority(
        "source-child-contact",
        "contact",
        1,
        2,
        "contact-task",
        "src.standalone_crm_census_tasks.run_standalone_crm_census_unit",
        "contact-task",
        "sha256:" + "a" * 64,
    )
    budget = StandaloneCrmSourceChildBudgetAuthorization(
        "contact-authority",
        "sha256:" + "c" * 64,
        unit.census_id,
        unit.stream_kind,
        unit.generation,
        unit.fence_token,
        unit.fence_owner_id,
        unit.task_name,
        unit.task_id,
        unit.payload_digest,
        2,
        10,
        4,
        20,
        "2099-01-01T00:00:00Z",
        "2099-01-02T00:00:00Z",
    )
    return ContactSourceChildEnvelope(
        scope,
        unit,
        10,
        5,
        StandaloneCrmSourceAvailability("2026-08-29T00:00:00Z"),
        budget,
        ContactBindingSubposition(6, 0),
    )


def _canonical_contact_request() -> str:
    return canonical_request_payload(_source_request("contact"))


def _source_request(stream_kind: StandaloneCrmStreamKind) -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        "portal-a",
        "control-a",
        f"{stream_kind}-source-child-occurrence",
        (stream_kind,),
        StandaloneCrmBudget(2, 10, 3600, 4, 20, 2, "2099-01-02T00:00:00Z"),
        "policy-a",
        "association-a",
        "configuration-a",
        SourceSyncAuthority("mapping-a", "mapping-digest", "projection-a", "projection-digest"),
    )


def _prepare_claimable_publication(parameters: dict[str, object]) -> dict[str, object]:
    stream_value = parameters["stream_kind"]
    if stream_value == "contact":
        stream_kind: StandaloneCrmStreamKind = "contact"
    elif stream_value == "lead":
        stream_kind = "lead"
    elif stream_value == "company":
        stream_kind = "company"
    else:
        raise ValueError("test publication requires a standalone CRM source stream")
    task_id = parameters["task_id"]
    census_id = parameters["census_id"]
    assert isinstance(task_id, str) and isinstance(census_id, str)
    publication = StandaloneCrmChildEnvelope(
        census_id,
        1,
        stream_kind,
        10,
        None,
        "src.standalone_crm_census_tasks.run_standalone_crm_census_unit",
        task_id,
        "ingestion",
    )
    payload = {
        "census_id": publication.census_id,
        "generation": publication.generation,
        "stream_kind": publication.stream_kind,
        "frozen_upper_id": publication.frozen_upper_id,
        "revision_id": publication.revision_id,
        "task_name": publication.task_name,
        "task_id": publication.task_id,
        "queue": publication.queue,
        "payload_version": publication.payload_version,
    }
    request = _source_request(stream_kind)
    parameters.update(
        request_json=canonical_request_payload(request),
        authority_revision=authority_revision(request),
        authority_json=authority_context(request),
        task_name=publication.task_name,
        payload_digest=publication.payload_digest(),
        payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        payload_version=publication.payload_version,
        queue=publication.queue,
    )
    return payload


def _claim_parameters(parameters: dict[str, object], *, owner_id: str) -> dict[str, object]:
    return {
        key: parameters[key]
        for key in (
            "census_id",
            "generation",
            "stream_kind",
            "frozen_upper_id",
            "task_name",
            "task_id",
            "payload_digest",
            "payload_json",
            "payload_version",
            "queue",
            "source_key",
            "source_instance_id",
            "control_instance_id",
            "request_json",
            "authority_revision",
            "authority_json",
            "occurrence_deadline",
            "attempt_call_limit",
            "occurrence_call_limit",
            "attempt_row_limit",
            "occurrence_row_limit",
        )
    } | {"owner_id": owner_id, "lease_seconds": 120}


def _expire_and_reclaim(driver: Driver, parameters: dict[str, object]) -> tuple[int, str]:
    with driver.session() as session:
        session.run(
            "MATCH (fence:StandaloneCrmCensusFence {census_id: $census_id, generation: $generation, "
            "stream_kind: $stream_kind}) SET fence.lease_until = datetime() - duration({seconds: 1})",
            **parameters,
        ).consume()
        claimed = session.run(
            CLAIM_PUBLISHED_CHILD, **_claim_parameters(parameters, owner_id="recovery-task")
        ).single(strict=True)
    token = claimed["fence_token"]
    owner = claimed["fence_owner_id"]
    assert isinstance(token, int) and isinstance(owner, str)
    return token, owner


def _commit_empty_contact_membership(
    driver: Driver,
    parameters: dict[str, object],
    expected_head: CrmCompanyMembershipHead | None = None,
) -> str:
    envelope = _contact_envelope(parameters)
    receipt = parameters["source_receipts_json"]
    assert isinstance(receipt, str)
    source = json.loads(receipt)
    assert isinstance(source, list) and len(source) == 1 and isinstance(source[0], dict)
    item = source[0]
    snapshot = normalize_company_membership_snapshot(
        subject_type="contact",
        subject_id="6",
        payloads=(),
    )
    record = CrmCompanyMembershipSnapshotRecord(
        envelope.scope,
        snapshot,
        "bitrix-crm-contact-6",
        str(item["source_record_pk"]),
        int(item["source_record_version"]),
        str(item["record_hash"]),
        str(item["observed_at"]),
        envelope.availability,
        0,
    )
    head = CrmCompanyMembershipHead(envelope.scope, "contact", "6", record)
    mutation = CrmCompanyMembershipMutation(
        record,
        (),
        CrmCompanyMembershipHeadCompareAndSet(expected_head, head),
    )
    expected = StandaloneCrmCheckpoint(
        "source-child-contact", "contact", 10, None, 5, 6, 0, 1, 0, 1, 2
    )
    request = build_company_membership_commit(
        envelope,
        mutation,
        expected,
        replace(expected, binding_offset=0),
        StandaloneCrmUnitAccountingDelta(0, 0, 0),
    )
    repository = CrmCompanyMembershipRepository(cast(Neo4jClient, DriverClient(driver)))
    return repository.commit_unit(request).decision


def _legacy_membership_head(parameters: dict[str, object]) -> CrmCompanyMembershipHead:
    envelope = _contact_envelope(parameters)
    snapshot = normalize_company_membership_snapshot(
        subject_type="contact",
        subject_id="6",
        payloads=(),
    )
    record = CrmCompanyMembershipSnapshotRecord(
        envelope.scope,
        snapshot,
        "bitrix-crm-contact-6",
        "legacy-contact-source-pk",
        1,
        "sha256:" + "e" * 64,
        "2026-08-27T00:00:00Z",
        StandaloneCrmSourceAvailability("2026-08-28T00:00:00Z"),
        0,
    )
    return CrmCompanyMembershipHead(envelope.scope, "contact", "6", record)


def test_deferred_contact_receipt_replays_then_closes_without_person_topology(
    neo4j_driver: Driver,
) -> None:
    """A crash after #302 resumes at #303/close without a second row account."""
    parameters = _parameters()
    parameters["request_json"] = _canonical_contact_request()
    _seed_contact(neo4j_driver, parameters)
    with neo4j_driver.session() as session:
        assert session.run(CLAIM_PAGE, **parameters).single(strict=True)["decision"] == "apply"
        assert (
            session.run(FINALIZE_PAGE, **parameters).single(strict=True)["receipt_key"]
            == parameters["receipt_key"]
        )
        assert session.run(CLAIM_PAGE, **parameters).single(strict=True)["decision"] == "replayed"
        checkpoint = session.run(
            """
            MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id, stream_kind: 'contact'})
            MATCH (receipt:StandaloneCrmSourceFactPageReceipt {receipt_key: $receipt_key})
            RETURN checkpoint.last_committed_id AS cursor, checkpoint.binding_subject_id AS subject,
              checkpoint.binding_offset AS offset, checkpoint.processed_rows AS processed,
              receipt.pending_binding_subject_id AS receipt_subject, receipt.status AS receipt_status
            """,
            **parameters,
        ).single(strict=True)
        assert dict(checkpoint) == {
            "cursor": 5,
            "subject": 6,
            "offset": 0,
            "processed": 1,
            "receipt_subject": 6,
            "receipt_status": "committed",
        }
    assert _commit_empty_contact_membership(neo4j_driver, parameters) == "committed"
    assert _commit_empty_contact_membership(neo4j_driver, parameters) == "idempotent"
    with neo4j_driver.session() as session:
        assert (
            session.run(CLOSE_CONTACT_BINDING_POSITION, **parameters).single(strict=True)[
                "last_committed_id"
            ]
            == 6
        )
        assert session.run(CLOSE_CONTACT_BINDING_POSITION, **parameters).single() is None
        final = session.run(
            """
            MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id, stream_kind: 'contact'})
            OPTIONAL MATCH (person:Person)
            OPTIONAL MATCH ()-[matching:HAS_FACT|IDENTIFIED_BY]->()
            RETURN checkpoint.last_committed_id AS cursor, checkpoint.processed_rows AS processed,
              checkpoint.binding_subject_id AS subject, checkpoint.binding_offset AS offset,
              count(DISTINCT person) AS persons, count(DISTINCT matching) AS matching_edges
            """,
            **parameters,
        ).single(strict=True)
    assert dict(final) == {
        "cursor": 6,
        "processed": 1,
        "subject": None,
        "offset": None,
        "persons": 0,
        "matching_edges": 0,
    }


def test_broker_delivery_before_parent_publication_confirmation_is_retryable_without_claiming(
    neo4j_driver: Driver,
) -> None:
    parameters = _parameters()
    _prepare_claimable_publication(parameters)
    _seed_contact(neo4j_driver, parameters)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (publication:StandaloneCrmChildPublication {census_id: $census_id, "
            "generation: $generation, stream_kind: $stream_kind}) SET publication.status = 'publishing'",
            **parameters,
        ).consume()
        pending = session.run(
            PRECONFIRM_PUBLISHED_CHILD, **_claim_parameters(parameters, owner_id="ignored")
        ).single(strict=True)
        state = session.run(
            "MATCH (fence:StandaloneCrmCensusFence {census_id: $census_id, generation: $generation, "
            "stream_kind: $stream_kind}) RETURN fence.token AS token, fence.owner_id AS owner",
            **parameters,
        ).single(strict=True)
    assert pending["pending"] == 1
    assert dict(state) == {"token": 2, "owner": "contact-task"}


def test_first_effect_pause_continues_through_real_lifecycle_and_rebinds_zero_checkpoint(
    neo4j_driver: Driver,
) -> None:
    """A first-effect failure creates a real continuation-ready zero checkpoint."""
    parameters = _parameters()
    _prepare_claimable_publication(parameters)
    _seed_contact(neo4j_driver, parameters)
    repository = StandaloneCrmCensusRepository(cast(Neo4jClient, DriverClient(neo4j_driver)))
    current = StandaloneCrmChildEnvelope(
        "source-child-contact",
        1,
        "contact",
        10,
        None,
        "src.standalone_crm_census_tasks.run_standalone_crm_census_unit",
        "contact-task",
        "ingestion",
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id, "
            "stream_kind: $stream_kind}) DELETE checkpoint",
            **parameters,
        ).consume()
        session.run(
            "MATCH (fence:StandaloneCrmCensusFence {census_id: $census_id, generation: $generation, "
            "stream_kind: $stream_kind}) SET fence.status = 'retired'",
            **parameters,
        ).consume()
    claimed = repository.claim_published_child(
        current,
        owner_id="first-effect-worker",
        payload_json=str(parameters["payload_json"]),
    )
    assert claimed is not None
    token = claimed["fence_token"]
    assert isinstance(token, int)
    zero = StandaloneCrmCheckpoint(
        "source-child-contact", "contact", 10, None, 0, None, None, 0, 0, 1, token
    )
    assert repository.pause_claimed_unit(
        "source-child-contact",
        1,
        "contact",
        token,
        "first-effect-worker",
        current.task_name,
        current.task_id,
        current.payload_digest(),
        10,
        zero,
        "source_effect_failed",
        "test first effect failed before source-fact commit",
    )
    with neo4j_driver.session() as session:
        paused = session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: 1}) "
            "MATCH (unit:StandaloneCrmCensusUnit {census_id: $census_id, stream_kind: $stream_kind}) "
            "MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id, stream_kind: $stream_kind}) "
            "RETURN census.status AS census, attempt.status AS attempt, unit.state AS unit, "
            "checkpoint.last_committed_id AS cursor, checkpoint.processed_rows AS processed, "
            "checkpoint.binding_subject_id AS subject, checkpoint.binding_offset AS offset",
            **parameters,
        ).single(strict=True)
    assert dict(paused) == {
        "census": "paused_with_checkpoint",
        "attempt": "paused_with_checkpoint",
        "unit": "paused",
        "cursor": 0,
        "processed": 0,
        "subject": None,
        "offset": None,
    }
    assert (
        repository.create_continuation("source-child-contact", 1, _source_request("contact")) == 2
    )
    assert repository.resumable_units("source-child-contact", 2)[0].stream_kind == "contact"
    publication = StandaloneCrmChildEnvelope(
        "source-child-contact",
        2,
        "contact",
        10,
        None,
        "src.standalone_crm_census_tasks.run_standalone_crm_census_unit",
        "contact-task-v2",
        "ingestion",
    )
    assert repository.reserve_child_envelope(publication)
    assert repository.confirm_publication(
        StandaloneCrmPublication(
            publication.census_id,
            publication.generation,
            publication.stream_kind,
            publication.task_id,
            publication.payload_digest(),
            "pending",
        )
    )
    resumed_payload = json.dumps(
        {
            "census_id": publication.census_id,
            "generation": publication.generation,
            "stream_kind": publication.stream_kind,
            "frozen_upper_id": publication.frozen_upper_id,
            "revision_id": publication.revision_id,
            "task_name": publication.task_name,
            "task_id": publication.task_id,
            "queue": publication.queue,
            "payload_version": publication.payload_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    resumed_claim = repository.claim_published_child(
        publication,
        owner_id="resume-worker",
        payload_json=resumed_payload,
    )
    assert resumed_claim is not None
    with neo4j_driver.session() as session:
        checkpoint = session.run(
            "MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id, stream_kind: $stream_kind}) "
            "RETURN checkpoint.generation AS generation, checkpoint.fence_token AS token, "
            "checkpoint.last_committed_id AS cursor, checkpoint.processed_rows AS processed",
            **parameters,
        ).single(strict=True)
    assert resumed_claim["fence_owner_id"] == "resume-worker"
    assert dict(checkpoint) == {
        "generation": 2,
        "token": resumed_claim["fence_token"],
        "cursor": 0,
        "processed": 0,
    }


def test_existing_unpositioned_lead_checkpoint_pauses_and_continues_without_null_cas_loss(
    neo4j_driver: Driver,
) -> None:
    """Null-safe pause equality preserves an already progressed lead checkpoint."""
    parameters = _parameters()
    parameters.update(
        census_id="pause-existing-lead",
        stream_kind="lead",
        task_id="pause-lead-task",
        fence_owner_id="pause-lead-task",
        parent_task_id="parent-pause-lead-task",
        call_intent_id="pause-lead-page-intent",
        receipt_key="pause-existing-lead:1:lead:pause-lead-page-intent",
        expected_cursor=6,
        expected_processed=1,
        proposed_cursor=7,
        proposed_processed=2,
        proposed_binding_subject=None,
        proposed_binding_offset=None,
    )
    _prepare_claimable_publication(parameters)
    _seed_contact(neo4j_driver, parameters)
    repository = StandaloneCrmCensusRepository(cast(Neo4jClient, DriverClient(neo4j_driver)))
    current = StandaloneCrmChildEnvelope(
        "pause-existing-lead",
        1,
        "lead",
        10,
        None,
        "src.standalone_crm_census_tasks.run_standalone_crm_census_unit",
        "pause-lead-task",
        "ingestion",
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (fence:StandaloneCrmCensusFence {census_id: $census_id, generation: $generation, "
            "stream_kind: $stream_kind}) SET fence.status = 'retired'",
            **parameters,
        ).consume()
    claimed = repository.claim_published_child(
        current,
        owner_id="pause-existing-lead-worker",
        payload_json=str(parameters["payload_json"]),
    )
    assert claimed is not None
    token = claimed["fence_token"]
    assert isinstance(token, int)
    expected = StandaloneCrmCheckpoint(
        "pause-existing-lead", "lead", 10, None, 6, None, None, 1, 0, 1, token
    )
    unexpected_position = StandaloneCrmCheckpoint(
        "pause-existing-lead", "lead", 10, None, 6, 7, 0, 1, 0, 1, token
    )
    assert not repository.pause_claimed_unit(
        "pause-existing-lead",
        1,
        "lead",
        token,
        "pause-existing-lead-worker",
        current.task_name,
        current.task_id,
        current.payload_digest(),
        10,
        unexpected_position,
        "source_effect_failed",
        "unexpected binding position must not match an unpositioned checkpoint",
    )
    with neo4j_driver.session() as session:
        rejected_state = session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: 1}) "
            "MATCH (unit:StandaloneCrmCensusUnit {census_id: $census_id, stream_kind: $stream_kind}) "
            "MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id, "
            "stream_kind: $stream_kind}) RETURN census.status AS census, attempt.status AS attempt, "
            "unit.state AS unit, checkpoint.binding_subject_id AS subject, "
            "checkpoint.binding_offset AS offset",
            **parameters,
        ).single(strict=True)
    assert dict(rejected_state) == {
        "census": "running",
        "attempt": "running",
        "unit": "running",
        "subject": None,
        "offset": None,
    }
    assert repository.pause_claimed_unit(
        "pause-existing-lead",
        1,
        "lead",
        token,
        "pause-existing-lead-worker",
        current.task_name,
        current.task_id,
        current.payload_digest(),
        10,
        expected,
        "source_effect_failed",
        "lead effect failed after one durable source-fact checkpoint",
    )
    assert repository.create_continuation("pause-existing-lead", 1, _source_request("lead")) == 2
    assert repository.resumable_units("pause-existing-lead", 2)[0].stream_kind == "lead"
    resumed = StandaloneCrmChildEnvelope(
        "pause-existing-lead",
        2,
        "lead",
        10,
        None,
        "src.standalone_crm_census_tasks.run_standalone_crm_census_unit",
        "pause-lead-task-v2",
        "ingestion",
    )
    assert repository.reserve_child_envelope(resumed)
    assert repository.confirm_publication(
        StandaloneCrmPublication(
            resumed.census_id,
            resumed.generation,
            resumed.stream_kind,
            resumed.task_id,
            resumed.payload_digest(),
            "pending",
        )
    )
    resumed_payload = json.dumps(
        {
            "census_id": resumed.census_id,
            "generation": resumed.generation,
            "stream_kind": resumed.stream_kind,
            "frozen_upper_id": resumed.frozen_upper_id,
            "revision_id": resumed.revision_id,
            "task_name": resumed.task_name,
            "task_id": resumed.task_id,
            "queue": resumed.queue,
            "payload_version": resumed.payload_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    resumed_claim = repository.claim_published_child(
        resumed,
        owner_id="pause-existing-lead-resume-worker",
        payload_json=resumed_payload,
    )
    assert resumed_claim is not None
    with neo4j_driver.session() as session:
        checkpoint = session.run(
            "MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id, "
            "stream_kind: $stream_kind}) RETURN checkpoint.generation AS generation, "
            "checkpoint.fence_token AS token, checkpoint.last_committed_id AS cursor, "
            "checkpoint.processed_rows AS processed, checkpoint.skipped_rows AS skipped, "
            "checkpoint.binding_subject_id AS subject, checkpoint.binding_offset AS offset",
            **parameters,
        ).single(strict=True)
    assert dict(checkpoint) == {
        "generation": 2,
        "token": resumed_claim["fence_token"],
        "cursor": 6,
        "processed": 1,
        "skipped": 0,
        "subject": None,
        "offset": None,
    }


def test_contact_receipt_and_pending_checkpoint_recover_after_fence_rollover(
    neo4j_driver: Driver,
) -> None:
    """Fence N+1 rebinds the pending contact checkpoint but reads the N receipt."""
    parameters = _parameters()
    _prepare_claimable_publication(parameters)
    _seed_contact(neo4j_driver, parameters)
    with neo4j_driver.session() as session:
        assert session.run(CLAIM_PAGE, **parameters).single(strict=True)["decision"] == "apply"
        session.run(FINALIZE_PAGE, **parameters).consume()

    token, owner = _expire_and_reclaim(neo4j_driver, parameters)
    assert token == 3 and owner == "recovery-task"
    parameters["fence_token"] = token
    parameters["fence_owner_id"] = owner
    parameters["owner_id"] = owner
    parameters["binding_subject_id"] = 6
    with neo4j_driver.session() as session:
        checkpoint = session.run(
            "MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id, "
            "stream_kind: 'contact'}) RETURN checkpoint.fence_token AS token, "
            "checkpoint.last_committed_id AS cursor, checkpoint.binding_subject_id AS subject, "
            "checkpoint.binding_offset AS offset, checkpoint.processed_rows AS processed",
            **parameters,
        ).single(strict=True)
        receipt = session.run(READ_PENDING_CONTACT_RECEIPT, **parameters).single(strict=True)
        assert (
            session.run(CLOSE_CONTACT_BINDING_POSITION, **parameters).single(strict=True)[
                "last_committed_id"
            ]
            == 6
        )
    assert dict(checkpoint) == {"token": 3, "cursor": 5, "subject": 6, "offset": 0, "processed": 1}
    assert receipt["receipt_count"] == 1


def test_lead_receipt_and_checkpoint_recover_after_fence_rollover(
    neo4j_driver: Driver,
) -> None:
    """A lead handoff committed at N is recoverable by the N+1 claimant."""
    parameters = _parameters()
    parameters.update(
        census_id="source-child-lead",
        stream_kind="lead",
        task_id="lead-task",
        fence_owner_id="lead-task",
        parent_task_id="parent-lead-task",
        call_intent_id="lead-page-intent",
        receipt_key="source-child-lead:1:lead:lead-page-intent",
        content_digest="sha256:" + "f" * 64,
        proposed_cursor=6,
        proposed_binding_subject=None,
        proposed_binding_offset=None,
        source_receipts_json=json.dumps(
            [
                {
                    "row_id": 6,
                    "source_record_pk": "lead-source-pk",
                    "source_record_version": 1,
                    "record_hash": "sha256:" + "d" * 64,
                    "observed_at": "2026-08-28T00:00:00Z",
                    "lead_company_id": "303",
                }
            ],
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    _prepare_claimable_publication(parameters)
    _seed_contact(neo4j_driver, parameters)
    with neo4j_driver.session() as session:
        assert session.run(CLAIM_PAGE, **parameters).single(strict=True)["decision"] == "apply"
        session.run(FINALIZE_PAGE, **parameters).consume()

    token, owner = _expire_and_reclaim(neo4j_driver, parameters)
    assert token == 3 and owner == "recovery-task"
    parameters["fence_token"] = token
    parameters["fence_owner_id"] = owner
    parameters["last_committed_id"] = 6
    with neo4j_driver.session() as session:
        checkpoint = session.run(
            "MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id, "
            "stream_kind: 'lead'}) RETURN checkpoint.fence_token AS token, "
            "checkpoint.last_committed_id AS cursor, checkpoint.processed_rows AS processed",
            **parameters,
        ).single(strict=True)
        receipt = session.run(READ_PENDING_LEAD_RECEIPT, **parameters).single(strict=True)
    assert dict(checkpoint) == {"token": 3, "cursor": 6, "processed": 1}
    assert receipt["receipt_count"] == 1
    assert '"lead_company_id":"303"' in receipt["source_receipts_json"]


def test_legacy_membership_head_without_source_key_advances_with_exact_cas(
    neo4j_driver: Driver,
) -> None:
    """Existing selected heads remain visible: source_key is not a required migration."""
    parameters = _parameters()
    parameters["request_json"] = _canonical_contact_request()
    _seed_contact(neo4j_driver, parameters)
    with neo4j_driver.session() as session:
        assert session.run(CLAIM_PAGE, **parameters).single(strict=True)["decision"] == "apply"
        session.run(FINALIZE_PAGE, **parameters).consume()
    legacy = _legacy_membership_head(parameters)
    legacy_record = legacy.snapshot_record
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:CrmCompanyMembershipHead {source_instance_id: $source_instance_id, "
            "control_instance_id: $control_instance_id, subject_kind: 'contact', subject_id: '6', "
            "selected_snapshot_id: $snapshot_id, available_at: datetime($available_at), "
            "source_record_version: $source_record_version, source_record_pk: $source_record_pk})",
            source_instance_id=legacy.scope.source_instance_id,
            control_instance_id=legacy.scope.control_instance_id,
            snapshot_id=legacy_record.snapshot_id,
            available_at=legacy_record.availability.available_at,
            source_record_version=legacy_record.source_record_version,
            source_record_pk=legacy_record.source_record_pk,
        ).consume()

    assert _commit_empty_contact_membership(neo4j_driver, parameters, legacy) == "committed"
    with neo4j_driver.session() as session:
        head = session.run(
            "MATCH (head:CrmCompanyMembershipHead {source_instance_id: 'portal-a', "
            "control_instance_id: 'control-a', subject_kind: 'contact', subject_id: '6'}) "
            "RETURN head.source_key AS source_key, head.selected_snapshot_id AS snapshot_id",
        ).single(strict=True)
    assert head["source_key"] is None
    assert head["snapshot_id"] != legacy_record.snapshot_id


@pytest.mark.parametrize(
    "field", ("fence_token", "task_id", "payload_digest", "control_instance_id")
)
def test_deferred_contact_claim_fails_closed_for_stale_child_authority(
    neo4j_driver: Driver,
    field: str,
) -> None:
    parameters = _parameters()
    _seed_contact(neo4j_driver, parameters)
    rejected = dict(parameters)
    value = rejected[field]
    rejected[field] = value + 1 if isinstance(value, int) else f"stale-{value}"
    with neo4j_driver.session() as session:
        assert session.run(CLAIM_PAGE, **rejected).single() is None
        row = session.run(
            """
            MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id, stream_kind: 'contact'})
            OPTIONAL MATCH (receipt:StandaloneCrmSourceFactPageReceipt)
            RETURN checkpoint.last_committed_id AS cursor, checkpoint.processed_rows AS processed,
              count(receipt) AS receipts
            """,
            **parameters,
        ).single(strict=True)
    assert dict(row) == {"cursor": 5, "processed": 0, "receipts": 0}
