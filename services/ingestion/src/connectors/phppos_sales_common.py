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
from typing import Any

from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.engine import Connection, Engine

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


def fetch_phppos_sales(
    engine: Engine,
    conn: Connection,
    source_system_key: str,
    chunk_size: int,
) -> Iterator[dict[str, JsonValue]]:
    """Yield one sales envelope per phppos_sales row.

    Uses reflection against the live DB rather than a hand-maintained
    schema module — the phppos installation may carry local column
    additions and we only read a known subset by name.

    A separate sidecar connection is opened for per-sale line-item and
    item lookups. Running those on the same connection as the primary
    streaming cursor aborts the stream after the first yield under
    pymysql (``Previous unbuffered result was left incomplete``).
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

    primary_stmt = select(sales_t).order_by(sales_t.c.sale_id)
    result = conn.execute(primary_stmt).yield_per(chunk_size)

    with engine.connect() as sidecar_conn:
        for sale in result:
            sale_id: int = sale.sale_id
            line_stmt = select(items_t).where(items_t.c.sale_id == sale_id)
            line_rows = list(sidecar_conn.execute(line_stmt))

            item_ids = [r.item_id for r in line_rows if r.item_id is not None]
            items_by_id: dict[int, Any] = {}
            if item_ids:
                item_stmt = select(item_t).where(
                    item_t.c.item_id.in_(list(set(item_ids)))
                )
                items_by_id = {r.item_id: r for r in sidecar_conn.execute(item_stmt)}

            yield _build_envelope(
                sale=sale,
                line_rows=line_rows,
                items_by_id=items_by_id,
                sales_cols=sales_cols,
                items_cols=items_cols,
                item_cols=item_cols,
                source_system_key=source_system_key,
            )


def _build_envelope(
    *,
    sale: Any,
    line_rows: list[Any],
    items_by_id: dict[int, Any],
    sales_cols: set[str],
    items_cols: set[str],
    item_cols: set[str],
    source_system_key: str,
) -> dict[str, JsonValue]:
    source_order_id = str(sale.sale_id)

    total = Decimal("0")
    line_items_payload: list[JsonValue] = []
    for idx, line in enumerate(line_rows, start=1):
        item = items_by_id.get(line.item_id) if line.item_id is not None else None
        unit_price = _decimal_to_float(getattr(line, "item_unit_price", None))
        qty_raw = getattr(line, "quantity_purchased", None)
        qty = float(qty_raw) if qty_raw is not None else None
        discount = _decimal_to_float(getattr(line, "discount", None)) or 0.0
        line_total_value: float | None = None
        if unit_price is not None and qty is not None:
            line_total_value = unit_price * qty - discount
            total += Decimal(str(line_total_value))

        line_items_payload.append(
            {
                "source_line_item_id": f"{source_order_id}:{getattr(line, 'line', idx)}",
                "line_no": getattr(line, "line", idx),
                "quantity": qty,
                "unit_price": unit_price,
                "line_total": line_total_value,
                "discount_amount": discount,
                "tax_amount": None,
                "metadata": {
                    "item_id": getattr(line, "item_id", None),
                    "item_variation_id": (
                        getattr(line, "item_variation_id", None)
                        if "item_variation_id" in items_cols
                        else None
                    ),
                    "serialnumber": (
                        getattr(line, "serialnumber", None)
                        if "serialnumber" in items_cols
                        else None
                    ),
                    "description": (
                        getattr(line, "description", None)
                        if "description" in items_cols
                        else None
                    ),
                },
                "product": _product_payload(item, source_system_key, item_cols)
                if item is not None
                else None,
            }
        )

    ordered_at = to_iso(getattr(sale, "sale_time", None))
    status_value: str | None = None
    if "sale_status" in sales_cols:
        status_value = str(sale.sale_status) if sale.sale_status is not None else None
    elif "suspended" in sales_cols:
        status_value = "suspended" if sale.suspended else "completed"

    customer_id = getattr(sale, "customer_id", None)

    order_no = (
        getattr(sale, "invoice_number", None) if "invoice_number" in sales_cols else None
    ) or source_order_id

    return build_envelope(
        source_record_id=f"{source_system_key}-sale-{source_order_id}",
        observed_at=ordered_at,
        identifiers=[],
        attributes={},
        record_type="sales",
        raw_payload={
            "order": {
                "source_order_id": source_order_id,
                "order_no": order_no,
                "ordered_at": ordered_at,
                "status": status_value,
                "total_amount": float(total),
                "currency": "SGD",
                "item_count": len(line_rows),
                "metadata": {
                    "customer_id": customer_id,
                    "employee_id": (
                        getattr(sale, "employee_id", None)
                        if "employee_id" in sales_cols
                        else None
                    ),
                    "register_id": (
                        getattr(sale, "register_id", None)
                        if "register_id" in sales_cols
                        else None
                    ),
                    "payment_type": (
                        getattr(sale, "payment_type", None)
                        if "payment_type" in sales_cols
                        else None
                    ),
                    "sale_type_id": (
                        getattr(sale, "sale_type_id", None)
                        if "sale_type_id" in sales_cols
                        else None
                    ),
                    "comment": (
                        getattr(sale, "comment", None)
                        if "comment" in sales_cols
                        else None
                    ),
                },
                "raw": serialize_row(sale),
            },
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


def _product_payload(
    item: Any, source_system_key: str, item_cols: set[str]
) -> dict[str, JsonValue]:
    item_id = item.item_id
    sku = getattr(item, "item_number", None) if "item_number" in item_cols else None
    name = getattr(item, "name", None)
    return {
        "source_product_id": str(item_id),
        "sku": sku,
        "name": name,
        "display_name": name,
        "category": None,
        "subcategory": None,
        "manufacturer": None,
        "is_active": True,
        "attributes": {
            "size": getattr(item, "size", None) if "size" in item_cols else None,
            "cost_price": _decimal_to_float(getattr(item, "cost_price", None))
            if "cost_price" in item_cols
            else None,
            "unit_price": _decimal_to_float(getattr(item, "unit_price", None))
            if "unit_price" in item_cols
            else None,
            "description": (
                getattr(item, "description", None)
                if "description" in item_cols
                else None
            ),
        },
    }
