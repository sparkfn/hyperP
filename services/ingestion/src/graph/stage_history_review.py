"""Atomic review-command coordinators for CRM stage-history evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal, TypedDict, cast

from neo4j import ManagedTransaction, Record

from src.bitrix_ingestion_models import FenceContext
from src.graph.client import Neo4jClient
from src.graph.crm_history_authority import (
    AuthorityDecision,
    AuthorityWriteContext,
    append_authority_decision_in_transaction,
)
from src.graph.ingestion_control import assert_active_bitrix_fence
from src.graph.queries.crm_history_authority import GET_CRM_HISTORY_AUTHORITY_HEAD
from src.graph.queries.stage_history_ingestion import (
    APPEND_STAGE_HISTORY_INVALIDATION_INTENTS,
    APPEND_STAGE_HISTORY_PARENT_DECISION,
    CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW,
    CLAIM_STAGE_HISTORY_REVIEW_COMMAND,
    COMPLETE_STAGE_HISTORY_REVIEW_COMMAND,
    GET_COMPLETED_STAGE_HISTORY_REVIEW_COMMAND,
    GET_STAGE_HISTORY_REVIEW_ASSOCIATION,
    GET_STAGE_HISTORY_REVIEW_COMMAND_CONTEXT,
    GET_STAGE_HISTORY_REVIEW_OCCURRENCE,
    GET_STAGE_HISTORY_REVIEW_RESUME_CONTEXT,
    GET_STAGE_HISTORY_REVIEW_VARIANT_SET,
    LOCK_STAGE_HISTORY_REVIEW_EVENT,
    PERSIST_STAGE_HISTORY_REVIEW_COMMAND,
    PROJECT_STAGE_HISTORY_REVIEW_OUTCOME,
    RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW,
    RESOLVE_STAGE_HISTORY_REVIEW_PARENT_CANDIDATES,
)
from src.source_instances import (
    LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
    LEGACY_DEFAULT_SOURCE_INSTANCE_ID,
    effective_control_instance_id,
)
from src.stage_history_identities import scope_stage_history_identity
from src.stage_history_ingestion_models import (
    StageHistoryAssociationState,
    StageHistoryAuthorityState,
    StageHistoryReviewCommand,
    StageHistoryReviewKind,
)

FailureInjector = Callable[[str], None]


class StageHistoryReviewError(RuntimeError):
    """A durable review command failed a fence, lease, or semantic CAS."""


@dataclass(frozen=True, slots=True)
class StageHistoryReviewResult:
    command_id: str
    authority_decision_id: str
    authority_state: StageHistoryAuthorityState
    head_version: int
    authority_token: int
    invalidation_count: int


@dataclass(frozen=True, slots=True)
class StageHistoryReviewExecution:
    command: StageHistoryReviewCommand
    occurrence_id: str
    authorization_reference: str
    configuration_fingerprint: str
    worker_task_id: str
    fence: FenceContext


@dataclass(frozen=True, slots=True)
class StageHistoryReviewResumeContext:
    logical_run_id: str
    logical_status: str
    run_type: str
    command: StageHistoryReviewCommand
    occurrence_id: str
    authorization_reference: str
    configuration_fingerprint: str
    worker_task_id: str | None


class _FenceParams(TypedDict):
    source_key: str
    control_instance_id: str
    logical_run_id: str
    ingest_run_id: str
    attempt_generation: int
    stream_generation: int
    fencing_token: int
    required_run_type: str


class _CommandParams(TypedDict):
    command_id: str
    review_kind: str
    target_event_identity: str
    target_occurrence_id: str
    request_payload_digest: str
    reviewer_actor: str
    authorization_reference: str
    available_at: str
    expected_head_version: int
    expected_authority_token: int
    expected_authority_state: str
    expected_variant_set_digest: str
    retry_sequence: int | None
    selected_variant_hash: str | None
    selected_association_decision_id: str | None
    correction_of_decision_id: str | None


@dataclass(frozen=True, slots=True)
class _Head:
    version: int
    token: int
    state: StageHistoryAuthorityState | None
    parent: tuple[str, str] | None


@dataclass(frozen=True, slots=True)
class _Association:
    decision_id: str
    state: StageHistoryAssociationState
    parent_system: str
    parent_record_id: str
    selected_pk: str | None


@dataclass(frozen=True, slots=True)
class _RetryClaim:
    attempt_count: int
    max_attempts: int


class StageHistoryReviewRepository:
    """Persist commands before execution and apply each mutation atomically."""

    def __init__(
        self,
        client: Neo4jClient,
        *,
        failure_injector: FailureInjector | None = None,
        control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
    ) -> None:
        self._client = client
        self._failure_injector = failure_injector
        self._control_instance_id = effective_control_instance_id(control_instance_id)

    def record_command(
        self,
        command: StageHistoryReviewCommand,
        *,
        occurrence_id: str,
        authorization_reference: str,
        fence: FenceContext,
    ) -> None:
        command = self._scoped_command(command)
        occurrence_id = self._scoped_occurrence_id(occurrence_id)
        if command.status != "pending":
            raise ValueError("new review commands must be pending")
        _require_text(occurrence_id, "occurrence_id")
        _require_text(authorization_reference, "authorization_reference")

        def _work(tx: ManagedTransaction) -> None:
            assert_active_bitrix_fence(tx, fence)
            row = tx.run(
                PERSIST_STAGE_HISTORY_REVIEW_COMMAND,
                **_fence(fence, command),
                **_command_params(command, occurrence_id, authorization_reference),
            ).single()
            _required(row, "review command conflicted with durable provenance")

        self._client.execute_write(_work)

    def load_execution(self, command_id: str) -> StageHistoryReviewExecution | None:
        _require_text(command_id, "command_id")
        command_id = self._scoped_command_id(command_id)

        def _read(tx: ManagedTransaction) -> Record | None:
            return tx.run(
                GET_STAGE_HISTORY_REVIEW_COMMAND_CONTEXT,
                command_id=command_id,
                control_instance_id=self._control_instance_id,
            ).single()

        row = self._client.execute_read(_read)
        if row is None:
            return None
        command_status = _string(row, "command_status")
        if command_status not in {"pending", "claimed", "completed"}:
            raise StageHistoryReviewError("review command is not executable")
        command = _command_from_record(row)
        occurrence_id = _string(row, "target_occurrence_id")
        if _string(row, "request_payload_digest") != _command_digest(command, occurrence_id):
            raise StageHistoryReviewError("review command payload digest changed")
        if _string(row, "run_type") != _required_run_type(command):
            raise StageHistoryReviewError("review command run type changed")
        return StageHistoryReviewExecution(
            command=command,
            occurrence_id=occurrence_id,
            authorization_reference=_string(row, "authorization_reference"),
            configuration_fingerprint=_string(row, "configuration_fingerprint"),
            worker_task_id=_string(row, "worker_task_id"),
            fence=FenceContext(
                logical_run_id=_string(row, "logical_run_id"),
                ingest_run_id=_string(row, "ingest_run_id"),
                source_key="bitrix_chat",
                stream_key="crm_stage_history",
                stream_generation=_positive_integer(row, "stream_generation"),
                fencing_token=_positive_integer(row, "fencing_token"),
                attempt_generation=_positive_integer(row, "attempt_generation"),
                control_instance_id=self._control_instance_id,
            ),
        )

    def load_resume_context(self, command_id: str) -> StageHistoryReviewResumeContext | None:
        _require_text(command_id, "command_id")
        command_id = self._scoped_command_id(command_id)

        def _read(tx: ManagedTransaction) -> Record | None:
            return tx.run(
                GET_STAGE_HISTORY_REVIEW_RESUME_CONTEXT,
                command_id=command_id,
                control_instance_id=self._control_instance_id,
            ).single()

        row = self._client.execute_read(_read)
        if row is None:
            return None
        command_status = _string(row, "command_status")
        if command_status not in {"pending", "claimed", "completed"}:
            raise StageHistoryReviewError("review command is not resumable")
        command = _command_from_record(row)
        if _string(row, "request_payload_digest") != _command_digest(
            command, _string(row, "target_occurrence_id")
        ):
            raise StageHistoryReviewError("review command payload digest changed")
        run_type = _string(row, "run_type")
        if run_type != _required_run_type(command):
            raise StageHistoryReviewError("review command run type changed")
        return StageHistoryReviewResumeContext(
            logical_run_id=_string(row, "logical_run_id"),
            logical_status=_string(row, "logical_status"),
            run_type=run_type,
            command=command,
            occurrence_id=_string(row, "target_occurrence_id"),
            authorization_reference=_string(row, "authorization_reference"),
            configuration_fingerprint=_string(row, "configuration_fingerprint"),
            worker_task_id=_optional_string(row.get("worker_task_id")),
        )

    def execute_command(
        self,
        command: StageHistoryReviewCommand,
        *,
        occurrence_id: str,
        authorization_reference: str,
        lease_owner: str,
        lease_expires_at: datetime,
        retry_backoff_seconds: int = 300,
        fence: FenceContext,
    ) -> StageHistoryReviewResult:
        command = self._scoped_command(command)
        occurrence_id = self._scoped_occurrence_id(occurrence_id)
        _require_text(occurrence_id, "occurrence_id")
        _require_text(authorization_reference, "authorization_reference")
        _require_text(lease_owner, "lease_owner")
        if lease_expires_at.tzinfo is None or lease_expires_at.utcoffset() is None:
            raise ValueError("lease_expires_at must be timezone-aware")
        if (
            isinstance(retry_backoff_seconds, bool)
            or not isinstance(retry_backoff_seconds, int)
            or retry_backoff_seconds < 1
        ):
            raise ValueError("retry_backoff_seconds must be positive")

        def _work(tx: ManagedTransaction) -> StageHistoryReviewResult:
            assert_active_bitrix_fence(tx, fence)
            command_params = _command_params(command, occurrence_id, authorization_reference)
            replay = tx.run(
                GET_COMPLETED_STAGE_HISTORY_REVIEW_COMMAND,
                **_fence(fence, command),
                **command_params,
            ).single()
            if replay is not None:
                return _completed_result(replay, command.command_id)
            claim = tx.run(
                CLAIM_STAGE_HISTORY_REVIEW_COMMAND,
                **_fence(fence, command),
                **command_params,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at.isoformat(),
            ).single()
            _required(claim, "review command lease could not be claimed")
            self._inject("after_claim")
            event_lock = tx.run(
                LOCK_STAGE_HISTORY_REVIEW_EVENT,
                **_fence(fence, command),
                command_id=command.command_id,
                event_identity=command.event_identity,
                lease_owner=lease_owner,
            ).single()
            _required(event_lock, "review command lost its event-identity lock")
            self._inject("after_event_lock")
            occurrence = _load_occurrence(tx, fence, command, occurrence_id)
            head = _load_head(tx, command.event_identity)
            if (
                head.version != command.expected_head_version
                or head.token != command.expected_authority_token
                or head.state != command.expected_authority_state
            ):
                raise StageHistoryReviewError("review command authority head is stale")
            variant_digest = _load_variant_set_digest(tx, fence, command)
            if variant_digest != command.expected_variant_set_digest:
                raise StageHistoryReviewError("review command variant set is stale")
            retry_claim: _RetryClaim | None = None
            if command.kind in {"resolve_parent", "reject_parent"}:
                retry_claim_row = tx.run(
                    CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW,
                    **_fence(fence, command),
                    occurrence_id=occurrence_id,
                    retry_sequence=command.retry_sequence,
                    review_command_id=command.command_id,
                    lease_owner=lease_owner,
                    lease_expires_at=lease_expires_at.isoformat(),
                ).single()
                claimed = _required(
                    retry_claim_row,
                    "review command could not claim its exact retry",
                )
                retry_claim = _RetryClaim(
                    attempt_count=_positive_integer(claimed, "attempt_count"),
                    max_attempts=_positive_integer(claimed, "max_attempts"),
                )
                if retry_claim.attempt_count > retry_claim.max_attempts:
                    raise StageHistoryReviewError("review retry exceeded its maximum attempts")
            self._inject("after_retry_claim")
            association = _resolve_association(tx, fence, command, occurrence)
            self._inject("after_parent")
            state = _review_authority_state(command, association)
            canonical_hash = _review_hash(command, occurrence)
            next_parent = None
            if state in {"effective", "corrected"}:
                if association is None:
                    raise StageHistoryReviewError("effective review lacks a selected association")
                next_parent = (association.parent_system, association.parent_record_id)
            targets = _target_union(head.parent, next_parent)
            target_digests = tuple(_stable_digest("stage-parent", *target) for target in targets)
            authority_id = _stable_id(
                "stage-review-authority",
                command.command_id,
                command.event_identity,
                state,
                canonical_hash,
                association.decision_id if association is not None else "",
                command.correction_of_decision_id or "",
                command.available_at.isoformat(),
            )
            result = append_authority_decision_in_transaction(
                tx,
                AuthorityWriteContext(
                    logical_run_id=fence.logical_run_id,
                    ingest_run_id=fence.ingest_run_id,
                    generation=fence.attempt_generation,
                    control_instance_id=fence.control_instance_id,
                    expected_head_version=head.version,
                    expected_authority_token=head.token,
                    next_authority_token=head.token + 1,
                ),
                AuthorityDecision(
                    decision_id=authority_id,
                    event_identity=command.event_identity,
                    canonical_hash=canonical_hash,
                    hash_version="bitrix-stage-history-v1",
                    decision_kind=_decision_kind(command, state),
                    authority_state=state,
                    available_at=command.available_at.isoformat(),
                    logical_parent_source_system=_parent_system(occurrence, association),
                    logical_parent_source_record_id=_parent_record_id(occurrence, association),
                    correction_of_decision_id=command.correction_of_decision_id,
                    association_decision_id=(
                        association.decision_id if association is not None else None
                    ),
                    expected_invalidation_target_count=len(targets),
                    expected_invalidation_target_digests=target_digests,
                    require_existing_variant=True,
                    require_selected_association=state in {"effective", "corrected"},
                    review_command_id=command.command_id,
                ),
            )
            if result is None:
                raise StageHistoryReviewError("review authority head CAS failed")
            self._inject("after_authority")
            _persist_review_intents(
                tx,
                fence,
                command,
                authority_id,
                result.head_version,
                result.authority_token,
                state,
                targets,
            )
            self._inject("after_outbox")
            retry_resolution: str | None = None
            if retry_claim is not None:
                retry_resolution = _retry_resolution(command, association, retry_claim)
                next_attempt_at = command.available_at + timedelta(seconds=retry_backoff_seconds)
                retry_row = tx.run(
                    RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW,
                    **_fence(fence, command),
                    occurrence_id=occurrence_id,
                    retry_sequence=command.retry_sequence,
                    resolution=retry_resolution,
                    resolution_decision_id=authority_id,
                    review_command_id=command.command_id,
                    lease_owner=lease_owner,
                    next_attempt_at=next_attempt_at.isoformat(),
                ).single()
                retry_record = _required(retry_row, "review retry projection update failed")
                if _integer(retry_record, "resolved_retry_count") < 1:
                    raise StageHistoryReviewError(
                        "review command did not own an unresolved parent retry"
                    )
            self._inject("after_retry")
            projected_association = (
                association.state
                if association is not None
                else _association_state(occurrence["association_state"])
            )
            projected_retry = retry_resolution
            projection = tx.run(
                PROJECT_STAGE_HISTORY_REVIEW_OUTCOME,
                **_fence(fence, command),
                command_id=command.command_id,
                event_identity=command.event_identity,
                occurrence_id=occurrence_id,
                lease_owner=lease_owner,
                authority_decision_id=authority_id,
                authority_state=state,
                authority_head_version=result.head_version,
                authority_token=result.authority_token,
                association_state=projected_association,
                association_decision_id=(
                    association.decision_id if association is not None else None
                ),
                retry_state=projected_retry,
            ).single()
            projection_record = _required(
                projection, "review command current projection CAS failed"
            )
            if (
                _association_state(projection_record["association_state"]) != projected_association
                or _authority_state(_string(projection_record, "authority_state")) != state
                or _integer(projection_record, "projected_occurrence_count") < 1
            ):
                raise StageHistoryReviewError("review command current projection is invalid")
            actual_retry = _string(projection_record, "retry_state")
            if projected_retry is not None and actual_retry != projected_retry:
                raise StageHistoryReviewError("review command retry projection is invalid")
            self._inject("after_projection")
            completion_digest = _stable_digest(
                "stage-review-result",
                command.command_id,
                authority_id,
                state,
                str(result.head_version),
                str(result.authority_token),
                str(len(targets)),
            )
            completed = tx.run(
                COMPLETE_STAGE_HISTORY_REVIEW_COMMAND,
                **_fence(fence, command),
                command_id=command.command_id,
                completion_status="completed",
                result_digest=completion_digest,
                result_authority_decision_id=authority_id,
                result_authority_state=state,
                result_head_version=result.head_version,
                result_authority_token=result.authority_token,
                result_invalidation_count=len(targets),
                lease_owner=lease_owner,
            ).single()
            _required(completed, "review command completion lost its lease")
            self._inject("after_completion")
            return StageHistoryReviewResult(
                command_id=command.command_id,
                authority_decision_id=authority_id,
                authority_state=state,
                head_version=result.head_version,
                authority_token=result.authority_token,
                invalidation_count=len(targets),
            )

        return self._client.execute_write(_work)

    def _scoped_command(self, command: StageHistoryReviewCommand) -> StageHistoryReviewCommand:
        return replace(command, command_id=self._scoped_command_id(command.command_id))

    def _scoped_command_id(self, command_id: str) -> str:
        return scope_stage_history_identity(command_id, self._control_instance_id)

    def _scoped_occurrence_id(self, occurrence_id: str) -> str:
        return scope_stage_history_identity(occurrence_id, self._control_instance_id)

    def _inject(self, point: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(point)


def _resolve_association(
    tx: ManagedTransaction,
    fence: FenceContext,
    command: StageHistoryReviewCommand,
    occurrence: Record,
) -> _Association | None:
    occurrence_id = _string(occurrence, "occurrence_id")
    if command.kind in {"resolve_conflict", "apply_correction"}:
        decision_id = command.selected_association_decision_id
        if decision_id is None:
            if command.kind == "resolve_conflict":
                return None
            raise StageHistoryReviewError("correction review requires a selected association")
        row = _required(
            tx.run(
                GET_STAGE_HISTORY_REVIEW_ASSOCIATION,
                **_fence(fence, command),
                association_decision_id=decision_id,
                event_identity=command.event_identity,
                occurrence_id=occurrence_id,
            ).single(),
            "selected review association is unavailable",
        )
        association = _association(row)
        if association.parent_system != _string(
            occurrence, "logical_parent_source_system"
        ) or association.parent_record_id != _string(occurrence, "logical_parent_source_record_id"):
            raise StageHistoryReviewError(
                "selected review association belongs to another logical parent"
            )
        if association.state != "selected_active":
            raise StageHistoryReviewError("effective review requires an active parent")
        return association
    parent = _required(
        tx.run(
            RESOLVE_STAGE_HISTORY_REVIEW_PARENT_CANDIDATES,
            **_fence(fence, command),
            occurrence_id=occurrence_id,
            event_identity=command.event_identity,
            logical_parent_source_instance_id=LEGACY_DEFAULT_SOURCE_INSTANCE_ID,
        ).single(),
        "review parent candidates are unavailable",
    )
    parent_system = _string(parent, "logical_parent_source_system")
    parent_record_id = _string(parent, "logical_parent_source_record_id")
    state: StageHistoryAssociationState
    selected_pk: str | None
    if command.kind == "reject_parent":
        state = "rejected"
        selected_pk = None
    else:
        state = _association_state(parent["association_state"])
        selected_pk = _optional_string(parent.get("selected_parent_source_record_pk"))
    decision_id = _stable_id(
        "stage-review-parent",
        command.command_id,
        occurrence_id,
        state,
        selected_pk or "",
    )
    persisted = tx.run(
        APPEND_STAGE_HISTORY_PARENT_DECISION,
        **_fence(fence, command),
        occurrence_id=occurrence_id,
        event_identity=command.event_identity,
        logical_parent_source_system=parent_system,
        logical_parent_source_instance_id=LEGACY_DEFAULT_SOURCE_INSTANCE_ID,
        logical_parent_source_record_id=parent_record_id,
        association_state=state,
        selected_parent_source_record_pk=selected_pk,
        active_candidate_count=_integer(parent, "active_count"),
        pending_candidate_count=_integer(parent, "pending_count"),
        decision_id=decision_id,
        available_at=command.available_at.isoformat(),
        review_command_id=command.command_id,
    ).single()
    _required(persisted, "review parent decision lost its candidate CAS")
    return _Association(
        decision_id=decision_id,
        state=state,
        parent_system=parent_system,
        parent_record_id=parent_record_id,
        selected_pk=selected_pk,
    )


def _load_occurrence(
    tx: ManagedTransaction,
    fence: FenceContext,
    command: StageHistoryReviewCommand,
    occurrence_id: str,
) -> Record:
    return _required(
        tx.run(
            GET_STAGE_HISTORY_REVIEW_OCCURRENCE,
            **_fence(fence, command),
            occurrence_id=occurrence_id,
            event_identity=command.event_identity,
        ).single(),
        "review occurrence is unavailable",
    )


def _load_head(tx: ManagedTransaction, event_identity: str) -> _Head:
    row = _required(
        tx.run(GET_CRM_HISTORY_AUTHORITY_HEAD, event_identity=event_identity).single(),
        "review authority head read failed",
    )
    state_text = _optional_string(row.get("authority_state"))
    state = _authority_state(state_text) if state_text is not None else None
    parent_system = _optional_string(row.get("logical_parent_source_system"))
    parent_record = _optional_string(row.get("logical_parent_source_record_id"))
    parent = (
        (parent_system, parent_record)
        if state in {"effective", "corrected"}
        and parent_system is not None
        and parent_record is not None
        else None
    )
    return _Head(
        version=_integer(row, "head_version"),
        token=_integer(row, "authority_token"),
        state=state,
        parent=parent,
    )


def _load_variant_set_digest(
    tx: ManagedTransaction,
    fence: FenceContext,
    command: StageHistoryReviewCommand,
) -> str:
    row = _required(
        tx.run(
            GET_STAGE_HISTORY_REVIEW_VARIANT_SET,
            **_fence(fence, command),
            event_identity=command.event_identity,
        ).single(),
        "review variant set is unavailable",
    )
    hashes = _string_list(row, "canonical_hashes")
    if not hashes:
        raise StageHistoryReviewError("review event has no durable variants")
    return _stable_digest("stage-review-variant-set", *hashes)


def _persist_review_intents(
    tx: ManagedTransaction,
    fence: FenceContext,
    command: StageHistoryReviewCommand,
    decision_id: str,
    head_version: int,
    authority_token: int,
    state: StageHistoryAuthorityState,
    targets: tuple[tuple[str, str], ...],
) -> None:
    reason = _review_reason(command, state)
    target_digests = [_stable_digest("stage-parent", *target) for target in targets]
    intents = [
        {
            "intent_id": _stable_id("stage-invalidation", decision_id, digest, reason),
            "affected_parent_digest": digest,
            "reason": reason,
            "available_at": command.available_at.isoformat(),
            "payload_json": json.dumps(
                {
                    "authority_decision_id": decision_id,
                    "authority_state": state,
                    "event_digest": _stable_digest("stage-event", command.event_identity),
                    "parent_digest": digest,
                    "sequence": head_version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        for digest in target_digests
    ]
    row = tx.run(
        APPEND_STAGE_HISTORY_INVALIDATION_INTENTS,
        **_fence(fence, command),
        authority_decision_id=decision_id,
        authority_head_version=head_version,
        authority_token=authority_token,
        expected_intent_count=len(intents),
        expected_target_digests=target_digests,
        intents=intents,
    ).single()
    _required(row, "review invalidation set was incomplete")


def _review_authority_state(
    command: StageHistoryReviewCommand, association: _Association | None
) -> StageHistoryAuthorityState:
    if command.kind == "reject_parent":
        return "rejected"
    if command.kind == "resolve_conflict" and association is None:
        return "rejected"
    if command.kind == "apply_correction":
        return "corrected"
    if association is None:
        raise StageHistoryReviewError("review command lacks an active selected association")
    if association.state == "selected_active":
        return "effective"
    if command.kind == "resolve_parent" and association.state == "ambiguous":
        return "withheld_conflict"
    if command.kind == "resolve_parent" and association.state in {
        "selected_pending_review",
        "waiting",
    }:
        return "withheld_parent"
    raise StageHistoryReviewError("review command produced an incompatible association")


def _retry_resolution(
    command: StageHistoryReviewCommand,
    association: _Association | None,
    claim: _RetryClaim,
) -> Literal["pending", "resolved", "rejected", "quarantined"]:
    if command.kind == "reject_parent":
        return "rejected"
    if association is not None and association.state == "selected_active":
        return "resolved"
    if claim.attempt_count >= claim.max_attempts:
        return "quarantined"
    return "pending"


def _decision_kind(
    command: StageHistoryReviewCommand, state: StageHistoryAuthorityState
) -> Literal["accepted", "variant", "parent", "correction"]:
    if command.kind == "apply_correction":
        return "correction"
    if command.kind == "resolve_conflict" and state == "rejected":
        return "variant"
    if command.kind == "resolve_conflict":
        return "accepted"
    if state == "withheld_conflict":
        return "variant"
    if state == "effective":
        return "accepted"
    return "parent"


def _review_hash(command: StageHistoryReviewCommand, occurrence: Record) -> str:
    value = command.selected_variant_hash or _string(occurrence, "canonical_hash")
    if not value.startswith("sha256:"):
        raise StageHistoryReviewError("review selected an invalid canonical hash")
    return value


def _parent_system(occurrence: Record, association: _Association | None) -> str:
    return (
        association.parent_system
        if association is not None
        else _string(occurrence, "logical_parent_source_system")
    )


def _parent_record_id(occurrence: Record, association: _Association | None) -> str:
    return (
        association.parent_record_id
        if association is not None
        else _string(occurrence, "logical_parent_source_record_id")
    )


def _association(row: Record) -> _Association:
    return _Association(
        decision_id=_string(row, "decision_id"),
        state=_association_state(row["association_state"]),
        parent_system=_string(row, "logical_parent_source_system"),
        parent_record_id=_string(row, "logical_parent_source_record_id"),
        selected_pk=_optional_string(row.get("selected_parent_source_record_pk")),
    )


def _target_union(
    prior: tuple[str, str] | None, next_parent: tuple[str, str] | None
) -> tuple[tuple[str, str], ...]:
    return tuple(sorted({item for item in (prior, next_parent) if item is not None}))


def _review_reason(command: StageHistoryReviewCommand, state: StageHistoryAuthorityState) -> str:
    if command.kind == "apply_correction":
        return "correction"
    if command.kind == "resolve_conflict":
        return "conflict_resolved" if state == "effective" else "rejection"
    if command.kind == "reject_parent":
        return "rejection"
    return "parent_changed"


def _command_digest(command: StageHistoryReviewCommand, occurrence_id: str) -> str:
    return _stable_digest(
        "stage-review-command",
        command.command_id,
        command.kind,
        command.event_identity,
        occurrence_id,
        command.reviewer_id,
        command.available_at.isoformat(),
        str(command.expected_head_version),
        str(command.expected_authority_token),
        command.expected_authority_state,
        command.expected_variant_set_digest,
        str(command.retry_sequence or 0),
        command.selected_variant_hash or "",
        command.selected_association_decision_id or "",
        command.correction_of_decision_id or "",
    )


def _command_params(
    command: StageHistoryReviewCommand,
    occurrence_id: str,
    authorization_reference: str,
) -> _CommandParams:
    return {
        "command_id": command.command_id,
        "review_kind": command.kind,
        "target_event_identity": command.event_identity,
        "target_occurrence_id": occurrence_id,
        "request_payload_digest": _command_digest(command, occurrence_id),
        "reviewer_actor": command.reviewer_id,
        "authorization_reference": authorization_reference,
        "available_at": command.available_at.isoformat(),
        "expected_head_version": command.expected_head_version,
        "expected_authority_token": command.expected_authority_token,
        "expected_authority_state": command.expected_authority_state,
        "expected_variant_set_digest": command.expected_variant_set_digest,
        "retry_sequence": command.retry_sequence,
        "selected_variant_hash": command.selected_variant_hash,
        "selected_association_decision_id": command.selected_association_decision_id,
        "correction_of_decision_id": command.correction_of_decision_id,
    }


def _command_from_record(row: Record) -> StageHistoryReviewCommand:
    kind_text = _string(row, "review_kind")
    if kind_text not in {
        "resolve_parent",
        "reject_parent",
        "resolve_conflict",
        "apply_correction",
    }:
        raise StageHistoryReviewError("graph returned an invalid review kind")
    return StageHistoryReviewCommand(
        command_id=_string(row, "command_id"),
        kind=cast(StageHistoryReviewKind, kind_text),
        status="pending",
        event_identity=_string(row, "target_event_identity"),
        reviewer_id=_string(row, "reviewer_actor"),
        available_at=datetime.fromisoformat(_string(row, "available_at")),
        expected_head_version=_integer(row, "expected_head_version"),
        expected_authority_token=_integer(row, "expected_authority_token"),
        expected_authority_state=_authority_state(_string(row, "expected_authority_state")),
        expected_variant_set_digest=_string(row, "expected_variant_set_digest"),
        retry_sequence=_optional_integer(row.get("retry_sequence")),
        selected_variant_hash=_optional_string(row.get("selected_variant_hash")),
        selected_association_decision_id=_optional_string(
            row.get("selected_association_decision_id")
        ),
        correction_of_decision_id=_optional_string(row.get("correction_of_decision_id")),
    )


def _completed_result(record: Record, command_id: str) -> StageHistoryReviewResult:
    if _string(record, "command_id") != command_id:
        raise StageHistoryReviewError("completed review command identity changed")
    authority_decision_id = _string(record, "authority_decision_id")
    authority_state = _authority_state(_string(record, "authority_state"))
    head_version = _integer(record, "head_version")
    authority_token = _integer(record, "authority_token")
    invalidation_count = _integer(record, "invalidation_count")
    expected_digest = _stable_digest(
        "stage-review-result",
        command_id,
        authority_decision_id,
        authority_state,
        str(head_version),
        str(authority_token),
        str(invalidation_count),
    )
    if _string(record, "result_digest") != expected_digest:
        raise StageHistoryReviewError("completed review result digest is invalid")
    return StageHistoryReviewResult(
        command_id=command_id,
        authority_decision_id=authority_decision_id,
        authority_state=authority_state,
        head_version=head_version,
        authority_token=authority_token,
        invalidation_count=invalidation_count,
    )


def _fence(fence: FenceContext, command: StageHistoryReviewCommand) -> _FenceParams:
    if fence.source_key != "bitrix_chat" or fence.stream_key != "crm_stage_history":
        raise ValueError("stage review requires the active crm_stage_history fence")
    return {
        "source_key": fence.source_key,
        "control_instance_id": fence.control_instance_id,
        "logical_run_id": fence.logical_run_id,
        "ingest_run_id": fence.ingest_run_id,
        "attempt_generation": fence.attempt_generation,
        "stream_generation": fence.stream_generation,
        "fencing_token": fence.fencing_token,
        "required_run_type": _required_run_type(command),
    }


def _required_run_type(command: StageHistoryReviewCommand) -> str:
    if command.kind in {"resolve_parent", "reject_parent"}:
        return "parent_reconcile"
    if command.kind == "resolve_conflict":
        return "conflict_review"
    return "correction_review"


def _association_state(value: object) -> StageHistoryAssociationState:
    if value == "selected_active":
        return "selected_active"
    if value == "selected_pending_review":
        return "selected_pending_review"
    if value == "waiting":
        return "waiting"
    if value == "ambiguous":
        return "ambiguous"
    if value == "rejected":
        return "rejected"
    raise StageHistoryReviewError("graph returned an invalid association state")


def _authority_state(value: str) -> StageHistoryAuthorityState:
    if value == "effective":
        return "effective"
    if value == "withheld_parent":
        return "withheld_parent"
    if value == "withheld_conflict":
        return "withheld_conflict"
    if value == "rejected":
        return "rejected"
    if value == "corrected":
        return "corrected"
    raise StageHistoryReviewError("graph returned an invalid authority state")


def _required(record: Record | None, message: str) -> Record:
    if record is None:
        raise StageHistoryReviewError(message)
    return record


def _string(record: Record, key: str) -> str:
    value: object = record[key]
    if not isinstance(value, str) or not value:
        raise StageHistoryReviewError(f"graph returned invalid {key}")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise StageHistoryReviewError("graph returned an invalid optional string")
    return value


def _integer(record: Record, key: str) -> int:
    value: object = record[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StageHistoryReviewError(f"graph returned invalid {key}")
    return value


def _positive_integer(record: Record, key: str) -> int:
    value = _integer(record, key)
    if value < 1:
        raise StageHistoryReviewError(f"graph returned invalid {key}")
    return value


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StageHistoryReviewError("graph returned an invalid optional integer")
    return value


def _string_list(record: Record, key: str) -> tuple[str, ...]:
    value: object = record[key]
    if not isinstance(value, list):
        raise StageHistoryReviewError(f"graph returned invalid {key}")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise StageHistoryReviewError(f"graph returned invalid {key}")
        result.append(item)
    if result != sorted(result) or len(result) != len(set(result)):
        raise StageHistoryReviewError(f"graph returned non-canonical {key}")
    return tuple(result)


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _stable_id(domain: str, *parts: str) -> str:
    return f"{domain}:{_hash(domain, *parts)}"


def _stable_digest(domain: str, *parts: str) -> str:
    return f"sha256:{_hash(domain, *parts)}"


def _hash(domain: str, *parts: str) -> str:
    payload = json.dumps([domain, *parts], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
