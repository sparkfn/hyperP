"""Internal operator service for standalone CRM census state; no FastAPI/MCP surface."""

from __future__ import annotations

import uuid
from dataclasses import asdict

from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.standalone_crm_census_requests import StandaloneCrmCensusRequest
from src.standalone_crm_census_runtime import StandaloneCrmCensusRuntime, StandaloneCrmRunResult


class StandaloneCrmCensusControl:
    """Thin internal control facade; all mutations delegate to the durable repository/runtime."""

    def __init__(
        self, repository: StandaloneCrmCensusRepository, runtime: StandaloneCrmCensusRuntime
    ) -> None:
        self._repository = repository
        self._runtime = runtime

    def start(self, request: StandaloneCrmCensusRequest, *, task_id: str) -> StandaloneCrmRunResult:
        return self._runtime.start_or_recover(request, task_id=task_id)

    def status(self, census_id: str) -> dict[str, object] | None:
        status = self._repository.status(census_id)
        return None if status is None else asdict(status)

    def cancel(self, census_id: str, *, actor: str, reason: str) -> int:
        return self._runtime.cancel(census_id, actor=actor, reason=reason)

    def resume(self, census_id: str, *, task_id: str | None = None) -> StandaloneCrmRunResult:
        return self._runtime.resume(census_id, task_id=task_id or uuid.uuid4().hex)

    def reconcile(self, census_id: str) -> tuple[str, int]:
        return self._runtime.reconcile(census_id)

    def repair(self, publication_id: str) -> None:
        self._runtime.repair_publication(publication_id)

    def classify_reserved_call_unknown(self, census_id: str, *, intent_id: str) -> bool:
        """Classify a current durable reservation without accepting caller freshness fields."""
        admission, _request, _authority = self._repository.load_admitted_request(census_id)
        # This is a one-way settlement classification of an already-consumed
        # reservation, not new source work; it must remain operable for a
        # historical generation after source/control authority is disabled.
        return self._repository.classify_current_reserved_call_unknown(
            admission, intent_id=intent_id
        )
