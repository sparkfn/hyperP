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
    authority = _raw_mapping(raw, "authority")
    return _parsed_request(
        kind, _common_values(raw), authority, _external_contract(kind, authority)
    )


def parse_stored_census_request(raw: Mapping[str, object]) -> StandaloneCrmCensusRequest:
    _exact_keys(raw, _stored_request_keys())
    contract = _raw_text(raw, "contract")
    if contract not in {"standalone-crm-census-v1", "standalone-crm-census-v2"}:
        raise ValueError("unsupported stored census contract")
    return _parsed_request(
        _raw_text(raw, "kind"), _common_values(raw), _raw_mapping(raw, "authority"), contract
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
    kind: str,
    values: _CommonRequestValues,
    authority: Mapping[str, object],
    contract: str = "standalone-crm-census-v2",
) -> StandaloneCrmCensusRequest:
    if kind == "source_sync":
        _exact_keys(authority, _source_authority_keys(contract))
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
                _optional_text(authority, "mapping_active_revision_id"),
                _optional_int(authority, "mapping_active_revision_number"),
                _optional_text(authority, "projection_active_release_id"),
                _optional_int(authority, "projection_active_release_number"),
            ),
        )
    if kind == "mapping_prepare":
        _exact_keys(authority, _prepare_authority_keys(contract))
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
                _optional_text(authority, "completed_release_id"),
                _optional_text(authority, "completed_release_fingerprint"),
                _optional_text(authority, "expected_mapping_active_revision_id"),
                _optional_int(authority, "expected_mapping_active_revision_number"),
                _optional_text(authority, "expected_mapping_active_manifest_digest"),
                _optional_text(authority, "expected_projection_head_id"),
                _optional_text(authority, "expected_projection_active_release_id"),
                _optional_int(authority, "expected_projection_active_release_number"),
                _optional_text(authority, "expected_projection_active_release_fingerprint"),
            ),
        )
    if kind == "mapping_rollback":
        _exact_keys(authority, _rollback_authority_keys(contract))
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
                _optional_text(authority, "rollback_head_digest"),
                _optional_text(authority, "completed_release_id"),
                _optional_text(authority, "completed_release_fingerprint"),
                _optional_text(authority, "expected_mapping_active_revision_id"),
                _optional_int(authority, "expected_mapping_active_revision_number"),
                _optional_text(authority, "expected_mapping_active_manifest_digest"),
                _optional_text(authority, "expected_projection_head_id"),
                _optional_text(authority, "expected_projection_active_release_id"),
                _optional_int(authority, "expected_projection_active_release_number"),
                _optional_text(authority, "expected_projection_active_release_fingerprint"),
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


def _source_authority_keys(contract: str) -> set[str]:
    keys = {
        "mapping_head_id",
        "mapping_head_digest",
        "projection_head_id",
        "projection_head_digest",
    }
    if contract == "standalone-crm-census-v2":
        keys.update(
            {
                "mapping_active_revision_id",
                "mapping_active_revision_number",
                "projection_active_release_id",
                "projection_active_release_number",
            }
        )
    return keys


def _prepare_authority_keys(contract: str) -> set[str]:
    keys = {"prepared_revision_id", "prepared_revision_digest", "expected_current_head_id"}
    if contract == "standalone-crm-census-v2":
        keys.update(_activation_authority_keys())
    return keys


def _rollback_authority_keys(contract: str) -> set[str]:
    keys = {
        "target_revision_id",
        "target_revision_digest",
        "expected_current_head_id",
        "rollback_head_id",
    }
    if contract == "standalone-crm-census-v2":
        keys.add("rollback_head_digest")
        keys.update(_activation_authority_keys())
    return keys


def _activation_authority_keys() -> set[str]:
    return {
        "completed_release_id",
        "completed_release_fingerprint",
        "expected_mapping_active_revision_id",
        "expected_mapping_active_revision_number",
        "expected_mapping_active_manifest_digest",
        "expected_projection_head_id",
        "expected_projection_active_release_id",
        "expected_projection_active_release_number",
        "expected_projection_active_release_fingerprint",
    }


def _external_contract(kind: str, authority: Mapping[str, object]) -> str:
    expected = (
        _source_authority_keys("standalone-crm-census-v2")
        if kind == "source_sync"
        else _prepare_authority_keys("standalone-crm-census-v2")
        if kind == "mapping_prepare"
        else _rollback_authority_keys("standalone-crm-census-v2")
    )
    return "standalone-crm-census-v2" if set(authority) == expected else "standalone-crm-census-v1"


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


def _optional_text(raw: Mapping[str, object], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None


def _optional_int(raw: Mapping[str, object], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    return _raw_int(raw, key)


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
