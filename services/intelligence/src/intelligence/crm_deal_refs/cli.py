"""Fixed nested CLI controls for the CRM deal-reference export domain."""

from __future__ import annotations

import argparse
import json
from functools import partial
from pathlib import Path

from intelligence.crm_deal_refs.checkpoints import require_confined_directory
from intelligence.crm_deal_refs.commands import (
    ExtractRequest,
    ResumeRequest,
    accepted_snapshot_root,
    extract_handler,
    resume_handler,
)
from intelligence.crm_deal_refs.export import (
    read_boundary,
    snapshot_registered_inventory,
    snapshot_regular_sizes,
    verify_snapshot,
)
from intelligence.crm_deal_refs.export_support import validate_checkpoint_limits
from intelligence.crm_deal_refs.models import (
    MAX_PAGE_SIZE,
    MAX_RECORDS,
    canonical_digest,
    json_value,
    parse_cutoff,
    safe_identifier,
)
from intelligence.crm_deal_refs.snapshot_validation import read_checkpoint
from intelligence.registry import RegisteredCommand, Registry
from intelligence.runtime import IntelligenceRuntime


def add_crm_deal_refs_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the intentionally narrow ``crm deal-refs`` command family."""
    crm = commands.add_parser("crm")
    add_crm_deal_refs_domain_parser(crm.add_subparsers(dest="crm_domain", required=True))


def add_crm_deal_refs_domain_parser(
    crm_commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add deal-reference actions to an existing ``crm`` domain parser."""
    deal_refs = crm_commands.add_parser("deal-refs")
    actions = deal_refs.add_subparsers(dest="deal_refs_command", required=True)
    extract = actions.add_parser("extract")
    extract.add_argument("--source-instance-id", required=True)
    extract.add_argument("--as-of", required=True)
    extract.add_argument("--max-records", required=True, type=int)
    extract.add_argument("--page-size", type=int, default=100)
    actions.add_parser("resume").add_argument("run_id")
    actions.add_parser("verify").add_argument("run_id")
    actions.add_parser("status").add_argument("run_id")


def run_crm_deal_refs(arguments: argparse.Namespace, runtime: IntelligenceRuntime) -> int:
    """Execute one fixed CRM deal-reference command without generic command registration."""
    action = arguments.deal_refs_command
    if action == "extract":
        return _run_extract(arguments, runtime)
    run_id = _safe_run_id(arguments.run_id)
    if action == "resume":
        return _run_resume(runtime, run_id)
    root = accepted_snapshot_root(runtime.config.workspace, run_id)
    if action == "verify":
        result = _require_accepted_output(runtime, run_id, root)
        print(json.dumps(result, sort_keys=True))
        return 0
    return _run_status(runtime, run_id, root)


def _run_extract(arguments: argparse.Namespace, runtime: IntelligenceRuntime) -> int:
    request = _extract_request(arguments)
    command = RegisteredCommand(
        "crm_deal_refs_extract", True, partial(extract_handler, request=request), {}
    )
    print(json.dumps({"run_id": _run_with(runtime, command)}))
    return 0


def _run_resume(runtime: IntelligenceRuntime, run_id: str) -> int:
    previous = runtime.state.inspect(run_id)
    terminal = {"completed", "failed", "cancelled", "timed_out", "stale_recovered"}
    commands = {"crm_deal_refs_extract", "crm_deal_refs_resume"}
    if previous is None or previous.state not in terminal:
        raise ValueError("resume requires a terminal CRM deal-reference run")
    if previous.command not in commands:
        raise ValueError("resume requires a CRM deal-reference run")
    outputs = runtime.state.accepted_outputs(run_id)
    accepted = previous.state == "completed"
    if accepted != bool(outputs):
        raise ValueError("resume source acceptance evidence is inconsistent")
    if accepted:
        _require_accepted_output(
            runtime, run_id, accepted_snapshot_root(runtime.config.workspace, run_id)
        )
    command = RegisteredCommand(
        "crm_deal_refs_resume",
        True,
        partial(resume_handler, request=ResumeRequest(run_id, accepted)),
        {},
    )
    print(json.dumps({"run_id": _run_with(runtime, command)}))
    return 0


def _run_status(runtime: IntelligenceRuntime, run_id: str, root: Path) -> int:
    run = runtime.state.inspect(run_id)
    if run is None:
        print(json.dumps(None))
        return 0
    partial_root = runtime.config.workspace / "staging" / run_id / "snapshots" / "crm" / "deal-refs"
    accepted = _accepted_status(runtime, run_id, root)
    progress: dict[str, object] | None = None
    try:
        evidence_root = _existing_confined_root(runtime.config.workspace, root)
        if evidence_root is None:
            evidence_root = _existing_confined_root(runtime.config.workspace, partial_root)
    except ValueError:
        evidence_root = None
        progress = {"unsafe": True}
    if evidence_root is not None:
        checkpoint = evidence_root / "checkpoint.json"
        if checkpoint.is_file() and not checkpoint.is_symlink():
            try:
                boundary = read_boundary(evidence_root / "boundary.json")
                parsed = read_checkpoint(checkpoint, canonical_digest(json_value(boundary)))
                validate_checkpoint_limits(boundary, parsed)
                progress = {
                    "completed": parsed.completed,
                    "deal_records": parsed.deal_records,
                    "identity_records": parsed.identity_records,
                }
            except (OSError, ValueError):
                progress = {"unsafe": True}
    print(
        json.dumps(
            {
                "accepted": accepted,
                "command": run.command,
                "progress": progress,
                "run_id": run.run_id,
                "state": run.state,
            },
            sort_keys=True,
        )
    )
    return 0


def _extract_request(arguments: argparse.Namespace) -> ExtractRequest:
    source_instance_id = _safe_source_instance_id(arguments.source_instance_id)
    max_records = arguments.max_records
    page_size = arguments.page_size
    if (
        not isinstance(max_records, int)
        or isinstance(max_records, bool)
        or max_records < 1
        or max_records > MAX_RECORDS
    ):
        raise ValueError(f"max-records must be between 1 and {MAX_RECORDS}")
    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or page_size < 1
        or page_size > MAX_PAGE_SIZE
    ):
        raise ValueError(f"page-size must be between 1 and {MAX_PAGE_SIZE}")
    return ExtractRequest(source_instance_id, parse_cutoff(arguments.as_of), max_records, page_size)


def _run_with(runtime: IntelligenceRuntime, command: RegisteredCommand) -> str:
    registry = Registry((command,))
    scoped = IntelligenceRuntime(runtime.config, registry)
    try:
        return scoped.run(command.name)
    finally:
        scoped.close()


def _require_accepted_output(
    runtime: IntelligenceRuntime, run_id: str, root: Path
) -> dict[str, object]:
    run = runtime.state.inspect(run_id)
    outputs = runtime.state.accepted_outputs(run_id)
    if run is None or run.state != "completed" or not outputs:
        raise ValueError("verify requires an accepted CRM deal-reference output")
    if run.command not in {"crm_deal_refs_extract", "crm_deal_refs_resume"}:
        raise ValueError("verify requires a CRM deal-reference command")
    require_confined_directory(runtime.config.workspace, root)
    prefix = f"outputs/{run_id}/snapshots/crm/deal-refs/"
    registered_sizes = {(item.relative_path, item.byte_count) for item in outputs}
    actual_sizes = {(f"{prefix}{path}", size) for path, size in snapshot_regular_sizes(root)}
    if len(registered_sizes) != len(outputs) or actual_sizes != registered_sizes:
        raise ValueError("accepted CRM deal-reference inventory does not match output")
    if any(not item.relative_path.startswith(prefix) for item in outputs):
        raise ValueError("accepted CRM deal-reference inventory path is invalid")
    expected = tuple(sorted((path.removeprefix(prefix), size) for path, size in registered_sizes))
    actual = {
        (f"{prefix}{path}", digest, size)
        for path, digest, size in snapshot_registered_inventory(root, expected)
    }
    registered = {(item.relative_path, item.sha256, item.byte_count) for item in outputs}
    if actual != registered:
        raise ValueError("accepted CRM deal-reference inventory does not match output")
    return verify_snapshot(root)


def _safe_run_id(value: str) -> str:
    return safe_identifier(value, "run identifier")


def _safe_source_instance_id(value: object) -> str:
    return safe_identifier(value, "source-instance-id")


def _accepted_status(runtime: IntelligenceRuntime, run_id: str, root: Path) -> bool:
    try:
        _require_accepted_output(runtime, run_id, root)
    except (OSError, ValueError):
        return False
    return True


def _existing_confined_root(workspace: Path, root: Path) -> Path | None:
    require_confined_directory(workspace, root, allow_missing=True)
    try:
        root.lstat()
    except FileNotFoundError:
        return None
    return require_confined_directory(workspace, root)
