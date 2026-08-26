"""Focused graph-only CRM-deal repair inventory tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TypeVar, cast

from neo4j import ManagedTransaction

from src.crm_deal_identity_repair.inventory import collect_repair_inventory
from src.graph.client import Neo4jClient
from src.graph.queries.crm_deal_identity_repair import (
    INVENTORY_ACTIVE_CRM_DEALS,
    INVENTORY_CRM_DEAL_PROJECTIONS,
    INVENTORY_STALE_RUN_CONTROL_PLANE,
)

_T = TypeVar("_T")


class _Rows(tuple[dict[str, object], ...]):
    def single(self, *, strict: bool) -> dict[str, object] | None:
        assert strict
        if len(self) != 1:
            raise AssertionError("expected exactly one stale-run row")
        return self[0]


class _Transaction:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self._rows = rows

    def run(self, query: str, **parameters: object) -> _Rows:
        if query == INVENTORY_ACTIVE_CRM_DEALS:
            assert parameters == {"source_system": "bitrix_chat"}
            return _Rows(self._rows)
        if query == INVENTORY_CRM_DEAL_PROJECTIONS:
            assert parameters == {"source_system": "bitrix_chat"}
            return _Rows(
                {"source_record_pk": row["source_record_pk"], "projection": projection}
                for row in self._rows
                for projection in cast(list[dict[str, object]], row["projections"])
            )
        assert query == INVENTORY_STALE_RUN_CONTROL_PLANE
        assert parameters["stale_run_id"] == "e5deb1d6-7333-4660-be4f-c44fcf5af686"
        return _Rows(
            (
                {
                    "stale_run_id": parameters["stale_run_id"],
                    "stale_run_state": "unknown",
                    "run_status": None,
                    "associated_source_system": None,
                    "logical_run_association_count": 0,
                    "checkpoint_association_count": 0,
                },
            )
        )


class _Client:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self._rows = rows

    def execute_read(self, work: Callable[[ManagedTransaction], _T]) -> _T:
        return work(cast(ManagedTransaction, _Transaction(self._rows)))


def _row(
    *,
    source_record_pk: str = "deal-pk",
    source_record_id: str = "bitrix-crm-deal-10",
    raw_payload: object = {"crm_deal_identity_policy_version": "legacy"},
    lifecycle_status: str | None = "active",
    is_latest: bool = True,
    links: list[dict[str, object]] | None = None,
    descendants: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "source_record_pk": source_record_pk,
        "source_record_id": source_record_id,
        "source_record_version": "1",
        "lifecycle_status": lifecycle_status,
        "is_latest": is_latest,
        "record_hash": "record-hash",
        "observed_at": "2026-08-25T00:00:00Z",
        "raw_payload": raw_payload,
        "normalized_payload": {},
        "linked_people": links or [],
        "logical_versions": [
            {
                "source_record_pk": source_record_pk,
                "source_record_version": "1",
                "lifecycle_status": lifecycle_status,
                "is_latest": is_latest,
                "raw_payload": raw_payload,
                "normalized_payload": {},
            }
        ],
        "descendants": descendants or [],
        "decisions_and_reviews": [],
        "owner_impacts": [],
        "projections": [],
    }


def _link(person_id: str, *, active: bool = True) -> dict[str, object]:
    return {
        "person_id": person_id,
        "is_active": active,
        "relationship_type": "LINKED_TO",
        "relationship_properties": {"source_record_pk": "deal-pk", "is_active": active},
    }


def _inventory(*rows: dict[str, object]):
    return collect_repair_inventory(cast(Neo4jClient, _Client(tuple(rows))))


def test_inventory_query_is_read_only_and_captures_closure_families() -> None:
    catalog = (
        INVENTORY_ACTIVE_CRM_DEALS,
        INVENTORY_CRM_DEAL_PROJECTIONS,
        INVENTORY_STALE_RUN_CONTROL_PLANE,
    )
    forbidden = (" CREATE ", " MERGE ", " SET ", " DELETE ", " REMOVE ")
    assert all(token not in query.upper() for query in catalog for token in forbidden)
    assert "logical_versions" in INVENTORY_ACTIVE_CRM_DEALS
    assert "descendants" in INVENTORY_ACTIVE_CRM_DEALS
    assert "decisions_and_reviews" in INVENTORY_ACTIVE_CRM_DEALS
    assert "owner_impacts" in INVENTORY_ACTIVE_CRM_DEALS
    assert "DESCRIBES_ADDRESS" in INVENTORY_CRM_DEAL_PROJECTIONS
    assert "'unknown' AS stale_run_state" in INVENTORY_STALE_RUN_CONTROL_PLANE
    assert "orphaned_candidate" not in INVENTORY_STALE_RUN_CONTROL_PLANE


def test_inventory_includes_historical_versions_and_active_inactive_links() -> None:
    historical = _row(
        source_record_pk="deal-pk-old",
        raw_payload={"crm_deal_identity_policy_version": "legacy"},
        lifecycle_status="superseded",
        is_latest=False,
        links=[_link("person-old", active=False)],
    )
    v2 = _row(
        source_record_pk="deal-pk-new",
        raw_payload={"crm_deal_identity_policy_version": "crm_deal_identity_v2"},
        links=[_link("person-a"), _link("person-b")],
    )

    inventory = _inventory(historical, v2)

    assert [item.source_record_pk for item in inventory.items] == ["deal-pk-new", "deal-pk-old"]
    assert inventory.population_counts.active_link_count == 2
    assert inventory.population_counts.multi_linked_deal_count == 1
    assert inventory.population_counts.maximum_links_per_deal == 2
    policies = {
        item.source_record_pk: item.payload["lifecycle_policy_evidence"] for item in inventory.items
    }
    assert policies["deal-pk-old"]["classification"] == "pre_policy"
    assert policies["deal-pk-new"]["classification"] == "policy_v2"


def test_descendant_links_do_not_inflate_root_direct_link_counts() -> None:
    inventory = _inventory(
        _row(
            links=[_link("person-a")],
            descendants=[
                {
                    "record_type": "crm_history",
                    "source_record_pk": "history-pk",
                    "source_record_id": "history-1",
                    "lifecycle_status": "active",
                    "relationship_type": "LINKED_TO",
                    "relationship_is_active": True,
                    "owner_person_id": "person-b",
                }
            ],
        )
    )

    assert inventory.population_counts.active_link_count == 1
    assert inventory.population_counts.multi_linked_deal_count == 0
    assert inventory.population_counts.maximum_distinct_owners_per_deal == 1


def test_inactive_historical_multilink_does_not_inflate_active_baseline() -> None:
    historical = _row(
        source_record_pk="deal-pk-old",
        lifecycle_status="superseded",
        is_latest=False,
        links=[_link("person-a"), _link("person-b")],
        descendants=[
            {
                "record_type": "crm_history",
                "source_record_pk": "history-old",
                "source_record_id": "history-old",
                "lifecycle_status": "superseded",
                "relationship_type": "LINKED_TO",
                "relationship_is_active": True,
                "owner_person_id": "person-c",
            }
        ],
    )
    current = _row(source_record_pk="deal-pk-current", links=[_link("person-a")])

    inventory = _inventory(historical, current)

    assert inventory.population_counts.authoritative_version_count == 1
    assert inventory.population_counts.active_deal_count == 1
    assert inventory.population_counts.active_link_count == 1
    assert inventory.population_counts.multi_linked_deal_count == 0
    assert inventory.population_counts.maximum_distinct_owners_per_deal == 1


def test_missing_policy_provenance_is_explicit_investigate_evidence() -> None:
    inventory = _inventory(_row(raw_payload={}, lifecycle_status="active"))

    policy = inventory.items[0].payload["lifecycle_policy_evidence"]
    assert policy["classification"] == "missing_policy_provenance"
    assert policy["disposition"] == "investigate"


def test_malformed_persisted_policy_is_explicit_investigate_evidence() -> None:
    inventory = _inventory(_row(raw_payload="{not-json"))

    item = inventory.items[0]
    policy = item.payload["lifecycle_policy_evidence"]
    assert policy["classification"] == "conflicting_or_invalid_policy"
    assert policy["disposition"] == "investigate"
    assert item.partition == "projection_cleanup"
    assert inventory.stale_run_evidence["state"] == "unknown"


def test_inventory_query_projects_match_decision_engine_type() -> None:
    assert "engine_type: decision.engine_type" in INVENTORY_ACTIVE_CRM_DEALS
