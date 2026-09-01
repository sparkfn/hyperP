"""Fail-closed parent orchestration for the standalone CRM census control plane."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.graph.standalone_crm_census_records import StandaloneCrmRuntimeSnapshot
from src.ingestion_config import BitrixOpenLinesConfig
from src.standalone_crm_census_authority import StandaloneCrmCensusAuthority
from src.standalone_crm_census_models import (
    MappingPrepareCensusRequest,
    MappingRollbackCensusRequest,
    NoSourceWindow,
    SourceSyncCensusRequest,
    SourceWindow,
    StandaloneCrmCensusAuthorityError,
    StandaloneCrmCensusConflictError,
    StandaloneCrmCensusRequest,
    StandaloneCrmCensusUnit,
    StandaloneCrmChildEnvelope,
    StandaloneCrmPublication,
    StandaloneCrmReason,
    StandaloneCrmStreamKind,
    StandaloneCrmTerminalState,
)
from src.standalone_crm_census_requests import mapping_work_identity

SOURCE_CHILD_TASK_NAME = "src.standalone_crm_census_tasks.run_standalone_crm_census_unit"
MAPPING_CHILD_TASK_NAME = "src.standalone_crm_census_tasks.run_standalone_crm_mapping_activation"


class StandaloneCrmCensusProbe(Protocol):
    """Read-only source capability. Each probe is independently reserved by its client hook."""

    def upper_bound(self, stream_kind: StandaloneCrmStreamKind) -> int: ...


@runtime_checkable
class StandaloneCrmCensusClosableProbe(StandaloneCrmCensusProbe, Protocol):
    def close(self) -> None: ...


class StandaloneCrmCensusProbeFactory(Protocol):
    def create(
        self,
        repository: StandaloneCrmCensusRepository,
        request: SourceSyncCensusRequest,
        census_id: str,
        generation: int,
        fence_token: int,
        child_task_id: str | None = None,
    ) -> StandaloneCrmCensusProbe: ...


class StandaloneCrmChildPublisher(Protocol):
    def has_handler(self, task_name: str) -> bool: ...

    def publish(self, task_name: str, task_id: str, queue: str, payload_json: str) -> None: ...


@dataclass(frozen=True)
class StandaloneCrmRuntimeResult:
    census_id: str
    state: str
    generation: int
    detail: str


class StandaloneCrmCensusRuntime:
    """Coordinates durable transitions only; source and broker effects are injected."""

    def __init__(
        self,
        repository: StandaloneCrmCensusRepository,
        authority: StandaloneCrmCensusAuthority,
        config: BitrixOpenLinesConfig,
        *,
        probe: StandaloneCrmCensusProbe | None = None,
        probe_factory: StandaloneCrmCensusProbeFactory | None = None,
        publisher: StandaloneCrmChildPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._authority = authority
        self._config = config
        self._probe = probe
        self._probe_factory = probe_factory
        self._publisher = publisher

    def run_parent(
        self, census_id: str, request: StandaloneCrmCensusRequest
    ) -> StandaloneCrmRuntimeResult:
        self._require_enabled()
        snapshot = self._repository.runtime_snapshot(census_id)
        if snapshot is None:
            return StandaloneCrmRuntimeResult(census_id, "missing", 0, "census not found")
        try:
            self._revalidate(request)
        except StandaloneCrmCensusAuthorityError:
            return self._converge_authority_failure(census_id, snapshot)
        if snapshot.cancel_requested:
            if snapshot.window_frozen:
                return self.settle_cancellation(census_id, request)
            return self._freeze_failed(
                census_id, snapshot.generation, request, "cancelled before window"
            )
        generation = snapshot.generation + 1
        fence_token = generation
        self._revalidate(request)
        if not self._repository.claim_attempt(census_id, generation, fence_token, request):
            exhausted = self._converge_limit_denial(census_id, snapshot, request)
            if exhausted is not None:
                return exhausted
            return StandaloneCrmRuntimeResult(
                census_id, snapshot.state, snapshot.generation, "claim denied"
            )
        if isinstance(request, SourceSyncCensusRequest):
            return self._freeze_source(census_id, request, generation, fence_token)
        return self._freeze_mapping(census_id, request, generation)

    def reconcile(self, census_id: str) -> StandaloneCrmRuntimeResult:
        snapshot = self._repository.runtime_snapshot(census_id)
        if snapshot is None:
            return StandaloneCrmRuntimeResult(census_id, "missing", 0, "census not found")
        if not isinstance(snapshot.request, SourceSyncCensusRequest):
            try:
                receipt = self._repository.find_mapping_receipt(census_id)
                if receipt is not None:
                    receipt_result = self._settle_mapping_receipt(census_id, snapshot, receipt)
                    if receipt_result is not None:
                        return receipt_result
            except (json.JSONDecodeError, StandaloneCrmCensusConflictError, ValueError):
                return StandaloneCrmRuntimeResult(
                    census_id,
                    "paused_with_checkpoint",
                    snapshot.generation,
                    "activation receipt conflicts with persisted mapping work",
                )
        self._require_enabled()
        try:
            self._revalidate(snapshot.request)
        except StandaloneCrmCensusAuthorityError:
            return self._converge_authority_failure(census_id, snapshot)
        takeover = self._repository.recover_or_take_over_attempt(
            census_id,
            snapshot.generation,
            snapshot.generation + 1,
            snapshot.generation + 1,
        )
        if takeover is not None:
            if snapshot.window_frozen:
                units = self._repository.resumable_units(census_id, takeover.generation)
                if units:
                    return self._allocate_and_publish(
                        census_id, snapshot.request, takeover.generation, units
                    )
                return self.repair_publications(census_id)
            if isinstance(snapshot.request, SourceSyncCensusRequest):
                return self._freeze_source(
                    census_id,
                    snapshot.request,
                    takeover.generation,
                    takeover.fence_token,
                )
            return self._freeze_mapping(census_id, snapshot.request, takeover.generation)
        classified = self._repository.classify_unresolved_calls(census_id)
        if snapshot.cancel_requested:
            return self.settle_cancellation(census_id, snapshot.request)
        exhausted = self._converge_limit_denial(census_id, snapshot, snapshot.request)
        if exhausted is not None:
            return exhausted
        repaired = self.repair_publications(census_id)
        if repaired.state == "paused_with_checkpoint":
            return repaired
        current = self._repository.runtime_snapshot(census_id)
        if current is None:
            return StandaloneCrmRuntimeResult(census_id, "missing", 0, "census disappeared")
        if current.cancel_requested:
            return self.settle_cancellation(census_id, current.request)
        if current.state == "paused_with_checkpoint":
            return self.continue_after_pause(census_id)
        if current.window_frozen:
            settled = self._repository.settle_attempt(census_id, current.generation)
            if settled:
                terminal = self._terminalize(
                    census_id,
                    current.generation,
                    current.request,
                    "completed",
                    f"reconciled classified={classified}",
                )
                if terminal.state == "completed":
                    return terminal
                failed = self._terminalize(
                    census_id,
                    current.generation,
                    current.request,
                    "failed",
                    "reconciled terminal child failure",
                )
                if failed.state == "failed":
                    return failed
        return StandaloneCrmRuntimeResult(
            census_id,
            current.state,
            current.generation,
            f"classified={classified}; {repaired.detail}",
        )

    def _settle_mapping_receipt(
        self,
        census_id: str,
        snapshot: StandaloneCrmRuntimeSnapshot,
        receipt: dict[str, object],
    ) -> StandaloneCrmRuntimeResult | None:
        from src.standalone_crm_mapping_child import parse_mapping_publication

        payload = receipt.get("payload_json")
        release_id = receipt.get("release_id")
        activated_at = receipt.get("activated_at")
        if not all(isinstance(value, str) for value in (payload, release_id, activated_at)):
            raise StandaloneCrmCensusConflictError("mapping activation receipt is malformed")
        assert isinstance(payload, str)
        assert isinstance(release_id, str)
        assert isinstance(activated_at, str)
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise StandaloneCrmCensusConflictError("mapping activation payload is malformed")
        payload_json, envelope = parse_mapping_publication(decoded)
        if not self._repository.reconcile_mapping_receipt(
            envelope,
            payload_json=payload_json,
            release_id=release_id,
            activated_at=activated_at,
        ):
            return None
        return StandaloneCrmRuntimeResult(
            census_id,
            "completed",
            snapshot.generation,
            "activation receipt settled",
        )

    def repair_publications(self, census_id: str) -> StandaloneCrmRuntimeResult:
        snapshot = self._repository.runtime_snapshot(census_id)
        if snapshot is None:
            return StandaloneCrmRuntimeResult(census_id, "missing", 0, "census not found")
        self._require_enabled()
        self._revalidate(snapshot.request)
        repairs = self._repository.repair_publications(census_id)
        if not repairs:
            return StandaloneCrmRuntimeResult(
                census_id, snapshot.state, snapshot.generation, "no unsettled publications"
            )
        if self._publisher is None:
            return self._pause(
                census_id,
                snapshot.generation,
                snapshot.request,
                "child_handler_unavailable",
                "publisher unavailable for repair",
            )
        for repair in repairs:
            if repair.state == "published":
                continue
            if not self._publisher.has_handler(repair.task_name):
                return self._pause(
                    census_id,
                    snapshot.generation,
                    snapshot.request,
                    "handler_missing",
                    "child handler unavailable for repair",
                )
            publication = StandaloneCrmPublication(
                census_id,
                repair.generation,
                repair.stream_kind,
                repair.task_id,
                repair.payload_digest,
                "pending",
            )
            payload_json = self._repository.mark_publication_publishing(publication)
            if payload_json is None or payload_json != repair.payload_json:
                return self._pause(
                    census_id,
                    snapshot.generation,
                    snapshot.request,
                    "publication_conflict",
                    "stored publication payload conflicts",
                )
            try:
                self._publisher.publish(
                    repair.task_name, repair.task_id, repair.queue, payload_json
                )
            except Exception:
                return StandaloneCrmRuntimeResult(
                    census_id,
                    "publishing",
                    snapshot.generation,
                    "broker repair remains recoverable",
                )
            try:
                confirmed = self._repository.confirm_publication(publication)
            except Exception:
                return StandaloneCrmRuntimeResult(
                    census_id, "publishing", snapshot.generation, "confirmation requires repair"
                )
            if not confirmed:
                return StandaloneCrmRuntimeResult(
                    census_id, "publishing", snapshot.generation, "confirmation requires repair"
                )
        return StandaloneCrmRuntimeResult(
            census_id, "running", snapshot.generation, f"repaired={len(repairs)}"
        )

    def continue_after_pause(self, census_id: str) -> StandaloneCrmRuntimeResult:
        snapshot = self._repository.runtime_snapshot(census_id)
        if snapshot is None:
            return StandaloneCrmRuntimeResult(census_id, "missing", 0, "census not found")
        self._require_enabled()
        self._revalidate(snapshot.request)
        if not snapshot.window_frozen:
            if not self._repository.resume(census_id):
                exhausted = self._converge_limit_denial(census_id, snapshot, snapshot.request)
                if exhausted is not None:
                    return exhausted
                return StandaloneCrmRuntimeResult(
                    census_id, snapshot.state, snapshot.generation, "pre-window resume rejected"
                )
            if isinstance(snapshot.request, SourceSyncCensusRequest):
                return self._freeze_source(
                    census_id,
                    snapshot.request,
                    snapshot.generation,
                    snapshot.generation,
                )
            return self._freeze_mapping(census_id, snapshot.request, snapshot.generation)
        generation = self._repository.create_continuation(
            census_id, snapshot.generation, snapshot.request
        )
        if generation is None:
            exhausted = self._converge_limit_denial(census_id, snapshot, snapshot.request)
            if exhausted is not None:
                return exhausted
            return StandaloneCrmRuntimeResult(
                census_id, snapshot.state, snapshot.generation, "continuation rejected"
            )
        units = self._repository.resumable_units(census_id, generation)
        if units:
            return self._allocate_and_publish(census_id, snapshot.request, generation, units)
        return self.repair_publications(census_id)

    def settle_cancellation(
        self, census_id: str, request: StandaloneCrmCensusRequest
    ) -> StandaloneCrmRuntimeResult:
        snapshot = self._repository.runtime_snapshot(census_id)
        if snapshot is None:
            return StandaloneCrmRuntimeResult(census_id, "missing", 0, "census not found")
        try:
            self._revalidate(request)
        except StandaloneCrmCensusAuthorityError:
            return self._converge_authority_failure(census_id, snapshot)
        if not snapshot.window_frozen:
            failed = self._repository.fail_freeze(
                census_id,
                snapshot.generation,
                StandaloneCrmReason("cancelled", "cancelled before window"),
            )
            return StandaloneCrmRuntimeResult(
                census_id,
                "freeze_failed" if failed else snapshot.state,
                snapshot.generation,
                "cancelled before freeze",
            )
        reason = StandaloneCrmReason("cancelled", "operator cancellation settled")
        state: StandaloneCrmTerminalState = "cancelled_with_checkpoint"
        if not self._repository.settle_cancellation(census_id, snapshot.generation):
            return StandaloneCrmRuntimeResult(
                census_id, snapshot.state, snapshot.generation, "cancellation settlement rejected"
            )
        terminalized = self._repository.terminalize(
            census_id, snapshot.generation, state, reason, self._authority_revision(request)
        )
        return StandaloneCrmRuntimeResult(
            census_id,
            state if terminalized else snapshot.state,
            snapshot.generation,
            "cancellation settlement",
        )

    def _freeze_source(
        self,
        census_id: str,
        request: SourceSyncCensusRequest,
        generation: int,
        fence_token: int,
    ) -> StandaloneCrmRuntimeResult:
        probe = self._probe
        owned_probe = False
        if probe is None and self._probe_factory is not None:
            self._revalidate(request)
            probe = self._probe_factory.create(
                self._repository, request, census_id, generation, fence_token
            )
            owned_probe = True
        if probe is None:
            return self._pause(
                census_id, generation, request, "child_handler_unavailable", "probe unavailable"
            )
        bounds: list[tuple[StandaloneCrmStreamKind, int]] = []
        try:
            for stream_kind in request.selected_kinds:
                self._revalidate(request)
                bounds.append((stream_kind, probe.upper_bound(stream_kind)))
        except (RuntimeError, ValueError, StandaloneCrmCensusAuthorityError) as exc:
            return self._freeze_failed(census_id, generation, request, str(exc))
        finally:
            if owned_probe and isinstance(probe, StandaloneCrmCensusClosableProbe):
                probe.close()
        window = SourceWindow(tuple(bounds))
        self._revalidate(request)
        if not self._repository.freeze_source_window(census_id, generation, window):
            return self._freeze_failed(census_id, generation, request, "atomic freeze rejected")
        units = tuple(
            StandaloneCrmCensusUnit(
                census_id,
                generation,
                kind,
                "no_work" if window.bound_for(kind) == 0 else "pending_publication",
                window.bound_for(kind),
                None,
            )
            for kind in request.selected_kinds
        )
        return self._allocate_and_publish(census_id, request, generation, units)

    def _freeze_mapping(
        self,
        census_id: str,
        request: MappingPrepareCensusRequest | MappingRollbackCensusRequest,
        generation: int,
    ) -> StandaloneCrmRuntimeResult:
        revision_id, revision_digest = mapping_work_identity(request.authority)
        self._revalidate(request)
        if not self._repository.freeze_no_source_window(
            census_id, generation, NoSourceWindow(revision_id, revision_digest)
        ):
            return self._freeze_failed(census_id, generation, request, "mapping freeze rejected")
        unit = StandaloneCrmCensusUnit(
            census_id,
            generation,
            request.selected_kinds[0],
            "pending_publication",
            None,
            revision_id,
        )
        return self._allocate_and_publish(census_id, request, generation, (unit,))

    def _allocate_and_publish(
        self,
        census_id: str,
        request: StandaloneCrmCensusRequest,
        generation: int,
        units: tuple[StandaloneCrmCensusUnit, ...],
    ) -> StandaloneCrmRuntimeResult:
        self._revalidate(request)
        if self._repository.allocate_units(census_id, generation, units) != len(units):
            return self._pause(
                census_id, generation, request, "recovery_required", "unit allocation conflict"
            )
        positive = tuple(unit for unit in units if unit.state != "no_work")
        if not positive:
            return self._terminalize(
                census_id, generation, request, "completed", "all selected units are zero"
            )
        if self._publisher is None:
            return self._pause(
                census_id, generation, request, "child_handler_unavailable", "publisher unavailable"
            )
        for unit in positive:
            task_name = (
                SOURCE_CHILD_TASK_NAME
                if isinstance(request, SourceSyncCensusRequest)
                else MAPPING_CHILD_TASK_NAME
            )
            if not self._publisher.has_handler(task_name):
                return self._pause(
                    census_id, generation, request, "handler_missing", "child handler unavailable"
                )
            envelope = StandaloneCrmChildEnvelope(
                census_id,
                generation,
                unit.stream_kind,
                unit.frozen_upper_id,
                unit.revision_id,
                task_name,
                uuid.uuid5(uuid.NAMESPACE_URL, f"{census_id}:{generation}:{unit.stream_kind}").hex,
                "ingestion",
            )
            self._revalidate(request)
            if not self._repository.reserve_child_envelope(envelope):
                return self._pause(
                    census_id, generation, request, "publication_conflict", "payload conflict"
                )
            publication = StandaloneCrmPublication(
                census_id,
                generation,
                unit.stream_kind,
                envelope.task_id,
                envelope.payload_digest(),
                "pending",
            )
            payload_json = self._repository.mark_publication_publishing(publication)
            if payload_json is None:
                return self._pause(
                    census_id,
                    generation,
                    request,
                    "publication_conflict",
                    "publishing claim rejected",
                )
            try:
                self._publisher.publish(
                    envelope.task_name, envelope.task_id, envelope.queue, payload_json
                )
            except Exception:
                return StandaloneCrmRuntimeResult(
                    census_id, "publishing", generation, "broker publication requires repair"
                )
            self._revalidate(request)
            try:
                confirmed = self._repository.confirm_publication(publication)
            except Exception:
                return StandaloneCrmRuntimeResult(
                    census_id, "publishing", generation, "confirmation requires repair"
                )
            if not confirmed:
                return StandaloneCrmRuntimeResult(
                    census_id, "publishing", generation, "confirmation requires repair"
                )
        return StandaloneCrmRuntimeResult(census_id, "running", generation, "units published")

    def _terminalize(
        self,
        census_id: str,
        generation: int,
        request: StandaloneCrmCensusRequest,
        state: StandaloneCrmTerminalState,
        detail: str,
    ) -> StandaloneCrmRuntimeResult:
        self._revalidate(request)
        reason = StandaloneCrmReason(
            "completed" if state == "completed" else "freeze_incomplete", detail
        )
        terminalized = self._repository.terminalize(
            census_id, generation, state, reason, self._authority_revision(request)
        )
        return StandaloneCrmRuntimeResult(
            census_id, state if terminalized else "running", generation, detail
        )

    def _freeze_failed(
        self,
        census_id: str,
        generation: int,
        request: StandaloneCrmCensusRequest,
        detail: str,
    ) -> StandaloneCrmRuntimeResult:
        reason = StandaloneCrmReason("freeze_failed", detail)
        terminalized = self._repository.fail_freeze(census_id, generation, reason)
        return StandaloneCrmRuntimeResult(
            census_id, "freeze_failed" if terminalized else "running", generation, detail
        )

    def _converge_authority_failure(
        self, census_id: str, snapshot: StandaloneCrmRuntimeSnapshot
    ) -> StandaloneCrmRuntimeResult:
        if not snapshot.window_frozen:
            failed = self._repository.fail_freeze(
                census_id,
                snapshot.generation,
                StandaloneCrmReason("authority_stale", "authority changed before window"),
            )
            return StandaloneCrmRuntimeResult(
                census_id,
                "freeze_failed" if failed else "recovery_required",
                snapshot.generation,
                "authority changed before window",
            )
        reason = StandaloneCrmReason("authority_stale", "authority changed after window")
        persisted = self._repository.fail_after_window_authority(
            census_id, snapshot.generation, reason
        )
        return StandaloneCrmRuntimeResult(
            census_id,
            "failed" if persisted else "recovery_required",
            snapshot.generation,
            "authority changed after window"
            if persisted
            else "authority failure retained for reconciliation",
        )

    def _pause(
        self,
        census_id: str,
        generation: int,
        request: StandaloneCrmCensusRequest,
        code: str,
        detail: str,
    ) -> StandaloneCrmRuntimeResult:
        self._revalidate(request)
        persisted = self._repository.pause(census_id, generation, code, detail)
        return StandaloneCrmRuntimeResult(
            census_id,
            "paused_with_checkpoint" if persisted else "recovery_required",
            generation,
            detail,
        )

    def _converge_limit_denial(
        self,
        census_id: str,
        snapshot: StandaloneCrmRuntimeSnapshot,
        request: StandaloneCrmCensusRequest,
    ) -> StandaloneCrmRuntimeResult | None:
        reason_code = (
            "deadline_exhausted"
            if datetime.now(UTC)
            >= datetime.fromisoformat(request.budget.occurrence_deadline.replace("Z", "+00:00"))
            else "attempts_exhausted"
        )
        state = self._repository.converge_limit_denial(
            census_id, snapshot.generation, request, reason_code
        )
        if state is None:
            return None
        return StandaloneCrmRuntimeResult(
            census_id,
            state,
            snapshot.generation,
            "standalone census occurrence limit exhausted",
        )

    def _require_enabled(self) -> None:
        if not self._config.standalone_crm_identity_enabled:
            raise StandaloneCrmCensusAuthorityError("standalone CRM identity is disabled")

    def _revalidate(self, request: StandaloneCrmCensusRequest) -> None:
        self._authority.verify(request)
        if isinstance(request, SourceSyncCensusRequest):
            self._repository.require_active_source(request)

    @staticmethod
    def _authority_revision(request: StandaloneCrmCensusRequest) -> str:
        if isinstance(request, SourceSyncCensusRequest):
            return (
                request.authority.mapping_head_digest
                + ":"
                + request.authority.projection_head_digest
            )
        if isinstance(request, MappingPrepareCensusRequest):
            return request.authority.prepared_revision_digest
        return mapping_work_identity(request.authority)[1]
