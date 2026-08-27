"""Default-off runtime for the bounded standalone CRM census parent."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.graph.standalone_crm_census_types import StandaloneCrmCensusAdmission
from src.standalone_crm_census_models import FrozenSourceWindow, StandaloneCrmAttempt
from src.standalone_crm_census_publication_runtime import (
    ChildPublisher,
    PublicationRunOutcome,
    publish_sources,
    repair_publication,
    run_mapping_only,
)
from src.standalone_crm_census_requests import (
    MappingPrepareCensusRequest,
    MappingRollbackCensusRequest,
    SourceSyncAuthoritySnapshot,
    SourceSyncCensusRequest,
    StandaloneCrmCensusRequest,
)
from src.standalone_crm_census_runtime_state import (
    attempt_from_status as _attempt_from_status,
)
from src.standalone_crm_census_runtime_state import (
    frozen_window,
)
from src.standalone_crm_census_runtime_state import (
    int_field as _int_field,
)
from src.standalone_crm_http_calls import StandaloneCrmHttpReservationAdapter


class StandaloneCrmAuthorityUnavailableError(RuntimeError):
    """The #275 mapping/projection authority is intentionally unavailable."""


class StandaloneCrmAuthorityReader(Protocol):
    """Narrow #275 boundary; production supplies no implementation before #275."""

    def source_sync_heads(
        self, request: SourceSyncCensusRequest
    ) -> SourceSyncAuthoritySnapshot: ...

    def validate_mapping_prepare(self, request: MappingPrepareCensusRequest) -> None: ...

    def validate_mapping_rollback(self, request: MappingRollbackCensusRequest) -> None: ...


class BitrixControlAdmission(Protocol):
    """Actual #272 source/control admission before a census/client/source I/O."""

    def admit(self, *, control_instance_id: str, source_instance_id: str) -> None: ...


class CensusProbeClient(Protocol):
    def probe_crm_contact_upper_id(self) -> int: ...

    def probe_crm_lead_upper_id(self) -> int: ...

    def probe_crm_company_upper_id(self) -> int: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class StandaloneCrmRunResult:
    census_id: str
    generation: int
    state: str
    frozen: bool
    published_children: int
    no_work_children: int


class StandaloneCrmCensusRuntime:
    """Coordinates durable control-only transitions and never writes CRM domain facts."""

    def __init__(
        self,
        *,
        repository: StandaloneCrmCensusRepository,
        admission: BitrixControlAdmission,
        authority: StandaloneCrmAuthorityReader,
        publisher: ChildPublisher,
        probe_client_factory: Callable[[StandaloneCrmHttpReservationAdapter], CensusProbeClient],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._admission = admission
        self._authority = authority
        self._publisher = publisher
        self._probe_client_factory = probe_client_factory
        self._clock = clock

    def start_or_recover(
        self, request: StandaloneCrmCensusRequest, *, task_id: str
    ) -> StandaloneCrmRunResult:
        authority = self._capture_or_validate_authority(request)
        self._admission.admit(
            control_instance_id=request.control_instance_id,
            source_instance_id=request.source_instance_id,
        )
        admitted = self._repository.admit(request, authority=authority)
        attempt = self._repository.claim_attempt(admitted, request, task_id=task_id)
        return self._run(admitted, attempt, request, authority)

    def resume(self, census_id: str, *, task_id: str) -> StandaloneCrmRunResult:
        admitted, request, authority = self._repository.load_admitted_request(census_id)
        self._revalidate(request, authority)
        attempt = self._repository.continue_attempt(admitted, request, task_id=task_id)
        return self._run(admitted, attempt, request, authority)

    def cancel(self, census_id: str, *, actor: str, reason: str) -> int:
        admission, request, authority = self._repository.load_admitted_request(census_id)
        self._revalidate(request, authority)
        return self._repository.request_cancel(admission, actor=actor, reason=reason)

    def repair_publication(self, publication_id: str) -> None:
        try:
            repair_publication(self._repository, self._publisher, publication_id, self._revalidate)
        except RuntimeError as exc:
            raise StandaloneCrmAuthorityUnavailableError(str(exc)) from exc

    def reconcile(self, census_id: str) -> tuple[str, int]:
        admitted, request, authority = self._repository.load_admitted_request(census_id)
        self._revalidate(request, authority)
        status = self._repository.status(census_id)
        if status is None or not status.attempts:
            raise StandaloneCrmAuthorityUnavailableError("census has no durable attempt")
        current = max(status.attempts, key=lambda item: _int_field(item, "generation"))
        attempt = _attempt_from_status(current)
        state, accounting = self._repository.reconcile_terminal(admitted, attempt)
        return state, accounting.expected_units

    def revalidate_admitted(
        self, request: StandaloneCrmCensusRequest, authority: SourceSyncAuthoritySnapshot | None
    ) -> None:
        self._revalidate(request, authority)

    def _run(
        self,
        admitted: StandaloneCrmCensusAdmission,
        attempt: StandaloneCrmAttempt,
        request: StandaloneCrmCensusRequest,
        authority: SourceSyncAuthoritySnapshot | None,
    ) -> StandaloneCrmRunResult:
        if isinstance(request, SourceSyncCensusRequest):
            return self._run_source_sync(admitted, attempt, request, authority)
        return self._mapping_result(
            admitted,
            attempt,
            run_mapping_only(
                self._repository, self._publisher, admitted, attempt, request, self._revalidate
            ),
        )

    def _capture_or_validate_authority(
        self, request: StandaloneCrmCensusRequest
    ) -> SourceSyncAuthoritySnapshot | None:
        if isinstance(request, SourceSyncCensusRequest):
            return self._authority.source_sync_heads(request)
        if isinstance(request, MappingPrepareCensusRequest):
            self._authority.validate_mapping_prepare(request)
        else:
            self._authority.validate_mapping_rollback(request)
        return None

    def _revalidate(
        self, request: StandaloneCrmCensusRequest, authority: SourceSyncAuthoritySnapshot | None
    ) -> None:
        if self._capture_or_validate_authority(request) != authority:
            raise StandaloneCrmAuthorityUnavailableError(
                "standalone CRM authority changed after admission"
            )
        self._admission.admit(
            control_instance_id=request.control_instance_id,
            source_instance_id=request.source_instance_id,
        )

    def _run_source_sync(
        self,
        admitted: StandaloneCrmCensusAdmission,
        attempt: StandaloneCrmAttempt,
        request: SourceSyncCensusRequest,
        authority: SourceSyncAuthoritySnapshot | None,
    ) -> StandaloneCrmRunResult:
        status = self._repository.status(admitted.census_id)
        window = frozen_window(status.census if status is not None else {})
        if window is None:
            window = self._freeze_source_window(admitted, attempt, request, authority)
        no_work = sum(upper == 0 for _kind, upper in window.upper_bounds)
        outcome = publish_sources(
            self._repository,
            self._publisher,
            admitted,
            attempt,
            request,
            authority,
            window,
            self._repository.status(admitted.census_id),
            self._revalidate,
        )
        if outcome.state == "paused_with_checkpoint":
            return self._source_result(admitted, attempt, outcome, no_work)
        if outcome.published_children == 0 and no_work == len(window.selected_kinds):
            self._revalidate(request, authority)
            state, _accounting = self._repository.reconcile_terminal(admitted, attempt)
            outcome = PublicationRunOutcome(state, 0, no_work)
        return self._source_result(admitted, attempt, outcome, no_work)

    def _freeze_source_window(
        self,
        admitted: StandaloneCrmCensusAdmission,
        attempt: StandaloneCrmAttempt,
        request: SourceSyncCensusRequest,
        authority: SourceSyncAuthoritySnapshot | None,
    ) -> FrozenSourceWindow:
        adapter = StandaloneCrmHttpReservationAdapter(
            repository=self._repository,
            attempt=attempt,
            freshness=admitted.freshness,
            max_calls_per_attempt=request.budget.max_calls_per_attempt,
            max_calls_per_occurrence=request.budget.max_calls_per_occurrence,
        )
        client: CensusProbeClient | None = None
        try:
            self._revalidate(request, authority)
            client = self._probe_client_factory(adapter)
            bounds = self._probe_selected(client, request)
            self._revalidate(request, authority)
            window = FrozenSourceWindow(
                request.selected_kinds,
                tuple((kind, bounds[kind]) for kind in request.selected_kinds),
            )
            self._repository.freeze_source_window(admitted, attempt, window)
            return window
        except Exception:
            self._repository.freeze_failed(admitted, attempt, reason="freeze_incomplete")
            raise
        finally:
            if client is not None:
                client.close()

    @staticmethod
    def _probe_selected(
        client: CensusProbeClient, request: SourceSyncCensusRequest
    ) -> dict[str, int]:
        probes: Mapping[str, Callable[[], int]] = {
            "contact": client.probe_crm_contact_upper_id,
            "lead": client.probe_crm_lead_upper_id,
            "company": client.probe_crm_company_upper_id,
        }
        bounds: dict[str, int] = {}
        for kind in request.selected_kinds:
            bound = probes[kind]()
            if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
                raise RuntimeError("CRM source probe returned an invalid upper bound")
            bounds[kind] = bound
        return bounds

    @staticmethod
    def _source_result(
        admitted: StandaloneCrmCensusAdmission,
        attempt: StandaloneCrmAttempt,
        outcome: PublicationRunOutcome,
        no_work: int,
    ) -> StandaloneCrmRunResult:
        return StandaloneCrmRunResult(
            admitted.census_id,
            attempt.generation,
            outcome.state,
            True,
            outcome.published_children,
            no_work,
        )

    @staticmethod
    def _mapping_result(
        admitted: StandaloneCrmCensusAdmission,
        attempt: StandaloneCrmAttempt,
        outcome: PublicationRunOutcome,
    ) -> StandaloneCrmRunResult:
        return StandaloneCrmRunResult(
            admitted.census_id,
            attempt.generation,
            outcome.state,
            True,
            outcome.published_children,
            outcome.no_work_children,
        )
