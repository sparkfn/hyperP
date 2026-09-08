"""Nested CLI adapter for the CRM activity archive domain."""

from __future__ import annotations

import argparse
import json
from typing import Protocol, cast

from intelligence.config import RuntimeConfig
from intelligence.crm.activities.acceptance import (
    accepted_manifest as _accepted_manifest,
)
from intelligence.crm.activities.acceptance import (  # noqa: F401
    assert_registered_snapshot as _assert_registered_snapshot,
)
from intelligence.crm.activities.acceptance import (
    record_acceptance as _record_acceptance,
)
from intelligence.crm.activities.acceptance import (
    record_verification as _record_verification,
)
from intelligence.crm.activities.commands import (
    Operation,
    registry,
    request_from_config,
    verification_registry,
)
from intelligence.crm.activities.config import CrmActivitiesConfig
from intelligence.crm.activities.models import validate_snapshot_id
from intelligence.crm.activities.status import (
    _acceptance_from_checkpoint,
    _required_text,
)
from intelligence.crm.activities.status import (
    status as _status,
)
from intelligence.runtime import IntelligenceRuntime


class _SubparserAdder(Protocol):
    def add_parser(self, name: str) -> argparse.ArgumentParser: ...


def add_parser(parent: _SubparserAdder) -> None:
    activities = parent.add_parser("activities")
    actions = activities.add_subparsers(dest="crm_activities_command", required=True)
    for name in ("extract", "resume", "status"):
        command = actions.add_parser(name)
        command.add_argument("--snapshot-id", required=True, dest="checkpoint_id")
    verify = actions.add_parser("verify")
    verify.add_argument("--checkpoint-id", required=True)
    verify.add_argument("--accepted-run-id")


def main(arguments: argparse.Namespace) -> int:
    command = str(arguments.crm_activities_command)
    if command == "status":
        workspace = RuntimeConfig.from_environment().workspace
        print(json.dumps(_status(workspace, arguments.checkpoint_id), sort_keys=True))
        return 0
    if command == "verify":
        return _run_verification(arguments.checkpoint_id, arguments.accepted_run_id)
    config = CrmActivitiesConfig.from_environment()
    request = request_from_config(arguments.checkpoint_id, config)
    runtime = IntelligenceRuntime(
        RuntimeConfig.from_environment(),
        registry(cast(Operation, command), request, config),
    )
    try:
        run_id = runtime.run(f"crm_activities_{command}")
        acceptance = _record_acceptance(runtime, request.snapshot_id, run_id)
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "checkpoint_id": request.snapshot_id,
                    "snapshot_id": acceptance["snapshot_id"],
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        runtime.close()


def _run_verification(checkpoint_id: str, supplied_run_id: str | None) -> int:
    validate_snapshot_id(checkpoint_id)
    config = RuntimeConfig.from_environment()
    acceptance = _acceptance_from_checkpoint(config.workspace, checkpoint_id)
    snapshot_id = _required_text(acceptance, "snapshot_id")
    accepted_run_id = _required_text(acceptance, "run_id")
    if supplied_run_id is not None and supplied_run_id != accepted_run_id:
        raise RuntimeError("supplied accepted run does not match checkpoint acceptance")
    admission = _accepted_manifest(config, accepted_run_id, snapshot_id)
    digest = admission["manifest_digest"]
    if not isinstance(digest, str):
        raise RuntimeError("accepted snapshot has no manifest digest")
    runtime = IntelligenceRuntime(
        config,
        verification_registry(snapshot_id, accepted_run_id, digest),
    )
    try:
        run_id = runtime.run("crm_activities_verify")
        _record_verification(runtime, checkpoint_id, accepted_run_id, digest, run_id)
        print(
            json.dumps(
                {"checkpoint_id": checkpoint_id, "run_id": run_id, "snapshot_id": snapshot_id},
                sort_keys=True,
            )
        )
        return 0
    finally:
        runtime.close()
