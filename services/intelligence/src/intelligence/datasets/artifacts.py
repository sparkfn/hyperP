"""Canonical immutable dataset publication and artifact-only verification facade."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from intelligence.datasets.artifact_codec import (
    DatasetDescriptor,
    descriptor_relative_path,
    inventory_dict,
    inventory_item,
    parse_descriptor,
    read_descriptor,
)
from intelligence.datasets.artifact_io import (
    inventory,
    json_object,
    ndjson,
    safe_directory,
    write_json,
    write_ndjson,
)
from intelligence.datasets.artifact_validation import logical_schemas, manifest_shape, verify_counts
from intelligence.datasets.bounds import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_ENTRIES,
    MAX_ARTIFACT_FILE_BYTES,
    MAX_ARTIFACT_ROWS,
    ReadBudget,
    digest_file,
    registered_tree,
)
from intelligence.datasets.definition import DatasetComputation, content_digest
from intelligence.datasets.models import (
    DATASET_DESCRIPTOR_SCHEMA,
    DATASET_SCHEMA_VERSION,
    AcceptedInputs,
    canonical_digest,
    parse_config,
)
from intelligence.datasets.specification import definition, schema
from intelligence.models import OutputInventory


@dataclass(frozen=True)
class DatasetArtifact:
    """One complete staged or accepted immutable dataset publication."""

    descriptor: DatasetDescriptor
    content_digest: str
    row_count: int


def write_dataset(
    staging: Path,
    inputs: AcceptedInputs,
    computation: DatasetComputation,
) -> DatasetArtifact:
    """Write one complete immutable dataset beneath only the current run's staging root."""
    content = content_digest(inputs, computation)
    dataset_id = f"crm-deal-state-v1-{content[:24]}"
    root = staging / "datasets" / dataset_id
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_json(root / "definition.json", definition())
    write_json(root / "config.json", inputs.config())
    write_json(root / "schema.json", schema())
    write_ndjson(root / "rows.ndjson", tuple(item.as_dict() for item in computation.rows))
    write_ndjson(root / "dispositions.ndjson", computation.dispositions)
    payload = inventory(root, dataset_id, include_manifest=False)
    manifest = _manifest(dataset_id, inputs, computation, content, payload)
    write_json(root / "manifest.json", manifest)
    dataset_inventory = inventory(root, dataset_id, include_manifest=True)
    descriptor = _descriptor(staging.name, dataset_id, inputs, manifest, dataset_inventory)
    path = staging / descriptor_relative_path(dataset_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(path, descriptor.as_dict())
    return DatasetArtifact(descriptor, content, len(computation.rows))


def verify_dataset(
    root: Path,
    descriptor: DatasetDescriptor,
    budget: ReadBudget | None = None,
) -> dict[str, object]:
    """Verify registered bytes, strict contracts, and logical content linkage."""
    safe_directory(root)
    read_budget = budget or ReadBudget(
        MAX_ARTIFACT_BYTES * 3,
        MAX_ARTIFACT_ENTRIES,
        MAX_ARTIFACT_ROWS,
    )
    registered_tree(
        root,
        f"datasets/{descriptor.dataset_id}/",
        descriptor.dataset_inventory,
        read_budget,
        maximum_file_bytes=MAX_ARTIFACT_FILE_BYTES,
    )
    if inventory(root, descriptor.dataset_id, True, read_budget) != descriptor.dataset_inventory:
        raise ValueError("dataset inventory differs from acceptance descriptor")
    manifest = json_object(root / "manifest.json", read_budget)
    _verify_manifest_linkage(root, descriptor, manifest, read_budget)
    logical = _logical_content(root, read_budget)
    if logical["definition"] != definition() or logical["schema"] != schema():
        raise ValueError("dataset definition or schema is incompatible")
    logical_schemas(logical)
    content = manifest.get("content_digest")
    expected_id = f"crm-deal-state-v1-{content[:24]}" if isinstance(content, str) else ""
    if content != canonical_digest(logical) or descriptor.dataset_id != expected_id:
        raise ValueError("dataset logical content digest is invalid")
    verify_counts(manifest, logical)
    return manifest


def _verify_manifest_linkage(
    root: Path,
    descriptor: DatasetDescriptor,
    manifest: Mapping[str, object],
    budget: ReadBudget,
) -> None:
    if (
        digest_file(root / "manifest.json", budget, maximum_file_bytes=MAX_ARTIFACT_FILE_BYTES)
        != descriptor.manifest_digest
    ):
        raise ValueError("dataset manifest bytes differ from acceptance descriptor")
    manifest_shape(manifest)
    config = parse_config(json_object(root / "config.json", budget))
    if (
        manifest.get("dataset_id") != descriptor.dataset_id
        or manifest.get("request_digest") != descriptor.request_digest
        or manifest.get("config") != descriptor.inputs
        or config != descriptor.inputs
        or manifest.get("schema_version") != DATASET_SCHEMA_VERSION
    ):
        raise ValueError("dataset manifest linkage is invalid")
    payload = manifest.get("payload_inventory")
    expected = inventory(root, descriptor.dataset_id, False, budget)
    if not isinstance(payload, list) or tuple(inventory_item(item) for item in payload) != expected:
        raise ValueError("dataset payload inventory is invalid")


def _logical_content(root: Path, budget: ReadBudget) -> dict[str, object]:
    return {
        "config": parse_config(json_object(root / "config.json", budget)),
        "definition": json_object(root / "definition.json", budget),
        "dispositions": ndjson(root / "dispositions.ndjson", budget),
        "rows": ndjson(root / "rows.ndjson", budget),
        "schema": json_object(root / "schema.json", budget),
    }


def _manifest(
    dataset_id: str,
    inputs: AcceptedInputs,
    computation: DatasetComputation,
    content: str,
    payload: tuple[OutputInventory, ...],
) -> dict[str, object]:
    rows = tuple(item.as_dict() for item in computation.rows)
    return {
        "config": inputs.config(),
        "content_digest": content,
        "dataset_id": dataset_id,
        "disposition_counts": _counts(computation.dispositions, "primary_disposition"),
        "payload_inventory": [inventory_dict(item) for item in payload],
        "provenance": {
            "activity": {
                "bitrix_completeness_asserted": False,
                "completeness": "legacy_partial_snapshot",
                "source_population": "neo4j_existing_records",
            }
        },
        "request_digest": inputs.request.digest,
        "row_counts": _counts(rows, "disposition"),
        "row_count": len(rows),
        "schema_version": DATASET_SCHEMA_VERSION,
    }


def _descriptor(
    run_id: str,
    dataset_id: str,
    inputs: AcceptedInputs,
    manifest: Mapping[str, object],
    dataset_inventory: tuple[OutputInventory, ...],
) -> DatasetDescriptor:
    unsigned = {
        "command": "dataset_build",
        "dataset_id": dataset_id,
        "dataset_inventory": [inventory_dict(item) for item in dataset_inventory],
        "inputs": inputs.config(),
        "manifest_digest": canonical_digest(manifest),
        "request_digest": inputs.request.digest,
        "run_id": run_id,
        "schema_version": DATASET_DESCRIPTOR_SCHEMA,
    }
    return DatasetDescriptor(
        dataset_id,
        inputs.request.digest,
        canonical_digest(manifest),
        run_id,
        dataset_inventory,
        inputs.config(),
        canonical_digest(unsigned),
    )


def _counts(values: tuple[dict[str, object], ...], key: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        item = value.get(key)
        if not isinstance(item, str):
            raise ValueError("dataset disposition is invalid")
        result[item] = result.get(item, 0) + 1
    return dict(sorted(result.items()))


__all__ = (
    "DatasetArtifact",
    "DatasetDescriptor",
    "descriptor_relative_path",
    "parse_descriptor",
    "read_descriptor",
    "verify_dataset",
    "write_dataset",
)
