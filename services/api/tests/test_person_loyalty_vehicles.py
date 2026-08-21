"""Tests for Person loyalty + vehicles read-through and sales points mapping."""

from __future__ import annotations

import json

import pytest
from src.graph.mappers import map_person
from src.graph.mappers_sales import map_sales_order
from src.types import LoyaltySummary, Person, PersonStatus, VehicleSummary
from src.types_sales import SalesOrder

# --- types -------------------------------------------------------------------


def test_loyalty_summary_model() -> None:
    ls = LoyaltySummary(
        source_system="eko_phppos", points=250, disable_loyalty=False,
        current_spend_for_points=12.5, current_sales_for_discount=99.0,
        observed_at="2026-01-01",
    )
    assert ls.points == 250


def test_vehicle_summary_model() -> None:
    vs = VehicleSummary(
        vehicle_id="v1", product="Segway X", product_sku="sku-x",
        manufacturer="Segway", model="X-200",
        lta_tag="LTA1", serial_number="S1", relationship="OWNS",
        is_active=True, conflict_flag=False, observed_at="2026-01-01",
    )
    assert vs.relationship == "OWNS"
    assert vs.product_sku == "sku-x"


def test_person_carries_loyalty_and_vehicles() -> None:
    p = Person(person_id="p1", status=PersonStatus.ACTIVE)
    assert p.loyalty is None
    assert p.vehicles is None


def test_sales_order_carries_points() -> None:
    o = SalesOrder(source_order_id="1")
    assert o.points_used is None
    assert o.points_gained is None


# --- map_person loyalty + vehicles -------------------------------------------


def _record(
    *,
    raw_loyalty: dict[str, object] | None = None,
    source_system: str = "eko_phppos",
    observed_at: str = "2026-01-01",
    vehicles: list[dict[str, object]] | None = None,
    extra_loyalty_rows: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    rows: list[dict[str, object]] = [
        {
            "source_system": source_system,
            "observed_at": observed_at,
            "raw_payload": json.dumps(
                {"person": {}, "loyalty": raw_loyalty}
            ) if raw_loyalty is not None else json.dumps({"person": {}}),
        }
    ]
    if extra_loyalty_rows:
        rows.extend(extra_loyalty_rows)
    out: dict[str, object] = {
        "person": {
            "person_id": "p1",
            "status": "active",
            "profile_completeness_score": 0.0,
        },
        "loyalty_rows": rows,
    }
    if vehicles is not None:
        out["vehicles"] = vehicles
    return out


def test_map_person_loyalty_from_block() -> None:
    rec = _record(
        raw_loyalty={
            "points": 250, "disable_loyalty": False,
            "current_spend_for_points": 1.0, "current_sales_for_discount": 2.0,
        },
    )
    p = map_person(rec)
    assert p.loyalty is not None and len(p.loyalty) == 1
    assert p.loyalty[0].points == 250
    assert p.loyalty[0].source_system == "eko_phppos"


def test_map_person_loyalty_picks_latest_observed_at() -> None:
    rec = _record(
        raw_loyalty={
            "points": 250, "disable_loyalty": False,
            "current_spend_for_points": 1.0, "current_sales_for_discount": 2.0,
        },
        extra_loyalty_rows=[
            {
                "source_system": "eko_phppos", "observed_at": "2026-02-01",
                "raw_payload": json.dumps(
                    {"person": {}, "loyalty": {"points": 999}}
                ),
            }
        ],
    )
    p = map_person(rec)
    assert p.loyalty is not None and len(p.loyalty) == 1
    assert p.loyalty[0].points == 999  # latest observed_at wins
    assert p.loyalty[0].source_system == "eko_phppos"


def test_map_person_loyalty_skips_records_without_block() -> None:
    rec = _record()  # raw_payload has no loyalty key
    p = map_person(rec)
    assert p.loyalty is None


def test_map_person_loyalty_dedup_same_source_system_latest_wins() -> None:
    # Latest (by observed_at) wins; two records from the same source_system
    # collapse to one LoyaltySummary entry.
    rec = _record(
        raw_loyalty={
            "points": 100, "disable_loyalty": False,
            "current_spend_for_points": 1.0, "current_sales_for_discount": 2.0,
        },
        observed_at="2026-01-01",
        extra_loyalty_rows=[
            {
                "source_system": "eko_phppos", "observed_at": "2026-03-01",
                "raw_payload": json.dumps(
                    {"person": {}, "loyalty": {"points": 200}}
                ),
            },
        ],
    )
    p = map_person(rec)
    assert p.loyalty is not None and len(p.loyalty) == 1
    assert p.loyalty[0].points == 200  # "2026-03-01" > "2026-01-01"


def test_map_person_vehicles() -> None:
    rec = _record(
        vehicles=[
            {
                "vehicle_id": "v1", "product": "Segway X", "product_sku": "sku-x",
                "manufacturer": "Segway", "model": "X-200",
                "lta_tag": "LTA1", "serial_number": "S1",
                "rel_type": "OWNS_VEHICLE",
                "is_active": True, "conflict_flag": False, "observed_at": "2026-01-01",
            },
            {
                "vehicle_id": "v2", "product": None, "product_sku": None,
                "manufacturer": None, "model": None,
                "lta_tag": None, "serial_number": "S2",
                "rel_type": "BOUGHT_VEHICLE",
                "is_active": None, "conflict_flag": True, "observed_at": None,
            },
        ]
    )
    p = map_person(rec)
    assert p.vehicles is not None and len(p.vehicles) == 2
    assert p.vehicles[0].relationship == "OWNS"
    assert p.vehicles[1].relationship == "BOUGHT"
    assert p.vehicles[1].conflict_flag is True


def test_map_person_vehicles_empty() -> None:
    p = map_person(_record())
    assert p.vehicles is None


def test_map_person_vehicles_dedup_same_vehicle_multiple_rels() -> None:
    # A person can have multiple BOUGHT_VEHICLE rels to one vehicle (MERGEd on
    # distinct source_order_id). The mapper must dedup to a single VehicleSummary.
    rec = _record(
        vehicles=[
            {
                "vehicle_id": "v1", "product": "Segway X", "product_sku": "sku-x",
                "manufacturer": None, "model": None,
                "lta_tag": "LTA1", "serial_number": "S1",
                "rel_type": "BOUGHT_VEHICLE",
                "is_active": True, "conflict_flag": False, "observed_at": "2026-01-01",
            },
            {
                "vehicle_id": "v1", "product": "Segway X", "product_sku": "sku-x",
                "manufacturer": None, "model": None,
                "lta_tag": "LTA1", "serial_number": "S1",
                "rel_type": "BOUGHT_VEHICLE",
                "is_active": True, "conflict_flag": False, "observed_at": "2026-01-01",
            },
        ]
    )
    p = map_person(rec)
    assert p.vehicles is not None and len(p.vehicles) == 1
    assert p.vehicles[0].vehicle_id == "v1"


def test_map_person_vehicles_owns_wins_over_bought() -> None:
    # When the same vehicle has both OWNS_VEHICLE and BOUGHT_VEHICLE edges,
    # OWNS wins (stronger ownership claim).
    rec = _record(
        vehicles=[
            {
                "vehicle_id": "v1", "product": "Segway X", "product_sku": "sku-x",
                "manufacturer": None, "model": None,
                "lta_tag": "LTA1", "serial_number": "S1",
                "rel_type": "BOUGHT_VEHICLE",
                "is_active": True, "conflict_flag": False, "observed_at": "2026-01-01",
            },
            {
                "vehicle_id": "v1", "product": "Segway X", "product_sku": "sku-x",
                "manufacturer": None, "model": None,
                "lta_tag": "LTA1", "serial_number": "S1",
                "rel_type": "OWNS_VEHICLE",
                "is_active": True, "conflict_flag": False, "observed_at": "2026-01-01",
            },
        ]
    )
    p = map_person(rec)
    assert p.vehicles is not None and len(p.vehicles) == 1
    assert p.vehicles[0].relationship == "OWNS"


# --- map_sales_order points --------------------------------------------------


def test_map_sales_order_points() -> None:
    rec = {
        "order_no": "INV1", "source_order_id": "1", "order_date": None,
        "release_date": None, "total_amount": 10.0, "currency": "SGD",
        "source_system": "eko_phppos:sales", "entity_name": "Eko",
        "line_items": [], "points_used": 20, "points_gained": 5,
    }
    o = map_sales_order(rec)
    assert o.points_used == 20
    assert o.points_gained == 5


def test_map_sales_order_points_none() -> None:
    rec = {
        "order_no": "INV1", "source_order_id": "1", "order_date": None,
        "release_date": None, "total_amount": 10.0, "currency": "SGD",
        "source_system": "eko_phppos:sales", "entity_name": "Eko",
        "line_items": [],
    }
    o = map_sales_order(rec)
    assert o.points_used is None
    assert o.points_gained is None


def test_map_sales_order_normalizes_points_independently_and_warns_safely(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_order_id = "private-order-mapper-241"
    rec = {
        "order_no": "INV1",
        "source_order_id": raw_order_id,
        "order_date": None,
        "release_date": None,
        "total_amount": 10.0,
        "currency": "SGD",
        "source_system": "eko_phppos:sales",
        "entity_name": "Eko",
        "line_items": [],
        "points_used": "malformed-secret-value",
        "points_gained": "14000.0000000000",
    }

    first = map_sales_order(rec)
    second = map_sales_order(rec)

    assert first.points_used is None
    assert first.points_gained == 14000
    assert second.points_used is None
    messages = [record.getMessage() for record in caplog.records]
    matching = [message for message in messages if "loyalty_points_conversion_failed" in message]
    assert len(matching) == 1
    assert "malformed-secret-value" not in matching[0]
    assert raw_order_id not in matching[0]
