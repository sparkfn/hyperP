"""Tests for phppos per-sale loyalty activity capture + Order node properties."""

from __future__ import annotations

from decimal import Decimal

from src.connectors.dumps.connectors import _build_phppos_sales_envelope
from src.connectors.dumps.reader import DumpRow
from src.connectors.phppos_sales_common import _build_order_payload


def _sale(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sale_id": 1,
        "sale_time": "2026-01-01 00:00:00",
        "customer_id": 5,
        "invoice_number": "INV1",
        "suspended": 0,
        "sale_status": None,
        "total": Decimal("10.00"),
        "employee_id": 2,
        "register_id": 1,
        "payment_type": "cash",
        "sale_type_id": None,
        "comment": None,
        "points_used": 20,
        "points_gained": 5,
        "did_redeem_discount": 1,
        "is_purchase_points": 0,
    }
    base.update(overrides)
    return base


def test_order_payload_has_loyalty_block() -> None:
    sale = _sale()
    payload = _build_order_payload(
        sale=sale, source_order_id="1", ordered_at="2026-01-01",
        release_date=None, sales_cols=set(sale.keys()), line_rows=[],
        total=Decimal("10.00"),
    )
    assert payload["loyalty"] == {
        "points_used": 20,
        "points_gained": 5,
        "did_redeem_discount": 1,
        "is_purchase_points": 0,
    }


def test_order_payload_loyalty_none_when_columns_absent() -> None:
    sale = _sale()
    cols = set(sale.keys()) - {
        "points_used", "points_gained", "did_redeem_discount", "is_purchase_points",
    }
    payload = _build_order_payload(
        sale=sale, source_order_id="1", ordered_at="2026-01-01",
        release_date=None, sales_cols=cols, line_rows=[],
        total=Decimal("10.00"),
    )
    assert payload["loyalty"] == {
        "points_used": None,
        "points_gained": None,
        "did_redeem_discount": None,
        "is_purchase_points": None,
    }


def test_dump_sales_envelope_loyalty_block_coerces_strings() -> None:
    # Dump rows carry loyalty columns as strings (the dump round-trips as text).
    sale = DumpRow(_mapping={  # type: ignore[arg-type]
        "sale_id": "1", "sale_time": "2026-01-01 00:00:00", "customer_id": "5",
        "invoice_number": "INV1", "sale_status": None, "suspended": "0",
        "points_used": "20", "points_gained": "5",
        "did_redeem_discount": "1", "is_purchase_points": "0",
    })
    env = _build_phppos_sales_envelope(
        sale=sale, line_rows=[], items_by_id={}, source_system_key="eko_phppos:sales",
    )
    raw = env.get("raw_payload")
    assert isinstance(raw, dict)
    order_block = raw.get("order")
    assert isinstance(order_block, dict)
    assert order_block["loyalty"] == {
        "points_used": 20,
        "points_gained": 5,
        "did_redeem_discount": 1,
        "is_purchase_points": 0,
    }