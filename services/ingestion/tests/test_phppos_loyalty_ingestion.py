"""Tests for phppos identity loyalty capture (Eko + SpeedZone, live + dump join)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from src.connectors.phppos_loyalty import loyalty_block_from_row
from src.connectors.dumps.connectors import _join_eko_row, _join_speedzone_row
from src.connectors.dumps.reader import DumpRow


def _row(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "points": 250,
        "disable_loyalty": 0,
        "current_spend_for_points": 12.5,
        "current_sales_for_discount": 99.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --- live connector loyalty block -------------------------------------------


def test_loyalty_block_from_row_maps_values() -> None:
    block = loyalty_block_from_row(_row())
    assert block == {
        "points": 250,
        "disable_loyalty": False,
        "current_spend_for_points": 12.5,
        "current_sales_for_discount": 99.0,
    }


def test_loyalty_block_from_row_handles_decimal_and_none() -> None:
    block = loyalty_block_from_row(
        _row(points=Decimal("300.000"), disable_loyalty=1,
             current_spend_for_points=Decimal("5.50"), current_sales_for_discount=None)
    )
    assert block == {
        "points": 300,
        "disable_loyalty": True,
        "current_spend_for_points": 5.5,
        "current_sales_for_discount": None,
    }


def test_loyalty_block_from_row_all_none() -> None:
    block = loyalty_block_from_row(
        _row(points=None, disable_loyalty=None,
             current_spend_for_points=None, current_sales_for_discount=None)
    )
    assert block == {
        "points": None,
        "disable_loyalty": None,
        "current_spend_for_points": None,
        "current_sales_for_discount": None,
    }


def test_loyalty_block_from_row_truthy_flag() -> None:
    block = loyalty_block_from_row(_row(points=80, disable_loyalty=1))
    assert block["points"] == 80
    assert block["disable_loyalty"] is True


def test_loyalty_block_coerces_string_values_from_dump() -> None:
    # Dump rows carry loyalty columns as strings (the dump round-trips as text).
    block = loyalty_block_from_row(
        SimpleNamespace(
            points="300.000", disable_loyalty="1",
            current_spend_for_points="12.50", current_sales_for_discount="0.00",
        )
    )
    assert block == {
        "points": 300,
        "disable_loyalty": True,
        "current_spend_for_points": 12.5,
        "current_sales_for_discount": 0.0,
    }


# --- dump-path join parity ---------------------------------------------------


def _customer(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 10,
        "person_id": 1,
        "account_number": "ACC",
        "company_name": None,
        "points": 300,
        "disable_loyalty": 0,
        "current_spend_for_points": 5.0,
        "current_sales_for_discount": 1.0,
        **{f"custom_field_{i}_value": None for i in range(1, 11)},
    }
    base.update(overrides)
    return base


def _row_obj(d: dict[str, object]) -> DumpRow:
    return DumpRow(_mapping=d)  # type: ignore[arg-type]


def test_join_eko_row_copies_loyalty_columns() -> None:
    person = _row_obj({"person_id": 1, "full_name": "A B", "phone_number": "9", "email": "a@b.com"})
    joined = _join_eko_row(person, _row_obj(_customer()))
    assert joined.points == 300
    assert joined.disable_loyalty == 0
    assert joined.current_spend_for_points == 5.0
    assert joined.current_sales_for_discount == 1.0


def test_join_eko_row_loyalty_defaults_none_when_absent() -> None:
    person = _row_obj({"person_id": 1, "full_name": "A B", "phone_number": "9", "email": "a@b.com"})
    customer = _customer()
    for key in ("points", "disable_loyalty", "current_spend_for_points",
                "current_sales_for_discount"):
        customer.pop(key)
    joined = _join_eko_row(person, _row_obj(customer))
    assert joined.points is None
    assert joined.disable_loyalty is None
    assert joined.current_spend_for_points is None
    assert joined.current_sales_for_discount is None


def test_join_speedzone_row_copies_loyalty_columns() -> None:
    person = _row_obj({"person_id": 1, "full_name": "A B"})
    joined = _join_speedzone_row(person, _row_obj(_customer(points=7, disable_loyalty=1)))
    assert joined.points == 7
    assert joined.disable_loyalty == 1