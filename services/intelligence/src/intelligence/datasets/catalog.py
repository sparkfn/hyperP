"""Bounded State-backed discovery for accepted immutable dataset publications."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from intelligence.artifacts_manifest import validate_manifest
from intelligence.crm.activities.path_safety import confined_directory
from intelligence.datasets.artifacts import DatasetDescriptor, parse_descriptor, verify_dataset
from intelligence.datasets.bounds import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_ENTRIES,
    MAX_ARTIFACT_FILE_BYTES,
    MAX_ARTIFACT_ROWS,
    MAX_DESCRIPTOR_BYTES,
    ReadBudget,
    canonical_json_object,
    digest_file,
    registered_tree,
)
from intelligence.datasets.models import MAX_CATALOG_RUNS, safe_component
from intelligence.models import OutputInventory, RunLogInventory
from intelligence.state_readonly import ReadOnlyState


@dataclass(frozen=True)
class CatalogEntry:
    """One fully State- and artifact-verified accepted dataset publication."""

    run_id: str
    descriptor: DatasetDescriptor
    manifest: dict[str, object]

    def summary(self) -> dict[str, object]:
        return {
            "dataset_id": self.descriptor.dataset_id,
            "request_digest": self.descriptor.request_digest,
            "row_count": self.manifest["row_count"],
            "run_id": self.run_id,
        }


def entries(workspace: Path, budget: ReadBudget | None = None) -> tuple[CatalogEntry, ...]:
    """Enumerate bounded accepted entries; overflow never means no replay conflict."""
    read_budget = budget or _budget()
    state = ReadOnlyState.open(workspace)
    try:
        run_ids = state.completed_run_ids("dataset_build", MAX_CATALOG_RUNS)
    finally:
        state.close()
    if len(run_ids) > MAX_CATALOG_RUNS:
        raise RuntimeError("dataset catalog bound exceeded")
    state = ReadOnlyState.open(workspace)
    try:
        result: list[CatalogEntry] = []
        for run_id in run_ids:
            result.extend(_run_entries(workspace, run_id, state, read_budget))
        return tuple(sorted(result, key=lambda item: (item.run_id, item.descriptor.dataset_id)))
    finally:
        state.close()


def find(workspace: Path, dataset_id: str, run_id: str) -> CatalogEntry:
    """Resolve an exact run directly, rather than paginating a global catalog first."""
    safe_component(dataset_id, "dataset id")
    safe_component(run_id, "dataset run")
    entry = find_run(workspace, run_id)
    if entry.descriptor.dataset_id != dataset_id:
        raise ValueError("accepted dataset publication is absent or ambiguous")
    return entry


def find_run(workspace: Path, run_id: str) -> CatalogEntry:
    """Resolve exactly one accepted dataset from one bounded named run directory."""
    safe_component(run_id, "dataset run")
    state = ReadOnlyState.open(workspace)
    try:
        values = _run_entries(workspace, run_id, state, _budget())
    finally:
        state.close()
    if len(values) != 1:
        raise ValueError("accepted dataset publication is absent or ambiguous")
    return values[0]


def conflicts(workspace: Path, request_digest: str, content_digest: str) -> None:
    """Fail closed when the same logical request has accepted different content."""
    for item in entries(workspace):
        if item.descriptor.request_digest == request_digest:
            if item.manifest.get("content_digest") != content_digest:
                raise RuntimeError("dataset replay conflicts with accepted logical content")


def list_entries(workspace: Path, limit: int, after: str | None) -> tuple[CatalogEntry, ...]:
    """Apply client pagination only after all bounded catalog validation."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("catalog limit must be between 1 and 100")
    if after is not None:
        safe_component(after, "catalog after run")
    return tuple(item for item in entries(workspace) if after is None or item.run_id > after)[
        :limit
    ]


def _run_entries(
    workspace: Path,
    run_id: str,
    state: ReadOnlyState,
    budget: ReadBudget,
) -> list[CatalogEntry]:
    safe_component(run_id, "dataset run")
    run = state.inspect(run_id)
    if run is None or run.state != "completed" or run.command != "dataset_build":
        return []
    accepted = state.accepted_outputs(run_id)
    if not accepted:
        return []
    _terminal_manifest(workspace, state, run_id, accepted, budget)
    root = _confined(workspace, ("outputs", run_id))
    _directory(root, "dataset output run")
    descriptor_directory = _confined(
        workspace,
        ("outputs", run_id, "acceptance-descriptors", "datasets"),
    )
    if not descriptor_directory.exists():
        raise ValueError("accepted dataset build is missing its descriptor directory")
    _directory(descriptor_directory, "dataset descriptor directory")
    paths = _files(descriptor_directory, budget, "dataset descriptor")
    if len(paths) > 1:
        raise ValueError("dataset run has multiple acceptance descriptors")
    if not paths:
        raise ValueError("accepted dataset build is missing its descriptor")
    path = paths[0]
    if path.suffix != ".json":
        raise ValueError("dataset descriptor path is invalid")
    descriptor_output = _registered_descriptor_output(accepted, run_id, path.name)
    metadata = path.lstat()
    if metadata.st_size != descriptor_output.byte_count or metadata.st_size > MAX_DESCRIPTOR_BYTES:
        raise ValueError("dataset descriptor bytes are not State-registered")
    if (
        digest_file(path, budget, maximum_file_bytes=MAX_DESCRIPTOR_BYTES)
        != descriptor_output.sha256
    ):
        raise ValueError("dataset descriptor checksum conflicts with State")
    descriptor = parse_descriptor(
        canonical_json_object(path, budget, maximum_file_bytes=MAX_DESCRIPTOR_BYTES)
    )
    if descriptor.run_id != run_id or path.name != f"{descriptor.dataset_id}.json":
        raise ValueError("dataset descriptor run linkage is invalid")
    expected = tuple(
        sorted(
            (
                descriptor_output,
                *(
                    OutputInventory(
                        f"outputs/{run_id}/{item.relative_path}", item.sha256, item.byte_count
                    )
                    for item in descriptor.dataset_inventory
                ),
            ),
            key=lambda item: item.relative_path,
        )
    )
    if accepted != expected:
        raise ValueError("dataset descriptor inventory conflicts with State acceptance")
    dataset_root = _confined(workspace, ("outputs", run_id, "datasets", descriptor.dataset_id))
    registered_tree(
        dataset_root,
        f"datasets/{descriptor.dataset_id}/",
        descriptor.dataset_inventory,
        budget,
        maximum_file_bytes=MAX_ARTIFACT_FILE_BYTES,
    )
    manifest = verify_dataset(dataset_root, descriptor, budget)
    return [CatalogEntry(run_id, descriptor, manifest)]


def _registered_descriptor_output(
    accepted: tuple[OutputInventory, ...], run_id: str, filename: str
) -> OutputInventory:
    relative = f"outputs/{run_id}/acceptance-descriptors/datasets/{filename}"
    matches = tuple(item for item in accepted if item.relative_path == relative)
    if len(matches) != 1:
        raise ValueError("dataset descriptor is not State-registered")
    return matches[0]


def _children(root: Path, budget: ReadBudget, label: str) -> tuple[Path, ...]:
    values: list[Path] = []
    try:
        for item in root.iterdir():
            budget.entry()
            _directory(item, label)
            values.append(item)
    except OSError as error:
        raise ValueError(f"{label} directory cannot be read") from error
    return tuple(sorted(values, key=lambda item: item.name))


def _manifest_run_ids(workspace: Path, budget: ReadBudget) -> tuple[str, ...]:
    directory = _confined(workspace, ("runs", "manifests"))
    values: list[str] = []
    for path in _files(directory, budget, "terminal manifest"):
        if path.suffix != ".json":
            raise ValueError("terminal manifest path is invalid")
        run_id = path.stem
        safe_component(run_id, "dataset run")
        values.append(run_id)
    if len(values) != len(set(values)):
        raise ValueError("duplicate terminal manifest run evidence")
    return tuple(sorted(values))


def _terminal_manifest(
    workspace: Path,
    state: ReadOnlyState,
    run_id: str,
    accepted: tuple[OutputInventory, ...],
    budget: ReadBudget,
) -> None:
    run = state.inspect(run_id)
    if run is None:
        raise ValueError("terminal manifest run is absent from State")
    path = _confined(workspace, ("runs", "manifests")) / f"{run_id}.json"
    value = canonical_json_object(path, budget, maximum_file_bytes=MAX_DESCRIPTOR_BYTES)
    run_log = _terminal_run_log(workspace, run_id, value, budget)
    validate_manifest(
        value,
        expected_run_id=run_id,
        expected_command=run.command,
        expected_state="completed",
        expected_outputs=accepted,
        expected_created_at=run.created_at,
        expected_started_at=run.started_at,
        expected_limits=dict(run.limits) if run.limits else None,
        expected_run_log=run_log,
        expected_command_provenance=(
            None if run.command_provenance is None else dict(run.command_provenance)
        ),
    )


def _terminal_run_log(
    workspace: Path,
    run_id: str,
    manifest: dict[str, object],
    budget: ReadBudget,
) -> RunLogInventory | None:
    value = manifest.get("run_log")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"path", "sha256", "byte_count"}:
        raise ValueError("terminal manifest log evidence is invalid")
    path_value, digest, byte_count = value.get("path"), value.get("sha256"), value.get("byte_count")
    expected_path = f"runs/logs/{run_id}.ndjson"
    if (
        path_value != expected_path
        or not isinstance(digest, str)
        or not isinstance(byte_count, int)
    ):
        raise ValueError("terminal manifest log evidence is invalid")
    path = _confined(workspace, ("runs", "logs")) / f"{run_id}.ndjson"
    if (
        path.lstat().st_size != byte_count
        or digest_file(path, budget, maximum_file_bytes=MAX_DESCRIPTOR_BYTES) != digest
    ):
        raise ValueError("terminal manifest log evidence is invalid")
    return RunLogInventory(expected_path, digest, byte_count)


def _files(root: Path, budget: ReadBudget, label: str) -> tuple[Path, ...]:
    values: list[Path] = []
    try:
        for item in root.iterdir():
            budget.entry()
            metadata = item.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise ValueError(f"{label} is unsafe")
            values.append(item)
    except OSError as error:
        raise ValueError(f"{label} directory cannot be read") from error
    return tuple(sorted(values, key=lambda item: item.name))


def _directory(path: Path, label: str) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} is unsafe")


def _budget() -> ReadBudget:
    return ReadBudget(
        MAX_ARTIFACT_BYTES * 3,
        max(MAX_ARTIFACT_ENTRIES, MAX_CATALOG_RUNS * 3),
        MAX_ARTIFACT_ROWS,
    )


def _confined(workspace: Path, parts: tuple[str, ...]) -> Path:
    for part in parts:
        safe_component(part, "dataset path component")
    return confined_directory(workspace, parts, create=False)
