"""Synchronous orchestration-free application service for projection materialization."""

from __future__ import annotations

from src.crm_tenant_mapping_repository import CrmTenantMappingMaterializationReader
from src.crm_tenant_projection_models import (
    CrmTenantProjectionCancelledError,
    CrmTenantProjectionConflictError,
    CrmTenantProjectionFailureCode,
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionMaterializationCommand,
    CrmTenantProjectionReleaseSummary,
)
from src.crm_tenant_projection_repository import CrmTenantProjectionRepository


class CrmTenantProjectionMaterializer:
    """Drive bounded capture/projection without source calls or activation."""

    def __init__(
        self,
        repository: CrmTenantProjectionRepository,
        mapping_reader: CrmTenantMappingMaterializationReader,
    ) -> None:
        self._repository = repository
        self._mapping_reader = mapping_reader

    def materialize(
        self, command: CrmTenantProjectionMaterializationCommand
    ) -> CrmTenantProjectionReleaseSummary:
        """Resume building work; terminal exact replays remain read-only and stable."""
        release = self._repository.allocate_or_replay(command)
        if release.terminal:
            return release
        try:
            self._mapping_reader.require_prepared_for_materialization(
                command.scope.mapping_scope,
                command.mapping_revision_id,
                command.mapping_manifest_digest,
                command.expected_mapping_head_boundary,
            )
            while release.phase == "capture":
                release = self._repository.capture_page(
                    release.release_id,
                    release.release_fingerprint,
                    command.page_limit,
                )
                _raise_if_cancelled(release)
                if release.terminal:
                    return release
            while release.phase == "projection":
                release = self._repository.project_page(
                    release.release_id,
                    release.release_fingerprint,
                    command.page_limit,
                )
                _raise_if_cancelled(release)
                if release.terminal:
                    return release
            return self._repository.complete(release.release_id, release.release_fingerprint)
        except CrmTenantProjectionCancelledError:
            raise
        except CrmTenantProjectionConflictError:
            _record_failure(self._repository, release, "boundary_conflict")
            raise
        except CrmTenantProjectionIntegrityError:
            _record_failure(self._repository, release, "integrity_error")
            raise
        except Exception:
            _record_failure(self._repository, release, "materialization_error")
            raise


def _raise_if_cancelled(release: CrmTenantProjectionReleaseSummary) -> None:
    if release.state == "cancelled":
        raise CrmTenantProjectionCancelledError("projection release was cancelled")


def _record_failure(
    repository: CrmTenantProjectionRepository,
    release: CrmTenantProjectionReleaseSummary,
    failure_code: CrmTenantProjectionFailureCode,
) -> None:
    """Do not replace a materialization exception if terminal failure recording fails."""
    try:
        repository.fail(release.release_id, release.release_fingerprint, failure_code)
    except Exception:
        pass
