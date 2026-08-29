"""Application-service tests for bounded CRM tenant projection materialization."""

from __future__ import annotations

from dataclasses import replace

import pytest
from _standalone_crm_lane_a_fakes import prepared_mapping_revision, projection_scope
from src.crm_tenant_mapping_identity import mapping_head_id
from src.crm_tenant_mapping_models import (
    CrmTenantMappingConflictError,
    CrmTenantMappingExpectedHeadBoundary,
    CrmTenantMappingIntegrityError,
)
from src.crm_tenant_projection_materializer import CrmTenantProjectionMaterializer
from src.crm_tenant_projection_models import (
    CrmTenantProjectionCancelledError,
    CrmTenantProjectionConflictError,
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionMaterializationCommand,
    CrmTenantProjectionReleaseSummary,
)

_DIGEST = "sha256:" + "a" * 64


class _MappingReader:
    def __init__(self) -> None:
        self.calls = 0

    def require_prepared_for_materialization(self, *args: object) -> object:
        self.calls += 1
        return object()


class _Repository:
    def __init__(self, release: CrmTenantProjectionReleaseSummary) -> None:
        self.release = release
        self.calls: list[str] = []
        self.failure_codes: list[str] = []

    def allocate_or_replay(self, command: object) -> CrmTenantProjectionReleaseSummary:
        self.calls.append("allocate")
        return self.release

    def capture_page(self, *args: object) -> CrmTenantProjectionReleaseSummary:
        self.calls.append("capture")
        self.release = replace(self.release, phase="projection")
        return self.release

    def project_page(self, *args: object) -> CrmTenantProjectionReleaseSummary:
        self.calls.append("project")
        self.release = replace(self.release, phase="complete")
        return self.release

    def complete(self, *args: object) -> CrmTenantProjectionReleaseSummary:
        self.calls.append("complete")
        self.release = replace(self.release, state="completed")
        return self.release

    def cancel(self, *args: object) -> CrmTenantProjectionReleaseSummary:
        return replace(self.release, state="cancelled")

    def fail(
        self, _release_id: str, _release_fingerprint: str, failure_code: str
    ) -> CrmTenantProjectionReleaseSummary:
        self.calls.append("fail")
        self.failure_codes.append(failure_code)
        return replace(self.release, state="failed")

    def get_completed(self, *args: object) -> None:
        return None


def _command() -> CrmTenantProjectionMaterializationCommand:
    scope = projection_scope()
    prepared = prepared_mapping_revision()
    return CrmTenantProjectionMaterializationCommand(
        scope,
        "request-a",
        "census-a",
        _DIGEST,
        prepared.revision_id,
        prepared.manifest_digest,
        CrmTenantMappingExpectedHeadBoundary(
            scope.mapping_scope, mapping_head_id(scope.mapping_scope), None
        ),
        None,
        5,
    )


def _release() -> CrmTenantProjectionReleaseSummary:
    command = _command()
    return CrmTenantProjectionReleaseSummary(
        command.scope,
        "release-a",
        1,
        command.request_id,
        command.release_fingerprint,
        command.source_census_id,
        command.mapping_revision_id,
        command.mapping_manifest_digest,
        "building",
        "capture",
        None,
        None,
        0,
        0,
        0,
        0,
    )


def test_materializer_drives_capture_projection_and_completion() -> None:
    reader = _MappingReader()
    repository = _Repository(_release())

    result = CrmTenantProjectionMaterializer(repository, reader).materialize(_command())

    assert result.state == "completed"
    assert reader.calls == 1
    assert repository.calls == ["allocate", "capture", "project", "complete"]


def test_terminal_exact_replay_is_read_only() -> None:
    reader = _MappingReader()
    repository = _Repository(replace(_release(), state="completed", phase="complete"))

    assert (
        CrmTenantProjectionMaterializer(repository, reader).materialize(_command()).state
        == "completed"
    )
    assert repository.calls == ["allocate"]
    assert reader.calls == 0


@pytest.mark.parametrize("state", ("failed", "cancelled"))
def test_terminal_failed_or_cancelled_exact_replay_skips_mapping_reader(state: str) -> None:
    reader = _MappingReader()
    failure_code = "boundary_conflict" if state == "failed" else None
    repository = _Repository(
        replace(_release(), state=state, phase="complete", failure_code=failure_code)
    )

    result = CrmTenantProjectionMaterializer(repository, reader).materialize(_command())

    assert result.state == state
    assert repository.calls == ["allocate"]
    assert reader.calls == 0


def test_materializer_marks_unexpected_failure_and_preserves_cancelled_error() -> None:
    reader = _MappingReader()

    class _BrokenRepository(_Repository):
        def capture_page(self, *args: object) -> CrmTenantProjectionReleaseSummary:
            raise RuntimeError("broken")

    broken = _BrokenRepository(_release())
    with pytest.raises(RuntimeError, match="broken"):
        CrmTenantProjectionMaterializer(broken, reader).materialize(_command())
    assert broken.calls == ["allocate", "fail"]
    assert broken.failure_codes == ["materialization_error"]

    class _CancelledRepository(_Repository):
        def capture_page(self, *args: object) -> CrmTenantProjectionReleaseSummary:
            return replace(self.release, state="cancelled")

    with pytest.raises(CrmTenantProjectionCancelledError):
        CrmTenantProjectionMaterializer(_CancelledRepository(_release()), reader).materialize(
            _command()
        )


@pytest.mark.parametrize(
    ("error", "failure_code"),
    (
        (CrmTenantProjectionConflictError("boundary"), "boundary_conflict"),
        (CrmTenantProjectionIntegrityError("integrity"), "integrity_error"),
    ),
)
def test_materializer_preserves_expected_failure_classification(
    error: Exception, failure_code: str
) -> None:
    class _BrokenRepository(_Repository):
        def capture_page(self, *args: object) -> CrmTenantProjectionReleaseSummary:
            raise error

    repository = _BrokenRepository(_release())
    with pytest.raises(type(error)):
        CrmTenantProjectionMaterializer(repository, _MappingReader()).materialize(_command())

    assert repository.failure_codes == [failure_code]


@pytest.mark.parametrize(
    ("error", "failure_code"),
    (
        (CrmTenantMappingConflictError("mapping boundary"), "boundary_conflict"),
        (CrmTenantMappingIntegrityError("mapping integrity"), "integrity_error"),
    ),
)
def test_materializer_classifies_mapping_reader_failure(
    error: Exception, failure_code: str
) -> None:
    class _BrokenMappingReader(_MappingReader):
        def require_prepared_for_materialization(self, *args: object) -> object:
            raise error

    repository = _Repository(_release())
    with pytest.raises(type(error)):
        CrmTenantProjectionMaterializer(repository, _BrokenMappingReader()).materialize(_command())

    assert repository.failure_codes == [failure_code]
