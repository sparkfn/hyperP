"""Canonical identities and immutable fingerprints for CRM tenant projection releases."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Protocol

from src.crm_tenant_projection_records import (
    CrmTenantProjectionExpectedHead,
    CrmTenantProjectionScope,
)
from src.models import JsonValue
from src.standalone_crm_census_types import _integer


class _Command(Protocol):
    """Immutable command properties required for its canonical fingerprint."""

    @property
    def scope(self) -> CrmTenantProjectionScope: ...

    @property
    def request_id(self) -> str: ...

    @property
    def source_census_id(self) -> str: ...

    @property
    def source_census_fingerprint(self) -> str: ...

    @property
    def mapping_revision_id(self) -> str: ...

    @property
    def mapping_manifest_digest(self) -> str: ...

    @property
    def expected_mapping_head_id(self) -> str: ...

    @property
    def expected_mapping_head_digest(self) -> str: ...

    @property
    def expected_prior_head(self) -> CrmTenantProjectionExpectedHead | None: ...

    @property
    def contract_version(self) -> str: ...


def projection_head_id(scope: CrmTenantProjectionScope) -> str:
    """Return the stable active-head identity for one projection scope."""
    return _digest("crm-tenant-projection-active-head-v1", [_scope_payload(scope)])


def projection_release_id(scope: CrmTenantProjectionScope, release_number: int) -> str:
    """Return one deterministic release identity under a scope-local sequence."""
    _integer(release_number, "release_number", 1)
    return _digest("crm-tenant-projection-release-v1", [_scope_payload(scope), release_number])


def projection_input_id(release_id: str, subject_kind: str, subject_id: str) -> str:
    """Return the canonical immutable input identity."""
    return _digest("crm-tenant-projection-input-id-v1", [release_id, subject_kind, subject_id])


def projection_association_id(
    release_id: str,
    input_id: str,
    subject_kind: str,
    subject_id: str,
    entity_key: str,
    relationship_kind: str,
) -> str:
    """Return the company-free association identity."""
    return _digest(
        "crm-tenant-projection-association-v1",
        [release_id, input_id, subject_kind, subject_id, entity_key, relationship_kind],
    )


def projection_support_id(association_id: str, observation_id: str, mapping_target_id: str) -> str:
    """Return an immutable support identity from its correlated evidence pair."""
    return _digest(
        "crm-tenant-projection-support-v1", [association_id, observation_id, mapping_target_id]
    )


def projection_support_digest(
    release_id: str,
    association_id: str,
    observation_id: str,
    mapping_target_id: str,
) -> str:
    """Return the #301 release-bound immutable support digest."""
    return _digest(
        "crm-tenant-projection-support-v1",
        [release_id, association_id, observation_id, mapping_target_id],
    )


def empty_capture_boundary_digest() -> str:
    """Return the deterministic digest for a valid generation-zero input boundary."""
    return _digest("crm-tenant-projection-capture-boundary-v1", [])


def extend_capture_boundary_digest(
    prior_digest: str,
    input_id: str,
    input_digest: str,
) -> str:
    """Extend the ordered immutable capture digest by one canonical input."""
    return _digest(
        "crm-tenant-projection-capture-boundary-step-v1",
        [prior_digest, input_id, input_digest],
    )


def command_fingerprint(command: _Command) -> str:
    """Fingerprint all immutable materialization inputs, excluding execution timestamps."""
    prior = command.expected_prior_head
    return _digest(
        "crm-tenant-projection-materialization-request-v1",
        [
            _scope_payload(command.scope),
            command.request_id,
            command.source_census_id,
            command.source_census_fingerprint,
            command.mapping_revision_id,
            command.mapping_manifest_digest,
            command.expected_mapping_head_id,
            command.expected_mapping_head_digest,
            {
                "present": prior is not None,
                "head_id": None if prior is None else prior.head_id,
                "release_id": None if prior is None else prior.active_release_id,
                "release_number": None if prior is None else prior.active_release_number,
                "release_fingerprint": None if prior is None else prior.active_release_fingerprint,
            },
            command.contract_version,
        ],
    )


def materialized_release_fingerprint(
    scope: CrmTenantProjectionScope,
    release_id: str,
    release_number: int,
    request_fingerprint: str,
    source_census_id: str,
    source_census_fingerprint: str,
    contact_boundary: Mapping[str, JsonValue],
    lead_boundary: Mapping[str, JsonValue],
    mapping_revision_id: str,
    mapping_revision_number: int,
    mapping_manifest_digest: str,
    expected_mapping_head: Mapping[str, JsonValue],
    expected_prior_head: Mapping[str, JsonValue],
    projection_head: str,
    contract_version: str,
) -> str:
    """Fingerprint every immutable authority value discovered for one release."""
    return _digest(
        "crm-tenant-projection-materialized-release-v1",
        [
            _scope_payload(scope),
            release_id,
            release_number,
            request_fingerprint,
            source_census_id,
            source_census_fingerprint,
            dict(contact_boundary),
            dict(lead_boundary),
            mapping_revision_id,
            mapping_revision_number,
            mapping_manifest_digest,
            dict(expected_mapping_head),
            dict(expected_prior_head),
            projection_head,
            contract_version,
        ],
    )


def _scope_payload(scope: CrmTenantProjectionScope) -> dict[str, JsonValue]:
    return {
        "source_key": scope.source_key,
        "source_instance_id": scope.source_instance_id,
        "control_instance_id": scope.control_instance_id,
    }


def _digest(namespace: str, payload: list[JsonValue]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(namespace.encode() + b"\x00" + encoded.encode()).hexdigest()
