"""Read-only collection and partitioning for CRM-deal repair inventory."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar, cast, runtime_checkable

from neo4j import ManagedTransaction, Record

from src.crm_deal_identity_repair.classifier import (
    classify_inventory_item,
    inventory_conditions,
)
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.models import RepairInventoryItem, RepairPartition
from src.graph.queries.crm_deal_identity_repair import (
    INVENTORY_ACTIVE_CRM_DEALS,
    INVENTORY_CRM_DEAL_PROJECTIONS,
    INVENTORY_STALE_RUN_CONTROL_PLANE,
)
from src.models import JsonValue

T = TypeVar("T")

_ACTIVE_DEAL_PAGE_SIZE = 100


class RepairInventoryReadClient(Protocol):
    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T: ...


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
    stale_run_evidence: dict[str, JsonValue]

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
    client: RepairInventoryReadClient,
    *,
    source_system: str = "bitrix_chat",
) -> RepairInventory:
    """Read all stored CRM-deal versions and partition graph evidence without mutation."""
    if source_system != "bitrix_chat":
        raise ValueError("CRM-deal repair inventory only supports source_system='bitrix_chat'")

    def _work(
        tx: ManagedTransaction,
    ) -> tuple[tuple[RepairInventoryItem, ...], dict[str, JsonValue]]:
        projections_by_pk: dict[str, list[JsonValue]] = {}
        for projection_record in tx.run(
            INVENTORY_CRM_DEAL_PROJECTIONS,
            source_system=source_system,
        ):
            source_record_pk = _required_string(projection_record, "source_record_pk")
            projection = _json_value(_value(projection_record, "projection"))
            projections_by_pk.setdefault(source_record_pk, []).append(projection)
        items: list[RepairInventoryItem] = []
        skip = 0
        while True:
            page = tuple(
                tx.run(
                    INVENTORY_ACTIVE_CRM_DEALS,
                    source_system=source_system,
                    skip=skip,
                    limit=_ACTIVE_DEAL_PAGE_SIZE,
                )
            )
            items.extend(
                _item_from_record(
                    record,
                    projections_by_pk.get(_required_string(record, "source_record_pk"), []),
                )
                for record in page
            )
            if len(page) < _ACTIVE_DEAL_PAGE_SIZE:
                break
            skip += _ACTIVE_DEAL_PAGE_SIZE
        stale_record = tx.run(
            INVENTORY_STALE_RUN_CONTROL_PLANE,
            source_system=source_system,
            stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
        ).single(strict=True)
        if stale_record is None:
            raise ValueError("repair stale-run graph evidence is unavailable")
        return tuple(items), _stale_run_evidence(stale_record)

    observed, stale_run_evidence = client.execute_read(_work)
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
    authoritative = tuple(item for item in observed if _is_authoritative_item(item))
    active_link_counts = tuple(_active_link_count(item) for item in authoritative)
    active_owner_counts = tuple(_active_owner_count(item) for item in authoritative)
    active_source_ids = {item.source_record_id for item in authoritative}
    cleanup_source_ids = {item.source_record_id for item in cleanup if _is_authoritative_item(item)}
    clean_source_ids = {item.source_record_id for item in clean if _is_authoritative_item(item)}
    multi_linked_source_ids = {
        item.source_record_id for item in authoritative if _active_owner_count(item) > 1
    }
    return RepairInventory(
        ownership_repairs=tuple(ownership),
        projection_cleanups=tuple(cleanup),
        negative_controls=tuple(clean),
        population_counts=RepairPopulationCounts(
            active_deal_count=len(active_source_ids),
            authoritative_version_count=len(authoritative),
            active_link_count=sum(active_link_counts),
            active_distinct_owner_count=sum(active_owner_counts),
            multi_linked_deal_count=len(multi_linked_source_ids),
            maximum_links_per_deal=max(active_link_counts, default=0),
            maximum_distinct_owners_per_deal=max(active_owner_counts, default=0),
            projection_cleanup_deal_count=len(cleanup_source_ids),
            clean_deal_count=len(clean_source_ids),
        ),
        stale_run_evidence=stale_run_evidence,
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
    logical_versions = _sorted_json_objects(_value(record, "logical_versions"), "logical_versions")
    descendants = _sorted_json_objects(_value(record, "descendants"), "descendants")
    decisions_and_reviews = _sorted_json_objects(
        _value(record, "decisions_and_reviews"), "decisions_and_reviews"
    )
    owner_impacts = _sorted_json_objects(_value(record, "owner_impacts"), "owner_impacts")
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
        "lifecycle_policy_evidence": _lifecycle_policy_evidence(raw_payload, normalized_payload),
        "descendants": descendants,
        "decisions_and_reviews": decisions_and_reviews,
        "owner_impacts": owner_impacts,
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


def _stale_run_evidence(record: Record) -> dict[str, JsonValue]:
    state = _required_string(record, "stale_run_state")
    if state != "unknown":
        raise ValueError("repair stale-run state is invalid")
    return {
        "stale_run_id": _required_string(record, "stale_run_id"),
        "state": state,
        "disposition": "investigate",
        "run_status": _optional_string(record, "run_status"),
        "associated_source_system": _optional_string(record, "associated_source_system"),
        "logical_run_association_count": _non_negative_int(record, "logical_run_association_count"),
        "checkpoint_association_count": _non_negative_int(record, "checkpoint_association_count"),
        "execution_allowed": False,
    }


def _lifecycle_policy_evidence(
    raw_payload: JsonValue,
    normalized_payload: JsonValue,
) -> dict[str, JsonValue]:
    raw_policy = _payload_policy(raw_payload)
    normalized_policy = _payload_policy(normalized_payload)
    if raw_policy == "legacy" and normalized_policy in {None, "legacy"}:
        classification = "pre_policy"
        disposition = "preserve"
    elif raw_policy == "crm_deal_identity_v2" and normalized_policy in {
        None,
        "crm_deal_identity_v2",
    }:
        classification = "policy_v2"
        disposition = "review"
    elif raw_policy is None:
        classification = "missing_policy_provenance"
        disposition = "investigate"
    else:
        classification = "conflicting_or_invalid_policy"
        disposition = "investigate"
    return {
        "raw_policy": raw_policy,
        "normalized_policy": normalized_policy,
        "classification": classification,
        "disposition": disposition,
    }


def _payload_policy(value: JsonValue) -> str | None:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return "malformed"
        value = _json_value(decoded)
    if value is None:
        return None
    if not isinstance(value, dict):
        return "malformed"
    policy = value.get("crm_deal_identity_policy_version")
    if policy is None:
        return None
    return policy if isinstance(policy, str) else "malformed"


def _non_negative_int(record: Record, key: str) -> int:
    value = _value(record, key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"repair inventory {key} must be a non-negative integer")
    return value


def _is_authoritative_item(item: RepairInventoryItem) -> bool:
    lifecycle_status = item.payload.get("lifecycle_status")
    is_latest = item.payload.get("is_latest")
    return lifecycle_status == "active" or (lifecycle_status is None and is_latest is True)


def _active_link_count(item: RepairInventoryItem) -> int:
    value = item.payload.get("linked_people")
    if not isinstance(value, list):
        raise ValueError("repair inventory linked_people must be a list")
    return sum(isinstance(link, dict) and link.get("is_active") is not False for link in value)


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
    authoritative_versions = [version for version in objects if _is_authoritative_version(version)]
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
        cast(dict[str, JsonValue], _canonicalize_inventory_value(item)) for item in converted
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


def rebuild_inventory_payload(
    source_properties: dict[str, JsonValue],
    linked_people: list[JsonValue],
    projections: list[JsonValue],
    logical_versions: list[JsonValue],
    descendants: list[JsonValue],
    decisions_and_reviews: list[JsonValue],
    owner_impacts: list[JsonValue],
) -> dict[str, JsonValue]:
    """Rebuild the #300 payload shape from an exact current-state graph snapshot."""
    source_record_pk = _required_mapping_string(source_properties, "source_record_pk")
    raw_payload = _mapping_value(source_properties, "raw_payload")
    normalized_payload = _mapping_value(source_properties, "normalized_payload")
    versions = _sorted_json_objects(logical_versions, "logical_versions")
    return {
        "source_record_version": _mapping_value(source_properties, "source_record_version"),
        "lifecycle_status": _mapping_value(source_properties, "lifecycle_status"),
        "is_latest": _mapping_value(source_properties, "is_latest"),
        "record_hash": _required_mapping_string(source_properties, "record_hash"),
        "observed_at": _optional_mapping_string(source_properties, "observed_at"),
        "raw_payload": raw_payload,
        "normalized_payload": normalized_payload,
        "linked_people": _sorted_json_objects(linked_people, "linked_people"),
        "projections": _sorted_json_objects(projections, "projections"),
        "logical_version_evidence": _logical_version_evidence(
            versions, current_source_record_pk=source_record_pk
        ),
        "lifecycle_policy_evidence": _lifecycle_policy_evidence(raw_payload, normalized_payload),
        "descendants": _sorted_json_objects(descendants, "descendants"),
        "decisions_and_reviews": _sorted_json_objects(
            decisions_and_reviews, "decisions_and_reviews"
        ),
        "owner_impacts": _sorted_json_objects(owner_impacts, "owner_impacts"),
    }


def _required_mapping_string(values: dict[str, JsonValue], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("repair inventory snapshot string is invalid")
    return value


def _optional_mapping_string(values: dict[str, JsonValue], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("repair inventory snapshot optional string is invalid")
    return value


def _mapping_value(values: dict[str, JsonValue], key: str) -> JsonValue:
    if key not in values:
        raise ValueError("repair inventory snapshot property is missing")
    return values[key]
