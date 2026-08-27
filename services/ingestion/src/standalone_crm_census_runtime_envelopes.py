"""Immutable child-envelope construction for standalone CRM census runtime."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Literal, cast

from src.graph.standalone_crm_census_types import StandaloneCrmCensusAdmission
from src.standalone_crm_census_models import StandaloneCrmAttempt, StandaloneCrmChildEnvelope
from src.standalone_crm_census_requests import (
    MappingPrepareCensusRequest,
    MappingRollbackCensusRequest,
    SourceSyncCensusRequest,
)


def source_envelope(
    admitted: StandaloneCrmCensusAdmission,
    attempt: StandaloneCrmAttempt,
    request: SourceSyncCensusRequest,
    unit_kind: Literal["contact", "lead", "company"],
    upper_id: int,
    sequence: int,
) -> StandaloneCrmChildEnvelope:
    return _envelope(
        admitted,
        attempt,
        request.source_instance_id,
        request.control_instance_id,
        unit_kind,
        upper_id,
        None,
        sequence,
    )


def mapping_envelope(
    admitted: StandaloneCrmCensusAdmission,
    attempt: StandaloneCrmAttempt,
    request: MappingPrepareCensusRequest | MappingRollbackCensusRequest,
    revision_id: str,
) -> StandaloneCrmChildEnvelope:
    return _envelope(
        admitted,
        attempt,
        request.source_instance_id,
        request.control_instance_id,
        request.census_kind,
        None,
        revision_id,
        1,
    )


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _envelope(
    admitted: StandaloneCrmCensusAdmission,
    attempt: StandaloneCrmAttempt,
    source_instance_id: str,
    control_instance_id: str,
    unit_kind: str,
    upper_id: int | None,
    revision_id: str | None,
    sequence: int,
) -> StandaloneCrmChildEnvelope:
    task_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"hyperp:{admitted.census_id}:{attempt.generation}:{unit_kind}:{sequence}",
    ).hex
    digest = _digest(
        {
            "census_id": admitted.census_id,
            "generation": attempt.generation,
            "parent_fence_token": attempt.parent_fence_token,
            "unit_kind": unit_kind,
            "upper_id": upper_id,
            "revision_id": revision_id,
        }
    )
    return StandaloneCrmChildEnvelope(
        admitted.census_id,
        attempt.generation,
        attempt.parent_fence_token,
        _unit_kind(unit_kind),
        upper_id,
        revision_id,
        f"{admitted.census_id}:{unit_kind}:{sequence}",
        task_id,
        digest,
        source_instance_id,
        control_instance_id,
    )


def _unit_kind(
    value: str,
) -> Literal["contact", "lead", "company", "mapping_prepare", "mapping_rollback"]:
    if value not in {"contact", "lead", "company", "mapping_prepare", "mapping_rollback"}:
        raise ValueError("invalid standalone CRM unit kind")
    return cast(Literal["contact", "lead", "company", "mapping_prepare", "mapping_rollback"], value)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
