"""Nested CLI adapter for manifest-gated CRM activity cleanup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal, cast

from intelligence.crm.activities.cleanup.models import (
    CleanupAuthorization,
    CleanupRequest,
    CleanupTarget,
)
from intelligence.crm.activities.cleanup.status import read_status


def add_parser(parent: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    cleanup = parent.add_parser("cleanup")
    actions = cleanup.add_subparsers(dest="crm_activity_cleanup_command", required=True)
    dry_run = actions.add_parser("dry-run")
    _authorization_and_target(dry_run)
    dry_run.add_argument("--cleanup-run-id", required=True)
    dry_run.add_argument("--batch-size", required=True, type=int)
    dry_run.add_argument("--quiescence-run-id", required=True)
    dry_run.add_argument("--quiescence-digest", required=True)
    for name in ("execute", "resume", "verify"):
        command = actions.add_parser(name)
        _authorization_and_target(command)
        command.add_argument("--receipt-run-id", required=True)
        command.add_argument("--receipt-digest", required=True)
        command.add_argument("--cleanup-run-id", required=True)
        command.add_argument("--quiescence-run-id", required=True)
        command.add_argument("--quiescence-digest", required=True)
    actions.add_parser("status").add_argument("--cleanup-run-id", required=True)
    evidence = actions.add_parser("quiescence-register")
    _authorization_and_target(evidence)
    evidence.add_argument("--source-key", required=True)
    evidence.add_argument("--source-instance-id", required=True)
    evidence.add_argument("--cleanup-identity-digest", required=True)
    evidence.add_argument("--boundary-digest", required=True)


def _authorization_and_target(command: argparse.ArgumentParser) -> None:
    command.add_argument("--checkpoint-id", required=True)
    command.add_argument("--accepted-run-id", required=True)
    command.add_argument("--snapshot-id", required=True)
    command.add_argument("--manifest-digest", required=True)
    command.add_argument("--environment-id", required=True)
    command.add_argument("--database-identity", required=True)


def main(arguments: argparse.Namespace) -> int:
    operation = str(arguments.crm_activity_cleanup_command)
    if operation == "status":
        print(
            json.dumps(
                read_status(_workspace(), arguments.cleanup_run_id).as_dict(), sort_keys=True
            )
        )
        return 0
    if operation == "quiescence-register":
        return _register_quiescence(arguments)
    return _run(cast(Literal["dry-run", "execute", "resume", "verify"], operation), arguments)


def _run(
    operation: Literal["dry-run", "execute", "resume", "verify"], arguments: argparse.Namespace
) -> int:
    from intelligence.config import RuntimeConfig
    from intelligence.crm.activities.cleanup.commands import registry
    from intelligence.crm.activities.cleanup.config import CleanupConfig
    from intelligence.crm.activities.cleanup.receipt import admit_published_receipt
    from intelligence.runtime import IntelligenceRuntime
    from intelligence.state import State

    config = RuntimeConfig.from_environment()
    if not config.mutations_enabled:
        raise RuntimeError("mutating execution is disabled")
    cleanup = CleanupConfig.from_environment()
    receipt_run_id: str | None = None
    receipt_digest: str | None = None
    quiescence_run_id = arguments.quiescence_run_id
    quiescence_digest = arguments.quiescence_digest
    if operation == "dry-run":
        batch_size = arguments.batch_size
    else:
        receipt_run_id = arguments.receipt_run_id
        receipt_digest = arguments.receipt_digest
        state = State(config.workspace)
        try:
            batch_size = admit_published_receipt(
                config.workspace, state, receipt_run_id, receipt_digest
            ).batch_size
        finally:
            state.close()
    request = CleanupRequest(
        CleanupAuthorization(
            arguments.checkpoint_id,
            arguments.accepted_run_id,
            arguments.snapshot_id,
            arguments.manifest_digest,
        ),
        CleanupTarget(arguments.environment_id, arguments.database_identity),
        batch_size,
    )
    runtime = IntelligenceRuntime(
        config,
        registry(
            operation,
            request,
            cleanup,
            receipt_run_id=receipt_run_id,
            receipt_digest=receipt_digest,
            cleanup_run_id=arguments.cleanup_run_id,
            quiescence_run_id=quiescence_run_id,
            quiescence_digest=quiescence_digest,
        ),
    )
    try:
        run_id = runtime.run("crm_activities_cleanup_" + operation.replace("-", "_"))
        print(json.dumps({"run_id": run_id, "operation": operation}, sort_keys=True))
        return 0
    finally:
        runtime.close()


def _workspace() -> Path:
    from os import environ

    return Path(environ.get("INTELLIGENCE_WORKSPACE", "/var/lib/intelligence"))


def _register_quiescence(arguments: argparse.Namespace) -> int:
    from intelligence.config import RuntimeConfig
    from intelligence.crm.activities.cleanup.quiescence import registration_registry
    from intelligence.crm.activities.cleanup.types import QuiescenceEvidence
    from intelligence.runtime import IntelligenceRuntime

    config = RuntimeConfig.from_environment()
    if not config.mutations_enabled:
        raise RuntimeError("mutating execution is disabled")
    evidence = QuiescenceEvidence.create(
        "quiescence-template",
        arguments.accepted_run_id,
        arguments.checkpoint_id,
        arguments.snapshot_id,
        arguments.manifest_digest,
        arguments.cleanup_identity_digest,
        arguments.source_key,
        arguments.source_instance_id,
        arguments.environment_id,
        arguments.database_identity,
        arguments.boundary_digest,
    )
    runtime = IntelligenceRuntime(config, registration_registry(evidence))
    try:
        run_id = runtime.run("crm_activities_cleanup_quiescence")
        from intelligence.crm.activities.cleanup.quiescence import admit_published_quiescence

        admitted = admit_published_quiescence(
            config.workspace, runtime.state, run_id, _published_digest(config.workspace, run_id)
        )
        print(
            json.dumps(
                {"run_id": run_id, "evidence_digest": admitted.evidence_digest}, sort_keys=True
            )
        )
        return 0
    finally:
        runtime.close()


def _published_digest(workspace: Path, run_id: str) -> str:
    from intelligence.crm.activities.bounded import ReadBudget, ReadLimits, read_published_evidence
    from intelligence.crm.activities.cleanup.quiescence import quiescence_relative_path
    from intelligence.crm.activities.cleanup.types import QuiescenceEvidence

    value, _ = read_published_evidence(
        workspace,
        run_id,
        quiescence_relative_path(run_id),
        ReadBudget(ReadLimits(2_000_000, 64, 1)),
    )
    return QuiescenceEvidence.parse(value).evidence_digest
