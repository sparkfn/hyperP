"""Fixed dataset command adapter; no source connections or generic executable input."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Protocol

from intelligence.config import RuntimeConfig
from intelligence.datasets.admission import admit
from intelligence.datasets.bounds import (
    MAX_DESCRIPTOR_BYTES,
    ReadBudget,
    canonical_json_object,
    digest_file,
)
from intelligence.datasets.catalog import find, find_run, list_entries
from intelligence.datasets.commands import build_registry, verify_registry
from intelligence.datasets.models import (
    DATASET_DEFINITION,
    DatasetRequest,
    current_config_compatible,
    parse_config,
)
from intelligence.runtime import IntelligenceRuntime


class _SubparserAdder(Protocol):
    def add_parser(self, name: str) -> argparse.ArgumentParser: ...


def add_parser(parent: _SubparserAdder) -> None:
    """Add the reviewed top-level dataset command family."""
    dataset = parent.add_parser("dataset")
    actions = dataset.add_subparsers(dest="dataset_command", required=True)
    build = actions.add_parser("build")
    build.add_argument("--deal-refs-run-id", required=True)
    build.add_argument("--activities-checkpoint-id", required=True)
    build.add_argument("--activities-accepted-run-id", required=True)
    build.add_argument("--definition", required=True, choices=(DATASET_DEFINITION,))
    build.add_argument("--feature-cutoff", required=True)
    build.add_argument("--label-cutoff", required=True)
    build.add_argument("--seed", required=True, type=int)
    verify = actions.add_parser("verify")
    verify.add_argument("dataset_id")
    verify.add_argument("--accepted-run-id", required=True)
    listing = actions.add_parser("list")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--after")
    inspect = actions.add_parser("inspect")
    inspect.add_argument("dataset_id")
    inspect.add_argument("--accepted-run-id", required=True)


def main(arguments: argparse.Namespace) -> int:
    """Execute one fixed dataset action."""
    config = RuntimeConfig.from_environment()
    action = str(arguments.dataset_command)
    if action == "list":
        items = list_entries(config.workspace, arguments.limit, arguments.after)
        print(json.dumps([item.summary() for item in items], sort_keys=True))
        return 0
    if action == "inspect":
        entry = find(config.workspace, arguments.dataset_id, arguments.accepted_run_id)
        print(json.dumps(_inspect(entry), sort_keys=True))
        return 0
    if not config.mutations_enabled:
        raise RuntimeError("mutating execution is disabled")
    if action == "build":
        return _build(config, _request(arguments))
    return _verify(config, arguments.dataset_id, arguments.accepted_run_id)


def _build(config: RuntimeConfig, request: DatasetRequest) -> int:
    runtime = IntelligenceRuntime(config)
    try:
        inputs = admit(runtime, request)
    finally:
        runtime.close()
    scoped = IntelligenceRuntime(config, build_registry(inputs))
    try:
        run_id = scoped.run("dataset_build")
    finally:
        scoped.close()
    entry = find(config.workspace, _dataset_id_for_run(config.workspace, run_id), run_id)
    print(json.dumps(entry.summary(), sort_keys=True))
    return 0


def _verify(config: RuntimeConfig, dataset_id: str, accepted_run_id: str) -> int:
    entry = find(config.workspace, dataset_id, accepted_run_id)
    current_config_compatible(entry.descriptor.inputs)
    request = _request_from_config(entry.descriptor.inputs)
    runtime = IntelligenceRuntime(config)
    try:
        inputs = admit(runtime, request)
    finally:
        runtime.close()
    if inputs.config() != entry.descriptor.inputs:
        raise RuntimeError("dataset input admission no longer matches accepted descriptor")
    scoped = IntelligenceRuntime(config, verify_registry(inputs, entry.descriptor))
    try:
        run_id = scoped.run("dataset_verify")
    finally:
        scoped.close()
    _verified_run_evidence(config.workspace, run_id, entry)
    print(
        json.dumps({"dataset_id": dataset_id, "run_id": run_id, "verified": True}, sort_keys=True)
    )
    return 0


def _request(arguments: argparse.Namespace) -> DatasetRequest:
    return DatasetRequest(
        arguments.deal_refs_run_id,
        arguments.activities_checkpoint_id,
        arguments.activities_accepted_run_id,
        arguments.definition,
        arguments.feature_cutoff,
        arguments.label_cutoff,
        arguments.seed,
    )


def _request_from_config(value: dict[str, object]) -> DatasetRequest:
    config = parse_config(value)
    inputs = config.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("dataset descriptor inputs are invalid")
    deals = inputs.get("deal_refs")
    activities = inputs.get("activities")
    if not isinstance(deals, dict) or not isinstance(activities, dict):
        raise ValueError("dataset descriptor input pins are invalid")
    return DatasetRequest(
        _text(deals, "run_id"),
        _text(activities, "checkpoint_id"),
        _text(activities, "accepted_run_id"),
        _text(config, "definition"),
        _text(config, "feature_cutoff"),
        _text(config, "label_cutoff"),
        _integer(config, "seed"),
    )


def _dataset_id_for_run(workspace: object, run_id: str) -> str:
    from pathlib import Path

    if not isinstance(workspace, Path):
        raise RuntimeError("Intelligence workspace is unavailable")
    try:
        return find_run(workspace, run_id).descriptor.dataset_id
    except ValueError as error:
        raise RuntimeError("completed dataset build has no accepted dataset descriptor") from error


def _inspect(entry: object) -> dict[str, object]:
    from intelligence.datasets.catalog import CatalogEntry

    if not isinstance(entry, CatalogEntry):
        raise ValueError("dataset catalog entry is invalid")
    return {
        "descriptor": entry.descriptor.as_dict(),
        "manifest": entry.manifest,
        "run_id": entry.run_id,
    }


def _text(value: dict[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} is invalid")
    return result


def _integer(value: dict[str, object], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool):
        raise ValueError(f"{key} is invalid")
    return result


def _verified_run_evidence(workspace: Path, run_id: str, entry: object) -> None:
    from intelligence.crm.activities.path_safety import confined_file
    from intelligence.datasets.catalog import CatalogEntry
    from intelligence.datasets.models import canonical_digest, safe_component
    from intelligence.state_readonly import ReadOnlyState

    if not isinstance(entry, CatalogEntry):
        raise ValueError("dataset catalog entry is invalid")
    state = ReadOnlyState.open(workspace)
    try:
        run = state.inspect(run_id)
        outputs = state.accepted_outputs(run_id)
    finally:
        state.close()
    relative = f"outputs/{run_id}/verifications/datasets/{entry.descriptor.dataset_id}.json"
    if (
        run is None
        or run.state != "completed"
        or run.command != "dataset_verify"
        or len(outputs) != 1
    ):
        raise RuntimeError("dataset verification did not complete successfully")
    output = outputs[0]
    parts = ("outputs", run_id, "verifications", "datasets", f"{entry.descriptor.dataset_id}.json")
    for part in parts:
        safe_component(part, "verification path component")
    path = confined_file(workspace, parts)
    if (
        output.relative_path != relative
        or output.byte_count > MAX_DESCRIPTOR_BYTES
        or path.lstat().st_size != output.byte_count
    ):
        raise RuntimeError("dataset verification output is not State-bound")
    budget = ReadBudget(MAX_DESCRIPTOR_BYTES * 2, 2, 1)
    if digest_file(path, budget, maximum_file_bytes=MAX_DESCRIPTOR_BYTES) != output.sha256:
        raise RuntimeError("dataset verification output is not State-bound")
    value = canonical_json_object(path, budget, maximum_file_bytes=MAX_DESCRIPTOR_BYTES)
    expected = {
        "accepted_dataset_run_id",
        "dataset_id",
        "descriptor_digest",
        "manifest_digest",
        "schema_version",
        "verified",
        "digest",
    }
    if set(value) != expected or value.get("schema_version") != "crm-deal-state-verification-v1":
        raise RuntimeError("dataset verification output is invalid")
    unsigned = dict(value)
    digest = unsigned.pop("digest")
    if (
        value.get("accepted_dataset_run_id") != entry.descriptor.run_id
        or value.get("dataset_id") != entry.descriptor.dataset_id
        or value.get("descriptor_digest") != entry.descriptor.digest
        or value.get("manifest_digest") != entry.descriptor.manifest_digest
        or value.get("verified") is not True
        or not isinstance(digest, str)
        or digest != canonical_digest(unsigned)
    ):
        raise RuntimeError("dataset verification output is not bound to descriptor")
