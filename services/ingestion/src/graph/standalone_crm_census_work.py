"""Window, unit, fence, publication, and checkpoint repository operations."""

from __future__ import annotations

import json
from dataclasses import asdict

from neo4j import ManagedTransaction

from src.graph.queries.standalone_crm_census import (
    ACQUIRE_UNIT_FENCE,
    ALLOCATE_UNITS,
    CLAIM_PUBLISHED_CHILD,
    CLOSE_CONTACT_BINDING_POSITION,
    CONFIRM_PUBLICATION,
    CONVERGE_OCCURRENCE_EXHAUSTION,
    FREEZE_WINDOW,
    GET_RESUMABLE_UNITS,
    LEASE_HELD_PUBLISHED_CHILD,
    MARK_PUBLICATION_PUBLISHING,
    REFRESH_PUBLISHED_CHILD,
    RENEW_UNIT_FENCE,
    REPAIR_PUBLICATIONS,
    RESERVE_PUBLICATION,
    SETTLE_UNIT,
    STORE_CHECKPOINT,
)
from src.graph.standalone_crm_census_records import (
    StandaloneCrmPublicationRepair,
    _StandaloneCrmCensusRepositoryBase,
    authority_context,
    authority_revision,
    stream_kind,
)
from src.standalone_crm_census_models import (
    NoSourceWindow,
    SourceSyncCensusRequest,
    SourceWindow,
    StandaloneCrmCensusConflictError,
    StandaloneCrmCensusUnit,
    StandaloneCrmCheckpoint,
    StandaloneCrmCheckpointResult,
    StandaloneCrmChildEnvelope,
    StandaloneCrmPublication,
    canonical_request_payload,
)


class StandaloneCrmCensusWorkRepository(_StandaloneCrmCensusRepositoryBase):
    def _authority_for(self, census_id: str, generation: int) -> str | None:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation:
            return None
        return authority_revision(snapshot.request)

    def freeze_window(
        self, census_id: str, generation: int, window: SourceWindow | NoSourceWindow
    ) -> bool:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation:
            return False
        is_source = isinstance(snapshot.request, SourceSyncCensusRequest)
        if is_source != isinstance(window, SourceWindow):
            return False
        selected_bounds = (
            [
                {"stream_kind": stream_kind, "upper_id": upper_id}
                for stream_kind, upper_id in window.selected_bounds
            ]
            if isinstance(window, SourceWindow)
            else []
        )
        window_json = json.dumps(asdict(window), sort_keys=True, separators=(",", ":"))

        def work(tx: ManagedTransaction) -> bool:
            return (
                tx.run(
                    FREEZE_WINDOW,
                    census_id=census_id,
                    generation=generation,
                    window_json=window_json,
                    authority_revision=authority_revision(snapshot.request),
                    authority_json=authority_context(snapshot.request),
                    occurrence_deadline=snapshot.request.budget.occurrence_deadline,
                    window_kind="source" if is_source else "mapping",
                    selected_kinds=list(snapshot.request.selected_kinds),
                    selected_bounds=selected_bounds,
                ).single()
                is not None
            )

        return self._client.execute_write(work)

    def resumable_units(
        self, census_id: str, generation: int
    ) -> tuple[StandaloneCrmCensusUnit, ...]:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation:
            return ()
        revision = authority_revision(snapshot.request)

        def work(tx: ManagedTransaction) -> tuple[StandaloneCrmCensusUnit, ...]:
            return tuple(
                StandaloneCrmCensusUnit(
                    census_id,
                    generation,
                    stream_kind(str(record["stream_kind"])),
                    "pending_publication",
                    record["frozen_upper_id"]
                    if isinstance(record["frozen_upper_id"], int)
                    else None,
                    record["revision_id"] if isinstance(record["revision_id"], str) else None,
                )
                for record in tx.run(
                    GET_RESUMABLE_UNITS,
                    census_id=census_id,
                    generation=generation,
                    authority_revision=revision,
                    authority_json=authority_context(snapshot.request),
                )
            )

        return self._client.execute_read(work)

    def freeze_source_window(self, census_id: str, generation: int, window: SourceWindow) -> bool:
        return self.freeze_window(census_id, generation, window)

    def freeze_no_source_window(
        self, census_id: str, generation: int, window: NoSourceWindow
    ) -> bool:
        return self.freeze_window(census_id, generation, window)

    def allocate_units(
        self, census_id: str, generation: int, units: tuple[StandaloneCrmCensusUnit, ...]
    ) -> int:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation:
            return 0
        revision = authority_revision(snapshot.request)
        serialized = [
            {
                "stream_kind": unit.stream_kind,
                "state": unit.state,
                "frozen_upper_id": unit.frozen_upper_id,
                "revision_id": unit.revision_id,
            }
            for unit in units
        ]

        def work(tx: ManagedTransaction) -> int:
            record = tx.run(
                ALLOCATE_UNITS,
                census_id=census_id,
                generation=generation,
                units=serialized,
                authority_revision=revision,
                authority_json=authority_context(snapshot.request),
                census_status=snapshot.state,
                occurrence_deadline=snapshot.request.budget.occurrence_deadline,
            ).single()
            if record is None or not isinstance(record["allocated"], int):
                raise StandaloneCrmCensusConflictError("census unit allocation conflicts")
            return int(record["allocated"])

        return self._client.execute_write(work)

    def acquire_unit_fence(
        self,
        census_id: str,
        generation: int,
        stream_kind: str,
        owner_id: str,
        *,
        lease_seconds: int = 120,
    ) -> int | None:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation:
            return None
        revision = authority_revision(snapshot.request)

        def work(tx: ManagedTransaction) -> int | None:
            record = tx.run(
                ACQUIRE_UNIT_FENCE,
                census_id=census_id,
                generation=generation,
                stream_kind=stream_kind,
                owner_id=owner_id,
                lease_seconds=lease_seconds,
                authority_revision=revision,
                authority_json=authority_context(snapshot.request),
                occurrence_deadline=snapshot.request.budget.occurrence_deadline,
            ).single()
            return None if record is None else int(record["fence_token"])

        return self._client.execute_write(work)

    def claim_published_child(
        self,
        envelope: StandaloneCrmChildEnvelope,
        *,
        owner_id: str,
        payload_json: str,
        lease_seconds: int = 120,
    ) -> dict[str, object] | None:
        """Atomically validate and fence an exact durable child publication."""
        snapshot = self.runtime_snapshot(envelope.census_id)
        if (
            snapshot is None
            or snapshot.generation != envelope.generation
            or not isinstance(snapshot.request, SourceSyncCensusRequest)
        ):
            return None

        def work(tx: ManagedTransaction) -> dict[str, object] | None:
            record = tx.run(
                CLAIM_PUBLISHED_CHILD,
                census_id=envelope.census_id,
                generation=envelope.generation,
                stream_kind=envelope.stream_kind,
                frozen_upper_id=envelope.frozen_upper_id,
                task_name=envelope.task_name,
                task_id=envelope.task_id,
                payload_digest=envelope.payload_digest(),
                payload_json=payload_json,
                payload_version=envelope.payload_version,
                queue=envelope.queue,
                source_key=snapshot.request.source_key,
                source_instance_id=snapshot.request.source_instance_id,
                control_instance_id=snapshot.request.control_instance_id,
                request_json=canonical_request_payload(snapshot.request),
                authority_revision=authority_revision(snapshot.request),
                authority_json=authority_context(snapshot.request),
                occurrence_deadline=snapshot.request.budget.occurrence_deadline,
                occurrence_call_limit=snapshot.request.budget.max_calls_per_occurrence,
                occurrence_row_limit=snapshot.request.budget.max_rows_per_occurrence,
                attempt_call_limit=snapshot.request.budget.max_calls_per_attempt,
                attempt_row_limit=snapshot.request.budget.max_rows_per_attempt,
                owner_id=owner_id,
                lease_seconds=lease_seconds,
            ).single()
            return None if record is None else dict(record)

        return self._client.execute_write(work)

    def published_child_lease_held(
        self,
        envelope: StandaloneCrmChildEnvelope,
        *,
        owner_id: str,
        payload_json: str,
    ) -> bool:
        """Classify only an exact active publication lease as retryable."""
        snapshot = self.runtime_snapshot(envelope.census_id)
        if (
            snapshot is None
            or snapshot.generation != envelope.generation
            or not isinstance(snapshot.request, SourceSyncCensusRequest)
        ):
            return False

        def work(tx: ManagedTransaction) -> bool:
            return (
                tx.run(
                    LEASE_HELD_PUBLISHED_CHILD,
                    census_id=envelope.census_id,
                    generation=envelope.generation,
                    stream_kind=envelope.stream_kind,
                    frozen_upper_id=envelope.frozen_upper_id,
                    task_name=envelope.task_name,
                    task_id=envelope.task_id,
                    payload_digest=envelope.payload_digest(),
                    payload_json=payload_json,
                    payload_version=envelope.payload_version,
                    queue=envelope.queue,
                    source_key=snapshot.request.source_key,
                    source_instance_id=snapshot.request.source_instance_id,
                    control_instance_id=snapshot.request.control_instance_id,
                    request_json=canonical_request_payload(snapshot.request),
                    authority_revision=authority_revision(snapshot.request),
                    authority_json=authority_context(snapshot.request),
                    occurrence_deadline=snapshot.request.budget.occurrence_deadline,
                    occurrence_call_limit=snapshot.request.budget.max_calls_per_occurrence,
                    occurrence_row_limit=snapshot.request.budget.max_rows_per_occurrence,
                    attempt_call_limit=snapshot.request.budget.max_calls_per_attempt,
                    attempt_row_limit=snapshot.request.budget.max_rows_per_attempt,
                    owner_id=owner_id,
                ).single()
                is not None
            )

        return self._client.execute_read(work)

    def refresh_published_child(
        self,
        envelope: StandaloneCrmChildEnvelope,
        *,
        owner_id: str,
        fence_token: int,
        payload_json: str,
    ) -> dict[str, object] | None:
        """Read the exact current checkpoint held by one already-claimed child."""
        snapshot = self.runtime_snapshot(envelope.census_id)
        if (
            snapshot is None
            or snapshot.generation != envelope.generation
            or not isinstance(snapshot.request, SourceSyncCensusRequest)
        ):
            return None

        def work(tx: ManagedTransaction) -> dict[str, object] | None:
            record = tx.run(
                REFRESH_PUBLISHED_CHILD,
                census_id=envelope.census_id,
                generation=envelope.generation,
                stream_kind=envelope.stream_kind,
                frozen_upper_id=envelope.frozen_upper_id,
                task_name=envelope.task_name,
                task_id=envelope.task_id,
                payload_digest=envelope.payload_digest(),
                payload_json=payload_json,
                payload_version=envelope.payload_version,
                queue=envelope.queue,
                source_key=snapshot.request.source_key,
                source_instance_id=snapshot.request.source_instance_id,
                control_instance_id=snapshot.request.control_instance_id,
                request_json=canonical_request_payload(snapshot.request),
                authority_revision=authority_revision(snapshot.request),
                authority_json=authority_context(snapshot.request),
                occurrence_deadline=snapshot.request.budget.occurrence_deadline,
                owner_id=owner_id,
                fence_token=fence_token,
            ).single()
            return None if record is None else dict(record)

        return self._client.execute_read(work)

    def close_contact_binding_position(
        self,
        census_id: str,
        generation: int,
        fence_token: int,
        owner_id: str,
        task_name: str,
        task_id: str,
        payload_digest: str,
        frozen_upper_id: int,
        last_committed_id: int,
        contact_id: int,
        binding_count: int,
    ) -> bool:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation or binding_count < 0:
            return False
        return self._client.execute_write(
            lambda tx: (
                tx.run(
                    CLOSE_CONTACT_BINDING_POSITION,
                    census_id=census_id,
                    generation=generation,
                    fence_token=fence_token,
                    owner_id=owner_id,
                    task_name=task_name,
                    task_id=task_id,
                    payload_digest=payload_digest,
                    frozen_upper_id=frozen_upper_id,
                    last_committed_id=last_committed_id,
                    contact_id=contact_id,
                    binding_count=binding_count,
                    authority_revision=authority_revision(snapshot.request),
                    authority_json=authority_context(snapshot.request),
                    occurrence_deadline=snapshot.request.budget.occurrence_deadline,
                ).single()
                is not None
            )
        )

    def renew_unit_fence(
        self, census_id: str, generation: int, stream_kind: str, fence_token: int, owner_id: str
    ) -> bool:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation:
            return False
        revision = authority_revision(snapshot.request)
        return self._client.execute_write(
            lambda tx: (
                tx.run(
                    RENEW_UNIT_FENCE,
                    census_id=census_id,
                    generation=generation,
                    stream_kind=stream_kind,
                    fence_token=fence_token,
                    owner_id=owner_id,
                    lease_seconds=120,
                    authority_revision=revision,
                    authority_json=authority_context(snapshot.request),
                    occurrence_deadline=snapshot.request.budget.occurrence_deadline,
                ).single()
                is not None
            )
        )

    def settle_unit(
        self,
        census_id: str,
        generation: int,
        stream_kind: str,
        fence_token: int,
        state: str,
        *,
        no_work: bool = False,
    ) -> bool:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation:
            return False
        return self._client.execute_write(
            lambda tx: (
                tx.run(
                    SETTLE_UNIT,
                    census_id=census_id,
                    generation=generation,
                    stream_kind=stream_kind,
                    fence_token=fence_token,
                    unit_state=state,
                    no_work=no_work,
                    authority_revision=authority_revision(snapshot.request),
                    authority_json=authority_context(snapshot.request),
                    allow_cancel_settlement=state == "cancelled",
                ).single()
                is not None
            )
        )

    def converge_occurrence_exhaustion(self, census_id: str, generation: int) -> bool:
        """Retire all units after an atomic writer has persisted occurrence exhaustion."""
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None or snapshot.generation != generation:
            return False
        return self._client.execute_write(
            lambda tx: (
                tx.run(
                    CONVERGE_OCCURRENCE_EXHAUSTION,
                    census_id=census_id,
                    generation=generation,
                    authority_revision=authority_revision(snapshot.request),
                    authority_json=authority_context(snapshot.request),
                ).single()
                is not None
            )
        )

    def repair_publications(self, census_id: str) -> tuple[StandaloneCrmPublicationRepair, ...]:
        snapshot = self.runtime_snapshot(census_id)
        if snapshot is None:
            return ()

        def work(tx: ManagedTransaction) -> tuple[StandaloneCrmPublicationRepair, ...]:
            return tuple(
                StandaloneCrmPublicationRepair(
                    str(record["task_id"]),
                    str(record["status"]),
                    str(record["payload_json"]),
                    str(record["task_name"]),
                    str(record["queue"]),
                    str(record["payload_digest"]),
                    stream_kind(str(record["stream_kind"])),
                    int(record["generation"]),
                )
                for record in tx.run(
                    REPAIR_PUBLICATIONS,
                    census_id=census_id,
                    generation=snapshot.generation,
                    authority_revision=authority_revision(snapshot.request),
                    authority_json=authority_context(snapshot.request),
                )
            )

        return self._client.execute_write(work)

    def reserve_publication(self, publication: StandaloneCrmPublication) -> bool:
        envelope = StandaloneCrmChildEnvelope(
            publication.census_id,
            publication.generation,
            publication.stream_kind,
            0,
            None,
            "standalone-crm-census-child",
            publication.task_id,
            "ingestion",
        )
        return self.reserve_child_envelope(envelope)

    def reserve_child_envelope(self, envelope: StandaloneCrmChildEnvelope) -> bool:
        snapshot = self.runtime_snapshot(envelope.census_id)
        if snapshot is None or snapshot.generation != envelope.generation:
            return False
        revision = authority_revision(snapshot.request)
        payload_json = json.dumps(asdict(envelope), sort_keys=True, separators=(",", ":"))

        def work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                RESERVE_PUBLICATION,
                census_id=envelope.census_id,
                generation=envelope.generation,
                stream_kind=envelope.stream_kind,
                task_id=envelope.task_id,
                payload_digest=envelope.payload_digest(),
                task_name=envelope.task_name,
                queue=envelope.queue,
                payload_json=payload_json,
                payload_version=envelope.payload_version,
                authority_revision=revision,
                authority_json=authority_context(snapshot.request),
                occurrence_deadline=snapshot.request.budget.occurrence_deadline,
            ).single()
            return record is not None and str(record["task_id"]) == envelope.task_id

        return self._client.execute_write(work)

    def confirm_publication(self, publication: StandaloneCrmPublication) -> bool:
        snapshot = self.runtime_snapshot(publication.census_id)
        if snapshot is None or snapshot.generation != publication.generation:
            return False
        revision = authority_revision(snapshot.request)
        return self._client.execute_write(
            lambda tx: (
                tx.run(
                    CONFIRM_PUBLICATION,
                    census_id=publication.census_id,
                    generation=publication.generation,
                    stream_kind=publication.stream_kind,
                    task_id=publication.task_id,
                    authority_revision=revision,
                    authority_json=authority_context(snapshot.request),
                    occurrence_deadline=snapshot.request.budget.occurrence_deadline,
                ).single()
                is not None
            )
        )

    def mark_publication_publishing(self, publication: StandaloneCrmPublication) -> str | None:
        snapshot = self.runtime_snapshot(publication.census_id)
        if snapshot is None or snapshot.generation != publication.generation:
            return None
        revision = authority_revision(snapshot.request)

        def work(tx: ManagedTransaction) -> str | None:
            record = tx.run(
                MARK_PUBLICATION_PUBLISHING,
                census_id=publication.census_id,
                generation=publication.generation,
                stream_kind=publication.stream_kind,
                task_id=publication.task_id,
                authority_revision=revision,
                authority_json=authority_context(snapshot.request),
                occurrence_deadline=snapshot.request.budget.occurrence_deadline,
            ).single()
            return None if record is None else str(record["payload_json"])

        return self._client.execute_write(work)

    def store_checkpoint(
        self, checkpoint: StandaloneCrmCheckpoint, *, attempt_rows: int, occurrence_rows: int
    ) -> StandaloneCrmCheckpointResult:
        del attempt_rows, occurrence_rows
        snapshot = self.runtime_snapshot(checkpoint.census_id)
        if snapshot is None or snapshot.generation != checkpoint.generation:
            return StandaloneCrmCheckpointResult("stale_or_conflict")

        def work(tx: ManagedTransaction) -> StandaloneCrmCheckpointResult:
            record = tx.run(
                STORE_CHECKPOINT,
                census_id=checkpoint.census_id,
                generation=checkpoint.generation,
                fence_token=checkpoint.fence_token,
                stream_kind=checkpoint.stream_kind,
                last_committed_id=checkpoint.last_committed_id,
                binding_subject_id=checkpoint.binding_subject_id,
                binding_offset=checkpoint.binding_offset,
                processed_rows=checkpoint.processed_rows,
                skipped_rows=checkpoint.skipped_rows,
                attempt_row_limit=snapshot.request.budget.max_rows_per_attempt,
                occurrence_row_limit=snapshot.request.budget.max_rows_per_occurrence,
                authority_revision=authority_revision(snapshot.request),
                authority_json=authority_context(snapshot.request),
                allow_cancel_checkpoint=snapshot.cancel_requested,
                occurrence_deadline=snapshot.request.budget.occurrence_deadline,
            ).single()
            if record is None or not isinstance(record["decision"], str):
                return StandaloneCrmCheckpointResult("stale_or_conflict")
            decision = record["decision"]
            if decision == "stored":
                return StandaloneCrmCheckpointResult("stored")
            if decision == "attempt_exhausted":
                return StandaloneCrmCheckpointResult("attempt_exhausted")
            if decision == "occurrence_exhausted":
                tx.run(
                    CONVERGE_OCCURRENCE_EXHAUSTION,
                    census_id=checkpoint.census_id,
                    generation=checkpoint.generation,
                    authority_revision=authority_revision(snapshot.request),
                    authority_json=authority_context(snapshot.request),
                ).consume()
                return StandaloneCrmCheckpointResult("occurrence_exhausted")
            return StandaloneCrmCheckpointResult("stale_or_conflict")

        return self._client.execute_write(work)
