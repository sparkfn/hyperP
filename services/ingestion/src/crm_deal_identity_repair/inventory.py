"""Read-only collection and partitioning for CRM-deal repair inventory."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from neo4j import ManagedTransaction, Record

from src.crm_deal_identity_repair.classifier import (
    classify_inventory_item,
    inventory_conditions,
)
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.models import RepairInventoryItem, RepairPartition
from src.graph.client import Neo4jClient
from src.graph.queries.crm_deal_identity_repair import (
    INVENTORY_ACTIVE_CRM_DEALS,
    INVENTORY_CRM_DEAL_PROJECTIONS,
)
from src.models import JsonValue

_DEFAULT_NEGATIVE_CONTROL_LIMIT = 100


@dataclass(frozen=True)
class RepairPopulationCounts:
    """Global active-deal baseline captured before control sampling."""

    active_deal_count: int
    authoritative_version_count: int
    active_link_count: int
    active_distinct_owner_count: int
    multi_linked_deal_count: int
    maximum_links_per_deal: int
    maximum_distinct_owners_per_deal: int
    projection_cleanup_deal_count: int
    clean_deal_count: int

    def to_dict(self) -> dict[str, int]:
        return {
            "active_deal_count": self.active_deal_count,
            "authoritative_version_count": self.authoritative_version_count,
            "active_link_count": self.active_link_count,
            "active_distinct_owner_count": self.active_distinct_owner_count,
            "multi_linked_deal_count": self.multi_linked_deal_count,
            "maximum_links_per_deal": self.maximum_links_per_deal,
            "maximum_distinct_owners_per_deal": self.maximum_distinct_owners_per_deal,
            "projection_cleanup_deal_count": self.projection_cleanup_deal_count,
            "clean_deal_count": self.clean_deal_count,
        }


@dataclass(frozen=True)
class RepairInventory:
    """One deterministic read-only graph snapshot partitioned for repair review."""

    ownership_repairs: tuple[RepairInventoryItem, ...]
    projection_cleanups: tuple[RepairInventoryItem, ...]
    negative_controls: tuple[RepairInventoryItem, ...]
    population_counts: RepairPopulationCounts

    @property
    def items(self) -> tuple[RepairInventoryItem, ...]:
        by_identity = {
            item.inventory_key: item
            for item in (
                *self.ownership_repairs,
                *self.projection_cleanups,
                *self.negative_controls,
            )
        }
        return tuple(by_identity[key] for key in sorted(by_identity))


def collect_repair_inventory(
    client: Neo4jClient,
    *,
    source_system: str = "bitrix_chat",
    negative_control_limit: int = _DEFAULT_NEGATIVE_CONTROL_LIMIT,
) -> RepairInventory:
    """Discover and partition active CRM deals without issuing graph mutation."""
    if source_system != "bitrix_chat":
        raise ValueError("CRM-deal repair inventory only supports source_system='bitrix_chat'")
    if negative_control_limit < 1:
        raise ValueError("repair negative control limit must be positive")

    def _work(tx: ManagedTransaction) -> tuple[RepairInventoryItem, ...]:
        projections_by_pk: dict[str, list[JsonValue]] = {}
        for projection_record in tx.run(
            INVENTORY_CRM_DEAL_PROJECTIONS,
            source_system=source_system,
        ):
            source_record_pk = _required_string(projection_record, "source_record_pk")
            projection = _json_value(_value(projection_record, "projection"))
            projections_by_pk.setdefault(source_record_pk, []).append(projection)
        return tuple(
            _item_from_record(
                record,
                projections_by_pk.get(_required_string(record, "source_record_pk"), []),
            )
            for record in tx.run(INVENTORY_ACTIVE_CRM_DEALS, source_system=source_system)
        )

    observed = client.execute_read(_work)
    ownership: list[RepairInventoryItem] = []
    cleanup: list[RepairInventoryItem] = []
    clean: list[RepairInventoryItem] = []
    for item in observed:
        partition = classify_inventory_item(item)
        ownership_condition, cleanup_condition = inventory_conditions(item)
        conditions: tuple[RepairPartition, ...]
        if ownership_condition and cleanup_condition:
            conditions = ("ownership_repair", "projection_cleanup")
        elif ownership_condition:
            conditions = ("ownership_repair",)
        elif cleanup_condition:
            conditions = ("projection_cleanup",)
        else:
            conditions = ("negative_control",)
        partitioned = RepairInventoryItem(
            source_system=item.source_system,
            source_record_id=item.source_record_id,
            source_record_pk=item.source_record_pk,
            deal_id=item.deal_id,
            partition=partition,
            repair_conditions=conditions,
            graph_fingerprint=item.graph_fingerprint,
            stored_payload_fingerprint=item.stored_payload_fingerprint,
            payload=item.payload,
        )
        if ownership_condition:
            ownership.append(partitioned)
        if cleanup_condition:
            cleanup.append(partitioned)
        if not ownership_condition and not cleanup_condition:
            clean.append(partitioned)
    active_link_counts = tuple(_active_link_count(item) for item in observed)
    active_owner_counts = tuple(_active_owner_count(item) for item in observed)
    return RepairInventory(
        ownership_repairs=tuple(ownership),
        projection_cleanups=tuple(cleanup),
        negative_controls=tuple(clean[:negative_control_limit]),
        population_counts=RepairPopulationCounts(
            active_deal_count=len({item.source_record_id for item in observed}),
            authoritative_version_count=len(observed),
            active_link_count=sum(active_link_counts),
            active_distinct_owner_count=sum(active_owner_counts),
            multi_linked_deal_count=len({item.source_record_id for item in ownership}),
            maximum_links_per_deal=max(active_link_counts, default=0),
            maximum_distinct_owners_per_deal=max(active_owner_counts, default=0),
            projection_cleanup_deal_count=len(
                {item.source_record_id for item in cleanup}
            ),
            clean_deal_count=len({item.source_record_id for item in clean}),
        ),
    )


def _item_from_record(
    record: Record,
    projection_rows: list[JsonValue],
) -> RepairInventoryItem:
    source_record_id = _required_string(record, "source_record_id")
    source_record_pk = _required_string(record, "source_record_pk")
    raw_payload = _json_value(_value(record, "raw_payload"))
    normalized_payload = _json_value(_value(record, "normalized_payload"))
    links = _sorted_json_objects(_value(record, "linked_people"), "linked_people")
    projections = _sorted_json_objects(projection_rows, "projections")
    logical_versions = _sorted_json_objects(
        _value(record, "logical_versions"), "logical_versions"
    )
    version_evidence = _logical_version_evidence(
        logical_versions,
        current_source_record_pk=source_record_pk,
    )
    payload: dict[str, JsonValue] = {
        "source_record_version": _json_value(_value(record, "source_record_version")),
        "lifecycle_status": _json_value(_value(record, "lifecycle_status")),
        "is_latest": _json_value(_value(record, "is_latest")),
        "record_hash": _required_string(record, "record_hash"),
        "observed_at": _optional_string(record, "observed_at"),
        "raw_payload": raw_payload,
        "normalized_payload": normalized_payload,
        "linked_people": links,
        "projections": projections,
        "logical_version_evidence": version_evidence,
    }
    return RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id=source_record_id,
        source_record_pk=source_record_pk,
        deal_id=_deal_id(source_record_id),
        partition="negative_control",
        graph_fingerprint=object_digest(b"crm-deal-repair-graph-v1\x00", payload),
        stored_payload_fingerprint=object_digest(
            b"crm-deal-repair-source-v1\x00",
            {
                "record_hash": payload["record_hash"],
                "raw_payload": raw_payload,
                "normalized_payload": normalized_payload,
            },
        ),
        payload=payload,
    )


def _active_link_count(item: RepairInventoryItem) -> int:
    value = item.payload.get("linked_people")
    if not isinstance(value, list):
        raise ValueError("repair inventory linked_people must be a list")
    return sum(
        isinstance(link, dict) and link.get("is_active") is not False
        for link in value
    )


def _active_owner_count(item: RepairInventoryItem) -> int:
    return len(_validated_active_owner_ids(item))


def _validated_active_owner_ids(item: RepairInventoryItem) -> frozenset[str]:
    value = item.payload.get("linked_people")
    if not isinstance(value, list):
        raise ValueError("repair inventory linked_people must be a list")
    owners: set[str] = set()
    for link in value:
        if not isinstance(link, dict) or link.get("is_active") is False:
            continue
        person_id = link.get("person_id")
        if isinstance(person_id, str) and person_id:
            owners.add(person_id)
    return frozenset(owners)


def _logical_version_evidence(
    versions: list[JsonValue],
    *,
    current_source_record_pk: str,
) -> dict[str, JsonValue]:
    objects = [cast(dict[str, JsonValue], item) for item in versions]
    anomaly_codes: set[str] = set()
    authoritative_versions = [
        version for version in objects if _is_authoritative_version(version)
    ]
    if len(authoritative_versions) != 1:
        anomaly_codes.add("multiple_authoritative_versions")
    version_numbers: list[int] = []
    current_found = False
    for version in objects:
        source_record_pk = version.get("source_record_pk")
        if source_record_pk == current_source_record_pk:
            current_found = True
        parsed_version = _positive_int(version.get("source_record_version"))
        if parsed_version is None:
            anomaly_codes.add("invalid_source_record_version")
        else:
            version_numbers.append(parsed_version)
        lifecycle_status = version.get("lifecycle_status")
        is_latest = version.get("is_latest")
        if lifecycle_status == "active" and is_latest is not True:
            anomaly_codes.add("active_version_not_latest")
        elif lifecycle_status is None and not isinstance(is_latest, bool):
            anomaly_codes.add("invalid_legacy_latest_marker")
        elif lifecycle_status not in {None, "active"} and is_latest is True:
            anomaly_codes.add("inactive_version_marked_latest")
    if len(version_numbers) != len(set(version_numbers)):
        anomaly_codes.add("duplicate_source_record_version")
    if version_numbers:
        ordered_versions = sorted(set(version_numbers))
        expected_versions = list(range(ordered_versions[0], ordered_versions[-1] + 1))
        if ordered_versions != expected_versions:
            anomaly_codes.add("non_contiguous_source_record_versions")
    if not current_found:
        anomaly_codes.add("current_version_missing_from_logical_evidence")
    anomaly_values: list[JsonValue] = [code for code in sorted(anomaly_codes)]
    return {
        "authoritative_versions": versions,
        "anomaly_codes": anomaly_values,
    }


def _is_authoritative_version(version: dict[str, JsonValue]) -> bool:
    lifecycle_status = version.get("lifecycle_status")
    return lifecycle_status == "active" or (
        lifecycle_status is None and version.get("is_latest") is True
    )


def _positive_int(value: JsonValue) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _sorted_json_objects(value: object, label: str) -> list[JsonValue]:
    converted = _json_value(value)
    if not isinstance(converted, list) or any(not isinstance(item, dict) for item in converted):
        raise ValueError(f"repair inventory {label} must contain objects")
    objects = [
        cast(dict[str, JsonValue], _canonicalize_inventory_value(item))
        for item in converted
    ]
    return sorted(objects, key=_canonical_json_sort_key)


def _canonicalize_inventory_value(value: JsonValue, *, key: str | None = None) -> JsonValue:
    if isinstance(value, dict):
        return {
            item_key: _canonicalize_inventory_value(item, key=item_key)
            for item_key, item in sorted(value.items())
        }
    if isinstance(value, list):
        converted = [_canonicalize_inventory_value(item) for item in value]
        if key == "labels":
            return sorted(converted, key=_canonical_json_sort_key)
        return converted
    return value


def _canonical_json_sort_key(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _value(record: Record, key: str) -> object:
    try:
        return record[key]
    except KeyError as exc:
        raise ValueError(f"repair inventory query omitted {key}") from exc


def _required_string(record: Record, key: str) -> str:
    value = _value(record, key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"repair inventory {key} must be a non-empty string")
    return value


def _optional_string(record: Record, key: str) -> str | None:
    value = _value(record, key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"repair inventory {key} must be a string or null")
    return value


def _deal_id(source_record_id: str) -> str:
    prefix = "bitrix-crm-deal-"
    if not source_record_id.startswith(prefix) or len(source_record_id) == len(prefix):
        raise ValueError("repair inventory source record must be a Bitrix CRM deal")
    return source_record_id.removeprefix(prefix)


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("repair inventory JSON object keys must be strings")
        return {cast(str, key): _json_value(item) for key, item in value.items()}
    if isinstance(value, _IsoFormatValue):
        return value.iso_format()
    raise ValueError("repair inventory query returned a non-JSON value")


@runtime_checkable
class _IsoFormatValue(Protocol):
    def iso_format(self) -> str: ...
