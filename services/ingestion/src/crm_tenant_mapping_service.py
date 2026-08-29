"""Application services for authorized immutable CRM tenant mapping lifecycle commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from src.crm_tenant_mapping_authorization import (
    CrmTenantMappingAuthorizer,
    UnavailableCrmTenantMappingAuthorizer,
)
from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingAuthorization,
    CrmTenantMappingRollbackProvenance,
)
from src.crm_tenant_mapping_models import (
    CrmTenantMappingAuthorizationError,
    CrmTenantMappingConflictError,
    CrmTenantMappingPrepareCommand,
    CrmTenantMappingRejectCommand,
    CrmTenantMappingRevisionSnapshot,
    CrmTenantMappingRollbackCommand,
    authorization_is_current,
    authorization_request_for_prepare,
    authorization_request_for_rejection,
    authorization_request_for_rollback,
)
from src.crm_tenant_mapping_repository import (
    CrmTenantMappingLifecycleRepository,
)

type Clock = Callable[[], str]


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class CrmTenantMappingService:
    """Performs replay-aware authorization before handing atomic work to its repository."""

    def __init__(
        self,
        repository: CrmTenantMappingLifecycleRepository,
        authorizer: CrmTenantMappingAuthorizer | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self._repository = repository
        self._authorizer = authorizer or UnavailableCrmTenantMappingAuthorizer()
        self._clock = clock

    def prepare(self, command: CrmTenantMappingPrepareCommand) -> CrmTenantMappingRevisionSnapshot:
        """Return an exact replay or authoritatively prepare a new immutable revision."""
        existing = self._repository.find_by_preparation_request(
            command.scope, command.preparation_request_id
        )
        if existing is not None:
            _require_exact_replay(existing, command.request_fingerprint)
            return existing
        operation = replace(command, operation_time=self._clock())
        _require_current(operation.authorization, operation.operation_time)
        self._authorizer.authorize(authorization_request_for_prepare(operation))
        return self._repository.prepare(operation)

    def rollback(
        self, command: CrmTenantMappingRollbackCommand
    ) -> CrmTenantMappingRevisionSnapshot:
        """Return an exact replay or authorize a higher prepared rollback revision."""
        existing = self._repository.find_by_preparation_request(
            command.scope, command.preparation_request_id
        )
        if existing is not None:
            _require_exact_replay(existing, command.request_fingerprint)
            return existing
        historical = self._repository.get_revision(
            command.scope, command.rollback_of_revision_id, command.rollback_of_manifest_digest
        )
        if historical is None:
            raise CrmTenantMappingConflictError("rollback target revision is missing")
        if historical.revision.state not in {"active", "superseded"}:
            raise CrmTenantMappingConflictError("rollback target was never effective")
        operation = replace(command, operation_time=self._clock())
        _require_current(operation.authorization, operation.operation_time)
        provenance = CrmTenantMappingRollbackProvenance(
            historical.revision.revision_id,
            historical.revision.revision_number,
            historical.revision.manifest_digest,
        )
        self._authorizer.authorize(
            authorization_request_for_rollback(operation, historical.manifest, provenance)
        )
        return self._repository.rollback(operation)

    def reject(self, command: CrmTenantMappingRejectCommand) -> CrmTenantMappingRevisionSnapshot:
        """Reject one prepared revision, allowing only an exact persisted rejection replay."""
        existing = self._repository.get_revision(
            command.scope, command.revision_id, command.manifest_digest
        )
        if existing is None:
            raise CrmTenantMappingConflictError("mapping revision is missing")
        if (
            existing.revision.state == "rejected"
            and existing.rejection_request_fingerprint == command.request_fingerprint
        ):
            return existing
        operation = replace(command, operation_time=self._clock())
        _require_current(operation.authorization, operation.operation_time)
        self._authorizer.authorize(authorization_request_for_rejection(operation, existing))
        return self._repository.reject(operation)


def _require_exact_replay(snapshot: CrmTenantMappingRevisionSnapshot, fingerprint: str) -> None:
    if snapshot.request_fingerprint != fingerprint:
        raise CrmTenantMappingConflictError(
            "preparation request ID was reused with different immutable input"
        )


def _require_current(authorization: CrmTenantMappingAuthorization, operation_time: str) -> None:
    if not authorization_is_current(authorization, operation_time):
        raise CrmTenantMappingAuthorizationError(
            "mapping authorization is not valid at operation time"
        )
