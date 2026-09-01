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
    mapping_active_revision_id: str | None = None
    mapping_active_revision_number: int | None = None
    projection_active_release_id: str | None = None
    projection_active_release_number: int | None = None

    def __post_init__(self) -> None:
        for field in (
            "mapping_head_id",
            "mapping_head_digest",
            "projection_head_id",
            "projection_head_digest",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        optional = (
            self.mapping_active_revision_id,
            self.mapping_active_revision_number,
            self.projection_active_release_id,
            self.projection_active_release_number,
        )
        if any(value is None for value in optional) and any(
            value is not None for value in optional
        ):
            raise ValueError("source-sync authority head snapshots must be complete or legacy")
        if self.mapping_active_revision_id is not None:
            projection_release_id = self.projection_active_release_id
            mapping_revision_number = self.mapping_active_revision_number
            projection_release_number = self.projection_active_release_number
            if (
                projection_release_id is None
                or mapping_revision_number is None
                or projection_release_number is None
            ):
                raise AssertionError("complete source authority was narrowed incorrectly")
            object.__setattr__(
                self,
                "mapping_active_revision_id",
                _text(self.mapping_active_revision_id, "mapping_active_revision_id"),
            )
            object.__setattr__(
                self,
                "projection_active_release_id",
                _text(projection_release_id, "projection_active_release_id"),
            )
            _integer(mapping_revision_number, "mapping_active_revision_number", 1)
            _integer(projection_release_number, "projection_active_release_number", 1)


@dataclass(frozen=True)
class MappingPrepareAuthority:
    prepared_revision_id: str
    prepared_revision_digest: str
    expected_current_head_id: str
    completed_release_id: str | None = None
    completed_release_fingerprint: str | None = None
    expected_mapping_active_revision_id: str | None = None
    expected_mapping_active_revision_number: int | None = None
    expected_mapping_active_manifest_digest: str | None = None
    expected_projection_head_id: str | None = None
    expected_projection_active_release_id: str | None = None
    expected_projection_active_release_number: int | None = None
    expected_projection_active_release_fingerprint: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "prepared_revision_id",
            "prepared_revision_digest",
            "expected_current_head_id",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        _validate_v2_mapping_activation_authority(self)


@dataclass(frozen=True)
class MappingRollbackAuthority:
    target_revision_id: str
    target_revision_digest: str
    expected_current_head_id: str
    rollback_head_id: str
    rollback_head_digest: str | None = None
    completed_release_id: str | None = None
    completed_release_fingerprint: str | None = None
    expected_mapping_active_revision_id: str | None = None
    expected_mapping_active_revision_number: int | None = None
    expected_mapping_active_manifest_digest: str | None = None
    expected_projection_head_id: str | None = None
    expected_projection_active_release_id: str | None = None
    expected_projection_active_release_number: int | None = None
    expected_projection_active_release_fingerprint: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "target_revision_id",
            "target_revision_digest",
            "expected_current_head_id",
            "rollback_head_id",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        _validate_v2_mapping_activation_authority(self)


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
    contract = _contract_version(request)
    payload = {
        "contract": contract,
        "kind": request.census_kind,
        "source_key": request.source_key,
        "source_instance_id": request.source_instance_id,
        "control_instance_id": request.control_instance_id,
        "occurrence_key": request.occurrence_key,
        "selected_kinds": request.selected_kinds,
        "budget": asdict(request.budget),
        "authority": canonical_authority_payload(request),
        "policy_version": request.policy_version,
        "association_contract_version": request.association_contract_version,
        "configuration_digest": request.configuration_digest,
    }
    return json.dumps(payload, default=list, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_authority_payload(
    request: StandaloneCrmCensusRequest,
) -> dict[str, object]:
    """Return the authority identity using the request's persisted contract version."""
    return _canonical_authority(request, _contract_version(request))


def _canonical_authority(request: StandaloneCrmCensusRequest, contract: str) -> dict[str, object]:
    values = asdict(request.authority)
    if contract == "standalone-crm-census-v1":
        return {key: value for key, value in values.items() if value is not None}
    return values


def _contract_version(request: StandaloneCrmCensusRequest) -> str:
    """Retain v1 payload/fingerprint compatibility until a complete v2 authority is captured."""
    authority = request.authority
    if isinstance(authority, SourceSyncAuthority):
        return (
            "standalone-crm-census-v2"
            if authority.mapping_active_revision_id is not None
            else "standalone-crm-census-v1"
        )
    return (
        "standalone-crm-census-v2"
        if authority.completed_release_id is not None
        else "standalone-crm-census-v1"
    )


def mapping_candidate_identity(
    authority: MappingPrepareAuthority | MappingRollbackAuthority,
) -> tuple[str, str]:
    """Return the prepared candidate identity; rollback history is provenance only."""
    if isinstance(authority, MappingPrepareAuthority):
        return authority.prepared_revision_id, authority.prepared_revision_digest
    if authority.rollback_head_digest is None:
        raise ValueError("legacy rollback authority has no candidate digest")
    return authority.rollback_head_id, authority.rollback_head_digest


def _validate_v2_mapping_activation_authority(
    authority: MappingPrepareAuthority | MappingRollbackAuthority,
) -> None:
    release_fields = (authority.completed_release_id, authority.completed_release_fingerprint)
    mapping_fields = (
        authority.expected_mapping_active_revision_id,
        authority.expected_mapping_active_revision_number,
        authority.expected_mapping_active_manifest_digest,
    )
    projection_fields = (
        authority.expected_projection_active_release_id,
        authority.expected_projection_active_release_number,
        authority.expected_projection_active_release_fingerprint,
    )
    all_fields = (
        release_fields
        + mapping_fields
        + (authority.expected_projection_head_id,)
        + projection_fields
    )
    if all(value is None for value in all_fields):
        return
    if any(value is None for value in release_fields):
        raise ValueError("mapping activation authority requires an exact completed release")
    if any(value is None for value in mapping_fields) and any(
        value is not None for value in mapping_fields
    ):
        raise ValueError("mapping predecessor must be complete or absent")
    if any(value is None for value in projection_fields) and any(
        value is not None for value in projection_fields
    ):
        raise ValueError("projection predecessor must be complete or absent")
    if authority.expected_projection_head_id is None:
        raise ValueError("mapping activation requires deterministic projection head identity")
    for field in (
        "completed_release_id",
        "expected_projection_head_id",
    ):
        object.__setattr__(authority, field, _text(getattr(authority, field), field))
    for field in ("completed_release_fingerprint",):
        value = getattr(authority, field)
        if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
            raise ValueError(f"{field} must be a canonical sha256 digest")
    if authority.expected_mapping_active_revision_id is not None:
        object.__setattr__(
            authority,
            "expected_mapping_active_revision_id",
            _text(
                authority.expected_mapping_active_revision_id, "expected_mapping_active_revision_id"
            ),
        )
        assert authority.expected_mapping_active_manifest_digest is not None
        _require_digest(
            authority.expected_mapping_active_manifest_digest,
            "expected_mapping_active_manifest_digest",
        )
        assert authority.expected_mapping_active_revision_number is not None
        _integer(
            authority.expected_mapping_active_revision_number,
            "expected_mapping_active_revision_number",
            1,
        )
    if authority.expected_projection_active_release_id is not None:
        object.__setattr__(
            authority,
            "expected_projection_active_release_id",
            _text(
                authority.expected_projection_active_release_id,
                "expected_projection_active_release_id",
            ),
        )
        assert authority.expected_projection_active_release_fingerprint is not None
        _require_digest(
            authority.expected_projection_active_release_fingerprint,
            "expected_projection_active_release_fingerprint",
        )
        assert authority.expected_projection_active_release_number is not None
        _integer(
            authority.expected_projection_active_release_number,
            "expected_projection_active_release_number",
            1,
        )
    if isinstance(authority, MappingRollbackAuthority):
        if authority.rollback_head_digest is None:
            raise ValueError("rollback activation authority requires rollback candidate digest")
        if len(
            authority.rollback_head_digest
        ) != 71 or not authority.rollback_head_digest.startswith("sha256:"):
            raise ValueError("rollback_head_digest must be a canonical sha256 digest")


def _require_digest(value: str, field: str) -> None:
    if len(value) != 71 or not value.startswith("sha256:"):
        raise ValueError(f"{field} must be a canonical sha256 digest")


def census_fingerprint(request: StandaloneCrmCensusRequest) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            (request.census_kind + ":" + canonical_request_payload(request)).encode("utf-8")
        ).hexdigest()
    )
