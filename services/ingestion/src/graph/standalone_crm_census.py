"""Neo4j repository for durable standalone CRM census control state."""

from __future__ import annotations

import json
import math
from typing import Literal

from neo4j import ManagedTransaction, Record

from src.graph.client import Neo4jClient
from src.graph.queries import standalone_crm_census as queries
from src.graph.standalone_crm_census_checkpoint_ops import (
    StandaloneCrmCheckpointOperations,
)
from src.graph.standalone_crm_census_child_ops import StandaloneCrmChildOperations
from src.graph.standalone_crm_census_core_ops import (
    StandaloneCrmCensusCoreOperations,
)
from src.graph.standalone_crm_census_core_ops import attempt_from_record as _attempt_from_record
from src.graph.standalone_crm_census_core_ops import (
    census_json as _json,
)
from src.graph.standalone_crm_census_core_ops import (
    freshness_guard as _guard,
)
from src.graph.standalone_crm_census_core_ops import (
    record_mapping as _mapping,
)
from src.graph.standalone_crm_census_core_ops import (
    record_mappings as _mappings,
)
from src.graph.standalone_crm_census_core_ops import (
    record_non_negative as _non_negative,
)
from src.graph.standalone_crm_census_core_ops import (
    record_text as _text,
)
from src.graph.standalone_crm_census_core_ops import (
    value_text as _value_text,
)
from src.graph.standalone_crm_census_types import (
    StandaloneCrmCensusAdmission,
    StandaloneCrmCensusStaleError,
    StandaloneCrmCensusStatus,
)
from src.standalone_crm_census_models import (
    FrozenSourceWindow,
    StandaloneCrmAttempt,
    StandaloneCrmTerminalAccounting,
)
from src.standalone_crm_census_requests import (
    SourceSyncAuthoritySnapshot,
    StandaloneCrmCensusRequest,
    request_from_persisted_json,
)


class StandaloneCrmCensusRepository(
    StandaloneCrmCensusCoreOperations,
    StandaloneCrmChildOperations,
    StandaloneCrmCheckpointOperations,
):
    """Generation/fence-scoped control repository. It contains no source-domain writes."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def freeze_source_window(
        self,
        admission: StandaloneCrmCensusAdmission,
        attempt: StandaloneCrmAttempt,
        window: FrozenSourceWindow,
    ) -> int:
        bounds = [{"unit_kind": kind, "upper_id": upper} for kind, upper in window.upper_bounds]
        record = self._require_mutation(
            queries.FREEZE_SOURCE_WINDOW,
            _guard(admission)
            | {
                "fingerprint": admission.fingerprint,
                "generation": attempt.generation,
                "parent_fence_token": attempt.parent_fence_token,
                "selected_kinds": list(window.selected_kinds),
                "bounds": bounds,
                "selection_size": len(window.selected_kinds),
                "window_json": _json(window),
            },
            "source window freeze rejected",
        )
        return _non_negative(record, "allocated_units")

    def freeze_no_source_window(
        self,
        admission: StandaloneCrmCensusAdmission,
        attempt: StandaloneCrmAttempt,
        *,
        unit_kind: Literal["mapping_prepare", "mapping_rollback"],
        revision_id: str,
    ) -> None:
        self._require_mutation(
            queries.FREEZE_NO_SOURCE_WINDOW,
            _guard(admission)
            | {
                "fingerprint": admission.fingerprint,
                "generation": attempt.generation,
                "parent_fence_token": attempt.parent_fence_token,
                "unit_kind": unit_kind,
                "revision_id": revision_id,
            },
            "no-source window freeze rejected",
        )

    def request_cancel(
        self, admission: StandaloneCrmCensusAdmission, *, actor: str, reason: str
    ) -> int:
        params = _guard(admission) | {"actor": actor[:200], "reason": reason[:1000]}

        def work(tx: ManagedTransaction) -> int:
            record = tx.run(queries.REQUEST_CANCELLATION, **params).single()  # type: ignore[arg-type]
            if record is None:
                raise StandaloneCrmCensusStaleError("cancellation rejected")
            if record["freeze_failed"] is True:
                released = tx.run(queries.RELEASE_PRE_WINDOW_SCOPE, **params).single()  # type: ignore[arg-type]
                if released is None:
                    raise StandaloneCrmCensusStaleError("pre-window scope release rejected")
            return _non_negative(record, "child_count")

        return self._client.execute_write(work)

    def pause(
        self, admission: StandaloneCrmCensusAdmission, attempt: StandaloneCrmAttempt, *, reason: str
    ) -> None:
        self._require_mutation(
            queries.PAUSE_ATTEMPT,
            _guard(admission)
            | {
                "generation": attempt.generation,
                "parent_fence_token": attempt.parent_fence_token,
                "reason": reason[:200],
            },
            "attempt pause rejected",
        )

    def continue_attempt(
        self,
        admission: StandaloneCrmCensusAdmission,
        request: StandaloneCrmCensusRequest,
        *,
        task_id: str,
        lease_seconds: int = 300,
    ) -> StandaloneCrmAttempt:
        """Atomically redeliver a running parent or advance one paused generation."""
        if not task_id.strip() or lease_seconds < 1:
            raise ValueError("task_id and lease_seconds must be positive")
        effective_lease_seconds = max(
            lease_seconds, math.ceil(request.budget.max_runtime_seconds_per_attempt)
        )
        params = _guard(admission) | {
            "fingerprint": admission.fingerprint,
            "task_id": task_id,
            "lease_seconds": effective_lease_seconds,
            "attempt_runtime_seconds": request.budget.max_runtime_seconds_per_attempt,
            "max_attempts": request.budget.max_attempts_per_occurrence,
            "max_calls_per_occurrence": request.budget.max_calls_per_occurrence,
            "max_rows_per_occurrence": request.budget.max_rows_per_occurrence,
        }

        def work(tx: ManagedTransaction) -> StandaloneCrmAttempt | None:
            record = tx.run(queries.CONTINUE_ATTEMPT, **params).single()  # type: ignore[arg-type]
            return None if record is None else _attempt_from_record(record)

        attempt = self._client.execute_write(work)
        if attempt is None:
            self._fail_if_exhausted(admission, request, "continuation_budget_exhausted")
            raise StandaloneCrmCensusStaleError("continuation rejected")
        return attempt

    def mark_authority_stale(self, admission: StandaloneCrmCensusAdmission) -> None:
        """Terminally block fresh external work while retaining settlement authority."""
        self._require_mutation(
            queries.MARK_CENSUS_AUTHORITY_STALE,
            _guard(admission) | {"fingerprint": admission.fingerprint},
            "authority-stale census update rejected",
        )

    def freeze_failed(
        self, admission: StandaloneCrmCensusAdmission, attempt: StandaloneCrmAttempt, *, reason: str
    ) -> None:
        self._require_mutation(
            queries.MARK_CENSUS_FREEZE_FAILED,
            _guard(admission)
            | {
                "generation": attempt.generation,
                "parent_fence_token": attempt.parent_fence_token,
                "reason": reason[:200],
            },
            "freeze failure rejected",
        )

    def reconcile_terminal(
        self, admission: StandaloneCrmCensusAdmission, attempt: StandaloneCrmAttempt
    ) -> tuple[str, StandaloneCrmTerminalAccounting]:
        record = self._require_mutation(
            queries.TERMINALIZE_CENSUS,
            _guard(admission)
            | {"generation": attempt.generation, "parent_fence_token": attempt.parent_fence_token},
            "terminal reconciliation rejected",
        )
        return _text(record, "terminal_state"), StandaloneCrmTerminalAccounting(
            _non_negative(record, "expected_units"),
            _non_negative(record, "processed"),
            _non_negative(record, "skipped"),
            _non_negative(record, "failed"),
            _non_negative(record, "no_work"),
        )

    def load_admitted_request(
        self, census_id: str
    ) -> tuple[
        StandaloneCrmCensusAdmission, StandaloneCrmCensusRequest, SourceSyncAuthoritySnapshot | None
    ]:
        """Load the immutable admission snapshot for an internal resume only."""
        status = self.status(census_id)
        if status is None:
            raise StandaloneCrmCensusStaleError("census is missing")
        census = status.census
        admission = self.admission_for_census(census_id)
        request_json = _value_text(census.get("request_json"), "request_json")
        request = request_from_persisted_json(request_json)
        authority_json = _value_text(census.get("authority_json"), "authority_json")
        raw_authority = json.loads(authority_json)
        authority: SourceSyncAuthoritySnapshot | None
        if raw_authority is None:
            authority = None
        elif isinstance(raw_authority, dict):
            mapping_id = raw_authority.get("mapping_head_id")
            mapping_digest = raw_authority.get("mapping_head_digest")
            projection_id = raw_authority.get("projection_head_id")
            if not isinstance(mapping_id, str) or not isinstance(mapping_digest, str):
                raise RuntimeError("persisted source authority is invalid")
            if projection_id is not None and not isinstance(projection_id, str):
                raise RuntimeError("persisted source authority is invalid")
            authority = SourceSyncAuthoritySnapshot(mapping_id, mapping_digest, projection_id)
        else:
            raise RuntimeError("persisted source authority is invalid")
        return admission, request, authority

    def admission_for_census(self, census_id: str) -> StandaloneCrmCensusAdmission:
        """Read a persisted freshness guard for an operator-initiated mutation."""
        status = self.status(census_id)
        if status is None:
            raise StandaloneCrmCensusStaleError("census is missing")
        census = status.census
        return StandaloneCrmCensusAdmission(
            census_id,
            _value_text(census.get("state"), "state"),
            _value_text(census.get("fingerprint"), "fingerprint"),
            _value_text(census.get("authority_digest"), "authority_digest"),
            _value_text(census.get("source_instance_id"), "source_instance_id"),
            _value_text(census.get("control_instance_id"), "control_instance_id"),
            False,
        )

    def status(self, census_id: str) -> StandaloneCrmCensusStatus | None:
        def work(tx: ManagedTransaction) -> StandaloneCrmCensusStatus | None:
            record = tx.run(queries.GET_CENSUS_STATUS, census_id=census_id).single()
            if record is None:
                return None
            return StandaloneCrmCensusStatus(
                _mapping(record, "census"),
                _mappings(record, "attempts"),
                _mappings(record, "units"),
                _mappings(record, "publications"),
                _mappings(record, "fences"),
            )

        return self._client.execute_read(work)

    def _require_mutation(self, query: str, params: dict[str, object], message: str) -> Record:
        def work(tx: ManagedTransaction) -> Record:
            record = tx.run(query, **params).single()  # type: ignore[arg-type]
            if record is None:
                raise StandaloneCrmCensusStaleError(message)
            return record

        return self._client.execute_write(work)
