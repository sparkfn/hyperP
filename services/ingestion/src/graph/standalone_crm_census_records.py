"""Read models and shared helpers for the standalone CRM census repository."""

from __future__ import annotations

import json
from dataclasses import dataclass

from src.graph.client import Neo4jClient
from src.standalone_crm_census_models import (
    MappingPrepareCensusRequest,
    MappingRollbackCensusRequest,
    SourceSyncCensusRequest,
    StandaloneCrmCensusConflictError,
    StandaloneCrmCensusRequest,
    StandaloneCrmStreamKind,
)
from src.standalone_crm_census_requests import (
    canonical_authority_payload,
    mapping_work_identity,
)


@dataclass(frozen=True)
class StandaloneCrmCensusAdmission:
    census_id: str
    status: str
    replayed: bool


@dataclass(frozen=True)
class StandaloneCrmCensusStatus:
    census_id: str
    state: str
    generation: int
    cancel_requested: bool
    window_frozen: bool
    attempts: int


@dataclass(frozen=True)
class StandaloneCrmAttemptTakeover:
    generation: int
    fence_token: int


@dataclass(frozen=True)
class StandaloneCrmRuntimeSnapshot:
    request: StandaloneCrmCensusRequest
    generation: int
    state: str
    cancel_requested: bool
    window_frozen: bool = False
    window_json: str | None = None
    attempt_deadline: str | None = None


@dataclass(frozen=True)
class StandaloneCrmPublicationRepair:
    task_id: str
    state: str
    payload_json: str
    task_name: str
    queue: str
    payload_digest: str
    stream_kind: StandaloneCrmStreamKind
    generation: int


class _StandaloneCrmCensusRepositoryBase:
    _client: Neo4jClient

    def runtime_snapshot(self, census_id: str) -> StandaloneCrmRuntimeSnapshot | None:
        raise NotImplementedError


def authority_revision(request: StandaloneCrmCensusRequest) -> str:
    if isinstance(request, SourceSyncCensusRequest):
        return (
            request.authority.mapping_head_digest + ":" + request.authority.projection_head_digest
        )
    if isinstance(request, MappingPrepareCensusRequest):
        return request.authority.prepared_revision_digest
    if isinstance(request, MappingRollbackCensusRequest):
        # v1 rollback payloads identify the historical target; v2 records use
        # the newly prepared rollback candidate digest.
        return mapping_work_identity(request.authority)[1]
    raise AssertionError("unreachable standalone census request")


def authority_context(request: StandaloneCrmCensusRequest) -> str:
    """Canonical exact authority identity retained independently of its short revision."""
    return json.dumps(canonical_authority_payload(request), sort_keys=True, separators=(",", ":"))


def terminal_window_expectations(
    request: StandaloneCrmCensusRequest, window_json: str | None
) -> list[dict[str, str | int | None]]:
    """Return typed immutable unit identities; terminalization never trusts graph units."""
    if window_json is None:
        return []
    decoded = json.loads(window_json)
    if not isinstance(decoded, dict):
        raise StandaloneCrmCensusConflictError("stored census window is malformed")
    if isinstance(request, SourceSyncCensusRequest):
        bounds = decoded.get("selected_bounds")
        if not isinstance(bounds, list):
            raise StandaloneCrmCensusConflictError("stored source window is malformed")
        values: list[dict[str, str | int | None]] = []
        selected_stream_kinds: list[StandaloneCrmStreamKind] = []
        for item in bounds:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or isinstance(item[1], bool)
                or not isinstance(item[1], int)
            ):
                raise StandaloneCrmCensusConflictError("stored source window bound is malformed")
            values.append(
                {
                    "stream_kind": stream_kind(item[0]),
                    "frozen_upper_id": item[1],
                    "revision_id": None,
                }
            )
            selected_stream_kinds.append(stream_kind(item[0]))
        if tuple(selected_stream_kinds) != request.selected_kinds:
            raise StandaloneCrmCensusConflictError("stored source window selection conflicts")
        return values
    revision_id = decoded.get("revision_id")
    revision_digest = decoded.get("revision_digest")
    if not isinstance(revision_id, str) or not isinstance(revision_digest, str):
        raise StandaloneCrmCensusConflictError("stored mapping window is malformed")
    expected_revision, expected_digest = mapping_work_identity(request.authority)
    if revision_id != expected_revision or revision_digest != expected_digest:
        raise StandaloneCrmCensusConflictError("stored mapping window authority conflicts")
    return [
        {
            "stream_kind": request.selected_kinds[0],
            "frozen_upper_id": None,
            "revision_id": revision_id,
        }
    ]


def stream_kind(value: str) -> StandaloneCrmStreamKind:
    if value == "contact":
        return "contact"
    if value == "lead":
        return "lead"
    if value == "company":
        return "company"
    raise StandaloneCrmCensusConflictError("stored publication stream kind is invalid")
