"""Fenced one-transaction persistence for standalone contact/lead source facts."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from neo4j import ManagedTransaction, Record

from src.connectors.bitrix_openlines.models import CrmContact
from src.graph.client import Neo4jClient
from src.graph.queries.standalone_crm_source_facts import (
    CLAIM_PAGE,
    FINALIZE_PAGE,
    READ_CENSUS_REQUEST,
    READ_PENDING_CONTACT_RECEIPT,
    READ_PENDING_LEAD_RECEIPT,
    READ_SOURCE_RECORD_RECEIPT,
    RESOLVE_COMMITTED_RECEIPT,
    STAMP_SOURCE_FACT_LINEAGE,
)
from src.models import IngestResult
from src.pipeline import IngestPipeline
from src.pipeline_normalization import (
    normalize_envelope_addresses,
    normalize_envelope_attributes,
    normalize_envelope_identifiers,
)
from src.record_lifecycle import (
    DuplicateVersion,
    PlannedVersion,
    load_locked_source_state,
    plan_incoming_version,
)
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_census_request_parser import parse_stored_census_request
from src.standalone_crm_census_requests import SourceSyncCensusRequest
from src.standalone_crm_child_contracts import ContactSourceChildEnvelope, LeadSourceChildEnvelope
from src.standalone_crm_source_fact_models import (
    MappedSourceFactRow,
    StandaloneCrmSourceFactCommitResult,
    StandaloneCrmSourceFactMutation,
    StandaloneCrmSourceFactReceipt,
    build_source_fact_commit,
    strict_row_id,
)
from src.standalone_crm_unit_repository import (
    StandaloneCrmAtomicUnitCommit,
    StandaloneCrmAtomicUnitRepository,
)


class _PageParameters(TypedDict):
    census_id: str
    request_json: str
    authorization_id: str
    authorization_digest: str
    generation: int
    fence_token: int
    fence_owner_id: str
    source_key: str
    source_instance_id: str
    control_instance_id: str
    stream_kind: str
    frozen_upper_id: int
    task_name: str
    task_id: str
    parent_task_id: str
    payload_digest: str
    available_at: str
    availability_contract_version: str
    attempt_deadline: str
    occurrence_deadline: str
    call_intent_id: str
    receipt_key: str
    content_digest: str
    checkpoint_absent: bool
    expected_cursor: int
    expected_processed: int
    expected_skipped: int
    proposed_cursor: int
    proposed_processed: int
    proposed_skipped: int
    proposed_binding_subject: int | None
    proposed_binding_offset: int | None
    processed_delta: int
    skipped_delta: int
    failed_delta: int
    source_receipts_json: str
    attempt_call_limit: int
    occurrence_call_limit: int
    attempt_row_limit: int
    occurrence_row_limit: int


type _ReceiptDecision = Literal["absent", "replayed", "conflict"]
type _PageClaimDecision = Literal[
    "apply",
    "replayed",
    "conflict",
    "authority_rejected",
    "attempt_exhausted",
    "occurrence_exhausted",
]


class SourceFactPipelineAdapter(Protocol):
    """Owned compatibility boundary around the private transaction-scoped pipeline."""

    def plan(
        self, tx: ManagedTransaction, row: MappedSourceFactRow
    ) -> DuplicateVersion | PlannedVersion:
        """Lock and plan a row before any page domain write."""

    def persist(
        self, tx: ManagedTransaction, row: MappedSourceFactRow, plan: PlannedVersion
    ) -> IngestResult:
        """Run matching and writes without opening a nested transaction."""


@dataclass(frozen=True)
class _PlannedRow:
    row: MappedSourceFactRow
    plan: PlannedVersion


class _PipelineAdapter:
    def __init__(self, client: Neo4jClient, control_instance_id: str) -> None:
        self._pipeline = IngestPipeline(client, control_instance_id=control_instance_id)

    def plan(
        self, tx: ManagedTransaction, row: MappedSourceFactRow
    ) -> DuplicateVersion | PlannedVersion:
        state = load_locked_source_state(
            tx,
            row.envelope.source_system,
            row.envelope.source_record_id,
            row.envelope.source_instance_id,
        )
        return plan_incoming_version(state, row.envelope.record_hash)

    def persist(
        self, tx: ManagedTransaction, row: MappedSourceFactRow, plan: PlannedVersion
    ) -> IngestResult:
        envelope = row.envelope
        envelope.source_record_version = str(plan.version)
        return self._pipeline._execute_ingest(
            tx,
            envelope,
            normalize_envelope_identifiers(envelope),
            normalize_envelope_addresses(envelope),
            normalize_envelope_attributes(envelope),
            ingest_run_id=None,
            lifecycle_plan=plan,
        )


class StandaloneCrmSourceFactRepository(
    StandaloneCrmAtomicUnitRepository[
        StandaloneCrmSourceFactMutation,
        StandaloneCrmSourceFactCommitResult,
    ]
):
    """Atomically validate, plan, write facts, account rows, and advance #301 state."""

    def __init__(
        self,
        client: Neo4jClient,
        *,
        adapter: SourceFactPipelineAdapter | None = None,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self._adapter = adapter
        self._failpoint = failpoint

    def commit_page(
        self, mutation: StandaloneCrmSourceFactMutation
    ) -> StandaloneCrmSourceFactCommitResult:
        """Compatibility entry point; all paths delegate to the #301 typed contract."""
        return self.commit_unit(build_source_fact_commit(mutation, skipped_rows=0))

    def pending_contact_receipt(
        self,
        envelope: ContactSourceChildEnvelope,
        binding_subject_id: int,
    ) -> StandaloneCrmSourceFactReceipt:
        """Read exactly one durable deferred-contact receipt or fail closed."""
        position = envelope.binding_subposition
        if position is None or position.binding_subject_id != binding_subject_id:
            raise ValueError("pending contact receipt requires its exact binding position")

        def work(tx: ManagedTransaction) -> StandaloneCrmSourceFactReceipt:
            record = tx.run(
                READ_PENDING_CONTACT_RECEIPT,
                census_id=envelope.unit.census_id,
                generation=envelope.unit.generation,
                fence_token=envelope.unit.fence_token,
                fence_owner_id=envelope.unit.fence_owner_id,
                source_key=envelope.scope.source_key,
                source_instance_id=envelope.scope.source_instance_id,
                control_instance_id=envelope.scope.control_instance_id,
                task_name=envelope.unit.task_name,
                task_id=envelope.unit.task_id,
                payload_digest=envelope.unit.payload_digest,
                frozen_upper_id=envelope.frozen_upper_id,
                binding_subject_id=binding_subject_id,
            ).single()
            if record is None or record["receipt_count"] != 1:
                raise RuntimeError("pending contact source receipt is missing or ambiguous")
            return _one_pending_contact_receipt(record, binding_subject_id)

        return self._client.execute_read(work)

    def pending_lead_receipt(
        self,
        envelope: LeadSourceChildEnvelope,
        checkpoint: StandaloneCrmCheckpoint,
    ) -> StandaloneCrmSourceFactReceipt | None:
        """Return the exact one-row handoff receipt at the durable lead cursor."""
        if checkpoint.binding_subject_id is not None or checkpoint.binding_offset is not None:
            raise ValueError("lead receipt lookup cannot carry a contact binding position")

        def work(tx: ManagedTransaction) -> StandaloneCrmSourceFactReceipt | None:
            record = tx.run(
                READ_PENDING_LEAD_RECEIPT,
                census_id=envelope.unit.census_id,
                generation=envelope.unit.generation,
                fence_token=envelope.unit.fence_token,
                fence_owner_id=envelope.unit.fence_owner_id,
                source_key=envelope.scope.source_key,
                source_instance_id=envelope.scope.source_instance_id,
                control_instance_id=envelope.scope.control_instance_id,
                task_name=envelope.unit.task_name,
                task_id=envelope.unit.task_id,
                payload_digest=envelope.unit.payload_digest,
                frozen_upper_id=envelope.frozen_upper_id,
                last_committed_id=checkpoint.last_committed_id,
            ).single()
            if record is None:
                return None
            if record["receipt_count"] != 1:
                raise RuntimeError("pending lead source receipt is ambiguous")
            return _one_pending_lead_receipt(record, checkpoint.last_committed_id)

        return self._client.execute_read(work)

    def commit_unit(
        self,
        request: StandaloneCrmAtomicUnitCommit[StandaloneCrmSourceFactMutation],
    ) -> StandaloneCrmSourceFactCommitResult:
        """Commit a source-fact page or roll back every domain/control mutation."""
        mutation = request.mutation
        adapter = self._adapter or _PipelineAdapter(
            self._client, request.envelope.scope.control_instance_id
        )

        def work(tx: ManagedTransaction) -> StandaloneCrmSourceFactCommitResult:
            receipt_record = tx.run(RESOLVE_COMMITTED_RECEIPT, **_parameters(request, "")).single()
            receipt = _receipt_decision(receipt_record)
            if receipt != "absent":
                if receipt != "replayed":
                    return _result(receipt)
                return _result(
                    receipt,
                    _replayed_receipts(receipt_record),
                    _replayed_accounting(receipt_record),
                )
            request_json = _read_and_validate_request(tx, request)
            if request_json is None:
                return StandaloneCrmSourceFactCommitResult("authority_rejected")
            claimed = _decision(tx.run(CLAIM_PAGE, **_parameters(request, request_json)).single())
            if claimed != "apply":
                return _result(claimed)
            planned, duplicates = _plan_every_row(tx, mutation.mapped_rows, adapter)
            skipped = len(duplicates)
            # Duplicate state is only knowable under the same source-record locks.  Rebuild
            # the immutable #301 request before the first persist, never mutate its inputs.
            actual = build_source_fact_commit(mutation, skipped_rows=skipped)
            _trip(self._failpoint, "after_planning")
            source_pks: dict[int, str] = dict(duplicates)
            for item in planned:
                result = adapter.persist(tx, item.row, item.plan)
                if result.source_record_pk is None:
                    raise RuntimeError("source-fact matching did not persist a source record")
                _stamp(tx, actual, result.source_record_pk)
                source_pks[item.row.row_id] = result.source_record_pk
            _trip(self._failpoint, "after_domain_writes")
            receipts = _source_receipts(tx, mutation.page.rows, mutation.mapped_rows, source_pks)
            finalized = tx.run(
                FINALIZE_PAGE,
                **_parameters(actual, request_json, _serialize_receipts(receipts)),
            ).single()
            if finalized is None:
                raise RuntimeError("source-fact final checkpoint CAS was rejected")
            _trip(self._failpoint, "after_final_cas")
            delta = actual.accounting_delta
            return StandaloneCrmSourceFactCommitResult(
                "committed",
                delta.processed_rows,
                delta.skipped_rows,
                delta.failed_rows,
                receipts,
            )

        return self._client.execute_write(work)


def _read_and_validate_request(
    tx: ManagedTransaction,
    request: StandaloneCrmAtomicUnitCommit[StandaloneCrmSourceFactMutation],
) -> str | None:
    envelope = request.envelope
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
        or budget.attempt_deadline > budget.occurrence_deadline
    ):
        return None
    return raw_json


def _plan_every_row(
    tx: ManagedTransaction,
    rows: tuple[MappedSourceFactRow, ...],
    adapter: SourceFactPipelineAdapter,
) -> tuple[tuple[_PlannedRow, ...], dict[int, str]]:
    planned: list[_PlannedRow] = []
    duplicates: dict[int, str] = {}
    for row in rows:
        outcome = adapter.plan(tx, row)
        if isinstance(outcome, DuplicateVersion):
            duplicates[row.row_id] = outcome.source_record_pk
        else:
            planned.append(_PlannedRow(row, outcome))
    return tuple(planned), duplicates


def _parameters(
    request: StandaloneCrmAtomicUnitCommit[StandaloneCrmSourceFactMutation],
    request_json: str,
    source_receipts_json: str = "",
) -> _PageParameters:
    page = request.mutation.page
    envelope = request.envelope
    expected = request.expected_checkpoint
    proposed = request.proposed_checkpoint
    delta = request.accounting_delta
    return {
        "census_id": envelope.unit.census_id,
        "request_json": request_json,
        "authorization_id": envelope.budget_authorization.authorization_id,
        "authorization_digest": envelope.budget_authorization.authorization_digest,
        "generation": envelope.unit.generation,
        "fence_token": envelope.unit.fence_token,
        "fence_owner_id": envelope.unit.fence_owner_id,
        "source_key": envelope.scope.source_key,
        "source_instance_id": envelope.scope.source_instance_id,
        "control_instance_id": envelope.scope.control_instance_id,
        "stream_kind": envelope.unit.stream_kind,
        "frozen_upper_id": envelope.frozen_upper_id,
        "task_name": envelope.unit.task_name,
        "task_id": envelope.unit.task_id,
        "parent_task_id": _parent_task_id(envelope.unit.census_id, envelope.unit.generation),
        "payload_digest": envelope.unit.payload_digest,
        "available_at": envelope.availability.available_at,
        "availability_contract_version": envelope.availability.contract_version,
        "attempt_deadline": envelope.budget_authorization.attempt_deadline,
        "occurrence_deadline": envelope.budget_authorization.occurrence_deadline,
        "call_intent_id": page.call_intent_id,
        "receipt_key": _receipt_key(request),
        "content_digest": page.content_digest,
        "checkpoint_absent": _is_absent(expected),
        "expected_cursor": expected.last_committed_id,
        "expected_processed": expected.processed_rows,
        "expected_skipped": expected.skipped_rows,
        "proposed_cursor": proposed.last_committed_id,
        "proposed_processed": proposed.processed_rows,
        "proposed_skipped": proposed.skipped_rows,
        "proposed_binding_subject": proposed.binding_subject_id,
        "proposed_binding_offset": proposed.binding_offset,
        "processed_delta": delta.processed_rows,
        "skipped_delta": delta.skipped_rows,
        "failed_delta": delta.failed_rows,
        "source_receipts_json": source_receipts_json,
        "attempt_call_limit": envelope.budget_authorization.max_calls_per_attempt,
        "occurrence_call_limit": envelope.budget_authorization.max_calls_per_occurrence,
        "attempt_row_limit": envelope.budget_authorization.max_rows_per_attempt,
        "occurrence_row_limit": envelope.budget_authorization.max_rows_per_occurrence,
    }


def _stamp(
    tx: ManagedTransaction,
    request: StandaloneCrmAtomicUnitCommit[StandaloneCrmSourceFactMutation],
    pk: str,
) -> None:
    page = request.mutation.page
    envelope = request.envelope
    record = tx.run(
        STAMP_SOURCE_FACT_LINEAGE,
        source_record_pk=pk,
        available_at=envelope.availability.available_at,
        census_id=envelope.unit.census_id,
        stream_kind=envelope.unit.stream_kind,
        generation=envelope.unit.generation,
        fence_token=envelope.unit.fence_token,
        task_id=envelope.unit.task_id,
        task_name=envelope.unit.task_name,
        payload_digest=envelope.unit.payload_digest,
        fence_owner_id=envelope.unit.fence_owner_id,
        call_intent_id=page.call_intent_id,
        authorization_id=envelope.budget_authorization.authorization_id,
        authorization_digest=envelope.budget_authorization.authorization_digest,
        availability_contract_version=envelope.availability.contract_version,
        frozen_upper_id=envelope.frozen_upper_id,
    ).single()
    if record is None:
        raise RuntimeError("source-fact lineage stamp was not applied")


def _parent_task_id(census_id: str, generation: int) -> str:
    return f"standalone-crm-parent:{census_id}:{generation}"


def _receipt_key(request: StandaloneCrmAtomicUnitCommit[StandaloneCrmSourceFactMutation]) -> str:
    page = request.mutation.page
    unit = request.envelope.unit
    return ":".join((unit.census_id, str(unit.generation), unit.stream_kind, page.call_intent_id))


def _is_absent(checkpoint: StandaloneCrmCheckpoint) -> bool:
    return (
        checkpoint.last_committed_id == 0
        and checkpoint.processed_rows == 0
        and checkpoint.skipped_rows == 0
        and checkpoint.binding_subject_id is None
        and checkpoint.binding_offset is None
    )


def _receipt_decision(record: Record | None) -> _ReceiptDecision:
    if record is None:
        raise RuntimeError("source-fact receipt lookup returned no decision")
    decision = record["decision"]
    if decision == "absent":
        return "absent"
    if decision == "replayed":
        return "replayed"
    if decision == "conflict":
        return "conflict"
    raise RuntimeError("source-fact receipt lookup returned an unknown decision")


def _decision(record: Record | None) -> _PageClaimDecision:
    if record is None:
        return "authority_rejected"
    decision = record["decision"]
    if decision == "apply":
        return "apply"
    if decision == "replayed":
        return "replayed"
    if decision == "conflict":
        return "conflict"
    if decision == "authority_rejected":
        return "authority_rejected"
    if decision == "attempt_exhausted":
        return "attempt_exhausted"
    if decision == "occurrence_exhausted":
        return "occurrence_exhausted"
    raise RuntimeError("source-fact claim returned an unknown decision")


def _result(
    decision: _PageClaimDecision | _ReceiptDecision,
    receipts: tuple[StandaloneCrmSourceFactReceipt, ...] = (),
    accounting: tuple[int, int, int] = (0, 0, 0),
) -> StandaloneCrmSourceFactCommitResult:
    if decision == "replayed":
        return StandaloneCrmSourceFactCommitResult("replayed", *accounting, receipts)
    if decision == "conflict":
        return StandaloneCrmSourceFactCommitResult("conflict")
    if decision == "authority_rejected":
        return StandaloneCrmSourceFactCommitResult("authority_rejected")
    if decision == "attempt_exhausted":
        return StandaloneCrmSourceFactCommitResult("attempt_exhausted")
    if decision == "occurrence_exhausted":
        return StandaloneCrmSourceFactCommitResult("occurrence_exhausted")
    raise RuntimeError("source-fact result cannot expose an unapplied decision")


def _source_receipts(
    tx: ManagedTransaction,
    page_rows: tuple[CrmContact, ...],
    rows: tuple[MappedSourceFactRow, ...],
    source_pks: dict[int, str],
) -> tuple[StandaloneCrmSourceFactReceipt, ...]:
    receipts: list[StandaloneCrmSourceFactReceipt] = []
    lead_companies = {
        strict_row_id(row.id): row.company_id for row in page_rows if row.kind == "lead"
    }
    for row in rows:
        source_record_pk = source_pks.get(row.row_id)
        if source_record_pk is None:
            raise RuntimeError("source-fact receipt has no persisted source record")
        record = tx.run(READ_SOURCE_RECORD_RECEIPT, source_record_pk=source_record_pk).single()
        if record is None:
            raise RuntimeError("source-fact receipt source record is missing")
        version = record["source_record_version"]
        record_hash = record["record_hash"]
        if not isinstance(version, int) or version < 1 or not isinstance(record_hash, str):
            raise RuntimeError("source-fact receipt source record is malformed")
        observed_at = row.envelope.observed_at
        if not isinstance(observed_at, str):
            raise RuntimeError("source-fact receipt observed_at is missing")
        receipts.append(
            StandaloneCrmSourceFactReceipt(
                row.row_id,
                source_record_pk,
                version,
                record_hash,
                observed_at,
                lead_companies.get(row.row_id),
            )
        )
    return tuple(receipts)


def _serialize_receipts(receipts: tuple[StandaloneCrmSourceFactReceipt, ...]) -> str:
    return json.dumps([item.__dict__ for item in receipts], sort_keys=True, separators=(",", ":"))


def _replayed_receipts(record: Record | None) -> tuple[StandaloneCrmSourceFactReceipt, ...]:
    if record is None or record["source_receipts_json"] is None:
        return ()
    value = record["source_receipts_json"]
    if not isinstance(value, str):
        raise RuntimeError("source-fact receipt replay is malformed")
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise RuntimeError("source-fact receipt replay is malformed")
    if not all(isinstance(item, dict) for item in decoded):
        raise RuntimeError("source-fact receipt replay is malformed")
    return tuple(StandaloneCrmSourceFactReceipt(**item) for item in decoded)


def _replayed_accounting(record: Record | None) -> tuple[int, int, int]:
    if record is None:
        return (0, 0, 0)
    processed = record["processed_rows"]
    skipped = record["skipped_rows"]
    failed = record["failed_rows"]
    if (
        isinstance(processed, bool)
        or not isinstance(processed, int)
        or processed < 0
        or isinstance(skipped, bool)
        or not isinstance(skipped, int)
        or skipped < 0
        or skipped > processed
        or isinstance(failed, bool)
        or not isinstance(failed, int)
        or failed < 0
    ):
        raise RuntimeError("source-fact receipt replay accounting is malformed")
    return (processed, skipped, failed)


def _one_pending_contact_receipt(
    record: Record, binding_subject_id: int
) -> StandaloneCrmSourceFactReceipt:
    value = record["source_receipts_json"]
    if not isinstance(value, str):
        raise RuntimeError("pending contact source receipt is malformed")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("pending contact source receipt is malformed") from exc
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        raise RuntimeError("pending contact source receipt is malformed")
    receipts = tuple(StandaloneCrmSourceFactReceipt(**item) for item in decoded)
    if len(receipts) != 1 or receipts[0].row_id != binding_subject_id:
        raise RuntimeError("pending contact source receipt is malformed")
    return receipts[0]


def _one_pending_lead_receipt(record: Record, lead_id: int) -> StandaloneCrmSourceFactReceipt:
    value = record["source_receipts_json"]
    if not isinstance(value, str):
        raise RuntimeError("pending lead source receipt is malformed")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("pending lead source receipt is malformed") from exc
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        raise RuntimeError("pending lead source receipt is malformed")
    receipts = tuple(StandaloneCrmSourceFactReceipt(**item) for item in decoded)
    if len(receipts) != 1 or receipts[0].row_id != lead_id:
        raise RuntimeError("pending lead source receipt is malformed")
    return receipts[0]


def _trip(failpoint: Callable[[str], None] | None, name: str) -> None:
    if failpoint is not None:
        failpoint(name)
