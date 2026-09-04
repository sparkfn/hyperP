"""Strict #254 inventory and authenticated count validation for #300 admission."""

from __future__ import annotations

import json
from collections.abc import Iterable

from src.connectors.bitrix_stage_history.artifact_manifest import (
    ArtifactManifest,
    canonical_json_bytes,
)
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.models import RepairInventoryItem, RepairPartition
from src.models import JsonValue

_INVENTORY_ROW_KEYS = frozenset(
    {
        "source_system",
        "source_record_id",
        "source_record_pk",
        "deal_id",
        "partition",
        "repair_conditions",
        "graph_fingerprint",
        "stored_payload_fingerprint",
        "payload",
        "execution_allowed",
    }
)
_POPULATION_COUNT_KEYS = frozenset(
    {
        "active_deal_count",
        "authoritative_version_count",
        "active_link_count",
        "active_distinct_owner_count",
        "multi_linked_deal_count",
        "maximum_links_per_deal",
        "maximum_distinct_owners_per_deal",
        "projection_cleanup_deal_count",
        "clean_deal_count",
    }
)
_INVENTORY_PAYLOAD_KEYS = frozenset(
    {
        "source_record_version",
        "lifecycle_status",
        "is_latest",
        "record_hash",
        "observed_at",
        "raw_payload",
        "normalized_payload",
        "linked_people",
        "projections",
        "logical_version_evidence",
        "lifecycle_policy_evidence",
        "descendants",
        "decisions_and_reviews",
        "owner_impacts",
    }
)
_GRAPH_FINGERPRINT_DOMAIN = b"crm-deal-repair-graph-v1\x00"
_SOURCE_FINGERPRINT_DOMAIN = b"crm-deal-repair-source-v1\x00"


def validate_artifact_count_boundary(
    manifest: ArtifactManifest,
    documents: dict[str, bytes],
    digest: str,
    inventory_row_count: int,
    eligible_count: int,
    negative_count: int,
) -> None:
    population_counts = _population_counts(manifest.metadata["population_counts"])
    counts = canonical_json_object(
        manifest.provenance.counts_json.encode("utf-8"), "repair artifact provenance counts"
    )
    expected_counts: dict[str, JsonValue] = {
        "inventory_rows": inventory_row_count,
        **population_counts,
    }
    if counts != expected_counts:
        raise RuntimeError("repair artifact provenance counts do not match authenticated metadata")
    if inventory_row_count != eligible_count + negative_count or eligible_count < 1:
        raise RuntimeError("repair artifact inventory population counts are inconsistent")
    _validate_count_documents(documents, digest, population_counts)


def inventory_source_record_pks(content: bytes) -> tuple[tuple[str, ...], int, int]:
    return inventory_source_record_pks_from_lines(content.splitlines(keepends=True))


def inventory_source_record_pks_from_lines(
    lines: Iterable[bytes],
) -> tuple[tuple[str, ...], int, int]:
    """Validate canonical inventory rows across one or more bounded files."""
    previous_key: str | None = None
    source_record_pks: list[str] = []
    observed_source_record_pks: set[str] = set()
    negative_count = 0
    for line in lines:
        if not line.endswith(b"\n"):
            raise RuntimeError("repair inventory JSONL is not canonical")
        item = _inventory_item(canonical_json_object(line, "repair inventory JSONL"))
        if previous_key is not None and item.inventory_key <= previous_key:
            raise RuntimeError("repair inventory rows are not in canonical inventory-key order")
        previous_key = item.inventory_key
        if item.source_record_pk in observed_source_record_pks:
            raise RuntimeError("repair inventory source record identities are invalid")
        observed_source_record_pks.add(item.source_record_pk)
        source_record_pks.append(item.source_record_pk)
        if item.partition == "negative_control":
            negative_count += 1
    if not source_record_pks:
        raise RuntimeError("repair inventory source record identities are invalid")
    eligible_count = len(source_record_pks) - negative_count
    if eligible_count < 1:
        raise RuntimeError("repair inventory contains no eligible repair units")
    return tuple(sorted(source_record_pks)), eligible_count, negative_count


def canonical_json_object(content: bytes, label: str) -> dict[str, JsonValue]:
    try:
        value: object = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(label + " is unreadable") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != content:
        raise RuntimeError(label + " is not canonical")
    return _json_value_object(value, label)


def _validate_count_documents(
    documents: dict[str, bytes], digest: str, population_counts: dict[str, int]
) -> None:
    impact = canonical_json_object(documents["impact-summary.json"], "repair impact summary")
    clean_boundary = canonical_json_object(
        documents["clean-boundary-plan.json"], "repair clean boundary"
    )
    if impact.get("inventory_digest") != digest or clean_boundary.get("inventory_digest") != digest:
        raise RuntimeError("repair artifact document inventory digest is inconsistent")
    if impact.get("population_counts") != population_counts:
        raise RuntimeError("repair artifact impact population counts are inconsistent")


def _population_counts(value: JsonValue) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != _POPULATION_COUNT_KEYS:
        raise RuntimeError("repair artifact population counts are invalid")
    counts: dict[str, int] = {}
    for key, count in value.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise RuntimeError("repair artifact population counts are invalid")
        counts[key] = count
    if counts["authoritative_version_count"] < counts["active_deal_count"]:
        raise RuntimeError("repair artifact authoritative population count is inconsistent")
    if (
        max(counts["multi_linked_deal_count"], counts["projection_cleanup_deal_count"])
        + counts["clean_deal_count"]
        > counts["active_deal_count"]
    ):
        raise RuntimeError("repair artifact population classifications are inconsistent")
    if counts["active_distinct_owner_count"] > counts["active_link_count"]:
        raise RuntimeError("repair artifact active owner count is inconsistent")
    if counts["maximum_distinct_owners_per_deal"] > counts["maximum_links_per_deal"]:
        raise RuntimeError("repair artifact maximum owner count is inconsistent")
    return counts


def _inventory_item(value: dict[str, JsonValue]) -> RepairInventoryItem:
    if set(value) != _INVENTORY_ROW_KEYS:
        raise RuntimeError("repair inventory row schema is invalid")
    source_system = _required_string(value, "source_system")
    source_record_id = _required_string(value, "source_record_id")
    source_record_pk = _required_string(value, "source_record_pk")
    deal_id = _required_string(value, "deal_id")
    if source_system != "bitrix_chat" or source_record_id != "bitrix-crm-deal-" + deal_id:
        raise RuntimeError("repair inventory row identity is invalid")
    if value["execution_allowed"] is not False:
        raise RuntimeError("repair inventory rows must remain non-executable")
    payload = value["payload"]
    conditions = value["repair_conditions"]
    if not isinstance(payload, dict) or not isinstance(conditions, list):
        raise RuntimeError("repair inventory row evidence is invalid")
    try:
        item = RepairInventoryItem(
            source_system=source_system,
            source_record_id=source_record_id,
            source_record_pk=source_record_pk,
            deal_id=deal_id,
            partition=_partition(value["partition"]),
            repair_conditions=tuple(_partition(condition) for condition in conditions),
            graph_fingerprint=_required_string(value, "graph_fingerprint"),
            stored_payload_fingerprint=_required_string(value, "stored_payload_fingerprint"),
            payload=payload,
        )
    except ValueError as exc:
        raise RuntimeError("repair inventory row is invalid") from exc
    if item.to_dict() != value:
        raise RuntimeError("repair inventory row is internally inconsistent")
    _validate_row_fingerprints(item)
    return item


def _validate_row_fingerprints(item: RepairInventoryItem) -> None:
    payload = item.payload
    if set(payload) != _INVENTORY_PAYLOAD_KEYS:
        raise RuntimeError("repair inventory row payload schema is invalid")
    record_hash = payload["record_hash"]
    if not isinstance(record_hash, str) or not record_hash:
        raise RuntimeError("repair inventory row record hash is invalid")
    graph_fingerprint, stored_payload_fingerprint = inventory_payload_fingerprints(payload)
    if item.graph_fingerprint != graph_fingerprint:
        raise RuntimeError("repair inventory graph fingerprint is inconsistent")
    if item.stored_payload_fingerprint != stored_payload_fingerprint:
        raise RuntimeError("repair inventory source fingerprint is inconsistent")


def inventory_payload_fingerprints(payload: dict[str, JsonValue]) -> tuple[str, str]:
    """Return the exact #300 graph and stored-payload fingerprints for one payload."""
    record_hash = payload.get("record_hash")
    if not isinstance(record_hash, str) or not record_hash:
        raise ValueError("repair inventory record hash is invalid")
    graph_fingerprint = object_digest(_GRAPH_FINGERPRINT_DOMAIN, payload)
    stored_payload_fingerprint = object_digest(
        _SOURCE_FINGERPRINT_DOMAIN,
        {
            "record_hash": record_hash,
            "raw_payload": payload.get("raw_payload"),
            "normalized_payload": payload.get("normalized_payload"),
        },
    )
    return graph_fingerprint, stored_payload_fingerprint


def _partition(value: JsonValue) -> RepairPartition:
    if value == "ownership_repair":
        return "ownership_repair"
    if value == "projection_cleanup":
        return "projection_cleanup"
    if value == "negative_control":
        return "negative_control"
    raise RuntimeError("repair inventory partition is invalid")


def _required_string(value: dict[str, JsonValue], key: str) -> str:
    item = value[key]
    if not isinstance(item, str) or not item:
        raise RuntimeError("repair inventory row string is invalid")
    return item


def _json_value_object(value: dict[object, object], label: str) -> dict[str, JsonValue]:
    converted: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RuntimeError(label + " has invalid object keys")
        converted[key] = _json_value(item, label)
    return converted


def _json_value(value: object, label: str) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item, label) for item in value]
    if isinstance(value, dict):
        return _json_value_object(value, label)
    raise RuntimeError(label + " has an invalid JSON value")
