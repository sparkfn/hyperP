"""Spawn-safe orchestration for manifest-gated CRM activity cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

from intelligence.crm.activities.cleanup.admission import admit
from intelligence.crm.activities.cleanup.command_evidence import (
    _authorization,
    _bind_plan_to_receipt,
    _create_receipt,
    _live_identities,
    _receipt,
    _receipt_identities,
    _require_receipt_identities,
)
from intelligence.crm.activities.cleanup.command_execution import (
    _execute,
    _run_id,
    _verify,
    _write,
)
from intelligence.crm.activities.cleanup.config import CleanupConfig
from intelligence.crm.activities.cleanup.models import CleanupRequest
from intelligence.crm.activities.cleanup.planning import BoundedCleanupRepository, plan_cleanup
from intelligence.crm.activities.cleanup.receipt import CleanupReceipt, receipt_relative_path
from intelligence.registry import Cancelled, RegisteredCommand, Registry
from intelligence.repositories.neo4j.crm_activity_cleanup import Neo4jCrmActivityCleanupRepository
from intelligence.repositories.protocols.crm_activity_cleanup import (
    CleanupPlan,
    CrmActivityCleanupRepository,
)
from intelligence.state import State

Operation = Literal["dry-run", "execute", "resume", "verify"]


@dataclass(frozen=True)
class _WorkspaceConfig:
    workspace: Path


@dataclass(frozen=True)
class _AdmissionRuntime:
    state: State
    config: _WorkspaceConfig


def registry(
    operation: Operation,
    request: CleanupRequest,
    config: CleanupConfig,
    *,
    receipt_run_id: str | None = None,
    receipt_digest: str | None = None,
    cleanup_run_id: str | None = None,
) -> Registry:
    """Register one picklable request-scoped handler; production remains empty."""
    handler = partial(
        _handler,
        operation,
        request,
        config,
        receipt_run_id,
        receipt_digest,
        cleanup_run_id,
    )
    command = RegisteredCommand(
        "crm_activities_cleanup_" + operation.replace("-", "_"),
        True,
        handler,
        {"domain": "crm_activities_cleanup", "operation": operation},
    )
    return Registry((command,))


def _handler(
    operation: Operation,
    request: CleanupRequest,
    config: CleanupConfig,
    receipt_run_id: str | None,
    receipt_digest: str | None,
    cleanup_run_id: str | None,
    staging: Path,
    cancelled: Cancelled,
) -> None:
    state = State(staging.parent.parent)
    repository: CrmActivityCleanupRepository = Neo4jCrmActivityCleanupRepository(
        config.neo4j_uri,
        config.neo4j_user,
        config.neo4j_password,
        config.neo4j_database,
    )
    try:
        _run(
            operation,
            request,
            config,
            state,
            repository,
            staging,
            cancelled,
            receipt_run_id,
            receipt_digest,
            cleanup_run_id,
        )
    finally:
        repository.close()
        state.close()


def _run(
    operation: Operation,
    request: CleanupRequest,
    config: CleanupConfig,
    state: State,
    repository: CrmActivityCleanupRepository,
    staging: Path,
    cancelled: Cancelled,
    receipt_run_id: str | None,
    receipt_digest: str | None,
    cleanup_run_id: str | None,
) -> CleanupReceipt | None:
    bounded_repository = BoundedCleanupRepository(repository)
    if operation in {"execute", "resume"} and not config.enabled:
        raise RuntimeError("CRM activity cleanup execution is disabled")
    admitted = admit(_AdmissionRuntime(state, _WorkspaceConfig(state.workspace)), request)
    authorization = _authorization(admitted)
    from intelligence.crm.activities.cleanup.types import CleanupTarget

    target = CleanupTarget(
        config.environment_id,
        request.target.environment_id,
        request.target.database_identity,
        bounded_repository.database_identity(),
    )
    planning = plan_cleanup(
        _live_identities(admitted),
        bounded_repository,
        min(config.archive.max_rows, admitted.descriptor.request.max_rows),
    )
    identities = planning.identities
    inspections = planning.inspections
    plan: CleanupPlan = planning.plan
    run_id = _run_id(cleanup_run_id)
    if operation == "dry-run":
        receipt = _create_receipt(
            run_id,
            authorization,
            target,
            request.batch_size,
            config,
            admitted,
            plan,
            inspections,
        )
        _write(staging, receipt_relative_path(staging.name), receipt.as_dict())
        from intelligence.crm.activities.cleanup import checkpoints

        root = checkpoints.checkpoint_root(state.workspace, run_id)
        if (root / "binding.json").exists():
            checkpoints.load(root, run_id, receipt)
        else:
            checkpoints.initialize(root, run_id, receipt)
        return receipt
    receipt = _receipt(
        state,
        receipt_run_id,
        receipt_digest,
        run_id,
        authorization,
        target,
        request,
    )
    _require_receipt_identities(receipt, identities)
    from intelligence.crm.activities.cleanup import checkpoints

    root = checkpoints.checkpoint_root(state.workspace, run_id, create=False)
    checkpoint = checkpoints.recover_durable_results(root, checkpoints.load(root, run_id, receipt))
    durable = dict(checkpoints.durable_outcomes(root, checkpoint).outcomes)
    processed_successful = frozenset(
        item.source_record_pk
        for item in receipt.identities[: checkpoint.cursor]
        if durable.get(item.source_record_pk) in {"deleted", "already_absent"}
    )
    successful_calls = frozenset(
        item.source_record_pk
        for item in receipt.identities[: checkpoint.cursor]
        if item.record_type == "call" and item.source_record_pk in processed_successful
    )
    current_identities = _receipt_identities(
        admitted,
        plan,
        inspections,
        receipt.authorized_companion_relationships,
        successful_calls,
    )
    plan = _bind_plan_to_receipt(receipt, current_identities, plan, processed_successful)
    if operation == "verify":
        _verify(receipt, run_id, state.workspace, bounded_repository, staging)
    else:
        _execute(
            receipt,
            run_id,
            state.workspace,
            bounded_repository,
            identities,
            plan,
            staging.name,
            cancelled,
        )
    return None
