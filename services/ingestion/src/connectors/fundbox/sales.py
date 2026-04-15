"""Connector for Fundbox orders (``source_key=fundbox_consumer_backend:sales``).

Emits one ``record_type='sales'`` SourceRecord per Fundbox order. The
payload carries the order header, line items, and the product catalogue
entries each line references — the pipeline turns this into the
``(Person)-[:PURCHASED]->(Order)-[:CONTAINS]->(LineItem)-[:OF_PRODUCT]->(Product)``
sub-graph.

Order-status filter: only ``acknowledged``, ``to release``, and
``completed`` orders are ingested — per product decision, these are the
statuses that count as realised sales. Other statuses (created, pending,
cancelled, …) are skipped.

Linking to a Person is indirect: ``orders.user_id`` is translated into
``fundbox_consumer_backend-user-{user_id}``, which is the
``source_record_id`` of the customer's identity record. The pipeline
handles FOR_CUSTOMER_RECORD resolution; if the identity record has not
been ingested yet the sales record is parked with
``link_status='pending_customer'``.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from src.connectors.fundbox.base import FundboxConnectorBase
from src.connectors.fundbox.builders import (
    build_envelope,
    serialize_row,
    to_iso,
)
from src.connectors.fundbox.schema import (
    merchant_products,
    merchants,
    order_items,
    orders,
    product_variants,
    products,
)
from src.models import JsonValue

_INGESTED_STATUSES: frozenset[str] = frozenset(
    {"acknowledged", "to release", "completed"}
)


def _decimal_to_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    return None


class FundboxSalesConnector(FundboxConnectorBase):
    """Yields one sales SourceRecord per Fundbox order (filtered by status)."""

    def get_source_key(self) -> str:
        return "fundbox_consumer_backend:sales"

    def build_records(self, conn: Connection) -> Iterator[dict[str, JsonValue]]:
        primary_stmt = (
            select(orders)
            .where(orders.c.status.in_(sorted(_INGESTED_STATUSES)))
            .where(orders.c.deleted_at.is_(None))
            .order_by(orders.c.id)
        )

        for chunk in self._chunked(
            self._stream(conn, primary_stmt), self._resolved_chunk_size()
        ):
            order_ids = [row.id for row in chunk]
            items_by_order = self._fetch_grouped(
                conn, order_items, "order_id", order_ids
            )
            merchant_names = self._fetch_merchant_names(
                conn, [row.merchant_id for row in chunk if row.merchant_id]
            )

            variant_ids = {
                item.merchant_product_id
                for items in items_by_order.values()
                for item in items
            }
            product_info = self._fetch_product_info(conn, variant_ids)

            for row in chunk:
                yield self._build_one(
                    row,
                    items_by_order.get(row.id, []),
                    merchant_names,
                    product_info,
                )

    def _fetch_merchant_names(
        self, conn: Connection, merchant_ids: list[int]
    ) -> dict[int, str]:
        if not merchant_ids:
            return {}
        target = self._sidecar_conn or conn
        stmt = select(merchants.c.id, merchants.c.name, merchants.c.official_name).where(
            merchants.c.id.in_(list(set(merchant_ids)))
        )
        result: dict[int, str] = {}
        for row in target.execute(stmt):
            result[row[0]] = row[1] or row[2] or f"merchant-{row[0]}"
        return result

    def _fetch_product_info(
        self, conn: Connection, merchant_product_ids: set[int]
    ) -> dict[int, dict[str, JsonValue]]:
        """Resolve merchant_product_id → {variant, product} bundle.

        merchant_products.product_variant_id → product_variants.id
        product_variants.product_id           → products.id
        """
        if not merchant_product_ids:
            return {}
        target = self._sidecar_conn or conn

        mp_stmt = select(
            merchant_products.c.id.label("merchant_product_id"),
            merchant_products.c.product_variant_id,
        ).where(merchant_products.c.id.in_(list(merchant_product_ids)))
        mp_rows = list(target.execute(mp_stmt))
        variant_ids = [r.product_variant_id for r in mp_rows]
        if not variant_ids:
            return {}

        variant_stmt = select(product_variants).where(
            product_variants.c.id.in_(variant_ids)
        )
        variants: dict[int, Any] = {r.id: r for r in target.execute(variant_stmt)}

        product_ids = [v.product_id for v in variants.values() if v.product_id]
        products_map: dict[int, Any] = {}
        if product_ids:
            product_stmt = select(products).where(products.c.id.in_(list(set(product_ids))))
            products_map = {r.id: r for r in target.execute(product_stmt)}

        bundle: dict[int, dict[str, JsonValue]] = {}
        for mp in mp_rows:
            variant = variants.get(mp.product_variant_id)
            if variant is None:
                continue
            product = products_map.get(variant.product_id)
            bundle[mp.merchant_product_id] = {
                "source_product_id": f"variant-{variant.id}",
                "sku": variant.sku,
                "name": variant.name,
                "display_name": product.name if product else variant.name,
                "category": product.category if product else None,
                "subcategory": product.sub_category if product else None,
                "manufacturer": product.make if product else None,
                "is_active": bool(variant.active),
                "attributes": {
                    "variant_attributes": variant.attributes,
                    "type": product.type if product else None,
                    "sub_type": product.sub_type if product else None,
                    "model": product.model if product else None,
                },
            }
        return bundle

    def _build_one(
        self,
        row: Any,
        line_rows: list[Any],
        merchant_names: dict[int, str],
        product_info: dict[int, dict[str, JsonValue]],
    ) -> dict[str, JsonValue]:
        source_order_id = str(row.id)
        line_items_payload: list[JsonValue] = []
        for idx, line in enumerate(line_rows, start=1):
            product = product_info.get(line.merchant_product_id)
            line_items_payload.append(
                {
                    "source_line_item_id": f"{source_order_id}:{line.id}",
                    "line_no": idx,
                    "quantity": line.quantity,
                    "unit_price": _decimal_to_float(line.price),
                    "line_total": _decimal_to_float(line.price) * line.quantity
                    if line.price is not None and line.quantity is not None
                    else None,
                    "discount_amount": None,
                    "tax_amount": None,
                    "metadata": {
                        "lta_tag": line.lta_tag,
                        "serial_no": line.serial_no,
                        "merchant_product_id": line.merchant_product_id,
                    },
                    "product": product,
                }
            )

        return build_envelope(
            source_record_id=f"fundbox_consumer_backend-order-{row.id}",
            observed_at=to_iso(row.updated_at or row.created_at),
            identifiers=[],
            attributes={},
            record_type="sales",
            raw_payload={
                "order": {
                    "source_order_id": source_order_id,
                    "order_no": row.order_no,
                    "ordered_at": to_iso(row.created_at),
                    "status": row.status,
                    "total_amount": _decimal_to_float(row.total_amount),
                    "currency": "SGD",
                    "item_count": row.total_items,
                    "metadata": {
                        "transaction_reference": row.transaction_reference,
                        "release_date": to_iso(row.release_date),
                        "merchant_id": row.merchant_id,
                        "merchant_name": merchant_names.get(row.merchant_id),
                        "merchant_staff_id": row.merchant_staff_id,
                        "expiry_at": row.expiry_at,
                    },
                    "raw": serialize_row(row),
                },
                "line_items": line_items_payload,
                "customer_link": {
                    "identity_source_record_id": (
                        f"fundbox_consumer_backend-user-{row.user_id}"
                        if row.user_id is not None
                        else None
                    ),
                    "source_system_key": "fundbox_consumer_backend",
                },
            },
        )
