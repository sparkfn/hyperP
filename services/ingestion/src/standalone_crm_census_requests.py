"""Immutable standalone CRM census request, authority, budget, and fingerprint contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

from src.standalone_crm_census_types import (
    _STREAM_KINDS,
    StandaloneCrmStreamKind,
    _integer,
    _text,
    _utc,
)

_CANONICAL_STREAM_ORDER: tuple[StandaloneCrmStreamKind, ...] = ("contact", "lead", "company")


@dataclass(frozen=True)
class StandaloneCrmBudget:
    max_calls_per_attempt: int
    max_rows_per_attempt: int
    max_runtime_seconds_per_attempt: int
    max_calls_per_occurrence: int
    max_rows_per_occurrence: int
    max_attempts_per_occurrence: int
    occurrence_deadline: str

    def __post_init__(self) -> None:
        for field in (
            "max_calls_per_attempt",
            "max_rows_per_attempt",
            "max_runtime_seconds_per_attempt",
            "max_calls_per_occurrence",
            "max_rows_per_occurrence",
            "max_attempts_per_occurrence",
        ):
            _integer(getattr(self, field), field, 1)
        if self.max_calls_per_attempt > self.max_calls_per_occurrence:
            raise ValueError("attempt calls cannot exceed occurrence calls")
        if self.max_rows_per_attempt > self.max_rows_per_occurrence:
            raise ValueError("attempt rows cannot exceed occurrence rows")
        object.__setattr__(
            self, "occurrence_deadline", _utc(self.occurrence_deadline, "occurrence_deadline")
        )


@dataclass(frozen=True)
class SourceSyncAuthority:
    mapping_head_id: str
    mapping_head_digest: str
    projection_head_id: str
    projection_head_digest: str

    def __post_init__(self) -> None:
        for field in (
            "mapping_head_id",
            "mapping_head_digest",
            "projection_head_id",
            "projection_head_digest",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))


@dataclass(frozen=True)
class MappingPrepareAuthority:
    prepared_revision_id: str
    prepared_revision_digest: str
    expected_current_head_id: str

    def __post_init__(self) -> None:
        for field in (
            "prepared_revision_id",
            "prepared_revision_digest",
            "expected_current_head_id",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))


@dataclass(frozen=True)
class MappingRollbackAuthority:
    target_revision_id: str
    target_revision_digest: str
    expected_current_head_id: str
    rollback_head_id: str

    def __post_init__(self) -> None:
        for field in (
            "target_revision_id",
            "target_revision_digest",
            "expected_current_head_id",
            "rollback_head_id",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))


@dataclass(frozen=True)
class _Request:
    source_key: str
    source_instance_id: str
    control_instance_id: str
    occurrence_key: str
    selected_kinds: tuple[StandaloneCrmStreamKind, ...]
    budget: StandaloneCrmBudget
    policy_version: str
    association_contract_version: str
    configuration_digest: str

    def _validate_common(self) -> None:
        for field in (
            "source_key",
            "source_instance_id",
            "control_instance_id",
            "occurrence_key",
            "policy_version",
            "association_contract_version",
            "configuration_digest",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if self.source_key != "bitrix_chat":
            raise ValueError("source_key must be bitrix_chat")
        selected = set(self.selected_kinds)
        normalized = tuple(kind for kind in _CANONICAL_STREAM_ORDER if kind in selected)
        if not normalized or any(kind not in _STREAM_KINDS for kind in normalized):
            raise ValueError("selected_kinds must be a non-empty CRM-kind subset")
        object.__setattr__(self, "selected_kinds", normalized)


@dataclass(frozen=True)
class SourceSyncCensusRequest(_Request):
    authority: SourceSyncAuthority
    census_kind: Literal["source_sync"] = "source_sync"

    def __post_init__(self) -> None:
        self._validate_common()


@dataclass(frozen=True)
class MappingPrepareCensusRequest(_Request):
    authority: MappingPrepareAuthority
    census_kind: Literal["mapping_prepare"] = "mapping_prepare"

    def __post_init__(self) -> None:
        self._validate_common()
        if len(self.selected_kinds) != 1:
            raise ValueError("mapping_prepare selects exactly one unit kind")


@dataclass(frozen=True)
class MappingRollbackCensusRequest(_Request):
    authority: MappingRollbackAuthority
    census_kind: Literal["mapping_rollback"] = "mapping_rollback"

    def __post_init__(self) -> None:
        self._validate_common()
        if len(self.selected_kinds) != 1:
            raise ValueError("mapping_rollback selects exactly one unit kind")


type StandaloneCrmCensusRequest = (
    SourceSyncCensusRequest | MappingPrepareCensusRequest | MappingRollbackCensusRequest
)


def canonical_request_payload(request: StandaloneCrmCensusRequest) -> str:
    payload = {
        "contract": "standalone-crm-census-v1",
        "kind": request.census_kind,
        "source_key": request.source_key,
        "source_instance_id": request.source_instance_id,
        "control_instance_id": request.control_instance_id,
        "occurrence_key": request.occurrence_key,
        "selected_kinds": request.selected_kinds,
        "budget": asdict(request.budget),
        "authority": asdict(request.authority),
        "policy_version": request.policy_version,
        "association_contract_version": request.association_contract_version,
        "configuration_digest": request.configuration_digest,
    }
    return json.dumps(payload, default=list, sort_keys=True, separators=(",", ":"), allow_nan=False)


def census_fingerprint(request: StandaloneCrmCensusRequest) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            (request.census_kind + ":" + canonical_request_payload(request)).encode("utf-8")
        ).hexdigest()
    )
