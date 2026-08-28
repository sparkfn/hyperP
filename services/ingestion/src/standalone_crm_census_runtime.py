"""Application-level runtime for bounded standalone Bitrix CRM censuses."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Protocol, cast, runtime_checkable

from src.graph.standalone_crm_census import (
    CensusRepositoryError,
    StandaloneCrmCensusRepository,
)
from src.standalone_crm_census_models import (
    AuthorityHeads,
    CensusBudgets,
    CensusConflictError,
    CensusIdentity,
    CensusKind,
    CensusPublicationError,
    CensusRequest,
    HttpCallKind,
    HttpCallState,
    MappingAuthorityUnavailableError,
    MappingPrepareCensusRequest,
    MappingRollbackCensusRequest,
    MissingCensusChildHandlerError,
    ParentState,
    SourceSyncCensusRequest,
    StandaloneCrmKind,
    census_fingerprint,
)


@runtime_checkable
class MappingAuthorityReader(Protocol):
    """Authoritative mapping head/revision reader supplied by #275 later."""

    def prepare_head(self, request: MappingPrepareCensusRequest) -> AuthorityHeads: ...

    def rollback_head(self, request: MappingRollbackCensusRequest) -> AuthorityHeads: ...


@runtime_checkable
class SourceAuthorityReader(Protocol):
    """Read the active immutable mapping and projection heads."""

    def source_heads(self) -> AuthorityHeads: ...


@runtime_checkable
class CensusProbeClient(Protocol):
    """Perform exactly one already-reserved upper-bound probe."""

    def probe_upper_id(self, kind: StandaloneCrmKind) -> int: ...


@runtime_checkable
class CensusChildPublisher(Protocol):
    """Publish one already-reserved immutable child envelope."""

    def publish(self, census_id: str, unit_kind: StandaloneCrmKind, payload: str) -> str: ...


class SourceInstanceAdmitter(Protocol):
    """Minimal #272 admission boundary used before census graph writes."""

    def admit(self, *, control_instance_id: str, source_instance_id: str) -> None: ...


_Clock = Callable[[], datetime]


class StandaloneCrmCensusRuntime:
    """Admit, freeze, and reconcile censuses without writing source-domain facts."""

    def __init__(
        self,
        *,
        source_repository: SourceInstanceAdmitter,
        census_repository: StandaloneCrmCensusRepository,
        authority: MappingAuthorityReader | SourceAuthorityReader | CensusProbeClient,
        clock: Callable[[], datetime],
        publisher: CensusChildPublisher | None = None,
    ) -> None:
        self._source_instances = source_repository
        self._census = census_repository
        self._authority = authority
        self._clock = clock
        self._publisher = publisher

    def start(
        self,
        *,
        kind: CensusKind,
        identity: CensusIdentity,
        request: CensusRequest,
        budget: CensusBudgets,
        now: datetime | None = None,
    ) -> str:
        if kind is CensusKind.SOURCE_SYNC and not isinstance(request, SourceSyncCensusRequest):
            raise CensusConflictError("source_sync request kind mismatch")
        if kind is CensusKind.MAPPING_PREPARE and not isinstance(
            request, MappingPrepareCensusRequest
        ):
            raise CensusConflictError("mapping_prepare request kind mismatch")
        if kind is CensusKind.MAPPING_ROLLBACK and not isinstance(
            request, MappingRollbackCensusRequest
        ):
            raise CensusConflictError("mapping_rollback request kind mismatch")

        # Registration/migration admission happens before any census graph write.
        self._source_instances.admit(
            control_instance_id=identity.control_instance_id,
            source_instance_id=identity.source_instance_id,
        )
        self._census.assert_ready()

        heads = self._authority_heads(kind, request)
        fingerprint = census_fingerprint(kind, identity, request, budget, heads)
        effective_now = now or self._clock()
        occurrence_deadline = effective_now + timedelta(
            seconds=budget.occurrence_wall_clock_seconds
        )
        census_id, _created = self._census.admit(
            source_instance_id=identity.source_instance_id,
            control_instance_id=identity.control_instance_id,
            census_kind=kind,
            occurrence_key=identity.occurrence_key,
            fingerprint=fingerprint,
            request_json=json.dumps(asdict(request), sort_keys=True, separators=(",", ":")),
            budget_json=json.dumps(asdict(budget), sort_keys=True, separators=(",", ":")),
            heads_json=json.dumps(asdict(heads), sort_keys=True, separators=(",", ":")),
            occurrence_deadline=occurrence_deadline,
            occurrence_calls=budget.occurrence_calls,
            occurrence_rows=budget.occurrence_rows,
            attempt_calls=budget.attempt_calls,
            attempt_rows=budget.attempt_rows,
            attempt_runtime_seconds=budget.attempt_runtime_seconds,
            max_attempts=budget.max_attempts,
        )
        attempt_deadline = effective_now + timedelta(seconds=budget.attempt_runtime_seconds)
        _generation, fence_token = self._census.claim_attempt(
            census_id=census_id,
            fingerprint=fingerprint,
            attempt_deadline=attempt_deadline,
        )
        if kind is CensusKind.SOURCE_SYNC:
            if isinstance(request, SourceSyncCensusRequest):
                self._freeze_source_window(
                    census_id=census_id,
                    fingerprint=fingerprint,
                    fence_token=fence_token,
                    attempt_deadline=attempt_deadline,
                    selected_kinds=request.selected_kinds,
                )
        else:
            self._census.commit_no_source_window(census_id=census_id, fingerprint=fingerprint)
        return census_id

    def _authority_heads(self, kind: CensusKind, request: CensusRequest) -> AuthorityHeads:
        if kind is CensusKind.SOURCE_SYNC:
            try:
                return cast(SourceAuthorityReader, self._authority).source_heads()
            except AttributeError as exc:
                raise MappingAuthorityUnavailableError(
                    "standalone source authority is unavailable"
                ) from exc
        if kind is CensusKind.MAPPING_PREPARE:
            if not isinstance(request, MappingPrepareCensusRequest):
                raise CensusConflictError("mapping_prepare request kind mismatch")
            try:
                return cast(MappingAuthorityReader, self._authority).prepare_head(request)
            except AttributeError as exc:
                raise MappingAuthorityUnavailableError(
                    "mapping prepare authority is unavailable"
                ) from exc
        if not isinstance(request, MappingRollbackCensusRequest):
            raise CensusConflictError("mapping_rollback request kind mismatch")
        try:
            return cast(MappingAuthorityReader, self._authority).rollback_head(request)
        except AttributeError as exc:
            raise MappingAuthorityUnavailableError(
                "mapping rollback authority is unavailable"
            ) from exc

    def pause(self, *, census_id: str, fingerprint: str, reason: str) -> None:
        self._census.pause(census_id=census_id, fingerprint=fingerprint, reason=reason)

    def cancel(self, *, census_id: str, fingerprint: str, actor: str) -> ParentState:
        return self._census.cancel(census_id=census_id, fingerprint=fingerprint, actor=actor)

    def continue_census(self, *, census_id: str, fingerprint: str) -> tuple[int, str]:
        runtime_seconds = self._census.continue_census(census_id=census_id, fingerprint=fingerprint)
        attempt_deadline = self._clock() + timedelta(seconds=runtime_seconds)
        return self._census.claim_attempt(
            census_id=census_id,
            fingerprint=fingerprint,
            attempt_deadline=attempt_deadline,
        )

    def finalize(
        self,
        *,
        census_id: str,
        fingerprint: str,
        terminal_state: ParentState,
        reason: str,
        allow_paused: bool = False,
    ) -> None:
        self._census.finalize(
            census_id=census_id,
            fingerprint=fingerprint,
            terminal_state=terminal_state,
            reason=reason,
            allow_paused=allow_paused,
        )

    def mark_child_terminal(
        self,
        *,
        census_id: str,
        fingerprint: str,
        fence_token: str,
        unit_kind: StandaloneCrmKind,
        terminal_state: str,
        reason: str,
    ) -> None:
        self._census.mark_child_terminal(
            census_id=census_id,
            fingerprint=fingerprint,
            fence_token=fence_token,
            unit_kind=unit_kind,
            terminal_state=terminal_state,
            reason=reason,
        )

    def claim_child(
        self,
        *,
        census_id: str,
        fingerprint: str,
        fence_token: str,
        unit_kind: StandaloneCrmKind,
    ) -> tuple[int, str]:
        return self._census.claim_child(
            census_id=census_id,
            fingerprint=fingerprint,
            fence_token=fence_token,
            unit_kind=unit_kind,
        )

    def advance_checkpoint(
        self,
        *,
        census_id: str,
        fingerprint: str,
        fence_token: str,
        unit_kind: StandaloneCrmKind,
        last_id: int,
        rows_processed: int,
        binding_position: int = 0,
    ) -> tuple[int, int]:
        return self._census.advance_checkpoint(
            census_id=census_id,
            fingerprint=fingerprint,
            fence_token=fence_token,
            unit_kind=unit_kind,
            last_id=last_id,
            rows_processed=rows_processed,
            binding_position=binding_position,
        )

    def reserve_publication(
        self,
        *,
        census_id: str,
        fingerprint: str,
        fence_token: str,
        unit_kind: StandaloneCrmKind,
        publication_sequence: int,
        task_name: str,
        task_id: str,
        queue: str,
        payload_version: str,
        payload_digest: str,
        payload_json: str,
    ) -> str:
        return self._census.reserve_publication(
            census_id=census_id,
            fingerprint=fingerprint,
            fence_token=fence_token,
            unit_kind=unit_kind,
            publication_sequence=publication_sequence,
            task_name=task_name,
            task_id=task_id,
            queue=queue,
            payload_version=payload_version,
            payload_digest=payload_digest,
            payload_json=payload_json,
        )

    def confirm_publication(self, *, census_id: str, task_id: str) -> None:
        self._census.confirm_publication(census_id=census_id, task_id=task_id)

    def publish_child(
        self,
        *,
        census_id: str,
        fingerprint: str,
        fence_token: str,
        unit_kind: StandaloneCrmKind,
        publication_sequence: int,
        payload_version: str,
        payload: dict[str, object],
        queue: str = "ingestion",
    ) -> str:
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        task_name = f"src.standalone_crm_census_tasks.run_{unit_kind}_child_task"
        task_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"standalone-crm-census:{census_id}:{unit_kind}:{publication_sequence}",
            )
        )
        reserved_task_id = self._census.reserve_publication(
            census_id=census_id,
            fingerprint=fingerprint,
            fence_token=fence_token,
            unit_kind=unit_kind,
            publication_sequence=publication_sequence,
            task_name=task_name,
            task_id=task_id,
            queue=queue,
            payload_version=payload_version,
            payload_digest=payload_digest,
            payload_json=payload_json,
        )
        if self._publisher is None:
            self.pause(
                census_id=census_id,
                fingerprint=fingerprint,
                reason="census_child_handler_missing",
            )
            raise MissingCensusChildHandlerError(
                "census child handler is not registered; no broker I/O was performed"
            )
        published_task_id = self._publisher.publish(census_id, unit_kind, payload_json)
        if published_task_id != reserved_task_id:
            raise CensusPublicationError("publisher returned a different immutable task identity")
        self._census.confirm_publication(census_id=census_id, task_id=reserved_task_id)
        return task_id

    def status(self, census_id: str) -> dict[str, object] | None:
        return self._census.status(census_id)

    def recover_stale_attempt(self, *, census_id: str, fingerprint: str) -> tuple[int, str]:
        return self._census.supersede_stale_attempt(census_id=census_id, fingerprint=fingerprint)

    def unresolved_publications(self, census_id: str) -> list[dict[str, object]]:
        return self._census.unresolved_publications(census_id)

    def _freeze_source_window(
        self,
        *,
        census_id: str,
        fingerprint: str,
        fence_token: str,
        attempt_deadline: datetime,
        selected_kinds: tuple[StandaloneCrmKind, ...],
    ) -> None:
        self._census.start_freezing(census_id=census_id, fingerprint=fingerprint)
        probe = getattr(self._authority, "probe_upper_id", None)
        if probe is None:
            self._census.finalize(
                census_id=census_id,
                fingerprint=fingerprint,
                terminal_state=ParentState.FREEZE_FAILED,
                reason="source_probe_authority_missing",
                allow_paused=False,
            )
            return
        bounds: dict[str, int] = {}
        for kind in sorted(selected_kinds):
            intent_id = uuid.uuid4().hex
            reserved = False
            try:
                reserved = self._census.reserve_http_call(
                    census_id=census_id,
                    fingerprint=fingerprint,
                    fence_token=fence_token,
                    intent_id=intent_id,
                    call_kind=HttpCallKind.PROBE.value,
                    unit_kind=kind,
                    frozen_upper_id=None,
                    cursor=None,
                    retry_ordinal=1,
                    deadline=attempt_deadline,
                )
                if not reserved:
                    raise CensusRepositoryError("existing HTTP intent cannot authorize I/O")
                bound = cast(CensusProbeClient, self._authority).probe_upper_id(kind)
                if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
                    raise CensusRepositoryError("upper-bound probe returned invalid data")
                bounds[kind] = bound
                self._census.record_http_outcome(
                    census_id=census_id,
                    intent_id=intent_id,
                    outcome=HttpCallState.SUCCEEDED,
                    outcome_detail="probe",
                )
            except Exception:
                if reserved:
                    self._census.record_http_outcome(
                        census_id=census_id,
                        intent_id=intent_id,
                        outcome=HttpCallState.FAILED,
                        outcome_detail="probe_failed",
                    )
                self._census.finalize(
                    census_id=census_id,
                    fingerprint=fingerprint,
                    terminal_state=ParentState.FREEZE_FAILED,
                    reason="probe_failed",
                    allow_paused=False,
                )
                return
        self._census.commit_source_window(
            census_id=census_id,
            fingerprint=fingerprint,
            selected_kinds=sorted(bounds),
            bounds_json=json.dumps(bounds, sort_keys=True, separators=(",", ":")),
        )
        self._census.allocate_source_units(
            census_id=census_id,
            fingerprint=fingerprint,
            units=[
                {
                    "unit_kind": kind,
                    "frozen_upper_id": bound,
                    "revision_id": "",
                    "state": "completed" if bound == 0 else "pending_publication",
                    "fence_token": fence_token,
                    "expected_rows": bound,
                }
                for kind, bound in sorted(bounds.items())
            ],
        )
