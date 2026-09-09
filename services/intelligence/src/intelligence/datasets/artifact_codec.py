"""Descriptor and inventory codecs for immutable dataset publications."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from intelligence.datasets.bounds import MAX_DESCRIPTOR_BYTES, ReadBudget, canonical_json_object
from intelligence.datasets.models import (
    DATASET_DESCRIPTOR_SCHEMA,
    DatasetRequest,
    canonical_digest,
    parse_config,
    safe_component,
)
from intelligence.models import OutputInventory


@dataclass(frozen=True)
class DatasetDescriptor:
    """State-registerable acceptance descriptor outside the dataset payload inventory."""

    dataset_id: str
    request_digest: str
    manifest_digest: str
    run_id: str
    dataset_inventory: tuple[OutputInventory, ...]
    inputs: dict[str, object]
    digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "command": "dataset_build",
            "dataset_id": self.dataset_id,
            "dataset_inventory": [inventory_dict(item) for item in self.dataset_inventory],
            "digest": self.digest,
            "inputs": self.inputs,
            "manifest_digest": self.manifest_digest,
            "request_digest": self.request_digest,
            "run_id": self.run_id,
            "schema_version": DATASET_DESCRIPTOR_SCHEMA,
        }


def descriptor_relative_path(dataset_id: str) -> str:
    safe_component(dataset_id, "dataset id")
    return f"acceptance-descriptors/datasets/{dataset_id}.json"


def parse_descriptor(value: Mapping[str, object]) -> DatasetDescriptor:
    expected = {
        "command",
        "dataset_id",
        "dataset_inventory",
        "digest",
        "inputs",
        "manifest_digest",
        "request_digest",
        "run_id",
        "schema_version",
    }
    raw = dict(value)
    if set(raw) != expected or raw.get("schema_version") != DATASET_DESCRIPTOR_SCHEMA:
        raise ValueError("dataset acceptance descriptor schema is invalid")
    if raw.get("command") != "dataset_build":
        raise ValueError("dataset acceptance descriptor command is invalid")
    digest = digest_value(raw.get("digest"), "descriptor digest")
    unsigned = dict(raw)
    unsigned.pop("digest")
    if digest != canonical_digest(unsigned):
        raise ValueError("dataset acceptance descriptor digest is invalid")
    dataset_id = text(raw, "dataset_id")
    run_id = text(raw, "run_id")
    safe_component(dataset_id, "dataset id")
    safe_component(run_id, "dataset run")
    inputs = parse_config(raw.get("inputs"))
    request_digest = digest_value(raw.get("request_digest"), "request digest")
    if request_digest != _request_from_config(inputs).digest:
        raise ValueError("dataset acceptance descriptor request linkage is invalid")
    return DatasetDescriptor(
        dataset_id,
        request_digest,
        digest_value(raw.get("manifest_digest"), "manifest digest"),
        run_id,
        inventory_values(raw.get("dataset_inventory"), dataset_id),
        inputs,
        digest,
    )


def read_descriptor(path: Path) -> DatasetDescriptor:
    return parse_descriptor(
        canonical_json_object(
            path,
            ReadBudget(MAX_DESCRIPTOR_BYTES, 1, 1),
            maximum_file_bytes=MAX_DESCRIPTOR_BYTES,
        )
    )


def inventory_values(value: object, dataset_id: str) -> tuple[OutputInventory, ...]:
    if not isinstance(value, list):
        raise ValueError("dataset inventory is invalid")
    parsed = tuple(inventory_item(item) for item in value)
    prefix = f"datasets/{dataset_id}/"
    if not parsed or parsed != tuple(sorted(parsed, key=lambda item: item.relative_path)):
        raise ValueError("dataset inventory is unordered")
    if len({item.relative_path for item in parsed}) != len(parsed):
        raise ValueError("dataset inventory has duplicate evidence")
    if any(not item.relative_path.startswith(prefix) for item in parsed):
        raise ValueError("dataset inventory path is invalid")
    return parsed


def inventory_item(value: object) -> OutputInventory:
    if not isinstance(value, Mapping) or set(value) != {"byte_count", "relative_path", "sha256"}:
        raise ValueError("dataset inventory item is invalid")
    path = value.get("relative_path")
    count = value.get("byte_count")
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or "\\" in path
        or ".." in PurePosixPath(path).parts
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
    ):
        raise ValueError("dataset inventory item is invalid")
    return OutputInventory(
        path, digest_value(value.get("sha256"), "dataset inventory digest"), count
    )


def inventory_dict(item: OutputInventory) -> dict[str, object]:
    return {
        "byte_count": item.byte_count,
        "relative_path": item.relative_path,
        "sha256": item.sha256,
    }


def text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} is invalid")
    return result


def digest_value(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _request_from_config(value: dict[str, object]) -> DatasetRequest:
    inputs = value.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("dataset descriptor inputs are invalid")
    deals = inputs.get("deal_refs")
    activities = inputs.get("activities")
    if not isinstance(deals, dict) or not isinstance(activities, dict):
        raise ValueError("dataset descriptor inputs are invalid")
    seed = value.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("dataset descriptor seed is invalid")
    return DatasetRequest(
        text(deals, "run_id"),
        text(activities, "checkpoint_id"),
        text(activities, "accepted_run_id"),
        text(value, "definition"),
        text(value, "feature_cutoff"),
        text(value, "label_cutoff"),
        seed,
    )
