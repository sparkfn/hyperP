"""Strict immutable census request schemas and canonical fingerprints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import cast

from src.standalone_crm_census_models import (
    SOURCE_UNIT_KINDS,
    StandaloneCrmBudgetSnapshot,
    StandaloneCrmCensusKind,
    StandaloneCrmSourceUnitKind,
    required_text,
)


@dataclass(frozen=True)
class SourceSyncCensusRequest:
    """Operator input; current authority is captured separately at admission."""

    source_key: str
    source_instance_id: str
    control_instance_id: str
    occurrence_key: str
    operator_id: str
    selected_kinds: tuple[StandaloneCrmSourceUnitKind, ...]
    policy_version: str
    association_contract_version: str
    configuration_digest: str
    budget: StandaloneCrmBudgetSnapshot
    fingerprint_version: str = "standalone-crm-census-v1"

    @property
    def census_kind(self) -> StandaloneCrmCensusKind:
        return "source_sync"

    def __post_init__(self) -> None:
        _validate_common_request(self)
        if not self.selected_kinds:
            raise ValueError("source_sync requires at least one selected CRM kind")
        if tuple(sorted(self.selected_kinds)) != self.selected_kinds:
            raise ValueError("selected_kinds must be canonical sorted order")
        if len(set(self.selected_kinds)) != len(self.selected_kinds):
            raise ValueError("selected_kinds must contain unique supported CRM kinds")
        if any(kind not in SOURCE_UNIT_KINDS for kind in self.selected_kinds):
            raise ValueError("selected_kinds must contain unique supported CRM kinds")


@dataclass(frozen=True)
class MappingPrepareCensusRequest:
    """Operator request for one #275-authoritative prepared revision."""

    source_key: str
    source_instance_id: str
    control_instance_id: str
    occurrence_key: str
    operator_id: str
    policy_version: str
    association_contract_version: str
    configuration_digest: str
    budget: StandaloneCrmBudgetSnapshot
    prepared_revision_id: str
    prepared_revision_digest: str
    expected_current_mapping_head_id: str | None
    fingerprint_version: str = "standalone-crm-census-v1"

    @property
    def census_kind(self) -> StandaloneCrmCensusKind:
        return "mapping_prepare"

    def __post_init__(self) -> None:
        _validate_common_request(self)
        required_text(self.prepared_revision_id, "prepared_revision_id")
        required_text(self.prepared_revision_digest, "prepared_revision_digest")
        if self.expected_current_mapping_head_id is not None:
            required_text(self.expected_current_mapping_head_id, "expected_current_mapping_head_id")


@dataclass(frozen=True)
class MappingRollbackCensusRequest:
    """Operator request for one #275-authoritative higher revision rollback."""

    source_key: str
    source_instance_id: str
    control_instance_id: str
    occurrence_key: str
    operator_id: str
    policy_version: str
    association_contract_version: str
    configuration_digest: str
    budget: StandaloneCrmBudgetSnapshot
    target_revision_id: str
    target_revision_digest: str
    expected_current_mapping_head_id: str
    intended_prior_mapping_head_id: str | None
    fingerprint_version: str = "standalone-crm-census-v1"

    @property
    def census_kind(self) -> StandaloneCrmCensusKind:
        return "mapping_rollback"

    def __post_init__(self) -> None:
        _validate_common_request(self)
        required_text(self.target_revision_id, "target_revision_id")
        required_text(self.target_revision_digest, "target_revision_digest")
        required_text(self.expected_current_mapping_head_id, "expected_current_mapping_head_id")
        if self.intended_prior_mapping_head_id is not None:
            required_text(self.intended_prior_mapping_head_id, "intended_prior_mapping_head_id")


type StandaloneCrmCensusRequest = (
    SourceSyncCensusRequest | MappingPrepareCensusRequest | MappingRollbackCensusRequest
)


@dataclass(frozen=True)
class SourceSyncAuthoritySnapshot:
    """Exact active heads captured by the authority reader at admission."""

    mapping_head_id: str
    mapping_head_digest: str
    projection_head_id: str | None

    def __post_init__(self) -> None:
        required_text(self.mapping_head_id, "mapping_head_id")
        required_text(self.mapping_head_digest, "mapping_head_digest")
        if self.projection_head_id is not None:
            required_text(self.projection_head_id, "projection_head_id")


def admitted_request_fingerprint(
    request: StandaloneCrmCensusRequest,
    authority: SourceSyncAuthoritySnapshot | None,
) -> str:
    """Fingerprint persisted admission input with captured, not caller-trusted, heads."""
    if isinstance(request, SourceSyncCensusRequest) and authority is None:
        raise ValueError("source_sync requires a captured authority snapshot")
    if not isinstance(request, SourceSyncCensusRequest) and authority is not None:
        raise ValueError("mapping census must not carry source-sync authority")
    payload: dict[str, object] = asdict(request)
    payload["census_kind"] = request.census_kind
    payload["captured_authority"] = None if authority is None else asdict(authority)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    domain = f"standalone-crm-census:{request.census_kind}:v1".encode("ascii")
    return "sha256:" + hashlib.sha256(domain + b"\x00" + encoded).hexdigest()


def request_fingerprint(request: StandaloneCrmCensusRequest) -> str:
    """Compatibility helper for mapping-only callers; source requests require admission capture."""
    if isinstance(request, SourceSyncCensusRequest):
        raise ValueError("source_sync fingerprint requires captured authority at admission")
    return admitted_request_fingerprint(request, None)


def _validate_common_request(request: StandaloneCrmCensusRequest) -> None:
    if request.source_key != "bitrix_chat":
        raise ValueError("standalone CRM census requires source_key='bitrix_chat'")
    for field in (
        "source_instance_id",
        "control_instance_id",
        "occurrence_key",
        "operator_id",
        "policy_version",
        "association_contract_version",
        "configuration_digest",
        "fingerprint_version",
    ):
        required_text(getattr(request, field), field)


def _persisted_text(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"persisted standalone CRM census {key} is invalid")
    return value


def _persisted_optional_text(raw: dict[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"persisted standalone CRM census {key} is invalid")
    return value


_COMMON_FIELDS = frozenset(
    {
        "source_key",
        "source_instance_id",
        "control_instance_id",
        "occurrence_key",
        "operator_id",
        "policy_version",
        "association_contract_version",
        "configuration_digest",
        "budget",
        "fingerprint_version",
        "census_kind",
    }
)
_KIND_FIELDS: dict[str, frozenset[str]] = {
    "source_sync": _COMMON_FIELDS | {"selected_kinds"},
    "mapping_prepare": _COMMON_FIELDS
    | {"prepared_revision_id", "prepared_revision_digest", "expected_current_mapping_head_id"},
    "mapping_rollback": _COMMON_FIELDS
    | {
        "target_revision_id",
        "target_revision_digest",
        "expected_current_mapping_head_id",
        "intended_prior_mapping_head_id",
    },
}


def operator_request_json(request: StandaloneCrmCensusRequest) -> str:
    """Serialize only caller-authorized operator fields, never captured authority."""
    payload: dict[str, object] = asdict(request)
    payload["census_kind"] = request.census_kind
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def operator_request_from_json(value: str) -> StandaloneCrmCensusRequest:
    """Strictly parse one operator request; reject unknown, missing, and cross-kind fields."""
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("operator standalone CRM census request is invalid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("operator standalone CRM census request is invalid")
    kind = raw.get("census_kind")
    expected = _KIND_FIELDS.get(kind) if isinstance(kind, str) else None
    if expected is None or not isinstance(kind, str) or set(raw) != expected:
        raise ValueError(
            "operator standalone CRM census request has missing, extra, or cross-kind fields"
        )
    if "captured_authority" in raw:
        raise ValueError("operator request must not provide captured authority")
    return _request_from_exact_mapping(raw, kind)


def admitted_request_json(request: StandaloneCrmCensusRequest) -> str:
    """Serialize the request persisted at admission; captured authority is stored separately."""
    return operator_request_json(request)


def request_from_persisted_json(value: str) -> StandaloneCrmCensusRequest:
    """Strictly parse an admitted persisted request; authority belongs to its separate snapshot."""
    return operator_request_from_json(value)


def _request_from_exact_mapping(raw: dict[str, object], kind: str) -> StandaloneCrmCensusRequest:
    budget = _persisted_budget(raw)
    common = _persisted_common(raw)
    if kind == "source_sync":
        return _source_request_from_mapping(raw, common, budget)
    if kind == "mapping_prepare":
        return _mapping_prepare_request_from_mapping(raw, common, budget)
    return _mapping_rollback_request_from_mapping(raw, common, budget)


def _persisted_budget(raw: dict[str, object]) -> StandaloneCrmBudgetSnapshot:
    budget_raw = raw["budget"]
    if not isinstance(budget_raw, dict):
        raise ValueError("standalone CRM census budget is invalid")
    return StandaloneCrmBudgetSnapshot(**budget_raw)


def _persisted_common(raw: dict[str, object]) -> dict[str, str]:
    common = {
        key: raw[key]
        for key in (
            "source_key",
            "source_instance_id",
            "control_instance_id",
            "occurrence_key",
            "operator_id",
            "policy_version",
            "association_contract_version",
            "configuration_digest",
            "fingerprint_version",
        )
    }
    if not all(isinstance(item, str) for item in common.values()):
        raise ValueError("standalone CRM census request has invalid text")
    return cast(dict[str, str], common)


def _source_request_from_mapping(
    raw: dict[str, object], common: dict[str, str], budget: StandaloneCrmBudgetSnapshot
) -> SourceSyncCensusRequest:
    selected = raw["selected_kinds"]
    if not isinstance(selected, list) or not all(item in SOURCE_UNIT_KINDS for item in selected):
        raise ValueError("standalone source census selection is invalid")
    return SourceSyncCensusRequest(
        **common,
        budget=budget,
        selected_kinds=tuple(cast(StandaloneCrmSourceUnitKind, item) for item in selected),
    )


def _mapping_prepare_request_from_mapping(
    raw: dict[str, object], common: dict[str, str], budget: StandaloneCrmBudgetSnapshot
) -> MappingPrepareCensusRequest:
    return MappingPrepareCensusRequest(
        **common,
        budget=budget,
        prepared_revision_id=_persisted_text(raw, "prepared_revision_id"),
        prepared_revision_digest=_persisted_text(raw, "prepared_revision_digest"),
        expected_current_mapping_head_id=_persisted_optional_text(
            raw, "expected_current_mapping_head_id"
        ),
    )


def _mapping_rollback_request_from_mapping(
    raw: dict[str, object], common: dict[str, str], budget: StandaloneCrmBudgetSnapshot
) -> MappingRollbackCensusRequest:
    return MappingRollbackCensusRequest(
        **common,
        budget=budget,
        target_revision_id=_persisted_text(raw, "target_revision_id"),
        target_revision_digest=_persisted_text(raw, "target_revision_digest"),
        expected_current_mapping_head_id=_persisted_text(raw, "expected_current_mapping_head_id"),
        intended_prior_mapping_head_id=_persisted_optional_text(
            raw, "intended_prior_mapping_head_id"
        ),
    )
