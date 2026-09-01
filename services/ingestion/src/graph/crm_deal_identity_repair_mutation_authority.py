"""Authority serialization and outcome staging for CRM-deal repair mutations."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeVar

from neo4j import ManagedTransaction

from src.crm_deal_identity_repair.inventory import (
    RepairInventoryReadClient,
    collect_repair_inventory,
)
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.crm_deal_identity_repair.mutation_models import (
    ProvenanceClass,
    RepairAuthorityEvidence,
    RepairMutationCommand,
    RepairMutationPlan,
)
from src.graph.crm_deal_identity_repair_mutation_errors import (
    RepairMutationDriftError,
)
from src.graph.crm_deal_identity_repair_mutation_payloads import (
    _object_rows,
    _record_int,
    _required_record_string,
)
from src.graph.queries.crm_deal_identity_repair_mutation import (
    CREATE_REPAIR_REVIEW_CASE,
    LOCK_SUPPORT_SOURCE_RECORDS,
    READ_LOCKED_REPAIR_AUTHORITY,
    STAGE_ACTIVE_REPAIR_LINK,
    STAGE_PROVISIONAL_REPAIR_LINK,
    STAGE_REPAIR_FACTS,
    STAGE_REPAIR_IDENTIFIERS,
)
from src.identifier_scopes import identifier_scope
from src.models import EngineType, JsonValue, MatchDecision, MatchResult, SourceRecordEnvelope
from src.pipeline_crm_identity import projected_identifiers
from src.pipeline_normalization import normalize_envelope_attributes, normalize_envelope_identifiers

T = TypeVar("T")


@dataclass(frozen=True)
class _TransactionReader(RepairInventoryReadClient):
    transaction: ManagedTransaction

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        return work(self.transaction)


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
            mutation_id=request.mutation_id,
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
            mutation_id=request.mutation_id,
        ).consume()


def _stage_review(
    tx: ManagedTransaction,
    request: RepairMutationCommand,
    envelope: SourceRecordEnvelope | None,
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
    sla_due_at: str | None = None
    if envelope is not None and envelope.observed_at is not None:
        observed = datetime.fromisoformat(envelope.observed_at.replace("Z", "+00:00"))
        sla_due_at = (observed + timedelta(days=7)).isoformat()
    tx.run(
        CREATE_REPAIR_REVIEW_CASE,
        match_decision_id=decision_id,
        review_case_id=request.mutation_id + ":review",
        mutation_id=request.mutation_id,
        sla_due_at=sla_due_at,
    ).consume()


def _authority_evidence(
    tx: ManagedTransaction,
    request: RepairMutationCommand,
    current_owner_ids: tuple[str, ...],
    *,
    source_record_pk: str | None = None,
) -> tuple[RepairAuthorityEvidence, ...]:
    evidence: list[RepairAuthorityEvidence] = []
    for row in tx.run(
        READ_LOCKED_REPAIR_AUTHORITY,
        source_record_pk=source_record_pk or request.inventory.source_record_pk,
        source_instance_id=request.source_instance_id,
        control_instance_id=request.control_instance_id,
        owner_ids=list(current_owner_ids),
    ):
        person_id = _required_record_string(row, "person_id")
        independent = _object_rows(row["independent_rows"])
        reviewed = _object_rows(row["reviewed_rows"])
        historical = _object_rows(row["historical_rows"])
        self_rows = _object_rows(row["self_rows"])
        classified: tuple[tuple[ProvenanceClass, list[dict[str, JsonValue]]], ...] = (
            ("independent_trusted", independent),
            ("reviewed_v2", reviewed),
            ("historical_deal_only", historical),
            ("self_supporting", self_rows),
        )
        for provenance, rows in classified:
            if rows:
                evidence.append(_evidence_item(person_id, provenance, rows, request))
        active_no_match_locks = _record_int(row, "active_no_match_locks")
        if active_no_match_locks > 0 or len(current_owner_ids) > 1:
            evidence.append(
                RepairAuthorityEvidence(
                    person_id,
                    "blocked_or_conflicting",
                    (request.inventory.source_record_pk,),
                    (
                        {
                            "active_no_match_lock_count": active_no_match_locks,
                            "current_owner_count": len(current_owner_ids),
                        },
                    ),
                )
            )
    return tuple(sorted(evidence, key=_evidence_sort_key))


def _evidence_item(
    person_id: str,
    provenance: ProvenanceClass,
    rows: list[dict[str, JsonValue]],
    request: RepairMutationCommand,
) -> RepairAuthorityEvidence:
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
    return RepairAuthorityEvidence(
        person_id,
        provenance,
        source_pks,
        tuple(sorted(rows, key=_canonical_row_json)),
    )


def _evidence_sort_key(item: RepairAuthorityEvidence) -> tuple[str, str, tuple[str, ...], str]:
    return (
        item.person_id,
        item.provenance_class,
        item.source_record_pks,
        json.dumps(
            list(item.evidence_rows), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ),
    )


def _canonical_row_json(row: dict[str, JsonValue]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _lock_support_records(
    tx: ManagedTransaction,
    request: RepairMutationCommand,
    evidence: tuple[RepairAuthorityEvidence, ...],
) -> None:
    support_rows = _support_lock_rows(request, evidence)
    if not support_rows:
        return
    record = tx.run(
        LOCK_SUPPORT_SOURCE_RECORDS,
        support_rows=support_rows,
        mutation_id=request.mutation_id,
    ).single()
    if record is None:
        raise RepairMutationDriftError("supporting authority cannot be serialized exactly")
    source_pks = {row["source_record_pk"] for row in support_rows}
    independent_count = sum(
        1 for row in support_rows if row["provenance_class"] == "independent_trusted"
    )
    reviewed_count = sum(1 for row in support_rows if row["provenance_class"] == "reviewed_v2")
    if (
        _record_int(record, "locked_count") != len(source_pks)
        or _record_int(record, "independent_chain_count") != independent_count
        or _record_int(record, "reviewed_chain_count") != reviewed_count
    ):
        raise RepairMutationDriftError("supporting authority cannot be serialized exactly")


def _support_lock_rows(
    request: RepairMutationCommand,
    evidence: tuple[RepairAuthorityEvidence, ...],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in evidence:
        if item.provenance_class not in {"independent_trusted", "reviewed_v2"}:
            continue
        for row in item.evidence_rows:
            source_record_pk = row.get("source_record_pk")
            if source_record_pk == request.inventory.source_record_pk:
                continue
            if not isinstance(source_record_pk, str) or not source_record_pk:
                raise RepairMutationDriftError("supporting authority identity is malformed")
            lock_row = {
                "provenance_class": item.provenance_class,
                "source_record_pk": source_record_pk,
                "person_id": item.person_id,
            }
            if item.provenance_class == "independent_trusted":
                for key in (
                    "identifier_type",
                    "identifier_scope",
                    "identifier_source_instance_id",
                    "source_entity_id",
                ):
                    value = row.get(key)
                    if not isinstance(value, str) or not value:
                        raise RepairMutationDriftError(
                            "independent authority identity is malformed"
                        )
                    lock_row[key] = value
            else:
                for key in ("match_decision_id", "review_case_id", "resolution"):
                    value = row.get(key)
                    if not isinstance(value, str) or not value:
                        raise RepairMutationDriftError("reviewed authority identity is malformed")
                    lock_row[key] = value
            rows.append(lock_row)
    return sorted(rows, key=lambda row: tuple(sorted(row.items())))


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
