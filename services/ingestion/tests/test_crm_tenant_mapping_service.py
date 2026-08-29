"""Replay-aware authorization coverage for immutable mapping lifecycle services."""

from __future__ import annotations

from dataclasses import replace

import pytest
from src.crm_tenant_mapping_authorization import UnavailableCrmTenantMappingAuthorizer
from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingAuthorization,
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingEntry,
    CrmTenantMappingEntryTarget,
    CrmTenantMappingExpectedHead,
    CrmTenantMappingManifest,
    CrmTenantMappingRevision,
    CrmTenantMappingScope,
    CrmTenantMappingTarget,
)
from src.crm_tenant_mapping_models import (
    CrmTenantMappingAuthorizationError,
    CrmTenantMappingConflictError,
    CrmTenantMappingExpectedHeadBoundary,
    CrmTenantMappingPrepareCommand,
    CrmTenantMappingRejectCommand,
    CrmTenantMappingRejection,
    CrmTenantMappingRevisionSnapshot,
    CrmTenantMappingRollbackCommand,
    authorization_request_for_rejection,
    mapping_head_id,
    mapping_revision_id,
)
from src.crm_tenant_mapping_service import CrmTenantMappingService

_DIGEST = "sha256:" + "a" * 64
_NOW = "2026-08-29T12:00:00Z"


class _Authorizer:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def authorize(self, request: object) -> None:
        self.requests.append(request)


class _DenyAuthorizer:
    def authorize(self, _request: object) -> None:
        raise CrmTenantMappingAuthorizationError("target denied")


class _Repository:
    def __init__(self) -> None:
        self.by_request: dict[str, CrmTenantMappingRevisionSnapshot] = {}
        self.by_revision: dict[str, CrmTenantMappingRevisionSnapshot] = {}
        self.prepare_calls = 0
        self.rollback_calls = 0
        self.reject_calls = 0

    def find_by_preparation_request(
        self, _scope: CrmTenantMappingScope, preparation_request_id: str
    ) -> CrmTenantMappingRevisionSnapshot | None:
        return self.by_request.get(preparation_request_id)

    def get_revision(
        self, _scope: CrmTenantMappingScope, revision_id: str, _manifest_digest: str
    ) -> CrmTenantMappingRevisionSnapshot | None:
        return self.by_revision.get(revision_id)

    def get_active_head(self, _scope: CrmTenantMappingScope) -> None:
        return None

    def get_active_revision(self, _scope: CrmTenantMappingScope) -> None:
        return None

    def prepare(self, command: CrmTenantMappingPrepareCommand) -> CrmTenantMappingRevisionSnapshot:
        self.prepare_calls += 1
        snapshot = _snapshot(command, 1, "prepared")
        self.by_request[command.preparation_request_id] = snapshot
        self.by_revision[snapshot.revision.revision_id] = snapshot
        return snapshot

    def rollback(
        self, command: CrmTenantMappingRollbackCommand
    ) -> CrmTenantMappingRevisionSnapshot:
        self.rollback_calls += 1
        historical = self.by_revision[command.rollback_of_revision_id]
        snapshot = _snapshot(command, 3, "prepared", historical.manifest)
        self.by_request[command.preparation_request_id] = snapshot
        self.by_revision[snapshot.revision.revision_id] = snapshot
        return snapshot

    def reject(self, command: CrmTenantMappingRejectCommand) -> CrmTenantMappingRevisionSnapshot:
        self.reject_calls += 1
        existing = self.by_revision[command.revision_id]
        if existing.revision.state == "rejected":
            if existing.rejection_request_fingerprint == command.request_fingerprint:
                return existing
            raise CrmTenantMappingConflictError("mapping rejection metadata conflicts")
        revision = replace(existing.revision, state="rejected")
        snapshot = replace(
            existing,
            revision=revision,
            rejection=command.rejection,
            rejected_at=command.operation_time,
            rejection_authorization=command.authorization,
            rejection_request_fingerprint=command.request_fingerprint,
        )
        self.by_revision[command.revision_id] = snapshot
        self.by_request[revision.preparation_request_id] = snapshot
        return snapshot


def _scope() -> CrmTenantMappingScope:
    return CrmTenantMappingScope("bitrix_chat", "portal-a", "control-a")


def _authorization(expires_at: str = "2026-08-30T00:00:00Z") -> CrmTenantMappingAuthorization:
    return CrmTenantMappingAuthorization(
        "reviewer", "approval", _DIGEST, "2026-08-29T00:00:00Z", expires_at
    )


def _manifest() -> CrmTenantMappingManifest:
    return CrmTenantMappingManifest(
        _scope(), (CrmTenantMappingCompanyEntry("10", (CrmTenantMappingTarget("entity-a"),)),)
    )


def _prepare(request_id: str = "prepare-a") -> CrmTenantMappingPrepareCommand:
    scope = _scope()
    return CrmTenantMappingPrepareCommand(
        scope,
        request_id,
        _manifest(),
        CrmTenantMappingExpectedHeadBoundary(scope, mapping_head_id(scope), None),
        _authorization(),
        "2026-08-29T01:00:00Z",
    )


def _snapshot(
    command: CrmTenantMappingPrepareCommand | CrmTenantMappingRollbackCommand,
    number: int,
    state: str,
    manifest: CrmTenantMappingManifest | None = None,
) -> CrmTenantMappingRevisionSnapshot:
    effective_manifest = (
        command.manifest if isinstance(command, CrmTenantMappingPrepareCommand) else manifest
    )
    assert effective_manifest is not None
    revision_id = mapping_revision_id(command.scope, number)
    entries = tuple(
        CrmTenantMappingEntry(revision_id, entry) for entry in effective_manifest.entries
    )
    targets = tuple(
        CrmTenantMappingEntryTarget(entry, target)
        for entry in entries
        for target in entry.company_entry.targets
    )
    revision = CrmTenantMappingRevision(
        command.scope,
        revision_id,
        number,
        effective_manifest.digest,
        len(entries),
        len(targets),
        command.preparation_request_id,
        command.authorization,
        state,  # type: ignore[arg-type]
    )
    return CrmTenantMappingRevisionSnapshot(
        revision,
        effective_manifest,
        command.expected_head_boundary,
        entries,
        targets,
        command.operation_time,
        command.request_fingerprint,
    )


def test_prepare_exact_replay_precedes_expired_authorization() -> None:
    repository = _Repository()
    authorizer = _Authorizer()
    command = replace(_prepare(), authorization=_authorization("2026-08-29T02:00:00Z"))
    service = CrmTenantMappingService(repository, authorizer, lambda: "2026-08-29T01:00:00Z")

    first = service.prepare(command)
    expired_service = CrmTenantMappingService(repository, authorizer, lambda: _NOW)

    assert expired_service.prepare(command) == first
    assert repository.prepare_calls == 1
    assert len(authorizer.requests) == 1


def test_prepare_conflicting_reuse_and_unavailable_authorizer_fail_closed() -> None:
    repository = _Repository()
    authorizer = _Authorizer()
    service = CrmTenantMappingService(repository, authorizer, lambda: _NOW)
    command = _prepare()
    service.prepare(command)
    conflict = replace(command, manifest=CrmTenantMappingManifest(_scope(), ()))

    with pytest.raises(CrmTenantMappingConflictError, match="reused"):
        service.prepare(conflict)
    with pytest.raises(CrmTenantMappingAuthorizationError, match="unavailable"):
        CrmTenantMappingService(
            repository, UnavailableCrmTenantMappingAuthorizer(), lambda: _NOW
        ).prepare(_prepare("prepare-b"))
    with pytest.raises(CrmTenantMappingAuthorizationError, match="target denied"):
        CrmTenantMappingService(repository, _DenyAuthorizer(), lambda: _NOW).prepare(
            _prepare("prepare-c")
        )
    assert repository.prepare_calls == 1


def test_rollback_requires_effective_lower_revision_and_rejection_is_exact_idempotent() -> None:
    repository = _Repository()
    authorizer = _Authorizer()
    service = CrmTenantMappingService(repository, authorizer, lambda: _NOW)
    active = _snapshot(_prepare("effective"), 1, "active")
    repository.by_revision[active.revision.revision_id] = active
    expected = CrmTenantMappingExpectedHead(
        mapping_head_id(_scope()), mapping_revision_id(_scope(), 2), 2, _DIGEST
    )
    rollback = CrmTenantMappingRollbackCommand(
        _scope(),
        "rollback-a",
        active.revision.revision_id,
        active.revision.manifest_digest,
        CrmTenantMappingExpectedHeadBoundary(_scope(), mapping_head_id(_scope()), expected),
        _authorization(),
        "2026-08-29T01:00:00Z",
    )

    prepared = service.rollback(rollback)
    assert prepared.revision.state == "prepared"
    assert prepared.manifest == active.manifest
    assert repository.rollback_calls == 1

    rejection = CrmTenantMappingRejectCommand(
        prepared.revision.scope,
        prepared.revision.revision_id,
        prepared.revision.manifest_digest,
        CrmTenantMappingRejection("reviewer", "case", "bad"),
        _authorization(),
        "2026-08-29T01:00:00Z",
    )
    rejected = service.reject(rejection)
    expired_service = CrmTenantMappingService(
        repository, authorizer, lambda: "2026-08-31T00:00:00Z"
    )
    assert expired_service.reject(rejection) == rejected
    assert repository.reject_calls == 1
    conflicting_rejection = replace(
        rejection,
        authorization=CrmTenantMappingAuthorization(
            "reviewer",
            "other-approval",
            _DIGEST,
            "2026-08-29T00:00:00Z",
            "2026-08-30T00:00:00Z",
        ),
    )
    with pytest.raises(CrmTenantMappingConflictError):
        service.reject(conflicting_rejection)


def test_rejection_authorization_binds_exact_snapshot_identity_and_targets() -> None:
    command = CrmTenantMappingRejectCommand(
        _scope(),
        mapping_revision_id(_scope(), 1),
        _manifest().digest,
        CrmTenantMappingRejection("reviewer", "case", "bad"),
        _authorization(),
        "2026-08-29T01:00:00Z",
    )
    first = _snapshot(_prepare("same-digest-a"), 1, "prepared")
    second = _snapshot(_prepare("same-digest-b"), 2, "prepared")
    first_request = authorization_request_for_rejection(command, first)
    second_command = replace(command, revision_id=second.revision.revision_id)
    second_request = authorization_request_for_rejection(second_command, second)

    assert first_request.revision_id == first.revision.revision_id
    assert second_request.revision_id == second.revision.revision_id
    assert first_request.revision_id != second_request.revision_id
    assert first_request.manifest_digest == second_request.manifest_digest
    assert first_request.target_entity_keys == ("entity-a",)
