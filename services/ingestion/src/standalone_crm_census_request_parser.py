"""Strict raw parsers for external and stored standalone CRM census requests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.standalone_crm_census_requests import (
    MappingPrepareAuthority,
    MappingPrepareCensusRequest,
    MappingRollbackAuthority,
    MappingRollbackCensusRequest,
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
    StandaloneCrmCensusRequest,
)
from src.standalone_crm_census_types import _CENSUS_KINDS, StandaloneCrmStreamKind


def parse_census_request(raw: Mapping[str, object]) -> StandaloneCrmCensusRequest:
    _exact_keys(raw, _external_request_keys())
    kind = _raw_text(raw, "census_kind")
    if kind not in _CENSUS_KINDS:
        raise ValueError("unsupported census_kind")
    return _parsed_request(kind, _common_values(raw), _raw_mapping(raw, "authority"))


def parse_stored_census_request(raw: Mapping[str, object]) -> StandaloneCrmCensusRequest:
    _exact_keys(raw, _stored_request_keys())
    if _raw_text(raw, "contract") != "standalone-crm-census-v1":
        raise ValueError("unsupported stored census contract")
    return _parsed_request(
        _raw_text(raw, "kind"), _common_values(raw), _raw_mapping(raw, "authority")
    )


@dataclass(frozen=True)
class _CommonRequestValues:
    source_key: str
    source_instance_id: str
    control_instance_id: str
    occurrence_key: str
    selected_kinds: tuple[StandaloneCrmStreamKind, ...]
    budget: StandaloneCrmBudget
    policy_version: str
    association_contract_version: str
    configuration_digest: str


def _parsed_request(
    kind: str, values: _CommonRequestValues, authority: Mapping[str, object]
) -> StandaloneCrmCensusRequest:
    if kind == "source_sync":
        _exact_keys(authority, _source_authority_keys())
        return SourceSyncCensusRequest(
            values.source_key,
            values.source_instance_id,
            values.control_instance_id,
            values.occurrence_key,
            values.selected_kinds,
            values.budget,
            values.policy_version,
            values.association_contract_version,
            values.configuration_digest,
            SourceSyncAuthority(
                _raw_text(authority, "mapping_head_id"),
                _raw_text(authority, "mapping_head_digest"),
                _raw_text(authority, "projection_head_id"),
                _raw_text(authority, "projection_head_digest"),
            ),
        )
    if kind == "mapping_prepare":
        _exact_keys(authority, _prepare_authority_keys())
        return MappingPrepareCensusRequest(
            values.source_key,
            values.source_instance_id,
            values.control_instance_id,
            values.occurrence_key,
            values.selected_kinds,
            values.budget,
            values.policy_version,
            values.association_contract_version,
            values.configuration_digest,
            MappingPrepareAuthority(
                _raw_text(authority, "prepared_revision_id"),
                _raw_text(authority, "prepared_revision_digest"),
                _raw_text(authority, "expected_current_head_id"),
            ),
        )
    if kind == "mapping_rollback":
        _exact_keys(authority, _rollback_authority_keys())
        return MappingRollbackCensusRequest(
            values.source_key,
            values.source_instance_id,
            values.control_instance_id,
            values.occurrence_key,
            values.selected_kinds,
            values.budget,
            values.policy_version,
            values.association_contract_version,
            values.configuration_digest,
            MappingRollbackAuthority(
                _raw_text(authority, "target_revision_id"),
                _raw_text(authority, "target_revision_digest"),
                _raw_text(authority, "expected_current_head_id"),
                _raw_text(authority, "rollback_head_id"),
            ),
        )
    raise ValueError("unsupported stored census kind")


def _external_request_keys() -> set[str]:
    return {
        "source_key",
        "source_instance_id",
        "control_instance_id",
        "occurrence_key",
        "selected_kinds",
        "budget",
        "policy_version",
        "association_contract_version",
        "configuration_digest",
        "census_kind",
        "authority",
    }


def _stored_request_keys() -> set[str]:
    return {
        "contract",
        "kind",
        "source_key",
        "source_instance_id",
        "control_instance_id",
        "occurrence_key",
        "selected_kinds",
        "budget",
        "policy_version",
        "association_contract_version",
        "configuration_digest",
        "authority",
    }


def _source_authority_keys() -> set[str]:
    return {
        "mapping_head_id",
        "mapping_head_digest",
        "projection_head_id",
        "projection_head_digest",
    }


def _prepare_authority_keys() -> set[str]:
    return {"prepared_revision_id", "prepared_revision_digest", "expected_current_head_id"}


def _rollback_authority_keys() -> set[str]:
    return {
        "target_revision_id",
        "target_revision_digest",
        "expected_current_head_id",
        "rollback_head_id",
    }


def _common_values(raw: Mapping[str, object]) -> _CommonRequestValues:
    selected_raw = raw["selected_kinds"]
    if not isinstance(selected_raw, list) or not all(
        isinstance(value, str) for value in selected_raw
    ):
        raise ValueError("selected_kinds must be a list of strings")
    budget_raw = _raw_mapping(raw, "budget")
    _exact_keys(budget_raw, _budget_keys())
    return _CommonRequestValues(
        _raw_text(raw, "source_key"),
        _raw_text(raw, "source_instance_id"),
        _raw_text(raw, "control_instance_id"),
        _raw_text(raw, "occurrence_key"),
        _stream_kinds(selected_raw),
        _budget_from_raw(budget_raw),
        _raw_text(raw, "policy_version"),
        _raw_text(raw, "association_contract_version"),
        _raw_text(raw, "configuration_digest"),
    )


def _budget_keys() -> set[str]:
    return {
        "max_calls_per_attempt",
        "max_rows_per_attempt",
        "max_runtime_seconds_per_attempt",
        "max_calls_per_occurrence",
        "max_rows_per_occurrence",
        "max_attempts_per_occurrence",
        "occurrence_deadline",
    }


def _exact_keys(raw: Mapping[str, object], allowed: set[str]) -> None:
    actual = set(raw)
    if actual != allowed:
        missing = sorted(allowed - actual)
        extra = sorted(actual - allowed)
        raise ValueError(f"request fields mismatch: missing={missing} extra={extra}")


def _raw_mapping(raw: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = raw.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    return value


def _raw_text(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _raw_int(raw: Mapping[str, object], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _stream_kinds(raw: list[str]) -> tuple[StandaloneCrmStreamKind, ...]:
    result: list[StandaloneCrmStreamKind] = []
    for value in raw:
        if value == "contact":
            result.append("contact")
        elif value == "lead":
            result.append("lead")
        elif value == "company":
            result.append("company")
        else:
            raise ValueError("selected_kinds contains an unsupported kind")
    return tuple(result)


def _budget_from_raw(raw: Mapping[str, object]) -> StandaloneCrmBudget:
    return StandaloneCrmBudget(
        _raw_int(raw, "max_calls_per_attempt"),
        _raw_int(raw, "max_rows_per_attempt"),
        _raw_int(raw, "max_runtime_seconds_per_attempt"),
        _raw_int(raw, "max_calls_per_occurrence"),
        _raw_int(raw, "max_rows_per_occurrence"),
        _raw_int(raw, "max_attempts_per_occurrence"),
        _raw_text(raw, "occurrence_deadline"),
    )
