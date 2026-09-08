"""Nested CLI adapter for the CRM activity archive domain."""

from __future__ import annotations

import argparse
import json
from typing import Protocol, cast

from intelligence.config import RuntimeConfig
from intelligence.crm.activities.acceptance import (
    accepted_publication,
    publication,
    verification,
)
from intelligence.crm.activities.bounded import ReadLimits
from intelligence.crm.activities.checkpoint_limits import CheckpointLimits
from intelligence.crm.activities.commands import (
    Operation,
    registry,
    request_from_config,
    verification_registry,
)
from intelligence.crm.activities.config import CrmActivitiesConfig
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
    """Run one fixed CRM activities command."""
    command = str(arguments.crm_activities_command)
    config = RuntimeConfig.from_environment()
    if command == "status":
        print(json.dumps(_status(config.workspace, arguments.checkpoint_id), sort_keys=True))
        return 0
    if not config.mutations_enabled:
        raise RuntimeError("mutating execution is disabled")
    if command == "verify":
        return _run_verification(config, arguments.checkpoint_id, arguments.accepted_run_id)
    archive_config = CrmActivitiesConfig.from_environment()
    request = request_from_config(arguments.checkpoint_id, archive_config)
    runtime = IntelligenceRuntime(
        config, registry(cast(Operation, command), request, archive_config)
    )
    try:
        existing = publication(runtime, request)
        if existing is not None:
            print(json.dumps(_publication_result(existing), sort_keys=True))
            return 0
        runtime.run(f"crm_activities_{command}")
        accepted = publication(runtime, request)
        if accepted is None:
            raise RuntimeError("completed archive has no accepted publication candidate")
        print(json.dumps(_publication_result(accepted), sort_keys=True))
        return 0
    finally:
        runtime.close()


def _run_verification(
    config: RuntimeConfig, checkpoint_id: str, supplied_run_id: str | None
) -> int:
    runtime = IntelligenceRuntime(config)
    try:
        selected = accepted_publication(runtime, checkpoint_id)
        if selected is None:
            raise RuntimeError("checkpoint has no accepted publication candidate")
        descriptor, pointer = selected
        accepted_run_id = descriptor.run_id
        if supplied_run_id is not None and supplied_run_id != accepted_run_id:
            raise RuntimeError("supplied accepted run does not match publication candidate")
        trusted_outputs = runtime.state.accepted_outputs(accepted_run_id)
        existing = verification(runtime, checkpoint_id)
        if existing is not None:
            print(json.dumps(_verification_result(existing), sort_keys=True))
            return 0
    finally:
        runtime.close()
    verified = IntelligenceRuntime(
        config,
        verification_registry(
            descriptor,
            pointer,
            trusted_outputs,
            CheckpointLimits(config.max_output_bytes, config.max_output_entries),
            ReadLimits(config.max_output_bytes, config.max_output_entries, 10_000),
            config.max_output_bytes,
            config.max_output_entries,
        ),
    )
    try:
        verified.run("crm_activities_verify")
        candidate = verification(verified, checkpoint_id)
        if candidate is None:
            raise RuntimeError("completed verification has no candidate")
        print(json.dumps(_verification_result(candidate), sort_keys=True))
        return 0
    finally:
        verified.close()


def _publication_result(candidate: dict[str, object]) -> dict[str, object]:
    return {
        "checkpoint_id": _text(candidate, "checkpoint_id"),
        "run_id": _text(candidate, "run_id"),
        "snapshot_id": _text(candidate, "snapshot_id"),
    }


def _verification_result(candidate: dict[str, object]) -> dict[str, object]:
    return {
        "checkpoint_id": _text(candidate, "checkpoint_id"),
        "run_id": _text(candidate, "run_id"),
        "snapshot_id": _text(candidate, "snapshot_id"),
    }


def _text(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"candidate {key} is invalid")
    return item
