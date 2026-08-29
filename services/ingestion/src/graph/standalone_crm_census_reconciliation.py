"""Status, cancellation, continuation, classification, and terminal repository operations."""

from __future__ import annotations

import json

from neo4j import ManagedTransaction

from src.graph.queries.standalone_crm_census import (
    CLASSIFY_UNRESOLVED_CALLS,
    CONVERGE_LIMIT_DENIAL,
    CREATE_CONTINUATION,
    FAIL_AFTER_WINDOW_AUTHORITY,
    FAIL_FREEZE,
    GET_CENSUS_REQUEST,
    GET_CENSUS_STATUS,
    PAUSE_CENSUS,
    PAUSE_CLAIMED_UNIT,
    REQUEST_CANCELLATION,
    REQUEST_UNIT_STOPS,
    RESUME_CENSUS,
    SETTLE_ATTEMPT,
    SETTLE_CANCELLATION,
    TERMINALIZE_CENSUS,
)
from src.graph.standalone_crm_census_records import (
    StandaloneCrmCensusStatus,
    StandaloneCrmRuntimeSnapshot,
    _StandaloneCrmCensusRepositoryBase,
    authority_context,
    terminal_window_expectations,
)
from src.graph.standalone_crm_census_records import (
    authority_revision as authority_revision_for_request,
)
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_census_models import (
    StandaloneCrmCensusConflictError,
    StandaloneCrmCensusRequest,
    StandaloneCrmReason,
    StandaloneCrmTerminalState,
    parse_stored_census_request,
)


def _attempt_task_id(census_id: str, generation: int) -> str:
    return f"standalone-crm-parent:{census_id}:{generation}"


class StandaloneCrmCensusReconciliationRepository(_StandaloneCrmCensusRepositoryBase):
    def settle_attempt(self, census_id: str, generation: int) -> bool:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation:
            return False
        return self._client.execute_write(
            lambda tx: (
                tx.run(
                    SETTLE_ATTEMPT,
                    census_id=census_id,
                    generation=generation,
                    authority_revision=authority_revision_for_request(snapshot.request),
                    authority_json=authority_context(snapshot.request),
                ).single()
                is not None
            )
        )

    def status(self, census_id: str) -> StandaloneCrmCensusStatus | None:
        def work(tx: ManagedTransaction) -> StandaloneCrmCensusStatus | None:
            record = tx.run(GET_CENSUS_STATUS, census_id=census_id).single()
            if record is None:
                return None
            generation = record["generation"]
            attempts = record["attempts"]
            if not isinstance(generation, int) or not isinstance(attempts, int):
                raise StandaloneCrmCensusConflictError("standalone census status is malformed")
            return StandaloneCrmCensusStatus(
                census_id=str(record["census_id"]),
                state=str(record["status"]),
                generation=generation,
                cancel_requested=record["cancel_requested"] is True,
                window_frozen=record["window_json"] is not None,
                attempts=attempts,
            )

        return self._client.execute_read(work)

    def runtime_snapshot(self, census_id: str) -> StandaloneCrmRuntimeSnapshot | None:
        def work(tx: ManagedTransaction) -> StandaloneCrmRuntimeSnapshot | None:
            record = tx.run(GET_CENSUS_REQUEST, census_id=census_id).single()
            if record is None:
                return None
            raw_json, generation, state = (
                record["request_json"],
                record["generation"],
                record["status"],
            )
            if (
                not isinstance(raw_json, str)
                or not isinstance(generation, int)
                or not isinstance(state, str)
            ):
                raise StandaloneCrmCensusConflictError("standalone census snapshot is malformed")
            parsed = json.loads(raw_json)
            if not isinstance(parsed, dict):
                raise StandaloneCrmCensusConflictError("standalone census request is malformed")
            return StandaloneCrmRuntimeSnapshot(
                parse_stored_census_request(parsed),
                generation,
                state,
                record["cancel_requested"] is True,
                record["window_json"] is not None,
                record["window_json"] if isinstance(record["window_json"], str) else None,
                None if record["attempt_deadline"] is None else str(record["attempt_deadline"]),
            )

        return self._client.execute_read(work)

    def request_cancellation(self, census_id: str, actor: str, reason: str) -> bool:
        if not actor.strip() or not reason.strip():
            raise ValueError("cancellation actor and reason must be non-empty")

        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None:
            return False

        def work(tx: ManagedTransaction) -> bool:
            requested = tx.run(
                REQUEST_CANCELLATION,
                census_id=census_id,
                actor=actor[:200],
                reason=reason[:1000],
                authority_revision=authority_revision_for_request(snapshot.request),
                authority_json=authority_context(snapshot.request),
            ).single()
            if requested is None:
                return False
            tx.run(
                REQUEST_UNIT_STOPS,
                census_id=census_id,
                generation=snapshot.generation,
                authority_revision=authority_revision_for_request(snapshot.request),
                authority_json=authority_context(snapshot.request),
            ).consume()
            return True

        return self._client.execute_write(work)

    def resume(self, census_id: str) -> bool:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None:
            return False
        return self._client.execute_write(
            lambda tx: (
                tx.run(
                    RESUME_CENSUS,
                    census_id=census_id,
                    generation=snapshot.generation,
                    authority_revision=authority_revision_for_request(snapshot.request),
                    authority_json=authority_context(snapshot.request),
                    occurrence_deadline=snapshot.request.budget.occurrence_deadline,
                    lease_seconds=120,
                ).single()
                is not None
            )
        )

    def create_continuation(
        self, census_id: str, generation: int, request: StandaloneCrmCensusRequest
    ) -> int | None:
        def work(tx: ManagedTransaction) -> int | None:
            record = tx.run(
                CREATE_CONTINUATION,
                census_id=census_id,
                generation=generation,
                next_generation=generation + 1,
                attempt_task_id=_attempt_task_id(census_id, generation + 1),
                authority_revision=authority_revision_for_request(request),
                authority_json=authority_context(request),
                max_attempts=request.budget.max_attempts_per_occurrence,
                occurrence_deadline=request.budget.occurrence_deadline,
                attempt_runtime_seconds=request.budget.max_runtime_seconds_per_attempt,
                lease_seconds=120,
            ).single()
            return None if record is None else int(record["generation"])

        return self._client.execute_write(work)

    def pause(self, census_id: str, generation: int, reason_code: str, detail: str) -> bool:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation:
            return False
        return self._client.execute_write(
            lambda tx: (
                tx.run(
                    PAUSE_CENSUS,
                    census_id=census_id,
                    generation=generation,
                    reason_code=reason_code,
                    detail=detail,
                    authority_revision=authority_revision_for_request(snapshot.request),
                    authority_json=authority_context(snapshot.request),
                ).single()
                is not None
            )
        )

    def pause_claimed_unit(
        self,
        census_id: str,
        generation: int,
        stream_kind: str,
        fence_token: int,
        owner_id: str,
        task_name: str,
        task_id: str,
        payload_digest: str,
        frozen_upper_id: int,
        checkpoint: StandaloneCrmCheckpoint,
        reason_code: str,
        detail: str,
    ) -> bool:
        """Durably pause one claimed child, creating its initial checkpoint if needed."""
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation:
            return False
        return self._client.execute_write(
            lambda tx: (
                tx.run(
                    PAUSE_CLAIMED_UNIT,
                    census_id=census_id,
                    generation=generation,
                    stream_kind=stream_kind,
                    fence_token=fence_token,
                    owner_id=owner_id,
                    task_name=task_name,
                    task_id=task_id,
                    payload_digest=payload_digest,
                    frozen_upper_id=frozen_upper_id,
                    last_committed_id=checkpoint.last_committed_id,
                    processed_rows=checkpoint.processed_rows,
                    skipped_rows=checkpoint.skipped_rows,
                    binding_subject_id=checkpoint.binding_subject_id,
                    binding_offset=checkpoint.binding_offset,
                    reason_code=reason_code,
                    detail=detail,
                    authority_revision=authority_revision_for_request(snapshot.request),
                    authority_json=authority_context(snapshot.request),
                    occurrence_deadline=snapshot.request.budget.occurrence_deadline,
                ).single()
                is not None
            )
        )

    def fail_freeze(self, census_id: str, generation: int, reason: StandaloneCrmReason) -> bool:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation:
            return False
        return self._client.execute_write(
            lambda tx: (
                tx.run(
                    FAIL_FREEZE,
                    census_id=census_id,
                    generation=generation,
                    reason_code=reason.code,
                    authority_revision=authority_revision_for_request(snapshot.request),
                    authority_json=authority_context(snapshot.request),
                ).single()
                is not None
            )
        )

    def fail_after_window_authority(
        self, census_id: str, generation: int, reason: StandaloneCrmReason
    ) -> bool:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation or not snapshot.window_frozen:
            return False
        return self._client.execute_write(
            lambda tx: (
                tx.run(
                    FAIL_AFTER_WINDOW_AUTHORITY,
                    census_id=census_id,
                    generation=generation,
                    reason_code=reason.code,
                    authority_revision=authority_revision_for_request(snapshot.request),
                    authority_json=authority_context(snapshot.request),
                ).single()
                is not None
            )
        )

    def converge_limit_denial(
        self, census_id: str, generation: int, request: StandaloneCrmCensusRequest, reason_code: str
    ) -> str | None:
        if reason_code not in {"attempts_exhausted", "deadline_exhausted"}:
            raise ValueError("invalid standalone census limit-denial reason")

        def work(tx: ManagedTransaction) -> str | None:
            record = tx.run(
                CONVERGE_LIMIT_DENIAL,
                census_id=census_id,
                generation=generation,
                reason_code=reason_code,
                max_attempts=request.budget.max_attempts_per_occurrence,
                occurrence_deadline=request.budget.occurrence_deadline,
                authority_revision=authority_revision_for_request(request),
                authority_json=authority_context(request),
            ).single()
            return None if record is None else str(record["status"])

        return self._client.execute_write(work)

    def settle_cancellation(self, census_id: str, generation: int) -> bool:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation:
            return False
        return self._client.execute_write(
            lambda tx: (
                tx.run(
                    SETTLE_CANCELLATION,
                    census_id=census_id,
                    generation=generation,
                    authority_revision=authority_revision_for_request(snapshot.request),
                    authority_json=authority_context(snapshot.request),
                ).single()
                is not None
            )
        )

    def classify_unresolved_calls(self, census_id: str) -> int:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None:
            return 0

        def work(tx: ManagedTransaction) -> int:
            record = tx.run(
                CLASSIFY_UNRESOLVED_CALLS,
                census_id=census_id,
                generation=snapshot.generation,
                authority_revision=authority_revision_for_request(snapshot.request),
                authority_json=authority_context(snapshot.request),
            ).single()
            return 0 if record is None else int(record["classified"])

        return self._client.execute_write(work)

    def terminalize(
        self,
        census_id: str,
        generation: int,
        terminal_state: StandaloneCrmTerminalState,
        reason: StandaloneCrmReason,
        authority_revision: str,
    ) -> bool:
        snapshot = self.runtime_snapshot(census_id)
        if (
            snapshot is None
            or snapshot.generation != generation
            or authority_revision != authority_revision_for_request(snapshot.request)
        ):
            return False
        try:
            expected_units = terminal_window_expectations(snapshot.request, snapshot.window_json)
        except StandaloneCrmCensusConflictError:
            return False
        return self._client.execute_write(
            lambda tx: (
                tx.run(
                    TERMINALIZE_CENSUS,
                    census_id=census_id,
                    generation=generation,
                    terminal_state=terminal_state,
                    reason_code=reason.code,
                    authority_revision=authority_revision,
                    authority_json=authority_context(snapshot.request),
                    occurrence_deadline=snapshot.request.budget.occurrence_deadline,
                    selected_kinds=list(snapshot.request.selected_kinds),
                    expected_units=expected_units,
                ).single()
                is not None
            )
        )
