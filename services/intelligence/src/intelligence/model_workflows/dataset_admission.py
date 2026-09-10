"""Parent and child artifact-only admission of exact accepted #356 datasets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from intelligence.datasets.artifact_io import ndjson
from intelligence.datasets.bounds import ReadBudget
from intelligence.datasets.catalog import find, find_metadata
from intelligence.datasets.models import parse_config
from intelligence.model_workflows.contracts import ACTIVITY_PROVENANCE, TrainRequest, digest


@dataclass(frozen=True)
class AdmittedDataset:
    """Exact immutable dataset bytes and typed rows admitted before recipe execution."""

    request: TrainRequest
    config: dict[str, object]
    manifest_digest: str
    content_digest: str
    rows: tuple[dict[str, object], ...]

    def pin(self) -> dict[str, object]:
        return {
            "accepted_run_id": self.request.accepted_run_id,
            "config_digest": digest(self.config),
            "content_digest": self.content_digest,
            "dataset_id": self.request.dataset_id,
            "manifest_digest": self.manifest_digest,
            "provenance": ACTIVITY_PROVENANCE,
        }

    def source_pin_scalars(self) -> dict[str, str]:
        inputs = self.config.get("inputs")
        if not isinstance(inputs, dict):
            raise ValueError("dataset configuration source pins are invalid")
        deals, activities = inputs.get("deal_refs"), inputs.get("activities")
        if not isinstance(deals, dict) or not isinstance(activities, dict):
            raise ValueError("dataset configuration source pins are invalid")
        fields = {
            "deal_run_id": deals.get("run_id"),
            "deal_boundary_digest": deals.get("boundary_digest"),
            "deal_inventory_digest": deals.get("inventory_digest"),
            "deal_snapshot_manifest_digest": deals.get("snapshot_manifest_digest"),
            "activity_checkpoint_id": activities.get("checkpoint_id"),
            "activity_accepted_run_id": activities.get("accepted_run_id"),
            "activity_logical_snapshot_id": activities.get("logical_snapshot_id"),
            "activity_boundary_digest": activities.get("boundary_digest"),
            "activity_descriptor_digest": activities.get("descriptor_digest"),
            "activity_inventory_digest": activities.get("inventory_digest"),
            "activity_manifest_digest": activities.get("manifest_digest"),
        }
        if not all(isinstance(value, str) and value for value in fields.values()):
            raise ValueError("dataset configuration source pins are invalid")
        return {key: str(value) for key, value in sorted(fields.items())}


def admit_dataset(workspace: Path, request: TrainRequest) -> AdmittedDataset:
    """Require exact State acceptance, schema, coverage, and content checksums before recipes."""
    # Full admission occurs only in the supervised child. It intentionally uses
    # the complete verifier so materialized rows remain bound to State checksums.
    entry = find(workspace, request.dataset_id, request.accepted_run_id)
    # Dataset producer code fingerprints are immutable provenance, not a consumer
    # compatibility gate: #357 must consume accepted merged #356 datasets.
    parse_config(entry.descriptor.inputs)
    manifest = entry.manifest
    if manifest.get("provenance") != {"activity": ACTIVITY_PROVENANCE}:
        raise ValueError("dataset partial-coverage provenance is incompatible")
    root = workspace / "outputs" / request.accepted_run_id / "datasets" / request.dataset_id
    rows = ndjson(root / "rows.ndjson", ReadBudget(20_000_000, 10_000, 10_000))
    if manifest.get("row_count") != len(rows) or any(
        row.get("activity_coverage") != "legacy_partial_snapshot" for row in rows
    ):
        raise ValueError("dataset row coverage or count is incompatible")
    content = manifest.get("content_digest")
    if not isinstance(content, str) or len(content) != 64:
        raise ValueError("dataset content digest is invalid")
    return AdmittedDataset(
        request, entry.descriptor.inputs, entry.descriptor.manifest_digest, content, tuple(rows)
    )


@dataclass(frozen=True)
class DatasetPin:
    """Bounded parent-side dataset identity; deliberately excludes materialized rows."""

    request: TrainRequest
    config: dict[str, object]
    manifest_digest: str
    content_digest: str

    def pin(self) -> dict[str, object]:
        return AdmittedDataset(
            self.request, self.config, self.manifest_digest, self.content_digest, ()
        ).pin()

    def source_pin_scalars(self) -> dict[str, str]:
        return AdmittedDataset(
            self.request, self.config, self.manifest_digest, self.content_digest, ()
        ).source_pin_scalars()


def admit_dataset_metadata(workspace: Path, request: TrainRequest) -> DatasetPin:
    """Read bounded descriptor/manifest metadata without materializing dataset rows."""
    entry = find_metadata(workspace, request.dataset_id, request.accepted_run_id)
    parse_config(entry.descriptor.inputs)
    manifest = entry.manifest
    if manifest.get("provenance") != {"activity": ACTIVITY_PROVENANCE}:
        raise ValueError("dataset partial-coverage provenance is incompatible")
    content = manifest.get("content_digest")
    if not isinstance(content, str) or len(content) != 64:
        raise ValueError("dataset content digest is invalid")
    return DatasetPin(request, entry.descriptor.inputs, entry.descriptor.manifest_digest, content)
