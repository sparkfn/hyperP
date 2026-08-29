"""Neo4j repository for fenced A-S2 company and membership commits."""

from __future__ import annotations

import json
from typing import TypedDict

from neo4j import ManagedTransaction, Record

from src.connectors.bitrix_openlines.models import CrmCompanyBindingPayload
from src.crm_company_contracts import (
    CrmCompanyDescriptionHead,
    CrmCompanyDescriptionObservation,
    CrmCompanyMembershipHead,
    CrmCompanyMembershipObservation,
    CrmCompanyMembershipSnapshotRecord,
    CrmCompanyReference,
)
from src.crm_company_membership_writer import (
    CrmCompanyDescriptionMutation,
    CrmCompanyMembershipCommitResult,
    CrmCompanyMembershipMutation,
    CrmCompanyMembershipUnitMutation,
)
from src.crm_identity_associations import (
    CrmIdentitySubjectType,
    normalize_company_membership_snapshot,
)
from src.graph.client import Neo4jClient
from src.graph.queries.crm_company_membership import (
    CLAIM_DESCRIPTION_TRANSITION,
    CLAIM_MEMBERSHIP_TRANSITION,
    READ_CENSUS_REQUEST,
    READ_CURRENT_DESCRIPTION_HEAD,
    READ_CURRENT_MEMBERSHIP_HEAD,
    UPSERT_COMPANY_REFERENCE,
    UPSERT_DESCRIPTION_OBSERVATION,
    UPSERT_MEMBERSHIP_OBSERVATION,
    UPSERT_MEMBERSHIP_SNAPSHOT,
    VERIFY_COMPANY_REFERENCE,
    VERIFY_DESCRIPTION_OBSERVATION,
    VERIFY_MEMBERSHIP_OBSERVATION,
    VERIFY_MEMBERSHIP_SNAPSHOT,
)
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_census_request_parser import parse_stored_census_request
from src.standalone_crm_census_requests import SourceSyncCensusRequest
from src.standalone_crm_child_contracts import (
    CompanySourceChildEnvelope,
    ContactSourceChildEnvelope,
    LeadSourceChildEnvelope,
    StandaloneCrmSourceAvailability,
    StandaloneCrmSourceChildEnvelope,
    StandaloneCrmSourceChildScope,
)
from src.standalone_crm_unit_repository import (
    StandaloneCrmAtomicUnitCommit,
    StandaloneCrmAtomicUnitRepository,
)


class _ClaimParameters(TypedDict):
    census_id: str
    generation: int
    fence_token: int
    fence_owner_id: str
    source_key: str
    source_instance_id: str
    control_instance_id: str
    frozen_upper_id: int
    task_name: str
    task_id: str
    payload_digest: str
    available_at: str
    attempt_deadline: str
    occurrence_deadline: str
    request_json: str
    expected_checkpoint_absent: bool
    expected_cursor: int
    expected_processed: int
    expected_skipped: int
    proposed_cursor: int
    proposed_processed: int
    proposed_skipped: int
    processed_delta: int
    attempt_row_limit: int
    occurrence_row_limit: int
    expected_head_id: str | None
    expected_head_at: str | None
    expected_head_version: int | None
    expected_head_pk: str | None
    proposed_head_id: str
    proposed_available_at: str
    proposed_head_version: int
    proposed_head_pk: str


class _DescriptionClaimParameters(_ClaimParameters):
    company_id: str


class _MembershipClaimParameters(_ClaimParameters):
    stream_kind: str
    subject_kind: str
    subject_id: str
    expected_binding_subject: int | None
    expected_binding_offset: int | None
    proposed_binding_subject: int | None
    proposed_binding_offset: int | None


class _ReferenceParameters(TypedDict):
    source_key: str
    source_instance_id: str
    control_instance_id: str
    company_id: str
    source_record_id: str
    identity_policy_version: str


class _DescriptionParameters(_ReferenceParameters):
    observation_id: str
    observation_digest: str
    source_record_pk: str
    source_record_version: int
    source_record_hash: str
    description: str | None
    observed_at: str | None
    available_at: str
    contract_version: str


class _SnapshotParameters(TypedDict):
    source_instance_id: str
    control_instance_id: str
    subject_kind: str
    subject_id: str
    snapshot_id: str
    snapshot_digest: str
    source_record_id: str
    source_record_pk: str
    source_record_version: int
    source_record_hash: str
    binding_count: int
    observed_at: str | None
    available_at: str
    contract_version: str


class _MembershipObservationParameters(TypedDict):
    source_instance_id: str
    control_instance_id: str
    subject_kind: str
    subject_id: str
    snapshot_id: str
    company_id: str
    observation_id: str
    sort: int | None
    role_id: str | None
    is_primary: bool


class CrmCompanyMembershipRepository(
    StandaloneCrmAtomicUnitRepository[
        CrmCompanyMembershipUnitMutation,
        CrmCompanyMembershipCommitResult,
    ]
):
    """Commit A-S2 domain facts and the #273 checkpoint in one transaction."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def current_description_head(
        self, scope: StandaloneCrmSourceChildScope, company_id: str
    ) -> CrmCompanyDescriptionHead | None:
        """Read one exact scoped description head for a subsequent-occurrence CAS."""

        def work(tx: ManagedTransaction) -> CrmCompanyDescriptionHead | None:
            record = tx.run(
                READ_CURRENT_DESCRIPTION_HEAD,
                source_key=scope.source_key,
                source_instance_id=scope.source_instance_id,
                control_instance_id=scope.control_instance_id,
                company_id=company_id,
            ).single()
            return None if record is None else _description_head(scope, company_id, record)

        return self._client.execute_read(work)

    def current_membership_head(
        self,
        scope: StandaloneCrmSourceChildScope,
        subject_kind: str,
        subject_id: str,
    ) -> CrmCompanyMembershipHead | None:
        """Read one exact scoped complete membership head for CAS and recovery."""
        if subject_kind not in {"contact", "lead"}:
            raise ValueError("membership head requires contact or lead subject")

        def work(tx: ManagedTransaction) -> CrmCompanyMembershipHead | None:
            record = tx.run(
                READ_CURRENT_MEMBERSHIP_HEAD,
                source_key=scope.source_key,
                source_instance_id=scope.source_instance_id,
                control_instance_id=scope.control_instance_id,
                subject_kind=subject_kind,
                subject_id=subject_id,
            ).single()
            return (
                None
                if record is None
                else _membership_head(scope, subject_kind, subject_id, record)
            )

        return self._client.execute_read(work)

    def commit_unit(
        self,
        request: StandaloneCrmAtomicUnitCommit[CrmCompanyMembershipUnitMutation],
    ) -> CrmCompanyMembershipCommitResult:
        """Validate persisted authority and atomically commit or reject the unit."""
        if request.accounting_delta.failed_rows != 0:
            raise ValueError("A-S2 does not persist failed rows")

        def work(tx: ManagedTransaction) -> CrmCompanyMembershipCommitResult:
            request_json = _read_and_validate_request(tx, request.envelope)
            if request_json is None:
                return CrmCompanyMembershipCommitResult("authority_rejected")
            mutation = request.mutation
            if isinstance(mutation, CrmCompanyDescriptionMutation):
                return _commit_description(tx, request, mutation, request_json)
            if isinstance(mutation, CrmCompanyMembershipMutation):
                return _commit_membership(tx, request, mutation, request_json)
            raise TypeError("unsupported A-S2 mutation type")

        return self._client.execute_write(work)


def _read_and_validate_request(
    tx: ManagedTransaction,
    envelope: StandaloneCrmSourceChildEnvelope,
) -> str | None:
    record = tx.run(
        READ_CENSUS_REQUEST,
        census_id=envelope.unit.census_id,
        generation=envelope.unit.generation,
    ).single()
    if record is None:
        return None
    raw_json = record["request_json"]
    if not isinstance(raw_json, str):
        raise RuntimeError("persisted standalone CRM request is malformed")
    try:
        decoded = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError("persisted standalone CRM request is malformed") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("persisted standalone CRM request is not an object")
    try:
        stored = parse_stored_census_request(decoded)
    except ValueError as exc:
        raise RuntimeError("persisted standalone CRM request is malformed") from exc
    if not isinstance(stored, SourceSyncCensusRequest):
        return None
    budget = envelope.budget_authorization
    if (
        stored.source_key != envelope.scope.source_key
        or stored.source_instance_id != envelope.scope.source_instance_id
        or stored.control_instance_id != envelope.scope.control_instance_id
        or envelope.unit.stream_kind not in stored.selected_kinds
        or stored.budget.max_calls_per_attempt != budget.max_calls_per_attempt
        or stored.budget.max_rows_per_attempt != budget.max_rows_per_attempt
        or stored.budget.max_calls_per_occurrence != budget.max_calls_per_occurrence
        or stored.budget.max_rows_per_occurrence != budget.max_rows_per_occurrence
        or stored.budget.occurrence_deadline != budget.occurrence_deadline
    ):
        return None
    return raw_json


def _commit_description(
    tx: ManagedTransaction,
    request: StandaloneCrmAtomicUnitCommit[CrmCompanyMembershipUnitMutation],
    mutation: CrmCompanyDescriptionMutation,
    request_json: str,
) -> CrmCompanyMembershipCommitResult:
    envelope = request.envelope
    if not isinstance(envelope, CompanySourceChildEnvelope):
        raise ValueError("description mutation requires a company envelope")
    parameters = _description_claim_parameters(request, mutation, request_json)
    decision = _claim_decision(tx.run(CLAIM_DESCRIPTION_TRANSITION, **parameters).single())
    reference = _reference_parameters(mutation.observation.company_reference)
    observation = _description_parameters(mutation.observation)
    if decision.decision == "committed":
        _require_row(tx.run(UPSERT_COMPANY_REFERENCE, **reference).single(), "company reference")
        _require_row(
            tx.run(UPSERT_DESCRIPTION_OBSERVATION, **observation).single(),
            "description observation",
        )
    elif decision.decision == "idempotent":
        _require_row(tx.run(VERIFY_COMPANY_REFERENCE, **reference).single(), "company reference")
        _require_row(
            tx.run(VERIFY_DESCRIPTION_OBSERVATION, **observation).single(),
            "description observation",
        )
    return decision


def _commit_membership(
    tx: ManagedTransaction,
    request: StandaloneCrmAtomicUnitCommit[CrmCompanyMembershipUnitMutation],
    mutation: CrmCompanyMembershipMutation,
    request_json: str,
) -> CrmCompanyMembershipCommitResult:
    envelope = request.envelope
    if not isinstance(envelope, (ContactSourceChildEnvelope, LeadSourceChildEnvelope)):
        raise ValueError("membership mutation requires a contact or lead envelope")
    parameters = _membership_claim_parameters(request, mutation, request_json)
    decision = _claim_decision(tx.run(CLAIM_MEMBERSHIP_TRANSITION, **parameters).single())
    if decision.decision not in {"committed", "idempotent"}:
        return decision
    create = decision.decision == "committed"
    for observation in mutation.observations:
        reference = _reference_parameters(observation.company_reference)
        query = UPSERT_COMPANY_REFERENCE if create else VERIFY_COMPANY_REFERENCE
        _require_row(tx.run(query, **reference).single(), "membership company reference")
    snapshot = _snapshot_parameters(mutation.snapshot_record)
    snapshot_query = UPSERT_MEMBERSHIP_SNAPSHOT if create else VERIFY_MEMBERSHIP_SNAPSHOT
    _require_row(tx.run(snapshot_query, **snapshot).single(), "membership snapshot")
    for observation in mutation.observations:
        item = _membership_observation_parameters(observation)
        query = UPSERT_MEMBERSHIP_OBSERVATION if create else VERIFY_MEMBERSHIP_OBSERVATION
        _require_row(tx.run(query, **item).single(), "membership observation")
    return decision


def _common_claim_parameters(
    request: StandaloneCrmAtomicUnitCommit[CrmCompanyMembershipUnitMutation],
    request_json: str,
    expected_head_id: str | None,
    expected_head_at: str | None,
    expected_head_version: int | None,
    expected_head_pk: str | None,
    proposed_head_id: str,
    proposed_available_at: str,
    proposed_head_version: int,
    proposed_head_pk: str,
) -> _ClaimParameters:
    envelope = request.envelope
    expected = request.expected_checkpoint
    proposed = request.proposed_checkpoint
    return {
        "census_id": envelope.unit.census_id,
        "generation": envelope.unit.generation,
        "fence_token": envelope.unit.fence_token,
        "fence_owner_id": envelope.unit.fence_owner_id,
        "source_key": envelope.scope.source_key,
        "source_instance_id": envelope.scope.source_instance_id,
        "control_instance_id": envelope.scope.control_instance_id,
        "frozen_upper_id": envelope.frozen_upper_id,
        "task_name": envelope.unit.task_name,
        "task_id": envelope.unit.task_id,
        "payload_digest": envelope.unit.payload_digest,
        "available_at": envelope.availability.available_at,
        "attempt_deadline": envelope.budget_authorization.attempt_deadline,
        "occurrence_deadline": envelope.budget_authorization.occurrence_deadline,
        "request_json": request_json,
        "expected_checkpoint_absent": _is_absent_checkpoint(expected),
        "expected_cursor": expected.last_committed_id,
        "expected_processed": expected.processed_rows,
        "expected_skipped": expected.skipped_rows,
        "proposed_cursor": proposed.last_committed_id,
        "proposed_processed": proposed.processed_rows,
        "proposed_skipped": proposed.skipped_rows,
        "processed_delta": request.accounting_delta.processed_rows,
        "attempt_row_limit": envelope.budget_authorization.max_rows_per_attempt,
        "occurrence_row_limit": envelope.budget_authorization.max_rows_per_occurrence,
        "expected_head_id": expected_head_id,
        "expected_head_at": expected_head_at,
        "expected_head_version": expected_head_version,
        "expected_head_pk": expected_head_pk,
        "proposed_head_id": proposed_head_id,
        "proposed_available_at": proposed_available_at,
        "proposed_head_version": proposed_head_version,
        "proposed_head_pk": proposed_head_pk,
    }


def _description_claim_parameters(
    request: StandaloneCrmAtomicUnitCommit[CrmCompanyMembershipUnitMutation],
    mutation: CrmCompanyDescriptionMutation,
    request_json: str,
) -> _DescriptionClaimParameters:
    expected = mutation.compare_and_set.expected_head
    proposed = mutation.compare_and_set.proposed_head
    common = _common_claim_parameters(
        request,
        request_json,
        None if expected is None else expected.observation.observation_id,
        None if expected is None else expected.order_key.available_at,
        None if expected is None else expected.order_key.source_record_version,
        None if expected is None else expected.order_key.source_record_pk,
        proposed.observation.observation_id,
        proposed.order_key.available_at,
        proposed.order_key.source_record_version,
        proposed.order_key.source_record_pk,
    )
    return {**common, "company_id": proposed.company_reference.company_id}


def _membership_claim_parameters(
    request: StandaloneCrmAtomicUnitCommit[CrmCompanyMembershipUnitMutation],
    mutation: CrmCompanyMembershipMutation,
    request_json: str,
) -> _MembershipClaimParameters:
    expected_head = mutation.compare_and_set.expected_head
    proposed_head = mutation.compare_and_set.proposed_head
    common = _common_claim_parameters(
        request,
        request_json,
        None if expected_head is None else expected_head.snapshot_record.snapshot_id,
        None if expected_head is None else expected_head.order_key.available_at,
        None if expected_head is None else expected_head.order_key.source_record_version,
        None if expected_head is None else expected_head.order_key.source_record_pk,
        proposed_head.snapshot_record.snapshot_id,
        proposed_head.order_key.available_at,
        proposed_head.order_key.source_record_version,
        proposed_head.order_key.source_record_pk,
    )
    expected = request.expected_checkpoint
    proposed = request.proposed_checkpoint
    return {
        **common,
        "stream_kind": request.envelope.unit.stream_kind,
        "subject_kind": proposed_head.subject_kind,
        "subject_id": proposed_head.subject_id,
        "expected_binding_subject": expected.binding_subject_id,
        "expected_binding_offset": expected.binding_offset,
        "proposed_binding_subject": proposed.binding_subject_id,
        "proposed_binding_offset": proposed.binding_offset,
    }


def _reference_parameters(reference: CrmCompanyReference) -> _ReferenceParameters:
    return {
        "source_key": reference.scope.source_key,
        "source_instance_id": reference.scope.source_instance_id,
        "control_instance_id": reference.scope.control_instance_id,
        "company_id": reference.company_id,
        "source_record_id": reference.source_record_id,
        "identity_policy_version": reference.identity_policy_version,
    }


def _description_parameters(
    observation: CrmCompanyDescriptionObservation,
) -> _DescriptionParameters:
    return {
        **_reference_parameters(observation.company_reference),
        "observation_id": observation.observation_id,
        "observation_digest": observation.observation_digest,
        "source_record_pk": observation.source_record_pk,
        "source_record_version": observation.source_record_version,
        "source_record_hash": observation.source_record_hash,
        "description": observation.description,
        "observed_at": observation.observed_at,
        "available_at": observation.availability.available_at,
        "contract_version": observation.contract_version,
    }


def _snapshot_parameters(record: CrmCompanyMembershipSnapshotRecord) -> _SnapshotParameters:
    return {
        "source_instance_id": record.scope.source_instance_id,
        "control_instance_id": record.scope.control_instance_id,
        "subject_kind": record.subject_kind,
        "subject_id": record.subject_id,
        "snapshot_id": record.snapshot_id,
        "snapshot_digest": record.snapshot_digest,
        "source_record_id": record.source_record_id,
        "source_record_pk": record.source_record_pk,
        "source_record_version": record.source_record_version,
        "source_record_hash": record.source_record_hash,
        "binding_count": record.binding_count,
        "observed_at": record.observed_at,
        "available_at": record.availability.available_at,
        "contract_version": record.contract_version,
    }


def _membership_observation_parameters(
    observation: CrmCompanyMembershipObservation,
) -> _MembershipObservationParameters:
    return {
        "source_instance_id": observation.snapshot_record.scope.source_instance_id,
        "control_instance_id": observation.snapshot_record.scope.control_instance_id,
        "subject_kind": observation.subject_kind,
        "subject_id": observation.subject_id,
        "snapshot_id": observation.snapshot_id,
        "company_id": observation.company_id,
        "observation_id": observation.observation_id,
        "sort": observation.sort,
        "role_id": observation.role_id,
        "is_primary": observation.is_primary,
    }


def _is_absent_checkpoint(checkpoint: StandaloneCrmCheckpoint) -> bool:
    return (
        checkpoint.last_committed_id == 0
        and checkpoint.processed_rows == 0
        and checkpoint.skipped_rows == 0
        and checkpoint.binding_subject_id is None
        and checkpoint.binding_offset is None
    )


def _claim_decision(record: Record | None) -> CrmCompanyMembershipCommitResult:
    if record is None:
        return CrmCompanyMembershipCommitResult("authority_rejected")
    value = record["decision"]
    if value == "committed":
        return CrmCompanyMembershipCommitResult("committed")
    if value == "idempotent":
        return CrmCompanyMembershipCommitResult("idempotent")
    if value == "stale_or_conflict":
        return CrmCompanyMembershipCommitResult("stale_or_conflict")
    if value == "attempt_exhausted":
        return CrmCompanyMembershipCommitResult("attempt_exhausted")
    if value == "occurrence_exhausted":
        return CrmCompanyMembershipCommitResult("occurrence_exhausted")
    raise RuntimeError("guarded A-S2 mutation returned an unknown decision")


def _require_row(record: Record | None, label: str) -> None:
    if record is None:
        raise RuntimeError(f"immutable {label} conflicts with persisted data")


def _description_head(
    scope: StandaloneCrmSourceChildScope,
    company_id: str,
    record: Record,
) -> CrmCompanyDescriptionHead:
    source_record_id = _required_text(record, "source_record_id")
    observation = CrmCompanyDescriptionObservation(
        CrmCompanyReference(scope, company_id, source_record_id),
        _required_text(record, "source_record_pk"),
        _required_positive_int(record, "source_record_version"),
        _required_text(record, "source_record_hash"),
        _optional_text(record, "description"),
        _optional_text(record, "observed_at"),
        StandaloneCrmSourceAvailability(_required_text(record, "available_at")),
        _required_text(record, "contract_version"),
    )
    return CrmCompanyDescriptionHead(observation.company_reference, observation)


def _membership_head(
    scope: StandaloneCrmSourceChildScope,
    subject_kind: str,
    subject_id: str,
    record: Record,
) -> CrmCompanyMembershipHead:
    bindings = record["bindings"]
    if not isinstance(bindings, list):
        raise RuntimeError("current membership head bindings are malformed")
    payloads = tuple(_binding_payload(item) for item in bindings if item is not None)
    subject_type: CrmIdentitySubjectType
    if subject_kind == "contact":
        subject_type = "contact"
    elif subject_kind == "lead":
        subject_type = "lead"
    else:
        raise RuntimeError("current membership head has malformed subject kind")
    snapshot = normalize_company_membership_snapshot(
        subject_type=subject_type,
        subject_id=subject_id,
        payloads=payloads,
    )
    membership = CrmCompanyMembershipSnapshotRecord(
        scope,
        snapshot,
        _required_text(record, "source_record_id"),
        _required_text(record, "source_record_pk"),
        _required_positive_int(record, "source_record_version"),
        _required_text(record, "source_record_hash"),
        _optional_text(record, "observed_at"),
        StandaloneCrmSourceAvailability(_required_text(record, "available_at")),
        _required_non_negative_int(record, "binding_count"),
        _required_text(record, "contract_version"),
    )
    return CrmCompanyMembershipHead(
        scope, membership.subject_kind, membership.subject_id, membership
    )


def _binding_payload(value: object) -> CrmCompanyBindingPayload:
    if not isinstance(value, dict):
        raise RuntimeError("current membership head binding is malformed")
    return CrmCompanyBindingPayload(
        value.get("company_id"),
        value.get("sort"),
        value.get("role_id"),
        value.get("is_primary"),
    )


def _required_text(record: Record, key: str) -> str:
    value = record[key]
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"current company head has malformed {key}")
    return value


def _optional_text(record: Record, key: str) -> str | None:
    value = record[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"current company head has malformed {key}")
    return value


def _required_positive_int(record: Record, key: str) -> int:
    value = record[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f"current company head has malformed {key}")
    return int(value)


def _required_non_negative_int(record: Record, key: str) -> int:
    value = record[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"current company head has malformed {key}")
    return int(value)
