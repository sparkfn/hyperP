"""Tests for CRM-deal repair graph discovery and inventory partitioning."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, cast

from neo4j import ManagedTransaction
from src.crm_deal_identity_repair.classifier import classify_inventory_item
from src.crm_deal_identity_repair.inventory import RepairInventory, collect_repair_inventory
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.graph.client import Neo4jClient
from src.graph.queries.crm_deal_identity_repair import (
    INVENTORY_ACTIVE_CRM_DEALS,
    INVENTORY_CRM_DEAL_PROJECTIONS,
)

_T = TypeVar("_T")


class _Transaction:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self._rows = rows

    def run(self, query: str, **parameters: object) -> tuple[dict[str, object], ...]:
        assert parameters == {"source_system": "bitrix_chat"}
        if query == INVENTORY_CRM_DEAL_PROJECTIONS:
            return tuple(
                {
                    "source_record_pk": row["source_record_pk"],
                    "projection": projection,
                }
                for row in self._rows
                for projection in cast(list[dict[str, object]], row["projections"])
            )
        assert query == INVENTORY_ACTIVE_CRM_DEALS
        return self._rows


class _Client:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self._rows = rows

    def execute_read(self, work: Callable[[ManagedTransaction], _T]) -> _T:
        return work(cast(ManagedTransaction, _Transaction(self._rows)))


def _endpoint(*, labels: list[str], person_id: str | None = None) -> dict[str, object]:
    return {
        "labels": labels,
        "person_id": person_id,
        "source_record_pk": None,
        "source_record_id": None,
        "identifier_type": None,
        "normalized_value": None,
        "address_id": None,
        "entity_key": None,
        "source_key": None,
    }


def _link(person_id: str | None, *, sequence: int = 1) -> dict[str, object]:
    return {
        "person_id": person_id,
        "is_active": True,
        "relationship_type": "LINKED_TO",
        "relationship_properties": {
            "source_record_pk": "deal-pk",
            "is_active": True,
            "confidence": 0.95,
            "sequence": sequence,
        },
        "start_endpoint": {
            "labels": ["SourceRecord", "Evidence"],
            "source_record_pk": "deal-pk",
            "source_record_id": "bitrix-crm-deal-10",
        },
        "end_endpoint": _endpoint(labels=["Person", "Profile"], person_id=person_id),
    }


def _logical_version(
    *,
    source_record_pk: str = "deal-pk",
    source_record_version: object = "1",
    lifecycle_status: str | None = "active",
    is_latest: bool = True,
) -> dict[str, object]:
    return {
        "source_record_pk": source_record_pk,
        "source_record_version": source_record_version,
        "lifecycle_status": lifecycle_status,
        "is_latest": is_latest,
    }


def _row(
    *,
    source_record_pk: str = "deal-pk",
    source_record_version: object = "1",
    links: list[dict[str, object]] | None = None,
    projections: list[dict[str, object]] | None = None,
    logical_versions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "source_record_pk": source_record_pk,
        "source_record_id": "bitrix-crm-deal-10",
        "source_record_version": source_record_version,
        "lifecycle_status": "active",
        "is_latest": True,
        "record_hash": "record-hash",
        "observed_at": "2026-08-25T00:00:00Z",
        "raw_payload": "{}",
        "normalized_payload": "{}",
        "linked_people": links if links is not None else [_link("person-a")],
        "projections": projections if projections is not None else [],
        "logical_versions": (
            logical_versions
            if logical_versions is not None
            else [
                _logical_version(
                    source_record_pk=source_record_pk,
                    source_record_version=source_record_version,
                )
            ]
        ),
    }


def _inventory(*rows: dict[str, object]) -> RepairInventory:
    return collect_repair_inventory(cast(Neo4jClient, _Client(tuple(rows))))


def _item(
    *,
    links: list[dict[str, object]],
    projections: list[dict[str, object]],
) -> RepairInventoryItem:
    linked_people = []
    for index, link in enumerate(links):
        copied_link = dict(link)
        copied_link.setdefault("person_id", f"person-{index + 1}")
        linked_people.append(copied_link)
    return RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-deal-10",
        source_record_pk="deal-pk",
        deal_id="10",
        partition="negative_control",
        graph_fingerprint="sha256:" + "a" * 64,
        stored_payload_fingerprint="sha256:" + "b" * 64,
        payload={
            "linked_people": linked_people,
            "projections": [dict(item) for item in projections],
        },
    )


def test_graph_query_captures_full_relationship_and_endpoint_evidence() -> None:
    assert "relationship_properties: properties(link)" in INVENTORY_ACTIVE_CRM_DEALS
    assert "start_endpoint:" in INVENTORY_ACTIVE_CRM_DEALS
    assert "end_endpoint:" in INVENTORY_ACTIVE_CRM_DEALS
    assert "logical_versions" in INVENTORY_ACTIVE_CRM_DEALS
    assert "source_record_version AS source_record_version" in INVENTORY_ACTIVE_CRM_DEALS
    assert "MATCH (start)-[projection]->(target)" in INVENTORY_CRM_DEAL_PROJECTIONS
    assert "relationship_properties: properties(projection)" in (
        INVENTORY_CRM_DEAL_PROJECTIONS
    )
    assert "endpoint_properties: properties(start)" in INVENTORY_CRM_DEAL_PROJECTIONS
    assert "CALL {" not in INVENTORY_CRM_DEAL_PROJECTIONS


def test_duplicate_links_to_one_person_are_not_multi_owner_contamination() -> None:
    inventory = _inventory(
        _row(links=[_link("person-a", sequence=1), _link("person-a", sequence=2)])
    )

    assert inventory.ownership_repairs == ()
    assert len(inventory.projection_cleanups) == 1
    assert inventory.population_counts.active_link_count == 2
    assert inventory.population_counts.active_distinct_owner_count == 1
    assert inventory.population_counts.maximum_links_per_deal == 2
    assert inventory.population_counts.maximum_distinct_owners_per_deal == 1


def test_distinct_validated_active_people_define_ownership_contamination() -> None:
    inventory = _inventory(_row(links=[_link("person-a"), _link("person-b")]))

    assert len(inventory.ownership_repairs) == 1
    assert inventory.population_counts.multi_linked_deal_count == 1
    assert inventory.population_counts.active_distinct_owner_count == 2


def test_active_link_without_a_valid_person_id_fails_closed() -> None:
    inventory = _inventory(_row(links=[_link(None)]))

    assert len(inventory.projection_cleanups) == 1
    assert inventory.negative_controls == ()


def test_duplicate_authoritative_versions_are_projection_cleanup() -> None:
    versions = [
        _logical_version(source_record_pk="deal-pk", source_record_version="1"),
        _logical_version(source_record_pk="deal-pk-2", source_record_version="2"),
    ]
    inventory = _inventory(
        _row(source_record_pk="deal-pk", source_record_version="1", logical_versions=versions),
        _row(source_record_pk="deal-pk-2", source_record_version="2", logical_versions=versions),
    )

    assert len(inventory.projection_cleanups) == 2
    assert inventory.population_counts.active_deal_count == 1
    assert inventory.population_counts.authoritative_version_count == 2
    for item in inventory.projection_cleanups:
        evidence = item.payload["logical_version_evidence"]
        assert isinstance(evidence, dict)
        assert evidence["anomaly_codes"] == ["multiple_authoritative_versions"]


def test_ownership_and_projection_populations_overlap_independently() -> None:
    inventory = _inventory(
        _row(
            links=[_link("person-a"), _link("person-b")],
            projections=[
                {
                    "is_active": True,
                    "relationship_type": "HAS_FACT",
                    "owner_person_id": "person-a",
                    "source_record_pk": "deal-pk",
                }
            ],
        )
    )

    assert len(inventory.ownership_repairs) == 1
    assert len(inventory.projection_cleanups) == 1
    assert len(inventory.items) == 1
    assert inventory.items[0].repair_conditions == (
        "ownership_repair",
        "projection_cleanup",
    )
    assert inventory.population_counts.multi_linked_deal_count == 1
    assert inventory.population_counts.projection_cleanup_deal_count == 1


def test_inactive_version_marked_latest_fails_closed() -> None:
    versions = [
        _logical_version(),
        _logical_version(
            source_record_pk="old-pk",
            source_record_version="2",
            lifecycle_status="superseded",
            is_latest=True,
        ),
    ]
    inventory = _inventory(_row(logical_versions=versions))

    assert len(inventory.projection_cleanups) == 1
    evidence = inventory.projection_cleanups[0].payload["logical_version_evidence"]
    assert isinstance(evidence, dict)
    assert "inactive_version_marked_latest" in evidence["anomaly_codes"]


def test_invalid_source_record_version_fails_closed_with_evidence() -> None:
    inventory = _inventory(_row(source_record_version="not-a-version"))

    assert len(inventory.projection_cleanups) == 1
    evidence = inventory.projection_cleanups[0].payload["logical_version_evidence"]
    assert isinstance(evidence, dict)
    assert evidence["anomaly_codes"] == ["invalid_source_record_version"]


def test_nested_endpoint_labels_and_relationship_rows_are_deterministic() -> None:
    first = _inventory(_row(links=[_link("person-b"), _link("person-a")]))
    second = _inventory(_row(links=[_link("person-a"), _link("person-b")]))

    first_item = first.ownership_repairs[0]
    second_item = second.ownership_repairs[0]
    assert first_item.graph_fingerprint == second_item.graph_fingerprint
    linked_people = first_item.payload["linked_people"]
    assert isinstance(linked_people, list)
    for link in linked_people:
        assert isinstance(link, dict)
        start = link["start_endpoint"]
        end = link["end_endpoint"]
        assert isinstance(start, dict)
        assert isinstance(end, dict)
        assert start["labels"] == ["Evidence", "SourceRecord"]
        assert end["labels"] == ["Person", "Profile"]


def test_active_deal_phone_projection_is_projection_cleanup() -> None:
    assert classify_inventory_item(
        _item(
            links=[{"is_active": True, "person_id": "person-a"}],
            projections=[
                {
                    "is_active": True,
                    "relationship_type": "IDENTIFIED_BY",
                    "identifier_type": "phone",
                    "identifier_value": "+6591234567",
                }
            ],
        )
    ) == "projection_cleanup"


def test_clean_single_linked_deal_is_a_negative_control() -> None:
    assert classify_inventory_item(
        _item(
            links=[{"is_active": True, "person_id": "person-a"}],
            projections=[
                {
                    "is_active": True,
                    "relationship_type": "IDENTIFIED_BY",
                    "identifier_type": "crm_contact_id",
                    "identifier_value": "contact-10",
                    "owner_person_id": "person-a",
                    "source_record_pk": "deal-pk",
                }
            ],
        )
    ) == "negative_control"


def test_canonical_projection_on_a_different_owner_requires_cleanup() -> None:
    assert classify_inventory_item(
        _item(
            links=[{"is_active": True, "person_id": "person-a"}],
            projections=[
                {
                    "is_active": True,
                    "relationship_type": "IDENTIFIED_BY",
                    "identifier_type": "crm_contact_id",
                    "identifier_value": "contact-10",
                    "owner_person_id": "person-b",
                    "source_record_pk": "deal-pk",
                }
            ],
        )
    ) == "projection_cleanup"


def test_unlinked_deal_is_not_a_negative_control() -> None:
    assert classify_inventory_item(_item(links=[], projections=[])) == "projection_cleanup"


def test_inventory_item_payload_is_isolated_from_input_and_returned_copies() -> None:
    original_links: list[dict[str, object]] = [
        {"is_active": True, "person_id": "person-a"}
    ]
    item = RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-deal-10",
        source_record_pk="deal-pk",
        deal_id="10",
        partition="negative_control",
        graph_fingerprint="sha256:" + "a" * 64,
        stored_payload_fingerprint="sha256:" + "b" * 64,
        payload={"linked_people": original_links, "projections": []},
    )

    original_links[0]["is_active"] = False
    first_payload = item.payload
    first_links = first_payload["linked_people"]
    assert isinstance(first_links, list)
    first_link = first_links[0]
    assert isinstance(first_link, dict)
    assert first_link["is_active"] is True

    first_link["is_active"] = False
    second_payload = item.payload
    second_links = second_payload["linked_people"]
    assert isinstance(second_links, list)
    second_link = second_links[0]
    assert isinstance(second_link, dict)
    assert second_link["is_active"] is True

    serialized = item.to_dict()
    serialized_payload = serialized["payload"]
    assert isinstance(serialized_payload, dict)
    serialized_payload["linked_people"] = []
    assert item.to_dict()["payload"] != serialized_payload
