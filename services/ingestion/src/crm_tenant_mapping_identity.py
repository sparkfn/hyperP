"""Canonical identities, fingerprints, and payload helpers for mapping authority."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Protocol

from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingAuthorization,
    CrmTenantMappingExpectedHead,
    CrmTenantMappingManifest,
    CrmTenantMappingScope,
)
from src.models import JsonValue
from src.standalone_crm_census_types import _integer, _text


class _ExpectedHeadBoundary(Protocol):
    @property
    def head_id(self) -> str: ...

    @property
    def expected_head(self) -> CrmTenantMappingExpectedHead | None: ...


class _PrepareCommand(Protocol):
    @property
    def scope(self) -> CrmTenantMappingScope: ...

    @property
    def preparation_request_id(self) -> str: ...

    @property
    def manifest(self) -> CrmTenantMappingManifest: ...

    @property
    def expected_head_boundary(self) -> _ExpectedHeadBoundary: ...

    @property
    def authorization(self) -> CrmTenantMappingAuthorization: ...


def mapping_head_id(scope: CrmTenantMappingScope) -> str:
    """Return the stable deterministic active-head identity for one scope."""
    return _digest("crm-tenant-mapping-active-head-v1", [_scope_payload(scope)])


def mapping_revision_id(scope: CrmTenantMappingScope, revision_number: int) -> str:
    """Return the stable deterministic immutable revision identity."""
    _integer(revision_number, "revision_number", 1)
    return _digest("crm-tenant-mapping-revision-v1", [_scope_payload(scope), revision_number])


def _target_keys(manifest: CrmTenantMappingManifest) -> tuple[str, ...]:
    return tuple(
        sorted({target.entity_key for entry in manifest.entries for target in entry.targets})
    )


def _prepare_payload(command: _PrepareCommand) -> list[JsonValue]:
    return [
        _scope_payload(command.scope),
        command.preparation_request_id,
        command.manifest.digest,
        _boundary_payload(command.expected_head_boundary),
        _authorization_payload(command.authorization),
    ]


def _scope_payload(scope: CrmTenantMappingScope) -> dict[str, JsonValue]:
    return {
        "source_key": scope.source_key,
        "source_instance_id": scope.source_instance_id,
        "control_instance_id": scope.control_instance_id,
    }


def _boundary_payload(boundary: _ExpectedHeadBoundary) -> dict[str, JsonValue]:
    expected = boundary.expected_head
    return {
        "head_id": boundary.head_id,
        "present": expected is not None,
        "active_revision_id": None if expected is None else expected.active_revision_id,
        "active_revision_number": None if expected is None else expected.active_revision_number,
        "active_manifest_digest": None if expected is None else expected.active_manifest_digest,
    }


def _authorization_payload(authorization: CrmTenantMappingAuthorization) -> dict[str, JsonValue]:
    return {
        "actor": authorization.actor,
        "authorization_reference": authorization.authorization_reference,
        "authorization_digest": authorization.authorization_digest,
        "authorized_at": authorization.authorized_at,
        "expires_at": authorization.expires_at,
    }


def _rejection_payload(actor: str, rejection_reference: str, reason: str) -> dict[str, JsonValue]:
    return {
        "actor": actor,
        "rejection_reference": rejection_reference,
        "reason": reason,
    }


def _digest(namespace: str, payload: list[JsonValue]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return (
        "sha256:"
        + hashlib.sha256(namespace.encode("utf-8") + b"\x00" + encoded.encode("utf-8")).hexdigest()
    )


def _require_sha256(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{field} must be a canonical sha256 digest")


def _bounded(value: str, field: str, maximum: int) -> str:
    text = _text(value, field)
    if len(text) > maximum:
        raise ValueError(f"{field} exceeds its bounded maximum length")
    return text


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
