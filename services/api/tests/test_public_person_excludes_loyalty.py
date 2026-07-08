"""Tests for public share-page exclusion of loyalty / vehicles / sales points."""

from __future__ import annotations

from src.routes.public_pages import (
    _strip_public_person,
    _strip_public_sales_order,
    _strip_public_source_record,
)
from src.types import LoyaltySummary, Person, PersonStatus, SourceRecord, VehicleSummary
from src.types_sales import SalesOrder


def _person() -> Person:
    return Person(
        person_id="p1",
        status=PersonStatus.ACTIVE,
        loyalty=[
            LoyaltySummary(
                source_system="eko_phppos", points=1, disable_loyalty=False,
                current_spend_for_points=None, current_sales_for_discount=None,
                observed_at=None,
            )
        ],
        vehicles=[
            VehicleSummary(
                vehicle_id="v1", product=None, product_sku=None,
                manufacturer=None, model=None,
                lta_tag=None, serial_number=None,
                relationship="OWNS", is_active=True,
                conflict_flag=False, observed_at=None,
            )
        ],
    )


def test_strip_public_person_clears_loyalty_and_vehicles() -> None:
    stripped = _strip_public_person(_person())
    assert stripped.loyalty is None
    assert stripped.vehicles is None


def test_strip_public_sales_order_clears_points() -> None:
    order = SalesOrder(source_order_id="1", points_used=20, points_gained=5)
    stripped = _strip_public_sales_order(order)
    assert stripped.points_used is None
    assert stripped.points_gained is None


def test_strip_public_source_record_removes_loyalty_from_raw_payload() -> None:
    sr = SourceRecord(
        source_record_pk="pk1",
        source_system="eko_phppos",
        source_record_id="eko_phppos-customer-1",
        link_status="linked",
        observed_at="2026-01-01",
        ingested_at="2026-01-01",
        raw_payload={
            "person": {"full_name": "A B", "points": 5000, "disable_loyalty": False},
            "loyalty": {"points": 5000},
        },
    )
    stripped = _strip_public_source_record(sr)
    assert stripped.raw_payload is not None
    # top-level structured block removed
    assert "loyalty" not in stripped.raw_payload
    # person sub-payload loyalty columns also scrubbed (serialize_row copies them)
    person = stripped.raw_payload.get("person")
    assert isinstance(person, dict)
    assert person.get("full_name") == "A B"
    assert "points" not in person
    assert "disable_loyalty" not in person


def test_strip_public_source_record_noop_without_loyalty() -> None:
    sr = SourceRecord(
        source_record_pk="pk1",
        source_system="eko_phppos",
        source_record_id="eko_phppos-customer-1",
        link_status="linked",
        observed_at="2026-01-01",
        ingested_at="2026-01-01",
        raw_payload={"person": {"full_name": "A B"}},
    )
    stripped = _strip_public_source_record(sr)
    assert stripped.raw_payload == {"person": {"full_name": "A B"}}