"""Exact configured allowlist for CRM tenant mapping prepare and rollback actions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from src.crm_tenant_mapping_contracts import CrmTenantMappingExpectedHead
from src.crm_tenant_mapping_models import (
    CrmTenantMappingAuthorizationError,
    CrmTenantMappingAuthorizationRequest,
)

type ConfiguredMappingAction = Literal["prepare", "rollback"]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class CrmTenantMappingConfiguredGrant:
    """One non-reusable exact configured permission for a canonical mutation."""

    action: ConfiguredMappingAction
    source_key: str
    source_instance_id: str
    control_instance_id: str
    preparation_request_id: str
    manifest_digest: str
    target_entity_keys: tuple[str, ...]
    expected_head: CrmTenantMappingExpectedHead | None
    rollback_of_revision_id: str | None
    rollback_of_revision_number: int | None
    rollback_of_manifest_digest: str | None
    actor: str
    authorization_reference: str
    authorization_digest: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.action not in {"prepare", "rollback"}:
            raise ValueError("configured mapping grant action must be prepare or rollback")
        for field in (
            "source_key",
            "source_instance_id",
            "control_instance_id",
            "preparation_request_id",
            "manifest_digest",
            "actor",
            "authorization_reference",
            "authorization_digest",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"configured mapping grant {field} must be non-empty")
        if self.source_key != "bitrix_chat":
            raise ValueError("configured mapping grant source_key must be bitrix_chat")
        _require_sha256(self.manifest_digest, "manifest_digest")
        _require_sha256(self.authorization_digest, "authorization_digest")
        if not isinstance(self.target_entity_keys, tuple) or (
            tuple(sorted(set(self.target_entity_keys))) != self.target_entity_keys
        ):
            raise ValueError("configured mapping grant targets must be canonical")
        if self.expected_head is not None and not isinstance(
            self.expected_head, CrmTenantMappingExpectedHead
        ):
            raise ValueError("configured mapping grant expected head is invalid")
        rollback_parts = (
            self.rollback_of_revision_id,
            self.rollback_of_revision_number,
            self.rollback_of_manifest_digest,
        )
        if self.action == "rollback":
            if any(value is None for value in rollback_parts):
                raise ValueError("rollback configured grant requires complete provenance")
        elif any(value is not None for value in rollback_parts):
            raise ValueError("prepare configured grant cannot include rollback provenance")
        if self.rollback_of_revision_number is not None and (
            isinstance(self.rollback_of_revision_number, bool)
            or self.rollback_of_revision_number < 1
        ):
            raise ValueError("configured mapping grant rollback revision number must be positive")
        if self.rollback_of_manifest_digest is not None:
            _require_sha256(self.rollback_of_manifest_digest, "rollback_of_manifest_digest")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("configured mapping grant expiry must be timezone-aware")


class ConfiguredCrmTenantMappingAuthorizer:
    """Fail-closed exact matcher for statically configured one-operation grants."""

    def __init__(
        self,
        grants: tuple[CrmTenantMappingConfiguredGrant, ...],
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(grants, tuple) or any(
            not isinstance(grant, CrmTenantMappingConfiguredGrant) for grant in grants
        ):
            raise ValueError("configured mapping grants must be an immutable grant tuple")
        self._grants = grants
        self._clock = clock or (lambda: datetime.now(UTC))

    def authorize(self, request: CrmTenantMappingAuthorizationRequest) -> None:
        """Authorize only one complete exact operation, otherwise deny without fallback."""
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("configured mapping authorization clock must be timezone-aware")
        if not any(_matches(grant, request, now) for grant in self._grants):
            raise CrmTenantMappingAuthorizationError(
                "CRM tenant mapping operation has no current exact configured grant"
            )


def _matches(
    grant: CrmTenantMappingConfiguredGrant,
    request: CrmTenantMappingAuthorizationRequest,
    now: datetime,
) -> bool:
    if request.action != grant.action:
        return False
    if request.preparation_request_id != grant.preparation_request_id:
        return False
    if request.revision_id is not None or request.rejection is not None:
        return False
    scope = request.scope
    if (
        scope.source_key != grant.source_key
        or scope.source_instance_id != grant.source_instance_id
        or scope.control_instance_id != grant.control_instance_id
        or request.manifest_digest != grant.manifest_digest
        or request.target_entity_keys != grant.target_entity_keys
        or request.expected_head_boundary is None
        or request.expected_head_boundary.expected_head != grant.expected_head
    ):
        return False
    authorization = request.authorization
    if (
        authorization.actor != grant.actor
        or authorization.authorization_reference != grant.authorization_reference
        or authorization.authorization_digest != grant.authorization_digest
        or authorization.expires_at != _utc_text(grant.expires_at)
        or now > grant.expires_at
    ):
        return False
    provenance = request.rollback_provenance
    return (provenance is None and grant.action == "prepare") or (
        provenance is not None
        and provenance.rollback_of_revision_id == grant.rollback_of_revision_id
        and provenance.rollback_of_revision_number == grant.rollback_of_revision_number
        and provenance.rollback_of_manifest_digest == grant.rollback_of_manifest_digest
    )


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _require_sha256(value: str, field: str) -> None:
    if (
        len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"configured mapping grant {field} must be a canonical sha256 digest")
