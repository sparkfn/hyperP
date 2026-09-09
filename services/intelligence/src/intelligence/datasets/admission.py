"""State-backed, artifact-bounded admission for immutable dataset source snapshots."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Protocol

from intelligence.crm.activities.acceptance import accepted_publication
from intelligence.crm.activities.bounded import ReadLimits, accepted_snapshot, verify_snapshot_input
from intelligence.crm.activities.snapshot_verifier import snapshot_inventory, verify_snapshot
from intelligence.crm_deal_refs.checkpoints import require_confined_directory
from intelligence.crm_deal_refs.commands import accepted_snapshot_root
from intelligence.crm_deal_refs.snapshot_validation import verify_complete
from intelligence.datasets.admission_codec import (
    activity_rows,
    boundary_dict,
    deal_reference,
    identity_revision,
    inventory_dict,
    verified_selected_count,
)
from intelligence.datasets.bounds import (
    MAX_INPUT_BYTES,
    MAX_INPUT_ENTRIES,
    MAX_INPUT_FILE_BYTES,
    ReadBudget,
    digest_file,
    registered_tree,
)
from intelligence.datasets.models import (
    MAX_ACTIVITY_RECORDS,
    SOURCE_SYSTEM,
    AcceptedInputs,
    ActivityInput,
    DatasetRequest,
    DealInput,
    canonical_digest,
    parse_utc,
)
from intelligence.models import OutputInventory, Run

_ACTIVITY_LIMITS = ReadLimits(100_000_000, 100_000, MAX_ACTIVITY_RECORDS)
_DEAL_COMMANDS = frozenset({"crm_deal_refs_extract", "crm_deal_refs_resume"})


class _StateReader(Protocol):
    def inspect(self, run_id: str) -> Run | None: ...

    def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]: ...


class _ConfigReader(Protocol):
    @property
    def workspace(self) -> Path: ...


class _RuntimeReader(Protocol):
    @property
    def config(self) -> _ConfigReader: ...

    @property
    def state(self) -> _StateReader: ...


def admit(runtime: _RuntimeReader, request: DatasetRequest) -> AcceptedInputs:
    """Admit both source snapshots only when State and exact artifacts agree."""
    deals = admit_deals(runtime.config.workspace, runtime.state, request.deal_refs_run_id)
    activities = admit_activities(runtime, request.activities_checkpoint_id)
    if activities.accepted_run_id != request.activities_accepted_run_id:
        raise ValueError("activity accepted run does not match the pinned request")
    if (
        deals.boundary.source_system != SOURCE_SYSTEM
        or activities.source_key != SOURCE_SYSTEM
        or deals.boundary.source_instance_id != activities.source_instance_id
    ):
        raise ValueError("source instance or namespace is incompatible")
    if parse_utc(request.label_cutoff, "label cutoff") > parse_utc(
        deals.boundary.as_of, "deal boundary"
    ):
        raise ValueError("label cutoff exceeds the deal-reference boundary")
    return AcceptedInputs(request, deals, activities)


def admit_deals(workspace: Path, state: _StateReader, run_id: str) -> DealInput:
    """Require a completed #353 snapshot and its exact State-registered inventory."""
    run = state.inspect(run_id)
    outputs = state.accepted_outputs(run_id)
    if run is None or run.state != "completed" or run.command not in _DEAL_COMMANDS or not outputs:
        raise ValueError("deal references require a completed accepted extraction or resume")
    root = accepted_snapshot_root(workspace, run_id)
    require_confined_directory(workspace, root)
    _registered_input_tree(root, f"outputs/{run_id}/snapshots/crm/deal-refs/", outputs)
    boundary, _checkpoint, _pages, raw_deals, raw_identities = verify_complete(root)
    inventory = _deal_inventory(root, run_id, outputs)
    if outputs != inventory:
        raise ValueError("deal-reference State inventory differs from exact snapshot inventory")
    return DealInput(
        run_id,
        boundary,
        canonical_digest(boundary_dict(boundary)),
        canonical_digest([inventory_dict(item) for item in inventory]),
        _inventory_digest_for(
            inventory, f"outputs/{run_id}/snapshots/crm/deal-refs/snapshot-manifest.json"
        ),
        tuple(deal_reference(row) for row in raw_deals),
        tuple(identity_revision(row) for row in raw_identities),
    )


def admit_activities(runtime: _RuntimeReader, checkpoint_id: str) -> ActivityInput:
    """Require the #354 State-proven publication and verified partial archive."""
    selected = accepted_publication(runtime, checkpoint_id)
    if selected is None:
        raise ValueError("activity checkpoint has no State-accepted publication")
    descriptor, _pointer = selected
    if descriptor.request.source_key != SOURCE_SYSTEM:
        raise ValueError("activity snapshot source namespace is incompatible")
    if descriptor.request.selection_contract_version != "crm-activities-selection-v2":
        raise ValueError("activity selection contract is incompatible")
    snapshot = accepted_snapshot(
        runtime.config.workspace, descriptor.run_id, descriptor.snapshot_id
    )
    prefix = f"snapshots/crm/activities/{descriptor.snapshot_id}/"
    _registered_input_tree(snapshot, prefix, descriptor.snapshot_inventory)
    verify_snapshot_input(
        snapshot,
        _ACTIVITY_LIMITS,
        verified_selected_count(snapshot, descriptor.snapshot_inventory, prefix),
    )
    evidence = verify_snapshot(snapshot)
    if evidence.get("manifest_digest") != descriptor.manifest_digest:
        raise ValueError("activity manifest digest differs from its accepted descriptor")
    inventory = tuple(
        OutputInventory(f"{prefix}{path}", digest, byte_count)
        for path, digest, byte_count in snapshot_inventory(snapshot)
    )
    if inventory != descriptor.snapshot_inventory:
        raise ValueError("activity snapshot inventory differs from its accepted descriptor")
    rows = activity_rows(snapshot, _record_pages)
    if len(rows.records) > MAX_ACTIVITY_RECORDS:
        raise ValueError("activity record count exceeds dataset admission bound")
    return ActivityInput(
        checkpoint_id,
        descriptor.run_id,
        descriptor.snapshot_id,
        descriptor.digest,
        descriptor.boundary_digest,
        descriptor.manifest_digest,
        canonical_digest([inventory_dict(item) for item in inventory]),
        descriptor.request.source_instance_id,
        descriptor.request.source_key,
        rows.records,
        rows.accepted_ids,
        rows.rejected_ids,
        rows.quarantined_ids,
    )


def _registered_input_tree(
    root: Path,
    prefix: str,
    registered: tuple[OutputInventory, ...],
) -> tuple[OutputInventory, ...]:
    return registered_tree(
        root,
        prefix,
        registered,
        ReadBudget(MAX_INPUT_BYTES, MAX_INPUT_ENTRIES, MAX_ACTIVITY_RECORDS),
        maximum_file_bytes=MAX_INPUT_FILE_BYTES,
    )


def _deal_inventory(
    root: Path,
    run_id: str,
    registered: tuple[OutputInventory, ...] | None = None,
) -> tuple[OutputInventory, ...]:
    prefix = f"outputs/{run_id}/snapshots/crm/deal-refs/"
    budget = ReadBudget(MAX_INPUT_BYTES, MAX_INPUT_ENTRIES, MAX_ACTIVITY_RECORDS)
    paths = (
        registered_tree(root, prefix, registered, budget, maximum_file_bytes=MAX_INPUT_FILE_BYTES)
        if registered is not None
        else _unregistered_tree(root, prefix, budget)
    )
    return tuple(_hashed_inventory_item(root, prefix, item, budget) for item in paths)


def _hashed_inventory_item(
    root: Path,
    prefix: str,
    item: OutputInventory,
    budget: ReadBudget,
) -> OutputInventory:
    relative = item.relative_path.removeprefix(prefix)
    path = root.joinpath(*relative.split("/"))
    return OutputInventory(
        item.relative_path,
        digest_file(path, budget, maximum_file_bytes=MAX_INPUT_FILE_BYTES),
        item.byte_count,
    )


def _unregistered_tree(root: Path, prefix: str, budget: ReadBudget) -> tuple[OutputInventory, ...]:
    metadata = root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("deal-reference snapshot inventory is unsafe")
    values: list[OutputInventory] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for candidate in directory.iterdir():
            budget.entry()
            item = candidate.lstat()
            if stat.S_ISLNK(item.st_mode):
                raise ValueError("deal-reference snapshot inventory is unsafe")
            if stat.S_ISDIR(item.st_mode):
                pending.append(candidate)
            elif stat.S_ISREG(item.st_mode) and item.st_nlink == 1:
                if item.st_size > MAX_INPUT_FILE_BYTES:
                    raise RuntimeError("dataset evidence exceeds per-file byte ceiling")
                relative = candidate.relative_to(root).as_posix()
                values.append(OutputInventory(f"{prefix}{relative}", "0" * 64, item.st_size))
            else:
                raise ValueError("deal-reference snapshot inventory is unsafe")
    return tuple(sorted(values, key=lambda item: item.relative_path))


def _record_pages(directory: Path, budget: ReadBudget) -> tuple[Path, ...]:
    metadata = directory.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("accepted activity record directory is unsafe")
    pages: list[Path] = []
    for candidate in directory.iterdir():
        budget.entry()
        item = candidate.lstat()
        if (
            stat.S_ISLNK(item.st_mode)
            or not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or not candidate.name.startswith("page-")
            or candidate.suffix != ".json"
        ):
            raise ValueError("accepted activity record page is unsafe")
        pages.append(candidate)
    return tuple(sorted(pages, key=lambda item: item.name))


def _inventory_digest_for(items: tuple[OutputInventory, ...], path: str) -> str:
    matches = tuple(item.sha256 for item in items if item.relative_path == path)
    if len(matches) != 1:
        raise ValueError("dataset input manifest is absent from registered inventory")
    return matches[0]
