"""Tests for Person loyalty + machine units read-through and sales points mapping."""

from __future__ import annotations

import json

from src.graph.mappers import map_person
from src.graph.mappers_sales import map_sales_order
from src.types import LoyaltySummary, MachineUnitSummary, Person, PersonStatus
from src.types_sales import SalesOrder


# --- types -------------------------------------------------------------------


def test_loyalty_summary_model() -> None:
    ls = LoyaltySummary(
        source_system="eko_phppos", points=250, disable_loyalty=False,
        current_spend_for_points=12.5, current_sales_for_discount=99.0,
        observed_at="2026-01-01",
    )
    assert ls.points == 250
    assert ls.disable_loyalty is False


def test_machine_unit_summary_model() -> None:
    mu = MachineUnitSummary(
        machine_unit_id="u1", machine_product="Widget", lta_tag="LTA1",
        serial_number="S1", relationship="OWNS", is_active=True,
        conflict_flag=False, observed_at="2026-01-01",
    )
    assert mu.relationship == "OWNS"


def test_person_carries_loyalty_and_machine_units() -> None:
    p = Person(person_id="p1", status=PersonStatus.ACTIVE)
    assert p.loyalty is None
    assert p.machine_units is None


def test_sales_order_carries_points() -> None:
    o = SalesOrder(source_order_id="1")
    assert o.points_used is None
    assert o.points_gained is None


# --- map_person loyalty + machine units --------------------------------------


def _record(
    *,
    raw_loyalty: dict[str, object] | None = None,
    source_system: str = "eko_phppos",
    observed_at: str = "2026-01-01",
    units: list[dict[str, object]] | None = None,
    extra_loyalty_rows: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    loyalty_rows: list[dict[str, object]] = [
        {
            "source_system": source_system,
            "observed_at": observed_at,
            "raw_payload": json.dumps(
                {"person": {}, "loyalty": raw_loyalty}
            ) if raw_loyalty is not None else json.dumps({"person": {}}),
        }
    ]
    if extra_loyalty_rows:
        loyalty_rows.extend(extra_loyalty_rows)
    return {
        "person": {"person_id": "p1", "status": "active"},
        "preferred_address": None,
        "source_record_count": 0,
        "connection_count": 0,
        "lifetime_value": None,
        "loyalty_rows": loyalty_rows,
        "machine_units": units or [],
    }


def test_map_person_loyalty_dedup_per_source_latest() -> None:
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
    assert p.loyalty == []


def test_map_person_loyalty_two_sources() -> None:
    rec = _record(
        raw_loyalty={"points": 10},
        extra_loyalty_rows=[
            {
                "source_system": "speedzone_phppos", "observed_at": "2026-01-01",
                "raw_payload": json.dumps({"person": {}, "loyalty": {"points": 20}}),
            }
        ],
    )
    p = map_person(rec)
    assert p.loyalty is not None
    sources = {ls.source_system for ls in p.loyalty}
    assert sources == {"eko_phppos", "speedzone_phppos"}


def test_map_person_loyalty_tiebreak_is_deterministic() -> None:
    # Two same-source rows with identical observed_at: the higher source_record_pk
    # wins deterministically (no flapping across fetches).
    rec = _record(
        raw_loyalty={"points": 100},
        extra_loyalty_rows=[
            {
                "source_system": "eko_phppos", "observed_at": "2026-01-01",
                "source_record_pk": "pk-zzz",
                "raw_payload": json.dumps({"person": {}, "loyalty": {"points": 200}}),
            }
        ],
    )
    # The _record helper's first row has no source_record_pk (defaults "").
    rec["loyalty_rows"][0]["source_record_pk"] = "pk-aaa"
    p1 = map_person(rec)
    p2 = map_person(rec)
    assert p1.loyalty is not None and len(p1.loyalty) == 1
    assert p1.loyalty[0].points == p2.loyalty[0].points
    assert p1.loyalty[0].points == 200  # "pk-zzz" > "pk-aaa"


def test_map_person_machine_units() -> None:
    rec = _record(
        units=[
            {
                "machine_unit_id": "u1", "machine_product": "Widget",
                "lta_tag": "LTA1", "serial_number": "S1", "rel_type": "OWNS_UNIT",
                "is_active": True, "conflict_flag": False, "observed_at": "2026-01-01",
            },
            {
                "machine_unit_id": "u2", "machine_product": None, "lta_tag": None,
                "serial_number": "S2", "rel_type": "BOUGHT_UNIT",
                "is_active": None, "conflict_flag": True, "observed_at": None,
            },
        ]
    )
    p = map_person(rec)
    assert p.machine_units is not None and len(p.machine_units) == 2
    assert p.machine_units[0].relationship == "OWNS"
    assert p.machine_units[1].relationship == "BOUGHT"
    assert p.machine_units[1].conflict_flag is True


def test_map_person_machine_units_empty() -> None:
    p = map_person(_record())
    assert p.machine_units == []


def test_map_person_machine_units_dedup_same_unit_multiple_rels() -> None:
    # A person can have multiple BOUGHT_UNIT rels to one unit (MERGEd on distinct
    # source_order_id). The mapper must dedup to a single MachineUnitSummary.
    rec = _record(
        units=[
            {
                "machine_unit_id": "u1", "machine_product": "Widget", "lta_tag": "L1",
                "serial_number": "S1", "rel_type": "BOUGHT_UNIT",
                "is_active": True, "conflict_flag": False, "observed_at": "2026-01-01",
            },
            {
                "machine_unit_id": "u1", "machine_product": "Widget", "lta_tag": "L1",
                "serial_number": "S1", "rel_type": "BOUGHT_UNIT",
                "is_active": True, "conflict_flag": False, "observed_at": "2026-02-01",
            },
        ]
    )
    p = map_person(rec)
    assert p.machine_units is not None and len(p.machine_units) == 1
    assert p.machine_units[0].machine_unit_id == "u1"


def test_map_person_machine_units_owns_wins_over_bought() -> None:
    # When a unit has both OWNS_UNIT and BOUGHT_UNIT edges, OWNS wins.
    rec = _record(
        units=[
            {"machine_unit_id": "u1", "machine_product": None, "lta_tag": None,
             "serial_number": "S1", "rel_type": "BOUGHT_UNIT",
             "is_active": None, "conflict_flag": None, "observed_at": None},
            {"machine_unit_id": "u1", "machine_product": None, "lta_tag": None,
             "serial_number": "S1", "rel_type": "OWNS_UNIT",
             "is_active": True, "conflict_flag": False, "observed_at": None},
        ]
    )
    p = map_person(rec)
    assert p.machine_units is not None and len(p.machine_units) == 1
    assert p.machine_units[0].relationship == "OWNS"


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