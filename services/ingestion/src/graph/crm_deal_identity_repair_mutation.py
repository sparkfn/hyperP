"""One managed Neo4j transaction for a qualified CRM-deal repair unit."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypedDict, TypeVar, cast

from neo4j import ManagedTransaction, Record

from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.inventory import (
    RepairInventoryReadClient,
    collect_repair_inventory,
)
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.crm_deal_identity_repair.mutation_classifier import (
    build_repair_plan,
    parse_repair_inventory,
)
from src.crm_deal_identity_repair.mutation_models import (
    MutationFailureStage,
    ProvenanceClass,
    RepairAtomicMutationResult,
    RepairAuthorityEvidence,
    RepairMutationCommand,
    RepairMutationPlan,
    RepairRollbackPayload,
    build_outbox_digest,
    build_result_digest,
)
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_mutation_records import atomic_result_from_record
from src.graph.queries.crm_deal_identity_repair_mutation import (
    ACTIVATE_REPAIRED_SOURCE_RECORD,
    CREATE_REPAIR_DECISION,
    CREATE_REPAIR_REVIEW_CASE,
    CREATE_REPAIRED_SOURCE_RECORD,
    FIND_COMMITTED_REPAIR_MUTATION,
    LOCK_AND_ASSERT_REPAIR_MUTATION_GUARD,
    LOCK_REPAIR_MUTATION_UNIT,
    LOCK_SUPPORT_SOURCE_RECORDS,
    PERSIST_REPAIR_MUTATION_LEDGER,
    READ_LOCKED_REPAIR_AUTHORITY,
    READ_MUTATION_GRAPH_SNAPSHOT,
    RETIRE_EXACT_CONTAMINATION,
    STAGE_ACTIVE_REPAIR_LINK,
    STAGE_PROVISIONAL_REPAIR_LINK,
    STAGE_REPAIR_FACTS,
    STAGE_REPAIR_IDENTIFIERS,
    STAGE_REVIEW_SOURCE_RECORD,
    VERIFY_REPAIRED_MUTATION_POSTCONDITIONS,
)
from src.identifier_scopes import identifier_scope
from src.models import EngineType, JsonValue, MatchDecision, MatchResult, SourceRecordEnvelope
from src.pipeline_crm_identity import projected_identifiers
from src.pipeline_normalization import (
    normalize_envelope_attributes,
    normalize_envelope_identifiers,
)
from src.source_version_keys import encode_source_version_key

T = TypeVar("T")


class _GuardParameters(TypedDict):
    run_id: str
    unit_id: str
    generation: int
    sequence: int
    attempt: int
    boundary_digest: str
    unit_fingerprint: str
    fence_id: str
    owner_id: str
    fence_token: str
    source_instance_id: str
    control_instance_id: str
    source_record_pk: str
    source_record_id: str
    mutation_id: str


class _SourceParameters(TypedDict):
    old_source_record_pk: str
    new_source_record_pk: str
    source_record_id: str
    source_instance_id: str
    source_record_version: str
    source_version_key: str
    entity_key: str
    observed_at: str
    record_hash: str
    raw_payload: str
    normalized_payload: str
    deal_id: str
    identity_link_key: str
    mutation_id: str


class _LedgerParameters(TypedDict):
    run_id: str
    unit_id: str
    generation: int
    sequence: int
    attempt: int
    owner_id: str
    fence_id: str
    fence_token: str
    boundary_digest: str
    unit_fingerprint: str
    expected_unit_state: str
    unit_state: str
    mutation_id: str
    rollback_image_id: str
    checkpoint_id: str
    outbox_event_id: str
    source_fingerprint: str
    image_digest: str
    repaired_state_digest: str
    evidence_digest: str
    payload_digest: str
    rollback_payload_json: str
    result_digest: str
    request_digest: str
    checkpoint_digest: str
    outbox_payload_digest: str
    outcome: str
    control_instance_id: str


class RepairMutationDriftError(RuntimeError):
    """Raised before commit when immutable request or frozen evidence differs."""


class RepairMutationAuthorityError(RuntimeError):
    """Raised when run, unit, fence, control, source, or reconstruction authority is absent."""


@dataclass(frozen=True)
class _TransactionReader(RepairInventoryReadClient):
    transaction: ManagedTransaction

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        return work(self.transaction)


class CrmDealIdentityRepairMutationRepository:
    """Commit one deal mutation and every ledger effect in one Neo4j transaction."""

    def __init__(
        self,
        client: Neo4jClient,
        *,
        failpoint: Callable[[MutationFailureStage], None] | None = None,
    ) -> None:
        self._client = client
        self._failpoint = failpoint

    def commit_atomic_mutation(self, request: RepairMutationCommand) -> RepairAtomicMutationResult:
        """Commit or exactly replay one pre-authorized unit."""
        return self._client.execute_write(lambda tx: self._commit(tx, request))

    def _commit(
        self,
        tx: ManagedTransaction,
        request: RepairMutationCommand,
    ) -> RepairAtomicMutationResult:
        self._lock_unit(tx, request)
        replay = self._replay(tx, request)
        if replay is not None:
            return replay
        guard = self._guard(tx, request)
        self._fail("after_guard")
        current_item = _current_inventory_item(tx, request)
        _assert_frozen_inventory(request.inventory, current_item)
        self._fail("after_source_lock")
        entity_key = _required_record_string(guard, "entity_key")
        parsed = parse_repair_inventory(current_item, request.source_instance_id, entity_key)
        evidence = _authority_evidence(tx, request, parsed.current_owner_ids)
        _lock_support_records(tx, request, evidence)
        plan = build_repair_plan(parsed, _match_result(parsed.current_owner_ids), evidence)
        if plan.source_record_payload is None or parsed.envelope is None:
            raise RepairMutationAuthorityError("qualified deal cannot be reconstructed as v2")
        self._fail("after_classification")
        expected_state = _expected_state(parsed.envelope, plan)
        snapshot = _snapshot(tx, request, plan.retired_source_record_pks)
        rollback = _rollback_payload(request, plan, snapshot, expected_state)
        self._fail("after_rollback_image")
        self._create_source(tx, request, plan)
        self._fail("after_source_record")
        tx.run(
            RETIRE_EXACT_CONTAMINATION,
            retired_source_record_pks=list(plan.retired_source_record_pks),
            mutation_id=request.mutation_id,
        ).consume()
        self._fail("after_retirement")
        decision_id = request.mutation_id + ":decision"
        self._create_decision(tx, request, plan, decision_id)
        self._fail("after_decision")
        self._stage_outcome(tx, request, parsed.envelope, plan, decision_id)
        self._fail("after_staging")
        observed_state = _postcondition_state(tx, plan.source_record_pk)
        if observed_state != expected_state:
            raise RuntimeError("repair transaction-local postcondition digest differs")
        self._guard(tx, request)
        result_digest = build_result_digest(request, plan, rollback)
        outbox_digest = build_outbox_digest(request, result_digest)
        persisted = tx.run(
            PERSIST_REPAIR_MUTATION_LEDGER,
            **_ledger_parameters(request, plan, rollback, result_digest, outbox_digest),
        ).single()
        if persisted is None:
            raise RepairMutationAuthorityError("repair ledger CAS rejected the mutation")
        self._fail("after_ledger")
        self._fail("after_checkpoint")
        self._fail("after_outbox")
        self._fail("after_postcondition")
        bundle = tx.run(
            FIND_COMMITTED_REPAIR_MUTATION,
            run_id=request.unit.run_id,
            unit_id=request.unit.unit_id,
        ).single()
        if bundle is None:
            raise RuntimeError("committed repair bundle readback is missing")
        return atomic_result_from_record(bundle, replayed=False)

    def _lock_unit(self, tx: ManagedTransaction, request: RepairMutationCommand) -> None:
        row = tx.run(
            LOCK_REPAIR_MUTATION_UNIT,
            run_id=request.unit.run_id,
            unit_id=request.unit.unit_id,
            mutation_id=request.mutation_id,
        ).single()
        if row is None:
            replay = self._replay(tx, request)
            if replay is not None:
                return
            raise RepairMutationDriftError("repair unit is bound to another mutation request")

    def _replay(
        self,
        tx: ManagedTransaction,
        request: RepairMutationCommand,
    ) -> RepairAtomicMutationResult | None:
        row = tx.run(
            FIND_COMMITTED_REPAIR_MUTATION,
            run_id=request.unit.run_id,
            unit_id=request.unit.unit_id,
        ).single()
        if row is None:
            return None
        result = _record_object(row, "result")
        if (
            result.get("mutation_id") != request.mutation_id
            or result.get("request_digest") != request.request_digest
        ):
            raise RepairMutationDriftError("repair unit was committed with changed bound evidence")
        return atomic_result_from_record(row, replayed=True)

    def _guard(self, tx: ManagedTransaction, request: RepairMutationCommand) -> Record:
        row = tx.run(
            LOCK_AND_ASSERT_REPAIR_MUTATION_GUARD,
            **_guard_parameters(request),
        ).single()
        if row is None:
            raise RepairMutationAuthorityError(
                "repair run/unit/fence/control/source guard rejected"
            )
        return row

    def _create_source(
        self,
        tx: ManagedTransaction,
        request: RepairMutationCommand,
        plan: RepairMutationPlan,
    ) -> None:
        row = tx.run(CREATE_REPAIRED_SOURCE_RECORD, **_source_values(request, plan)).single()
        if row is None or row["source_record_pk"] != plan.source_record_pk:
            raise RepairMutationDriftError("repair source-version CAS rejected")

    def _create_decision(
        self,
        tx: ManagedTransaction,
        request: RepairMutationCommand,
        plan: RepairMutationPlan,
        decision_id: str,
    ) -> None:
        row = tx.run(
            CREATE_REPAIR_DECISION,
            new_source_record_pk=plan.source_record_pk,
            match_decision_id=decision_id,
            mutation_id=request.mutation_id,
            decision="merge" if plan.disposition == "applied" else "review",
            reason_codes=list(plan.reason_codes),
            review_candidate_person_ids=list(plan.current_owner_ids),
            feature_snapshot=_canonical_json(
                {
                    "authority_digest": plan.authority_digest,
                    "repair_mutation_id": request.mutation_id,
                }
            ),
        ).single()
        if row is None:
            raise RuntimeError("repair decision creation failed")

    def _stage_outcome(
        self,
        tx: ManagedTransaction,
        request: RepairMutationCommand,
        envelope: SourceRecordEnvelope,
        plan: RepairMutationPlan,
        decision_id: str,
    ) -> None:
        if plan.disposition == "applied":
            assert plan.selected_person_id is not None
            _stage_active(tx, request, envelope, plan, decision_id)
            lifecycle_query = ACTIVATE_REPAIRED_SOURCE_RECORD
        else:
            _stage_review(tx, request, envelope, plan, decision_id)
            lifecycle_query = STAGE_REVIEW_SOURCE_RECORD
        row = tx.run(
            lifecycle_query,
            old_source_record_pk=request.inventory.source_record_pk,
            new_source_record_pk=plan.source_record_pk,
        ).single()
        if row is None:
            raise RepairMutationDriftError("repair source lifecycle CAS rejected")

    def _fail(self, stage: MutationFailureStage) -> None:
        if self._failpoint is not None:
            self._failpoint(stage)


def _stage_active(
    tx: ManagedTransaction,
    request: RepairMutationCommand,
    envelope: SourceRecordEnvelope,
    plan: RepairMutationPlan,
    decision_id: str,
) -> None:
    assert plan.selected_person_id is not None
    row = tx.run(
        STAGE_ACTIVE_REPAIR_LINK,
        new_source_record_pk=plan.source_record_pk,
        match_decision_id=decision_id,
        person_id=plan.selected_person_id,
        mutation_id=request.mutation_id,
    ).single()
    if row is None:
        raise RepairMutationDriftError("selected repair Person is absent")
    identifiers = projected_identifiers(envelope, normalize_envelope_identifiers(envelope))
    identifier_rows = [
        {
            "identifier_type": item.identifier_type,
            "identifier_scope": identifier_scope(item.identifier_type, item.source_instance_id),
            "normalized_value": item.normalized_value,
            "source_instance_id": item.source_instance_id,
            "is_verified": item.is_verified,
            "quality_flag": item.quality_flag.value,
        }
        for item in identifiers
        if item.quality_flag.value != "invalid_format"
    ]
    if identifier_rows:
        tx.run(
            STAGE_REPAIR_IDENTIFIERS,
            person_id=plan.selected_person_id,
            source_record_pk=plan.source_record_pk,
            identifiers=identifier_rows,
        ).consume()
    facts = [
        {
            "attribute_name": item.attribute_name,
            "attribute_value": item.attribute_value,
            "quality_flag": item.quality_flag.value,
        }
        for item in normalize_envelope_attributes(envelope)
        if item.quality_flag.value != "invalid_format"
    ]
    if facts and envelope.observed_at is not None:
        tx.run(
            STAGE_REPAIR_FACTS,
            person_id=plan.selected_person_id,
            source_record_pk=plan.source_record_pk,
            observed_at=envelope.observed_at,
            facts=facts,
        ).consume()


def _stage_review(
    tx: ManagedTransaction,
    request: RepairMutationCommand,
    envelope: SourceRecordEnvelope,
    plan: RepairMutationPlan,
    decision_id: str,
) -> None:
    if plan.provisional_person_id is not None:
        row = tx.run(
            STAGE_PROVISIONAL_REPAIR_LINK,
            new_source_record_pk=plan.source_record_pk,
            match_decision_id=decision_id,
            person_id=plan.provisional_person_id,
            mutation_id=request.mutation_id,
        ).single()
        if row is None:
            raise RepairMutationDriftError("provisional repair Person is absent")
    if envelope.observed_at is None:
        raise RepairMutationAuthorityError("review staging requires frozen observed_at")
    observed = datetime.fromisoformat(envelope.observed_at.replace("Z", "+00:00"))
    tx.run(
        CREATE_REPAIR_REVIEW_CASE,
        match_decision_id=decision_id,
        review_case_id=request.mutation_id + ":review",
        mutation_id=request.mutation_id,
        sla_due_at=(observed + timedelta(days=7)).isoformat(),
    ).consume()


def _authority_evidence(
    tx: ManagedTransaction,
    request: RepairMutationCommand,
    current_owner_ids: tuple[str, ...],
) -> tuple[RepairAuthorityEvidence, ...]:
    evidence: list[RepairAuthorityEvidence] = []
    for row in tx.run(
        READ_LOCKED_REPAIR_AUTHORITY,
        source_record_pk=request.inventory.source_record_pk,
        source_instance_id=request.source_instance_id,
    ):
        person_id = _required_record_string(row, "person_id")
        independent = _object_rows(row["independent_rows"])
        reviewed = _object_rows(row["reviewed_rows"])
        historical = _object_rows(row["historical_rows"])
        self_rows = _object_rows(row["self_rows"])
        blocked = _record_int(row, "active_no_match_locks") > 0 or len(current_owner_ids) > 1
        provenance: ProvenanceClass
        if blocked:
            provenance = "blocked_or_conflicting"
            rows = (*independent, *reviewed, *historical, *self_rows)
        elif independent:
            provenance = "independent_trusted"
            rows = tuple(independent)
        elif reviewed:
            provenance = "reviewed_v2"
            rows = tuple(reviewed)
        elif len(self_rows) > 1:
            provenance = "self_supporting"
            rows = tuple(self_rows)
        else:
            provenance = "historical_deal_only"
            rows = tuple(historical or self_rows)
        source_pks = tuple(
            sorted(
                {
                    value
                    for item in rows
                    if isinstance(value := item.get("source_record_pk"), str) and value
                }
                or {request.inventory.source_record_pk}
            )
        )
        evidence.append(RepairAuthorityEvidence(person_id, provenance, source_pks, rows))
    return tuple(evidence)


def _lock_support_records(
    tx: ManagedTransaction,
    request: RepairMutationCommand,
    evidence: tuple[RepairAuthorityEvidence, ...],
) -> None:
    rows = [
        {"source_record_pk": source_record_pk}
        for item in evidence
        if item.provenance_class in {"independent_trusted", "reviewed_v2"}
        for source_record_pk in item.source_record_pks
        if source_record_pk != request.inventory.source_record_pk
    ]
    if rows:
        tx.run(
            LOCK_SUPPORT_SOURCE_RECORDS,
            support_rows=rows,
            mutation_id=request.mutation_id,
        ).consume()


def _current_inventory_item(
    tx: ManagedTransaction,
    request: RepairMutationCommand,
) -> RepairInventoryItem:
    inventory = collect_repair_inventory(_TransactionReader(tx))
    matches = [
        item
        for item in inventory.items
        if item.source_record_pk == request.inventory.source_record_pk
    ]
    if len(matches) != 1:
        raise RepairMutationDriftError("qualified source record is absent from current inventory")
    return matches[0]


def _assert_frozen_inventory(expected: RepairInventoryItem, observed: RepairInventoryItem) -> None:
    if (
        expected.inventory_key != observed.inventory_key
        or expected.graph_fingerprint != observed.graph_fingerprint
        or expected.stored_payload_fingerprint != observed.stored_payload_fingerprint
    ):
        raise RepairMutationDriftError("frozen graph or stored-payload fingerprint changed")


def _match_result(current_owner_ids: tuple[str, ...]) -> MatchResult:
    if len(current_owner_ids) == 1:
        return MatchResult(
            decision=MatchDecision.MERGE,
            confidence=1.0,
            matched_person_id=current_owner_ids[0],
            reasons=["current_deal_owner_candidate"],
            engine_type=EngineType.DETERMINISTIC,
        )
    return MatchResult(
        decision=MatchDecision.REVIEW,
        confidence=1.0,
        review_candidate_person_ids=list(current_owner_ids),
        reasons=["repair_owner_is_ambiguous_or_missing"],
        engine_type=EngineType.DETERMINISTIC,
    )


def _snapshot(
    tx: ManagedTransaction,
    request: RepairMutationCommand,
    retired_source_record_pks: tuple[str, ...],
) -> dict[str, JsonValue]:
    row = tx.run(
        READ_MUTATION_GRAPH_SNAPSHOT,
        source_record_pk=request.inventory.source_record_pk,
        retired_source_record_pks=list(retired_source_record_pks),
    ).single()
    if row is None:
        raise RepairMutationDriftError("repair rollback snapshot source is missing")
    snapshot = {key: _json_value(row[key]) for key in row.keys()}
    relationships = snapshot.get("relationships")
    if isinstance(relationships, list):
        snapshot["relationships"] = _ordinal_relationships(relationships)
    return snapshot


def _ordinal_relationships(rows: list[JsonValue]) -> list[JsonValue]:
    result: list[JsonValue] = []
    counts: dict[str, int] = {}
    for value in rows:
        if not isinstance(value, dict):
            raise RuntimeError("rollback relationship snapshot is malformed")
        row = dict(value)
        row["left_identity"] = _endpoint_identity(
            row.get("left_labels"), row.get("left_properties")
        )
        row["right_identity"] = _endpoint_identity(
            row.get("right_labels"), row.get("right_properties")
        )
        key = _canonical_json(row)
        ordinal = counts.get(key, 0)
        counts[key] = ordinal + 1
        row["multiplicity_ordinal"] = ordinal
        result.append(row)
    return result


def _endpoint_identity(labels: JsonValue | None, properties: JsonValue | None) -> JsonValue:
    if not isinstance(labels, list) or not isinstance(properties, dict):
        raise RuntimeError("rollback relationship endpoint is malformed")
    for key in (
        "source_record_pk",
        "person_id",
        "match_decision_id",
        "review_case_id",
        "identifier_key",
        "address_id",
        "fact_id",
        "entity_key",
    ):
        value = properties.get(key)
        if isinstance(value, str) and value:
            return {"labels": labels, "key": key, "value": value}
    return {
        "labels": labels,
        "properties_digest": object_digest(b"graph-endpoint-v1\x00", properties),
    }


def _rollback_payload(
    request: RepairMutationCommand,
    plan: RepairMutationPlan,
    snapshot: dict[str, JsonValue],
    expected_state: dict[str, JsonValue],
) -> RepairRollbackPayload:
    return RepairRollbackPayload(
        payload={
            "contract_version": request.mutation_contract_version,
            "request": request.to_dict(),
            "pre_state": snapshot,
            "rollback_operations": [
                {
                    "operation": "delete_relationships_by_repair_mutation_id",
                    "repair_mutation_id": request.mutation_id,
                },
                {
                    "operation": "delete_created_nodes",
                    "source_record_pk": plan.source_record_pk,
                    "match_decision_id": request.mutation_id + ":decision",
                    "review_case_id": request.mutation_id + ":review",
                },
                {
                    "operation": "restore_source_and_relationship_properties",
                    "source_record_pk": request.inventory.source_record_pk,
                    "relationships": snapshot.get("relationships", []),
                },
            ],
        },
        expected_repaired_state=expected_state,
    )


def _expected_state(
    envelope: SourceRecordEnvelope,
    plan: RepairMutationPlan,
) -> dict[str, JsonValue]:
    if plan.disposition == "review_required":
        return {
            "lifecycle_status": "pending_review",
            "active_links": 0,
            "provisional_links": 1 if plan.provisional_person_id else 0,
            "authoritative_links": 0,
            "active_evidence": 0,
        }
    identifiers = projected_identifiers(envelope, normalize_envelope_identifiers(envelope))
    facts = normalize_envelope_attributes(envelope)
    return {
        "lifecycle_status": "active",
        "active_links": 1,
        "provisional_links": 0,
        "authoritative_links": 1,
        "active_evidence": len(identifiers) + len(facts),
    }


def _postcondition_state(tx: ManagedTransaction, source_record_pk: str) -> dict[str, JsonValue]:
    row = tx.run(
        VERIFY_REPAIRED_MUTATION_POSTCONDITIONS,
        new_source_record_pk=source_record_pk,
    ).single()
    if row is None:
        raise RuntimeError("repair postcondition readback is missing")
    return {
        "lifecycle_status": _required_record_string(row, "lifecycle_status"),
        "active_links": _record_int(row, "active_links"),
        "provisional_links": _record_int(row, "provisional_links"),
        "authoritative_links": _record_int(row, "authoritative_links"),
        "active_evidence": _record_int(row, "active_evidence"),
    }


def _source_values(
    request: RepairMutationCommand,
    plan: RepairMutationPlan,
) -> _SourceParameters:
    payload = plan.source_record_payload
    if payload is None:
        raise RepairMutationAuthorityError("repair source payload is unavailable")
    source_record_id = _payload_string(payload, "source_record_id")
    raw_payload = _payload_object(payload, "raw_payload")
    normalized_payload: dict[str, JsonValue] = {
        "identifiers": _json_value(payload.get("identifiers", [])),
        "addresses": _json_value(payload.get("addresses", [])),
        "attributes": _json_value(payload.get("attributes", {})),
    }
    return {
        "old_source_record_pk": request.inventory.source_record_pk,
        "new_source_record_pk": plan.source_record_pk,
        "source_record_id": source_record_id,
        "source_instance_id": request.source_instance_id,
        "source_record_version": str(plan.source_record_version),
        "source_version_key": encode_source_version_key(
            "bitrix_chat",
            source_record_id,
            str(plan.source_record_version),
            source_instance_id=request.source_instance_id,
        ),
        "entity_key": _payload_string(payload, "entity_key"),
        "observed_at": _payload_string(payload, "observed_at"),
        "record_hash": _payload_string(payload, "record_hash"),
        "raw_payload": _canonical_json(raw_payload),
        "normalized_payload": _canonical_json(normalized_payload),
        "deal_id": request.inventory.deal_id,
        "identity_link_key": _payload_string(payload, "identity_link_key"),
        "mutation_id": request.mutation_id,
    }


def _ledger_parameters(
    request: RepairMutationCommand,
    plan: RepairMutationPlan,
    rollback: RepairRollbackPayload,
    result_digest: str,
    outbox_digest: str,
) -> _LedgerParameters:
    checkpoint_digest = object_digest(
        b"crm-deal-identity-repair-checkpoint-v1\x00",
        {"result_digest": result_digest},
    )
    return {
        "run_id": request.unit.run_id,
        "unit_id": request.unit.unit_id,
        "generation": request.unit.generation,
        "sequence": request.unit.sequence,
        "attempt": request.unit.attempt,
        "owner_id": request.fence.owner_id,
        "fence_id": request.fence.fence_id,
        "fence_token": request.fence.token,
        "boundary_digest": request.unit.boundary_digest,
        "unit_fingerprint": request.unit.inventory_fingerprint,
        "expected_unit_state": request.unit.state,
        "unit_state": plan.disposition,
        "mutation_id": request.mutation_id,
        "rollback_image_id": request.rollback_image_id,
        "checkpoint_id": request.checkpoint_id,
        "outbox_event_id": request.outbox_event_id,
        "source_fingerprint": request.inventory.graph_fingerprint,
        "image_digest": rollback.image_digest,
        "repaired_state_digest": rollback.expected_repaired_digest,
        "evidence_digest": plan.authority_digest,
        "payload_digest": rollback.image_digest,
        "rollback_payload_json": _canonical_json(rollback.to_dict()),
        "result_digest": result_digest,
        "request_digest": request.request_digest,
        "checkpoint_digest": checkpoint_digest,
        "outbox_payload_digest": outbox_digest,
        "outcome": plan.disposition,
        "control_instance_id": request.control_instance_id,
    }


def _guard_parameters(request: RepairMutationCommand) -> _GuardParameters:
    return {
        "run_id": request.unit.run_id,
        "unit_id": request.unit.unit_id,
        "generation": request.unit.generation,
        "sequence": request.unit.sequence,
        "attempt": request.unit.attempt,
        "boundary_digest": request.unit.boundary_digest,
        "unit_fingerprint": request.unit.inventory_fingerprint,
        "fence_id": request.fence.fence_id,
        "owner_id": request.fence.owner_id,
        "fence_token": request.fence.token,
        "source_instance_id": request.source_instance_id,
        "control_instance_id": request.control_instance_id,
        "source_record_pk": request.inventory.source_record_pk,
        "source_record_id": request.inventory.source_record_id,
        "mutation_id": request.mutation_id,
    }


def _record_object(row: Record, key: str) -> dict[str, JsonValue]:
    value = row[key]
    if not isinstance(value, dict):
        raise RuntimeError("repair mutation readback is malformed: " + key)
    return {cast(str, item_key): _json_value(item) for item_key, item in value.items()}


def _required_record_string(row: Record, key: str) -> str:
    value = row[key]
    if not isinstance(value, str) or not value:
        raise RuntimeError("repair graph row is malformed: " + key)
    return value


def _record_int(row: Record, key: str) -> int:
    value: object = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("repair graph count is malformed: " + key)
    return value


def _object_rows(value: object) -> list[dict[str, JsonValue]]:
    if not isinstance(value, list):
        raise RuntimeError("repair authority rows are malformed")
    rows: list[dict[str, JsonValue]] = []
    for item in value:
        if item is None:
            continue
        converted = _json_value(item)
        if not isinstance(converted, dict):
            raise RuntimeError("repair authority row is malformed")
        rows.append(converted)
    return rows


def _payload_object(payload: dict[str, JsonValue], key: str) -> dict[str, JsonValue]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise RepairMutationAuthorityError("repair source payload is malformed: " + key)
    return value


def _payload_string(payload: dict[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RepairMutationAuthorityError("repair source payload is malformed: " + key)
    return value


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise RuntimeError("repair graph object has non-string keys")
        return {cast(str, key): _json_value(item) for key, item in value.items()}
    if hasattr(value, "iso_format"):
        formatted = value.iso_format()
        if isinstance(formatted, str):
            return formatted
    raise RuntimeError("repair graph value is not JSON serializable")


def _canonical_json(value: dict[str, JsonValue]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
