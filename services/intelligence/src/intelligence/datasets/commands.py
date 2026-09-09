"""Spawn-safe reviewed dataset build and verification handlers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from intelligence.artifacts import canonical_json
from intelligence.datasets.artifacts import DatasetDescriptor, verify_dataset, write_dataset
from intelligence.datasets.catalog import conflicts
from intelligence.datasets.definition import compute, content_digest
from intelligence.datasets.models import AcceptedInputs, canonical_digest
from intelligence.registry import Cancelled, RegisteredCommand, Registry
from intelligence.state_readonly import ReadOnlyState


@dataclass(frozen=True)
class _ReadConfig:
    workspace: Path


@dataclass(frozen=True)
class _ReadRuntime:
    config: _ReadConfig
    state: ReadOnlyState


def build_registry(inputs: AcceptedInputs) -> Registry:
    """Return only a request-scoped reviewed build handler; production registry stays empty."""
    return Registry(
        (
            RegisteredCommand(
                "dataset_build",
                True,
                partial(_build_handler, inputs),
                {"definition": inputs.request.definition, "domain": "datasets"},
            ),
        )
    )


def verify_registry(inputs: AcceptedInputs, descriptor: DatasetDescriptor) -> Registry:
    """Return only a request-scoped reviewed verification handler."""
    return Registry(
        (
            RegisteredCommand(
                "dataset_verify",
                True,
                partial(_verify_handler, inputs, descriptor),
                {"definition": inputs.request.definition, "domain": "datasets"},
            ),
        )
    )


def _build_handler(inputs: AcceptedInputs, staging: Path, cancelled: Cancelled) -> None:
    _cancel(cancelled)
    _revalidate_source_artifacts(staging.parent.parent, inputs)
    computation = compute(inputs)
    digest = content_digest(inputs, computation)
    _cancel(cancelled)
    conflicts(staging.parent.parent, inputs.request.digest, digest)
    artifact = write_dataset(staging, inputs, computation)
    if artifact.content_digest != digest:
        raise RuntimeError("dataset artifact content digest changed during publication")
    verify_dataset(staging / "datasets" / artifact.descriptor.dataset_id, artifact.descriptor)


def _verify_handler(
    inputs: AcceptedInputs,
    descriptor: DatasetDescriptor,
    staging: Path,
    cancelled: Cancelled,
) -> None:
    _cancel(cancelled)
    workspace = staging.parent.parent
    root = workspace / "outputs" / descriptor.run_id / "datasets" / descriptor.dataset_id
    manifest = verify_dataset(root, descriptor)
    _revalidate_source_artifacts(workspace, inputs)
    computation = compute(inputs)
    digest = content_digest(inputs, computation)
    if digest != manifest.get("content_digest"):
        raise RuntimeError("dataset deterministic reconstruction differs from accepted content")
    evidence = {
        "accepted_dataset_run_id": descriptor.run_id,
        "dataset_id": descriptor.dataset_id,
        "descriptor_digest": descriptor.digest,
        "manifest_digest": descriptor.manifest_digest,
        "schema_version": "crm-deal-state-verification-v1",
        "verified": True,
    }
    evidence["digest"] = canonical_digest(evidence)
    target = staging / "verifications" / "datasets" / f"{descriptor.dataset_id}.json"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_new(target, canonical_json(evidence).encode("utf-8"))


def _revalidate_source_artifacts(workspace: Path, inputs: AcceptedInputs) -> None:
    """Recheck pinned source bytes before writes without reconnecting to source systems."""
    from intelligence.datasets.admission import admit_activities, admit_deals

    state = ReadOnlyState.open(workspace)
    runtime = _ReadRuntime(_ReadConfig(workspace), state)
    try:
        deals = admit_deals(workspace, state, inputs.deals.run_id)
        activities = admit_activities(runtime, inputs.activities.checkpoint_id)
    finally:
        state.close()
    if deals.boundary_digest != inputs.deals.boundary_digest:
        raise RuntimeError("deal-reference boundary changed after dataset admission")
    if deals.inventory_digest != inputs.deals.inventory_digest:
        raise RuntimeError("deal-reference inventory changed after dataset admission")
    if deals.snapshot_manifest_digest != inputs.deals.snapshot_manifest_digest:
        raise RuntimeError("deal-reference manifest changed after dataset admission")
    if activities.accepted_run_id != inputs.activities.accepted_run_id:
        raise RuntimeError("activity accepted run changed after dataset admission")
    if activities.descriptor_digest != inputs.activities.descriptor_digest:
        raise RuntimeError("activity descriptor changed after dataset admission")
    if activities.manifest_digest != inputs.activities.manifest_digest:
        raise RuntimeError("activity manifest changed after dataset admission")
    if activities.inventory_digest != inputs.activities.inventory_digest:
        raise RuntimeError("activity inventory changed after dataset admission")


def _cancel(cancelled: Cancelled) -> None:
    if cancelled():
        raise RuntimeError("dataset operation was cancelled")


def _write_new(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
