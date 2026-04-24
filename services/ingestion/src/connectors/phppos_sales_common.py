"""Shared phppos sales extraction used by SpeedZone and Eko connectors.

phppos databases have an identical sales schema
(``phppos_sales``/``phppos_sales_items``/``phppos_items``). If any of
those tables are missing (e.g. a config-only fixture dump), the
connector yields nothing and logs a warning instead of failing the
whole task — identity ingestion continues unaffected.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from decimal import Decimal
from typing import cast

from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.engine import Connection, Engine, RowMapping

from src.connectors.fundbox.builders import (
    build_envelope,
    serialize_row,
    to_iso,
)
from src.models import JsonValue

logger = logging.getLogger(__name__)

_REQUIRED_SALES_TABLES: tuple[str, ...] = (
    "phppos_sales",
    "phppos_sales_items",
    "phppos_items",
)


def _decimal_to_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal | int | float):
        return float(value)
    return None


def sales_tables_present(engine: Engine) -> bool:
    """Return True iff every required phppos sales table exists."""
    existing = set(inspect(engine).get_table_names())
    missing = [t for t in _REQUIRED_SALES_TABLES if t not in existing]
    if missing:
        logger.warning(
            "phppos sales tables missing — skipping sales ingestion. missing=%s",
            missing,
        )
        return False
    return True


def _fetch_sale_items(
    sidecar: Connection,
    items_t: Table,
    item_t: Table,
    sale_id: int,
) -> tuple[list[RowMapping], dict[int, RowMapping]]:
    """Fetch line items and their product records for one sale."""
    line_rows = list(
        sidecar.execute(select(items_t).where(items_t.c.sale_id == sale_id)).mappings()
    )
    item_ids = [int(r["item_id"]) for r in line_rows if r.get("item_id") is not None]
    items_by_id: dict[int, RowMapping] = {}
    if item_ids:
        stmt = select(item_t).where(item_t.c.item_id.in_(list(set(item_ids))))
        items_by_id = {int(r["item_id"]): r for r in sidecar.execute(stmt).mappings()}
    return line_rows, items_by_id


def fetch_phppos_sales(
    engine: Engine, conn: Connection, source_system_key: str, chunk_size: int,
) -> Iterator[dict[str, JsonValue]]:
    """Yield one sales envelope per phppos_sales row.

    A sidecar connection handles per-sale lookups to avoid aborting
    the primary streaming cursor (pymysql unbuffered result limitation).
    """
    if not sales_tables_present(engine):
        return

    md = MetaData()
    sales_t = Table("phppos_sales", md, autoload_with=engine)
    items_t = Table("phppos_sales_items", md, autoload_with=engine)
    item_t = Table("phppos_items", md, autoload_with=engine)
    sales_cols = {c.name for c in sales_t.columns}
    items_cols = {c.name for c in items_t.columns}
    item_cols = {c.name for c in item_t.columns}

    result = (
        conn.execute(select(sales_t).order_by(sales_t.c.sale_id)).mappings().yield_per(chunk_size)
    )
    with engine.connect() as sidecar:
        for sale in result:
            sale_id = int(sale["sale_id"])
            line_rows, items_by_id = _fetch_sale_items(sidecar, items_t, item_t, sale_id)
            yield _build_envelope(
                sale=sale, line_rows=line_rows, items_by_id=items_by_id,
                sales_cols=sales_cols, items_cols=items_cols, item_cols=item_cols,
                source_system_key=source_system_key,
            )


def _build_envelope(
    *,
    sale: RowMapping,
    line_rows: list[RowMapping],
    items_by_id: dict[int, RowMapping],
    sales_cols: set[str],
    items_cols: set[str],
    item_cols: set[str],
    source_system_key: str,
) -> dict[str, JsonValue]:
    source_order_id = str(sale["sale_id"])

    total = Decimal("0")
    line_items_payload: list[JsonValue] = []
    for idx, line in enumerate(line_rows, start=1):
        item_id_raw = line.get("item_id")
        item = items_by_id.get(int(item_id_raw)) if item_id_raw is not None else None
        line_payload, line_total_value = _build_line_item(
            line, idx, source_order_id, item, items_cols, item_cols, source_system_key,
        )
        if line_total_value is not None:
            total += Decimal(str(line_total_value))
        line_items_payload.append(line_payload)

    ordered_at = to_iso(sale.get("sale_time"))
    # phppos stores the release date as invoice_date (the date the invoice was issued,
    # i.e. when the order/sale was released). Falls back to sale_time when invoice_date
    # is not present.
    release_date = to_iso(sale.get("invoice_date")) or ordered_at
    customer_id = sale.get("customer_id")

    return build_envelope(
        source_record_id=f"{source_system_key}-sale-{source_order_id}",
        observed_at=ordered_at,
        identifiers=[],
        attributes={},
        record_type="sales",
        raw_payload={
            "order": _build_order_payload(
                sale, source_order_id, ordered_at, release_date, sales_cols, line_rows, total,
            ),
            "line_items": line_items_payload,
            "customer_link": {
                "identity_source_record_id": (
                    f"{source_system_key}-customer-{customer_id}"
                    if customer_id not in (None, 0)
                    else None
                ),
                "source_system_key": source_system_key,
            },
        },
    )


def _build_line_item(
    line: RowMapping,
    idx: int,
    source_order_id: str,
    item: RowMapping | None,
    items_cols: set[str],
    item_cols: set[str],
    source_system_key: str,
) -> tuple[dict[str, JsonValue], float | None]:
    """Build a single line-item payload. Returns ``(payload, line_total)``."""
    unit_price = _decimal_to_float(line.get("item_unit_price"))
    qty_raw = line.get("quantity_purchased")
    qty = float(qty_raw) if qty_raw is not None else None
    discount = _decimal_to_float(line.get("discount")) or 0.0
    line_total_value: float | None = None
    if unit_price is not None and qty is not None:
        line_total_value = unit_price * qty - discount

    payload: dict[str, JsonValue] = {
        "source_line_item_id": f"{source_order_id}:{line.get('line', idx)}",
        "line_no": cast(JsonValue, line.get("line", idx)),
        "quantity": qty,
        "unit_price": unit_price,
        "line_total": line_total_value,
        "discount_amount": discount,
        "tax_amount": None,
        "metadata": {
            "item_id": cast(JsonValue, line.get("item_id")),
            "item_variation_id": _col_or_none(line, "item_variation_id", items_cols),
            "serialnumber": _col_or_none(line, "serialnumber", items_cols),
            "description": _col_or_none(line, "description", items_cols),
        },
        "product": _product_payload(item, source_system_key, item_cols)
        if item is not None
        else None,
    }
    return payload, line_total_value


def _build_order_payload(
    sale: RowMapping,
    source_order_id: str,
    ordered_at: str | None,
    release_date: str | None,
    sales_cols: set[str],
    line_rows: list[RowMapping],
    total: Decimal,
) -> dict[str, JsonValue]:
    """Build the ``order`` section of a phppos sales envelope."""
    status_value: str | None = None
    if "sale_status" in sales_cols:
        raw_status = sale.get("sale_status")
        status_value = str(raw_status) if raw_status is not None else None
    elif "suspended" in sales_cols:
        status_value = "suspended" if sale.get("suspended") else "completed"

    invoice_number = (
        cast(str | None, sale.get("invoice_number")) if "invoice_number" in sales_cols else None
    )
    order_no = invoice_number or source_order_id

    return {
        "source_order_id": source_order_id,
        "order_no": order_no,
        "ordered_at": ordered_at,
        "release_date": release_date,
        "status": status_value,
        "total_amount": float(total),
        "currency": "SGD",
        "item_count": len(line_rows),
        "metadata": {
            "customer_id": cast(JsonValue, sale.get("customer_id")),
            "employee_id": _col_or_none(sale, "employee_id", sales_cols),
            "register_id": _col_or_none(sale, "register_id", sales_cols),
            "payment_type": _col_or_none(sale, "payment_type", sales_cols),
            "sale_type_id": _col_or_none(sale, "sale_type_id", sales_cols),
            "comment": _col_or_none(sale, "comment", sales_cols),
        },
        "raw": serialize_row(sale),
    }


def _col_or_none(row: RowMapping, attr: str, available_cols: set[str]) -> JsonValue:
    """Return a column value if the column exists in this schema, else None."""
    if attr not in available_cols:
        return None
    return cast(JsonValue, row.get(attr))


def _product_payload(
    item: RowMapping, source_system_key: str, item_cols: set[str]
) -> dict[str, JsonValue]:
    item_id = item["item_id"]
    sku = cast(str | None, item.get("item_number")) if "item_number" in item_cols else None
    name = cast(str | None, item.get("name"))
    return {
        "source_product_id": str(item_id),
        "sku": sku,
        "name": name,
        "display_name": name,
        "category": _col_or_none(item, "category", item_cols),
        "subcategory": _col_or_none(item, "subcategory", item_cols),
        "manufacturer": None,
        "is_active": True,
        "attributes": {
            "size": _col_or_none(item, "size", item_cols),
            "cost_price": (
                _decimal_to_float(item.get("cost_price")) if "cost_price" in item_cols else None
            ),
            "unit_price": (
                _decimal_to_float(item.get("unit_price")) if "unit_price" in item_cols else None
            ),
            "description": _col_or_none(item, "description", item_cols),
        },
    }
