"""Deterministic partitioning for read-only CRM-deal repair inventory."""

from __future__ import annotations

from src.crm_deal_identity_repair.models import RepairInventoryItem, RepairPartition
from src.models import JsonValue


def classify_inventory_item(item: RepairInventoryItem) -> RepairPartition:
    """Return the primary display partition for one frozen deal version."""
    ownership_repair, projection_cleanup = inventory_conditions(item)
    if ownership_repair:
        return "ownership_repair"
    if projection_cleanup:
        return "projection_cleanup"
    return "negative_control"


def inventory_conditions(item: RepairInventoryItem) -> tuple[bool, bool]:
    """Return independent ownership and projection-cleanup membership flags."""
    links = _objects_from_payload(item.payload, "linked_people")
    active_links: list[dict[str, JsonValue]] = []
    active_owner_ids: set[str] = set()
    malformed_owner = False
    for link in links:
        if link.get("is_active") is False:
            continue
        active_links.append(link)
        person_id = link.get("person_id")
        if not isinstance(person_id, str) or not person_id:
            malformed_owner = True
        else:
            active_owner_ids.add(person_id)
    ownership_repair = len(active_owner_ids) > 1
    projection_cleanup = bool(_logical_version_anomalies(item))
    projection_cleanup = projection_cleanup or malformed_owner
    projection_cleanup = projection_cleanup or len(active_owner_ids) != 1
    projection_cleanup = projection_cleanup or len(active_links) != 1
    sole_owner_id = next(iter(active_owner_ids)) if len(active_owner_ids) == 1 else None
    projections = _objects_from_payload(item.payload, "projections")
    active_projection_keys: set[tuple[str, str, str]] = set()
    for projection in projections:
        if projection.get("is_active") is False:
            continue
        relationship_type = projection.get("relationship_type")
        if relationship_type != "IDENTIFIED_BY":
            projection_cleanup = True
            continue
        identifier_type = projection.get("identifier_type")
        identifier_value = projection.get("identifier_value")
        if identifier_type != "crm_contact_id":
            projection_cleanup = True
            continue
        if not isinstance(identifier_value, str) or not identifier_value:
            projection_cleanup = True
            continue
        owner_person_id = projection.get("owner_person_id")
        if owner_person_id != sole_owner_id:
            projection_cleanup = True
            continue
        source_record_pk = projection.get("source_record_pk")
        if source_record_pk != item.source_record_pk:
            projection_cleanup = True
            continue
        projection_key = (relationship_type, owner_person_id, identifier_value)
        if projection_key in active_projection_keys:
            projection_cleanup = True
        active_projection_keys.add(projection_key)
    return ownership_repair, projection_cleanup


def _logical_version_anomalies(item: RepairInventoryItem) -> tuple[str, ...]:
    evidence = item.payload.get("logical_version_evidence")
    if evidence is None:
        return ()
    if not isinstance(evidence, dict):
        return ("malformed_logical_version_evidence",)
    anomaly_codes = evidence.get("anomaly_codes")
    if not isinstance(anomaly_codes, list):
        return ("malformed_logical_version_evidence",)
    return tuple(code for code in anomaly_codes if isinstance(code, str) and code)


def _objects_from_payload(
    payload: dict[str, JsonValue], key: str
) -> tuple[dict[str, JsonValue], ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"repair inventory {key} must be a list")
    objects: list[dict[str, JsonValue]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"repair inventory {key} must contain objects")
        objects.append(item)
    return tuple(objects)
