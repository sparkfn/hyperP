"""Synchronous orchestration-free application service for projection materialization."""

from __future__ import annotations

from src.crm_tenant_mapping_models import (
    CrmTenantMappingConflictError,
    CrmTenantMappingIntegrityError,
)
from src.crm_tenant_mapping_repository import CrmTenantMappingMaterializationReader
from src.crm_tenant_projection_models import (
    CrmTenantProjectionCancelledError,
    CrmTenantProjectionConflictError,
    CrmTenantProjectionCursor,
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
                next_release = self._repository.capture_page(
                    release.release_id,
                    release.release_fingerprint,
                    command.page_limit,
                )
                _raise_if_cancelled(next_release)
                if next_release.terminal:
                    _require_capture_terminal_transition(release, next_release)
                    return next_release
                _require_capture_progress(release, next_release)
                release = next_release
            while release.phase == "projection":
                next_release = self._repository.project_page(
                    release.release_id,
                    release.release_fingerprint,
                    command.page_limit,
                )
                _raise_if_cancelled(next_release)
                if next_release.terminal:
                    _require_projection_terminal_transition(release, next_release)
                    return next_release
                _require_projection_progress(release, next_release)
                release = next_release
            return self._repository.complete(release.release_id, release.release_fingerprint)
        except CrmTenantProjectionCancelledError:
            raise
        except CrmTenantMappingConflictError:
            _record_failure(self._repository, release, "boundary_conflict")
            raise
        except CrmTenantMappingIntegrityError:
            _record_failure(self._repository, release, "integrity_error")
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


def _require_capture_progress(
    previous: CrmTenantProjectionReleaseSummary,
    current: CrmTenantProjectionReleaseSummary,
) -> None:
    _require_capture_not_regressed(previous, current)
    if current.phase == "capture":
        if current.input_count <= previous.input_count or not _cursor_is_after(
            previous.capture_cursor, current.capture_cursor
        ):
            raise CrmTenantProjectionIntegrityError("capture page made no monotonic progress")
        return
    if current.phase != "projection":
        raise CrmTenantProjectionIntegrityError("capture page transitioned to an invalid phase")


def _require_capture_terminal_transition(
    previous: CrmTenantProjectionReleaseSummary,
    current: CrmTenantProjectionReleaseSummary,
) -> None:
    _require_capture_not_regressed(previous, current)
    if current.state != "failed" or current.phase != "capture":
        raise CrmTenantProjectionIntegrityError(
            "capture page returned an invalid terminal transition"
        )


def _require_projection_progress(
    previous: CrmTenantProjectionReleaseSummary,
    current: CrmTenantProjectionReleaseSummary,
) -> None:
    _require_projection_not_regressed(previous, current)
    if current.phase == "projection":
        if current.decision_count <= previous.decision_count or not _cursor_is_after(
            previous.projection_cursor, current.projection_cursor
        ):
            raise CrmTenantProjectionIntegrityError("projection page made no monotonic progress")
        return
    if current.phase != "complete":
        raise CrmTenantProjectionIntegrityError("projection page transitioned to an invalid phase")


def _require_projection_terminal_transition(
    previous: CrmTenantProjectionReleaseSummary,
    current: CrmTenantProjectionReleaseSummary,
) -> None:
    _require_projection_not_regressed(previous, current)
    if current.state != "failed" or current.phase != "projection":
        raise CrmTenantProjectionIntegrityError(
            "projection page returned an invalid terminal transition"
        )


def _require_capture_not_regressed(
    previous: CrmTenantProjectionReleaseSummary,
    current: CrmTenantProjectionReleaseSummary,
) -> None:
    if current.input_count < previous.input_count or _cursor_is_before(
        previous.capture_cursor, current.capture_cursor
    ):
        raise CrmTenantProjectionIntegrityError("capture page regressed persisted progress")


def _require_projection_not_regressed(
    previous: CrmTenantProjectionReleaseSummary,
    current: CrmTenantProjectionReleaseSummary,
) -> None:
    if (
        current.decision_count < previous.decision_count
        or current.association_count < previous.association_count
        or current.support_count < previous.support_count
        or _cursor_is_before(previous.projection_cursor, current.projection_cursor)
    ):
        raise CrmTenantProjectionIntegrityError("projection page regressed persisted progress")


def _cursor_is_after(
    previous: CrmTenantProjectionCursor | None,
    current: CrmTenantProjectionCursor | None,
) -> bool:
    if current is None:
        return False
    if previous is None:
        return True
    return _cursor_key(current) > _cursor_key(previous)


def _cursor_is_before(
    previous: CrmTenantProjectionCursor | None,
    current: CrmTenantProjectionCursor | None,
) -> bool:
    if previous is None:
        return False
    if current is None:
        return True
    return _cursor_key(current) < _cursor_key(previous)


def _cursor_key(cursor: CrmTenantProjectionCursor) -> tuple[int, int]:
    return (0 if cursor.subject_kind == "contact" else 1, cursor.subject_id)


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
