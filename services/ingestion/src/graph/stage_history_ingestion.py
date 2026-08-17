"""Atomic repository coordinator for bounded CRM stage-history replay."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypedDict

from neo4j import ManagedTransaction, Record

from src.bitrix_ingestion_models import FenceContext
from src.connectors.bitrix_stage_history.canonical import (
    canonical_stage_hash_v1,
    encode_stage_source_record_id,
)
from src.crm_history_contract import stage_history_properties
from src.graph.client import Neo4jClient
from src.graph.crm_history_authority import (
    AuthorityDecision,
    AuthorityWriteContext,
    append_authority_decision_in_transaction,
)
from src.graph.ingestion_control import assert_active_bitrix_fence
from src.graph.ingestion_control_models import encode_json
from src.graph.queries.stage_history_ingestion import (
    APPEND_STAGE_HISTORY_INVALIDATION_INTENTS,
    APPEND_STAGE_HISTORY_PARENT_DECISION,
    COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT,
    CREATE_STAGE_HISTORY_UNIT,
    GET_STAGE_HISTORY_AUTHORITY_HEAD,
    GET_STAGE_HISTORY_COMMITTED_UNIT,
    PROJECT_STAGE_HISTORY_AUTHORITY_HEAD,
    RESOLVE_STAGE_HISTORY_PARENT_CANDIDATES,
    UPSERT_STAGE_HISTORY_FAILED_OCCURRENCE,
    UPSERT_STAGE_HISTORY_OCCURRENCE,
    UPSERT_STAGE_HISTORY_RETRY,
    UPSERT_STAGE_HISTORY_UNIT_ACCOUNTING,
    UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD,
)
from src.models import JsonValue
from src.stage_history_ingestion_models import (
    StageHistoryAssociationDecision,
    StageHistoryAssociationState,
    StageHistoryAuthorityState,
    StageHistoryAuthorityTransition,
    StageHistoryCheckpointSnapshot,
    StageHistoryInvalidationIntent,
    StageHistoryMalformedObservation,
    StageHistoryOccurrence,
    StageHistoryOutboxReason,
    StageHistoryReplayRunType,
    StageHistoryReplaySourceWindow,
    StageHistoryReplayUnit,
    StageHistoryRetry,
    StageHistoryUnitResult,
    StageHistoryValidObservation,
    advance_stage_history_checkpoint,
)

FailureInjector = Callable[[str], None]
ParentIdentity = tuple[str, str]


class _FenceParams(TypedDict):
    source_key: str
    logical_run_id: str
    ingest_run_id: str
    attempt_generation: int
    stream_generation: int
    fencing_token: int
    required_run_type: str


class _CheckpointParams(TypedDict):
    phase: str
    connector_version: str
    checkpoint_schema_version: int
    replay_boundary: str
    source_window_json: str
    expected_cursor_json: str
    expected_checkpoint_revision: int
    expected_last_page_sequence: int | None
    expected_committed_count: int
    expected_duplicate_count: int
    expected_excluded_count: int
    expected_retry_count: int


class StageHistoryPersistenceError(RuntimeError):
    """A fenced stage-history transaction rejected an invariant."""


@dataclass(frozen=True, slots=True)
class _AuthorityHead:
    head_version: int
    authority_token: int
    authority_state: StageHistoryAuthorityState | None
    decision_id: str | None
    selected_variant_hash: str | None
    parent: ParentIdentity | None
    selected_association_current: bool


@dataclass(frozen=True, slots=True)
class _CompatibilityCounts:
    committed: int
    duplicate: int
    excluded: int
    retry: int


class StageHistoryIngestionRepository:
    """Persist one immutable artifact page and its checkpoint atomically."""

    def __init__(
        self,
        client: Neo4jClient,
        *,
        failure_injector: FailureInjector | None = None,
        retry_max_attempts: int = 5,
    ) -> None:
        if isinstance(retry_max_attempts, bool) or retry_max_attempts < 1:
            raise ValueError("retry_max_attempts must be positive")
        self._client = client
        self._failure_injector = failure_injector
        self._retry_max_attempts = retry_max_attempts

    def persist_unit(
        self,
        unit: StageHistoryReplayUnit,
        expected_checkpoint: StageHistoryCheckpointSnapshot,
        fence: FenceContext,
    ) -> StageHistoryUnitResult:
        """Commit a page under one active stream fence and exact checkpoint CAS."""
        _validate_request(unit, expected_checkpoint, fence)

        def _work(tx: ManagedTransaction) -> StageHistoryUnitResult:
            assert_active_bitrix_fence(tx, fence)
            self._inject("after_fence")
            existing = _find_committed_unit(tx, fence, unit)
            if existing is not None:
                return _already_committed_result(existing, unit, expected_checkpoint)
            if unit.page_sequence != expected_checkpoint.committed_unit_count + 1:
                raise StageHistoryPersistenceError(
                    "checkpoint tail references a missing committed stage-history unit"
                )
            _create_unit(tx, fence, unit, expected_checkpoint)
            self._inject("after_unit")
            associations: list[StageHistoryAssociationDecision] = []
            transitions: list[StageHistoryAuthorityTransition] = []
            retries: list[StageHistoryRetry] = []
            intents: list[StageHistoryInvalidationIntent] = []
            for occurrence in unit.occurrences:
                _persist_occurrence(tx, fence, unit, occurrence)
                self._inject("after_occurrence")
                if not _has_domain_state(occurrence):
                    continue
                valid = occurrence.observation
                if not isinstance(valid, StageHistoryValidObservation):
                    raise StageHistoryPersistenceError("domain occurrence must be valid")
                _persist_variant(tx, fence, unit, valid, occurrence)
                self._inject("after_variant")
                association = _persist_parent_decision(tx, fence, unit, valid, occurrence)
                associations.append(association)
                self._inject("after_parent")
                retry = _persist_retry_if_required(
                    tx,
                    fence,
                    valid,
                    occurrence,
                    required_run_type=unit.run_type,
                    max_attempts=self._retry_max_attempts,
                )
                if retry is not None:
                    retries.append(retry)
                self._inject("after_retry")
                transition, emitted = _persist_authority_if_required(
                    tx,
                    fence,
                    valid,
                    occurrence,
                    association,
                    required_run_type=unit.run_type,
                    after_authority=self._inject,
                )
                if transition is not None:
                    transitions.append(transition)
                    intents.extend(emitted)
                self._inject("after_outbox")
            _persist_accounting(tx, fence, unit)
            self._inject("after_accounting")
            after = advance_stage_history_checkpoint(expected_checkpoint, unit)
            _commit_checkpoint(tx, fence, unit, expected_checkpoint, after)
            self._inject("after_checkpoint")
            return StageHistoryUnitResult(
                outcome="committed",
                unit=unit,
                checkpoint_before=expected_checkpoint,
                checkpoint_after=after,
                association_decisions=tuple(associations),
                authority_transitions=tuple(transitions),
                retries=tuple(retries),
                invalidation_intents=tuple(intents),
            )

        return self._client.execute_write(_work)

    def _inject(self, point: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(point)


def _validate_request(
    unit: StageHistoryReplayUnit,
    checkpoint: StageHistoryCheckpointSnapshot,
    fence: FenceContext,
) -> None:
    if fence.stream_key != "crm_stage_history":
        raise ValueError("stage history requires the crm_stage_history stream")
    if fence.source_key != "bitrix_chat":
        raise ValueError("stage history requires the bitrix_chat source")
    if unit.run_type != checkpoint.run_type:
        raise ValueError("unit and checkpoint run types must match")
    if unit.artifact_id != _artifact_id(checkpoint):
        raise ValueError("unit artifact does not match the authenticated source window")
    source_contract_uuid = checkpoint.source_window.source_contract_uuid
    for occurrence in unit.occurrences:
        observation = occurrence.observation
        if not isinstance(observation, StageHistoryValidObservation):
            continue
        if isinstance(checkpoint.source_window, StageHistoryReplaySourceWindow) and (
            observation.item.entity_type_id != checkpoint.source_window.entity_type_id
        ):
            raise ValueError("stage-history entity type disagrees with the frozen source window")
        expected_identity = encode_stage_source_record_id(
            source_contract_uuid,
            observation.item.entity_type_id,
            observation.item.history_id,
        )
        if observation.event_identity != expected_identity:
            raise ValueError("stage-history event identity disagrees with the frozen contract")
        expected_hash = canonical_stage_hash_v1(source_contract_uuid, observation.item)
        if observation.canonical_hash != expected_hash:
            raise ValueError("stage-history canonical hash disagrees with the frozen contract")
    next_page = unit.page_sequence == checkpoint.committed_unit_count + 1
    replayed_tail = (
        unit.page_sequence == checkpoint.committed_unit_count
        and checkpoint.last_unit_id == unit.unit_id
        and checkpoint.last_unit_digest == unit.page_digest
    )
    if not next_page and not replayed_tail:
        raise ValueError("stage-history pages must be committed contiguously")


def _fence_params(
    fence: FenceContext, required_run_type: StageHistoryReplayRunType
) -> _FenceParams:
    return {
        "source_key": fence.source_key,
        "logical_run_id": fence.logical_run_id,
        "ingest_run_id": fence.ingest_run_id,
        "attempt_generation": fence.attempt_generation,
        "stream_generation": fence.stream_generation,
        "fencing_token": fence.fencing_token,
        "required_run_type": required_run_type,
    }


def _checkpoint_params(checkpoint: StageHistoryCheckpointSnapshot) -> _CheckpointParams:
    counts = _compatibility_counts(checkpoint)
    return {
        "phase": checkpoint.phase,
        "connector_version": checkpoint.connector_version,
        "checkpoint_schema_version": checkpoint.schema_version,
        "replay_boundary": checkpoint.replay_boundary,
        "source_window_json": _source_window_json(checkpoint),
        "expected_cursor_json": _cursor_json(checkpoint),
        "expected_checkpoint_revision": checkpoint.revision,
        "expected_last_page_sequence": checkpoint.last_page_sequence,
        "expected_committed_count": counts.committed,
        "expected_duplicate_count": counts.duplicate,
        "expected_excluded_count": counts.excluded,
        "expected_retry_count": counts.retry,
    }


def _find_committed_unit(
    tx: ManagedTransaction, fence: FenceContext, unit: StageHistoryReplayUnit
) -> Record | None:
    return tx.run(
        GET_STAGE_HISTORY_COMMITTED_UNIT,
        **_fence_params(fence, unit.run_type),
        unit_id=unit.unit_id,
        artifact_id=unit.artifact_id,
        page_sequence=unit.page_sequence,
        unit_digest=unit.page_digest,
    ).single()


def _already_committed_result(
    record: Record,
    unit: StageHistoryReplayUnit,
    checkpoint: StageHistoryCheckpointSnapshot,
) -> StageHistoryUnitResult:
    if str(record["status"]) != "committed":
        raise StageHistoryPersistenceError("matching unit exists but is not committed")
    if checkpoint.last_unit_id != unit.unit_id or checkpoint.last_unit_digest != unit.page_digest:
        raise StageHistoryPersistenceError("committed unit is not the expected checkpoint tail")
    if _record_int(record, "next_checkpoint_revision") != checkpoint.revision:
        raise StageHistoryPersistenceError("committed unit revision disagrees with checkpoint")
    if str(record["next_cursor_json"]) != _cursor_json(checkpoint):
        raise StageHistoryPersistenceError("committed unit cursor disagrees with checkpoint")
    if _record_int(record, "fetched_count") != len(unit.occurrences):
        raise StageHistoryPersistenceError("committed unit fetched count disagrees with replay")
    return StageHistoryUnitResult(
        outcome="already_committed",
        unit=unit,
        checkpoint_before=checkpoint,
        checkpoint_after=checkpoint,
    )


def _create_unit(
    tx: ManagedTransaction,
    fence: FenceContext,
    unit: StageHistoryReplayUnit,
    checkpoint: StageHistoryCheckpointSnapshot,
) -> None:
    record = tx.run(
        CREATE_STAGE_HISTORY_UNIT,
        **_fence_params(fence, unit.run_type),
        **_checkpoint_params(checkpoint),
        unit_id=unit.unit_id,
        artifact_id=unit.artifact_id,
        page_sequence=unit.page_sequence,
        unit_digest=unit.page_digest,
        fetched_count=len(unit.occurrences),
    ).single()
    _require_record(record, "stage-history unit creation failed checkpoint CAS")


def _persist_occurrence(
    tx: ManagedTransaction,
    fence: FenceContext,
    unit: StageHistoryReplayUnit,
    occurrence: StageHistoryOccurrence,
) -> None:
    observation = occurrence.observation
    common = _fence_params(fence, unit.run_type)
    if isinstance(observation, StageHistoryMalformedObservation) or unit.run_type == (
        "capture_failure_accounting"
    ):
        safe_error = (
            observation.safe_error_code
            if isinstance(observation, StageHistoryMalformedObservation)
            else "capture_rejected_valid"
        )
        record = tx.run(
            UPSERT_STAGE_HISTORY_FAILED_OCCURRENCE,
            **common,
            unit_id=unit.unit_id,
            unit_digest=unit.page_digest,
            occurrence_id=observation.occurrence_id,
            artifact_id=observation.artifact_id,
            artifact_row_sequence=observation.row_sequence,
            row_digest=_observation_digest(observation),
            source_observed_at=observation.source_observed_at.isoformat(),
            terminal_disposition=occurrence.disposition,
            parse_scope=occurrence.parse_scope,
            retry_state=occurrence.retry_state,
            safe_error_code=safe_error,
        ).single()
    else:
        record = tx.run(
            UPSERT_STAGE_HISTORY_OCCURRENCE,
            **common,
            unit_id=unit.unit_id,
            unit_digest=unit.page_digest,
            occurrence_id=observation.occurrence_id,
            artifact_id=observation.artifact_id,
            artifact_row_sequence=observation.row_sequence,
            row_digest=_observation_digest(observation),
            source_observed_at=observation.source_observed_at.isoformat(),
            terminal_disposition=occurrence.disposition,
            event_identity=observation.event_identity,
            canonical_hash=observation.canonical_hash,
            hash_version="bitrix-stage-history-v1",
            identity_hash_state=occurrence.identity_hash_state,
            parse_scope=occurrence.parse_scope,
            association_state=occurrence.association_state,
            authority_state=occurrence.authority_state,
            retry_state=occurrence.retry_state,
            logical_parent_source_system=observation.logical_parent_source_system,
            logical_parent_source_record_id=observation.logical_parent_source_record_id,
        ).single()
    _require_record(record, "stage-history occurrence conflicted with immutable evidence")


def _persist_variant(
    tx: ManagedTransaction,
    fence: FenceContext,
    unit: StageHistoryReplayUnit,
    observation: StageHistoryValidObservation,
    occurrence: StageHistoryOccurrence,
) -> None:
    properties = stage_history_properties(observation.item)
    record = tx.run(
        UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD,
        **_fence_params(fence, unit.run_type),
        unit_id=unit.unit_id,
        unit_digest=unit.page_digest,
        occurrence_id=observation.occurrence_id,
        event_identity=observation.event_identity,
        canonical_hash=observation.canonical_hash,
        hash_version="bitrix-stage-history-v1",
        source_record_pk=_stable_id(
            "stage-source-record", observation.event_identity, observation.canonical_hash
        ),
        source_version_key=_stable_id(
            "stage-source-version", observation.event_identity, observation.canonical_hash
        ),
        history_kind=properties.history_kind,
        history_source=properties.history_source,
        history_projection_version=properties.history_projection_version,
        history_projection_source=properties.history_projection_source,
        event_category_id=properties.event_category_id,
        event_stage_id=properties.event_stage_id,
        event_stage_semantic_id=properties.event_stage_semantic_id,
        event_at=properties.event_at,
        source_observed_at=observation.source_observed_at.isoformat(),
        raw_payload=json.dumps(
            observation.item.raw_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ),
    ).single()
    row = _require_record(record, "stage-history variant conflicted with immutable evidence")
    created = _record_bool(row, "created")
    prior_different_count = _record_int(row, "prior_different_variant_count")
    if created:
        actual_state = "new_conflict_variant" if prior_different_count > 0 else "new_variant"
    else:
        actual_state = "existing_same_hash"
    if actual_state != occurrence.identity_hash_state:
        raise StageHistoryPersistenceError(
            "qualified identity/hash state disagrees with serialized graph evidence"
        )


def _persist_parent_decision(
    tx: ManagedTransaction,
    fence: FenceContext,
    unit: StageHistoryReplayUnit,
    observation: StageHistoryValidObservation,
    occurrence: StageHistoryOccurrence,
) -> StageHistoryAssociationDecision:
    resolved = tx.run(
        RESOLVE_STAGE_HISTORY_PARENT_CANDIDATES,
        **_fence_params(fence, unit.run_type),
        unit_id=unit.unit_id,
        occurrence_id=observation.occurrence_id,
        logical_parent_source_system=observation.logical_parent_source_system,
        logical_parent_source_record_id=observation.logical_parent_source_record_id,
    ).single()
    row = _require_record(resolved, "stage-history parent resolution failed")
    state = _association_state(row["association_state"])
    if state != occurrence.association_state:
        raise StageHistoryPersistenceError("parent state changed from the qualified unit plan")
    selected_pk = _optional_string(row.get("selected_parent_source_record_pk"))
    decision_id = _stable_id(
        "stage-parent-decision",
        observation.occurrence_id,
        state,
        selected_pk or "",
        observation.source_observed_at.isoformat(),
    )
    persisted = tx.run(
        APPEND_STAGE_HISTORY_PARENT_DECISION,
        **_fence_params(fence, unit.run_type),
        occurrence_id=observation.occurrence_id,
        event_identity=observation.event_identity,
        logical_parent_source_system=observation.logical_parent_source_system,
        logical_parent_source_record_id=observation.logical_parent_source_record_id,
        association_state=state,
        selected_parent_source_record_pk=selected_pk,
        active_candidate_count=_record_int(row, "active_count"),
        pending_candidate_count=_record_int(row, "pending_count"),
        decision_id=decision_id,
        available_at=observation.source_observed_at.isoformat(),
        review_command_id=None,
    ).single()
    _require_record(persisted, "stage-history parent decision lost its candidate CAS")
    return StageHistoryAssociationDecision(
        decision_id=decision_id,
        event_identity=observation.event_identity,
        state=state,
        available_at=observation.source_observed_at,
        logical_parent_source_system=observation.logical_parent_source_system,
        logical_parent_source_record_id=observation.logical_parent_source_record_id,
        selected_parent_source_record_pk=selected_pk,
    )


def _persist_retry_if_required(
    tx: ManagedTransaction,
    fence: FenceContext,
    observation: StageHistoryValidObservation,
    occurrence: StageHistoryOccurrence,
    *,
    required_run_type: StageHistoryReplayRunType,
    max_attempts: int,
) -> StageHistoryRetry | None:
    if occurrence.retry_state != "pending":
        return None
    retry_id = _stable_id("stage-retry", observation.occurrence_id, "1")
    record = tx.run(
        UPSERT_STAGE_HISTORY_RETRY,
        **_fence_params(fence, required_run_type),
        retry_id=retry_id,
        occurrence_id=observation.occurrence_id,
        retry_sequence=1,
        reason_code=occurrence.disposition,
        max_attempts=max_attempts,
        next_attempt_at=observation.source_observed_at.isoformat(),
        review_command_id=None,
    ).single()
    _require_record(record, "stage-history retry persistence failed")
    return StageHistoryRetry(
        retry_id=retry_id,
        occurrence_id=observation.occurrence_id,
        retry_sequence=1,
        state="pending",
        reason_code=occurrence.disposition,
        attempt_count=0,
        max_attempts=max_attempts,
    )


def _persist_authority_if_required(
    tx: ManagedTransaction,
    fence: FenceContext,
    observation: StageHistoryValidObservation,
    occurrence: StageHistoryOccurrence,
    association: StageHistoryAssociationDecision,
    *,
    required_run_type: StageHistoryReplayRunType,
    after_authority: FailureInjector,
) -> tuple[StageHistoryAuthorityTransition | None, tuple[StageHistoryInvalidationIntent, ...]]:
    if occurrence.disposition == "same_hash_replay":
        head = _load_authority_head(
            tx,
            fence,
            required_run_type,
            observation.event_identity,
        )
        if head.authority_state != occurrence.authority_state:
            raise StageHistoryPersistenceError(
                "same-hash replay authority state disagrees with the durable head"
            )
        if head.authority_state in {"effective", "corrected"}:
            expected_parent = (
                observation.logical_parent_source_system,
                observation.logical_parent_source_record_id,
            )
            if (
                head.selected_variant_hash != observation.canonical_hash
                or head.parent != expected_parent
                or association.state != "selected_active"
            ):
                raise StageHistoryPersistenceError(
                    "same-hash replay effective authority lacks its current variant or parent"
                )
        if head.decision_id is None or head.authority_state is None:
            raise StageHistoryPersistenceError("same-hash replay lacks a durable authority head")
        _project_authority_head(
            tx,
            fence,
            required_run_type,
            observation.event_identity,
            head.decision_id,
            head.authority_state,
            head.head_version,
            head.authority_token,
        )
        return None, ()
    state = occurrence.authority_state
    if state is None:
        raise StageHistoryPersistenceError("domain occurrence lacks authority state")
    head = _load_authority_head(
        tx,
        fence,
        required_run_type,
        observation.event_identity,
    )
    targets = _invalidation_targets(head, observation, state)
    target_digests = tuple(_stable_digest("stage-parent", *target) for target in targets)
    reason = _outbox_reason(occurrence.disposition)
    decision_id = _stable_id(
        "stage-authority-decision",
        observation.event_identity,
        state,
        observation.canonical_hash,
        association.decision_id,
        observation.source_observed_at.isoformat(),
    )
    kind: Literal["accepted", "variant", "parent", "correction"]
    if state == "effective":
        kind = "accepted"
    elif state == "withheld_conflict":
        kind = "variant"
    else:
        kind = "parent"
    result = append_authority_decision_in_transaction(
        tx,
        AuthorityWriteContext(
            logical_run_id=fence.logical_run_id,
            ingest_run_id=fence.ingest_run_id,
            generation=fence.attempt_generation,
            expected_head_version=head.head_version,
            expected_authority_token=head.authority_token,
            next_authority_token=head.authority_token + 1,
        ),
        AuthorityDecision(
            decision_id=decision_id,
            event_identity=observation.event_identity,
            canonical_hash=observation.canonical_hash,
            hash_version="bitrix-stage-history-v1",
            decision_kind=kind,
            authority_state=state,
            available_at=observation.source_observed_at.isoformat(),
            logical_parent_source_system=observation.logical_parent_source_system,
            logical_parent_source_record_id=observation.logical_parent_source_record_id,
            association_decision_id=(association.decision_id if state == "effective" else None),
            expected_invalidation_target_count=len(targets),
            expected_invalidation_target_digests=target_digests,
            require_existing_variant=True,
            require_selected_association=state == "effective",
        ),
    )
    if result is None:
        raise StageHistoryPersistenceError("stage-history authority head CAS failed")
    _project_authority_head(
        tx,
        fence,
        required_run_type,
        observation.event_identity,
        decision_id,
        state,
        result.head_version,
        result.authority_token,
    )
    after_authority("after_authority")
    emitted = _persist_invalidation_intents(
        tx,
        fence,
        decision_id,
        result.head_version,
        result.authority_token,
        targets,
        reason,
        state,
        observation,
        required_run_type,
    )
    transition = StageHistoryAuthorityTransition(
        decision_id=decision_id,
        event_identity=observation.event_identity,
        prior_state=head.authority_state,
        next_state=state,
        prior_head_version=head.head_version,
        next_head_version=result.head_version,
        prior_authority_token=head.authority_token,
        next_authority_token=result.authority_token,
        available_at=observation.source_observed_at,
        selected_variant_hash=(observation.canonical_hash if state == "effective" else None),
        association_decision_id=(association.decision_id if state == "effective" else None),
    )
    return transition, emitted


def _project_authority_head(
    tx: ManagedTransaction,
    fence: FenceContext,
    required_run_type: StageHistoryReplayRunType,
    event_identity: str,
    decision_id: str,
    state: StageHistoryAuthorityState,
    head_version: int,
    authority_token: int,
) -> None:
    row = tx.run(
        PROJECT_STAGE_HISTORY_AUTHORITY_HEAD,
        **_fence_params(fence, required_run_type),
        event_identity=event_identity,
        authority_decision_id=decision_id,
        authority_state=state,
        authority_head_version=head_version,
        authority_token=authority_token,
    ).single()
    projected = _require_record(row, "stage-history authority projection CAS failed")
    if _record_int(projected, "projected_occurrence_count") < 1:
        raise StageHistoryPersistenceError("stage-history authority projection updated no rows")


def _load_authority_head(
    tx: ManagedTransaction,
    fence: FenceContext,
    required_run_type: StageHistoryReplayRunType,
    event_identity: str,
) -> _AuthorityHead:
    row = _require_record(
        tx.run(
            GET_STAGE_HISTORY_AUTHORITY_HEAD,
            **_fence_params(fence, required_run_type),
            event_identity=event_identity,
        ).single(),
        "authority head read failed",
    )
    state_value = _optional_string(row.get("authority_state"))
    state = _authority_state(state_value) if state_value is not None else None
    parent_system = _optional_string(row.get("logical_parent_source_system"))
    parent_record_id = _optional_string(row.get("logical_parent_source_record_id"))
    parent = (
        (parent_system, parent_record_id)
        if state in {"effective", "corrected"}
        and parent_system is not None
        and parent_record_id is not None
        else None
    )
    selected_association_current = _record_bool(row, "selected_association_current")
    if state in {"effective", "corrected"} and not selected_association_current:
        raise StageHistoryPersistenceError(
            "effective authority head has a stale or corrupt selected association"
        )
    return _AuthorityHead(
        head_version=_record_int(row, "head_version"),
        authority_token=_record_int(row, "authority_token"),
        authority_state=state,
        decision_id=_optional_string(row.get("decision_id")),
        selected_variant_hash=_optional_string(row.get("selected_variant_hash")),
        parent=parent,
        selected_association_current=selected_association_current,
    )


def _invalidation_targets(
    head: _AuthorityHead,
    observation: StageHistoryValidObservation,
    next_state: StageHistoryAuthorityState,
) -> tuple[ParentIdentity, ...]:
    targets: set[ParentIdentity] = set()
    if head.parent is not None:
        targets.add(head.parent)
    if next_state in {"effective", "corrected"}:
        targets.add(
            (
                observation.logical_parent_source_system,
                observation.logical_parent_source_record_id,
            )
        )
    return tuple(sorted(targets))


def _persist_invalidation_intents(
    tx: ManagedTransaction,
    fence: FenceContext,
    decision_id: str,
    head_version: int,
    authority_token: int,
    targets: tuple[ParentIdentity, ...],
    reason: StageHistoryOutboxReason,
    authority_state: StageHistoryAuthorityState,
    observation: StageHistoryValidObservation,
    required_run_type: StageHistoryReplayRunType,
) -> tuple[StageHistoryInvalidationIntent, ...]:
    intents: list[StageHistoryInvalidationIntent] = []
    payloads: list[dict[str, object]] = []
    for parent in targets:
        digest = _stable_digest("stage-parent", parent[0], parent[1])
        intent_id = _stable_id("stage-invalidation", decision_id, digest, reason)
        payload = json.dumps(
            {
                "authority_decision_id": decision_id,
                "authority_state": authority_state,
                "event_digest": _stable_digest("stage-event", observation.event_identity),
                "parent_digest": digest,
                "sequence": head_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        payloads.append(
            {
                "intent_id": intent_id,
                "affected_parent_digest": digest,
                "reason": reason,
                "available_at": observation.source_observed_at.isoformat(),
                "payload_json": payload,
            }
        )
        intents.append(
            StageHistoryInvalidationIntent(
                intent_id=intent_id,
                authority_decision_id=decision_id,
                target_kind="crm_stage_timeline",
                affected_logical_parent_digest=digest,
                reason=reason,
                state="pending",
                sequence=head_version,
                available_at=observation.source_observed_at,
            )
        )
    record = tx.run(
        APPEND_STAGE_HISTORY_INVALIDATION_INTENTS,
        **_fence_params(fence, required_run_type),
        authority_decision_id=decision_id,
        authority_head_version=head_version,
        authority_token=authority_token,
        expected_intent_count=len(intents),
        expected_target_digests=[item.affected_logical_parent_digest for item in intents],
        intents=payloads,
    ).single()
    _require_record(record, "stage-history invalidation set was incomplete")
    return tuple(intents)


def _persist_accounting(
    tx: ManagedTransaction, fence: FenceContext, unit: StageHistoryReplayUnit
) -> None:
    terminal = unit.accounting.terminal
    identity = unit.accounting.identity
    association = unit.accounting.association
    authority = unit.accounting.authority
    retry = unit.accounting.retry
    record = tx.run(
        UPSERT_STAGE_HISTORY_UNIT_ACCOUNTING,
        **_fence_params(fence, unit.run_type),
        unit_id=unit.unit_id,
        run_kind=(
            "artifact_replay" if unit.run_type != "capture_failure_accounting" else "failed_capture"
        ),
        fetched_count=terminal.fetched,
        malformed_excluded_count=terminal.malformed_excluded,
        capture_rejected_valid_count=terminal.capture_rejected_valid,
        excluded_out_of_scope_count=terminal.excluded_out_of_scope,
        canonical_effective_count=terminal.canonical_effective,
        canonical_pending_parent_count=terminal.canonical_pending_parent,
        parent_waiting_count=terminal.parent_waiting,
        parent_ambiguous_count=terminal.parent_ambiguous,
        same_hash_replay_count=terminal.same_hash_replay,
        differing_hash_conflict_count=terminal.differing_hash_conflict,
        new_variant_count=identity.new_variant,
        existing_same_hash_count=identity.existing_same_hash,
        new_conflict_variant_count=identity.new_conflict_variant,
        selected_active_count=association.selected_active,
        selected_pending_review_count=association.selected_pending_review,
        waiting_count=association.waiting,
        ambiguous_count=association.ambiguous,
        association_rejected_count=association.rejected,
        effective_count=authority.effective,
        withheld_parent_count=authority.withheld_parent,
        withheld_conflict_count=authority.withheld_conflict,
        authority_rejected_count=authority.rejected,
        corrected_count=authority.corrected,
        retry_none_count=retry.none,
        retry_pending_count=retry.pending,
        retry_claimed_count=retry.claimed,
        retry_resolved_count=retry.resolved,
        retry_rejected_count=retry.rejected,
        retry_quarantined_count=retry.quarantined,
    ).single()
    _require_record(record, "stage-history unit accounting did not match occurrences")


def _commit_checkpoint(
    tx: ManagedTransaction,
    fence: FenceContext,
    unit: StageHistoryReplayUnit,
    before: StageHistoryCheckpointSnapshot,
    after: StageHistoryCheckpointSnapshot,
) -> None:
    old_counts = _compatibility_counts(before)
    new_counts = _compatibility_counts(after)
    record = tx.run(
        COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT,
        **_fence_params(fence, unit.run_type),
        **_checkpoint_params(before),
        unit_id=unit.unit_id,
        unit_digest=unit.page_digest,
        page_sequence=unit.page_sequence,
        next_cursor_json=_cursor_json(after),
        committed_delta=new_counts.committed - old_counts.committed,
        duplicate_delta=new_counts.duplicate - old_counts.duplicate,
        excluded_delta=new_counts.excluded - old_counts.excluded,
        retry_delta=new_counts.retry - old_counts.retry,
        next_committed_count=new_counts.committed,
        next_duplicate_count=new_counts.duplicate,
        next_excluded_count=new_counts.excluded,
        next_retry_count=new_counts.retry,
    ).single()
    _require_record(record, "stage-history checkpoint compare-and-swap failed")


def _compatibility_counts(checkpoint: StageHistoryCheckpointSnapshot) -> _CompatibilityCounts:
    terminal = checkpoint.accounting.terminal
    return _CompatibilityCounts(
        committed=(
            terminal.canonical_effective
            + terminal.canonical_pending_parent
            + terminal.parent_waiting
            + terminal.parent_ambiguous
            + terminal.differing_hash_conflict
        ),
        duplicate=terminal.same_hash_replay,
        excluded=(
            terminal.malformed_excluded
            + terminal.capture_rejected_valid
            + terminal.excluded_out_of_scope
        ),
        retry=(
            terminal.canonical_pending_parent + terminal.parent_waiting + terminal.parent_ambiguous
        ),
    )


def _source_window_json(checkpoint: StageHistoryCheckpointSnapshot) -> str:
    window = checkpoint.source_window
    if isinstance(window, StageHistoryReplaySourceWindow):
        value: dict[str, JsonValue] = {
            "stage_ingestion_artifact_id": window.stage_ingestion_artifact_id,
            "artifact_manifest_hmac": window.artifact_manifest_hmac,
            "source_contract_uuid": window.source_contract_uuid,
            "entity_type_id": window.entity_type_id,
            "owner_artifact_id": window.owner_artifact_id,
            "owner_manifest_digest": window.owner_manifest_digest,
            "stage_artifact_id": window.stage_artifact_id,
            "qualification_evidence_digest": window.qualification_evidence_digest,
            "canonical_hash_version": window.canonical_hash_version,
            "traversal_contract": window.traversal_contract,
            "configuration_digest": window.configuration_digest,
            "limits_digest": window.limits_digest,
        }
    else:
        value = {
            "failed_artifact_id": window.failed_artifact_id,
            "manifest_hmac": window.manifest_hmac,
            "source_contract_uuid": window.source_contract_uuid,
            "stage_artifact_id": window.stage_artifact_id,
            "qualification_evidence_digest": window.qualification_evidence_digest,
            "configuration_digest": window.configuration_digest,
            "limits_digest": window.limits_digest,
        }
    return encode_json(value)


def _cursor_json(checkpoint: StageHistoryCheckpointSnapshot) -> str:
    return encode_json(
        {
            "last_page_sequence": checkpoint.last_page_sequence,
            "revision": checkpoint.revision,
        }
    )


def _artifact_id(checkpoint: StageHistoryCheckpointSnapshot) -> str:
    window = checkpoint.source_window
    if isinstance(window, StageHistoryReplaySourceWindow):
        return window.stage_ingestion_artifact_id
    return window.failed_artifact_id


def _has_domain_state(occurrence: StageHistoryOccurrence) -> bool:
    return occurrence.identity_hash_state is not None


def _observation_digest(
    observation: StageHistoryValidObservation | StageHistoryMalformedObservation,
) -> str:
    if isinstance(observation, StageHistoryMalformedObservation):
        return observation.canonical_raw_row_digest
    return observation.canonical_hash


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
    raise StageHistoryPersistenceError("graph returned an invalid association state")


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
    raise StageHistoryPersistenceError("graph returned an invalid authority state")


def _outbox_reason(disposition: str) -> StageHistoryOutboxReason:
    if disposition == "canonical_effective":
        return "initial_effective"
    if disposition == "differing_hash_conflict":
        return "conflict_withheld"
    return "rejection"


def _stable_id(domain: str, *parts: str) -> str:
    return f"{domain}:{_digest(domain, *parts)}"


def _stable_digest(domain: str, *parts: str) -> str:
    return f"sha256:{_digest(domain, *parts)}"


def _digest(domain: str, *parts: str) -> str:
    payload = json.dumps([domain, *parts], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_record(record: Record | None, message: str) -> Record:
    if record is None:
        raise StageHistoryPersistenceError(message)
    return record


def _record_int(record: Record, key: str) -> int:
    value: object = record[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StageHistoryPersistenceError(f"graph returned invalid {key}")
    return value


def _record_bool(record: Record, key: str) -> bool:
    value: object = record[key]
    if not isinstance(value, bool):
        raise StageHistoryPersistenceError(f"graph returned invalid {key}")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise StageHistoryPersistenceError("graph returned an invalid optional string")
    return value
