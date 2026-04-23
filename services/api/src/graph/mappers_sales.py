"""Map raw Neo4j records to sales domain models."""

from __future__ import annotations

from src.graph.converters import (
    GraphRecord,
    GraphValue,
    to_iso_or_none,
    to_optional_float,
    to_optional_int,
    to_optional_str,
)
from src.types_sales import SalesLineItem, SalesOrder, SalesProduct


def _as_dict(value: GraphValue) -> GraphRecord:
    if isinstance(value, dict):
        return value
    return {}


def map_sales_order(record: GraphRecord) -> SalesOrder:
    """Map a single Neo4j result row to a SalesOrder with nested line items."""
    raw_items = record.get("line_items")
    line_items: list[SalesLineItem] = []
    if isinstance(raw_items, list):
        line_items = [
            SalesLineItem(
                line_no=to_optional_int(d.get("line_no")),
                quantity=to_optional_float(d.get("quantity")),
                unit_price=to_optional_float(d.get("unit_price")),
                subtotal=to_optional_float(d.get("subtotal")),
                product=SalesProduct(
                    display_name=to_optional_str(d.get("product_display_name")),
                    sku=to_optional_str(d.get("product_sku")),
                    category=to_optional_str(d.get("product_category")),
                ),
            )
            for raw in raw_items
            if (d := _as_dict(raw))
        ]
    return SalesOrder(
        order_no=to_optional_str(record.get("order_no")),
        source_order_id=to_optional_str(record.get("source_order_id")),
        order_date=to_iso_or_none(record.get("order_date")),
        release_date=to_iso_or_none(record.get("release_date")),
        total_amount=to_optional_float(record.get("total_amount")),
        currency=to_optional_str(record.get("currency")),
        source_system=to_optional_str(record.get("source_system")),
        entity_name=to_optional_str(record.get("entity_name")),
        line_items=line_items,
    )
