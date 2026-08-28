"""Admission, attempt ownership, and durable HTTP-call repository operations."""

from __future__ import annotations

import uuid

from neo4j import ManagedTransaction

from src.graph.bitrix_source_instances import BitrixSourceInstanceRepository
from src.graph.queries.standalone_crm_census import (
    ADMIT_CENSUS,
    CLAIM_ATTEMPT,
    RECORD_CALL_OUTCOME,
    RESERVE_CALL,
    TAKE_OVER_EXPIRED_ATTEMPT,
)
from src.graph.standalone_crm_census_migration import assert_standalone_crm_census_ready
from src.graph.standalone_crm_census_records import (
    StandaloneCrmAttemptTakeover,
    StandaloneCrmCensusAdmission,
    _StandaloneCrmCensusRepositoryBase,
    authority_context,
    authority_revision,
)
from src.standalone_crm_census_models import (
    StandaloneCrmCallIntent,
    StandaloneCrmCallOutcome,
    StandaloneCrmCensusConflictError,
    StandaloneCrmCensusRequest,
    canonical_request_payload,
    census_fingerprint,
)


def _attempt_task_id(census_id: str, generation: int) -> str:
    return f"standalone-crm-parent:{census_id}:{generation}"


class StandaloneCrmCensusAdmissionRepository(_StandaloneCrmCensusRepositoryBase):
    def require_active_source(self, request: StandaloneCrmCensusRequest) -> None:
        BitrixSourceInstanceRepository(self._client).require_active(
            request.source_key, request.source_instance_id
        )

    def admit(self, request: StandaloneCrmCensusRequest) -> StandaloneCrmCensusAdmission:
        assert_standalone_crm_census_ready(self._client)
        BitrixSourceInstanceRepository(self._client).admit(
            control_instance_id=request.control_instance_id,
            source_instance_id=request.source_instance_id,
        )
        fingerprint = census_fingerprint(request)

        def work(tx: ManagedTransaction) -> StandaloneCrmCensusAdmission:
            record = tx.run(
                ADMIT_CENSUS,
                census_id=uuid.uuid4().hex,
                source_key=request.source_key,
                source_instance_id=request.source_instance_id,
                control_instance_id=request.control_instance_id,
                census_kind=request.census_kind,
                occurrence_key=request.occurrence_key,
                scope_key="\x1f".join(
                    (
                        request.source_key,
                        request.source_instance_id,
                        request.control_instance_id,
                        request.census_kind,
                    )
                ),
                fingerprint=fingerprint,
                authority_revision=authority_revision(request),
                authority_json=authority_context(request),
                request_json=canonical_request_payload(request),
            ).single()
            if record is None or record["fingerprint_match"] is not True:
                raise StandaloneCrmCensusConflictError("standalone census occurrence conflicts")
            return StandaloneCrmCensusAdmission(
                census_id=str(record["census_id"]),
                status=str(record["status"]),
                replayed=record["replayed"] is True,
            )

        return self._client.execute_write(work)

    def claim_attempt(
        self, census_id: str, generation: int, fence_token: int, request: StandaloneCrmCensusRequest
    ) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            return (
                tx.run(
                    CLAIM_ATTEMPT,
                    census_id=census_id,
                    generation=generation,
                    fence_token=fence_token,
                    attempt_task_id=_attempt_task_id(census_id, generation),
                    lease_seconds=120,
                    max_attempts=request.budget.max_attempts_per_occurrence,
                    occurrence_deadline=request.budget.occurrence_deadline,
                    attempt_runtime_seconds=request.budget.max_runtime_seconds_per_attempt,
                    authority_revision=authority_revision(request),
                    authority_json=authority_context(request),
                ).single()
                is not None
            )

        return self._client.execute_write(work)

    def recover_or_take_over_attempt(
        self,
        census_id: str,
        prior_generation: int,
        next_generation: int,
        fence_token: int,
        *,
        lease_seconds: int = 120,
    ) -> StandaloneCrmAttemptTakeover | None:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != prior_generation:
            return None

        def work(tx: ManagedTransaction) -> StandaloneCrmAttemptTakeover | None:
            record = tx.run(
                TAKE_OVER_EXPIRED_ATTEMPT,
                census_id=census_id,
                prior_generation=prior_generation,
                next_generation=next_generation,
                fence_token=fence_token,
                attempt_task_id=_attempt_task_id(census_id, next_generation),
                lease_seconds=lease_seconds,
                authority_revision=authority_revision(snapshot.request),
                authority_json=authority_context(snapshot.request),
                max_attempts=snapshot.request.budget.max_attempts_per_occurrence,
                occurrence_deadline=snapshot.request.budget.occurrence_deadline,
                attempt_runtime_seconds=snapshot.request.budget.max_runtime_seconds_per_attempt,
            ).single()
            if record is None:
                return None
            return StandaloneCrmAttemptTakeover(
                int(record["generation"]), int(record["fence_token"])
            )

        return self._client.execute_write(work)

    def reserve_call(
        self, intent: StandaloneCrmCallIntent, fence_token: int, budget: StandaloneCrmCensusRequest
    ) -> bool:
        return self.reserve_call_with_sequence(intent, fence_token, budget) is not None

    def reserve_call_with_sequence(
        self, intent: StandaloneCrmCallIntent, fence_token: int, budget: StandaloneCrmCensusRequest
    ) -> int | None:
        def work(tx: ManagedTransaction) -> int | None:
            record = tx.run(
                RESERVE_CALL,
                census_id=intent.census_id,
                generation=intent.generation,
                fence_token=fence_token,
                intent_id=intent.intent_id,
                call_kind=intent.call_kind,
                stream_kind=intent.stream_kind,
                retry_ordinal=intent.retry_ordinal,
                cursor=intent.cursor,
                subject_id=intent.subject_id,
                deadline=intent.deadline,
                effective_deadline=intent.effective_deadline,
                task_id=intent.task_id or _attempt_task_id(intent.census_id, intent.generation),
                occurrence_call_limit=budget.budget.max_calls_per_occurrence,
                attempt_call_limit=budget.budget.max_calls_per_attempt,
                authority_revision=authority_revision(budget),
                authority_json=authority_context(budget),
            ).single()
            if record is None or not isinstance(record["call_sequence"], int):
                return None
            return int(record["call_sequence"])

        return self._client.execute_write(work)

    def record_call_outcome(self, outcome: StandaloneCrmCallOutcome) -> bool:
        return self._record_call_outcome(
            outcome.intent_id, outcome.state, outcome.upper_id, outcome.error_code
        )

    def _record_call_outcome(
        self, intent_id: str, state: str, upper_id: int | None, error_code: str | None
    ) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            return (
                tx.run(
                    RECORD_CALL_OUTCOME,
                    intent_id=intent_id,
                    status=state,
                    upper_id=upper_id,
                    error_code=error_code,
                ).single()
                is not None
            )

        return self._client.execute_write(work)
