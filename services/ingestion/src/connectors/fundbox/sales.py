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
from typing import TypedDict, cast

from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping

from src.connectors.fundbox.base import FundboxConnectorBase
from src.connectors.fundbox.builders import (
    build_envelope,
    coerce_float,
    serialize_row,
    to_iso,
)
from src.connectors.fundbox.schema import (
    basic_profiles,
    merchant_products,
    merchants,
    order_items,
    orders,
    product_variants,
    products,
    users,
)
from src.models import JsonValue

_INGESTED_STATUSES: frozenset[str] = frozenset({"acknowledged", "to release", "completed"})


class _CustomerContact(TypedDict):
    """Sale-level customer contact channels for the Vehicle matching heuristic."""

    customer_emails: list[str]
    customer_phones: list[str]
    customer_nric: str | None


def _decimal_to_float(value: object) -> float | None:
    return coerce_float(value)


def _variant_to_product(
    variant: RowMapping,
    product: RowMapping | None,
) -> dict[str, JsonValue]:
    """Build a product payload from a variant + optional parent product."""
    return {
        "source_product_id": f"variant-{variant.id}",
        "sku": variant.sku,
        "name": variant.name,
        "display_name": product.name if product else variant.name,
        "category": product.category if product else None,
        "subcategory": product.sub_category if product else None,
        "manufacturer": product.make if product else None,
        # ``vehicle_extraction.observations_from_sales_lines`` reads ``model``
        # from the top-level product dict, so mirror it out of ``attributes``.
        "model": product.model if product else None,
        "is_active": bool(variant.active),
        "attributes": {
            "variant_attributes": variant.attributes,
            "type": product.type if product else None,
            "sub_type": product.sub_type if product else None,
            "model": product.model if product else None,
        },
        "type": product.type if product else None,
        "sub_type": product.sub_type if product else None,
        # Secondary signals (vehicle extraction's primary gate is category).
        "has_serial_number": bool(product.has_serial_number) if product else False,
        "has_lta_tag": bool(product.has_lta_tag) if product else False,
    }


def _build_line_items(
    line_rows: list[RowMapping],
    source_order_id: str,
    product_info: dict[int, dict[str, JsonValue]],
    merchant_name: str | None,
) -> list[JsonValue]:
    """Build the line_items list for a Fundbox sales envelope."""
    items: list[JsonValue] = []
    for idx, line in enumerate(line_rows, start=1):
        product = product_info.get(line.merchant_product_id)
        unit_price = _decimal_to_float(line.price)
        line_total = (
            unit_price * line.quantity
            if unit_price is not None and line.quantity is not None
            else None
        )
        items.append(
            {
                "source_line_item_id": f"{source_order_id}:{line.id}",
                "line_no": idx,
                "quantity": line.quantity,
                "unit_price": unit_price,
                "line_total": line_total,
                "discount_amount": None,
                "tax_amount": None,
                "metadata": {
                    "lta_tag": line.lta_tag,
                    "serial_no": line.serial_no,
                    "merchant_product_id": line.merchant_product_id,
                    # Order-level merchant name — the pipeline assembles
                    # ``non_vehicle_lines`` from line product fields + this
                    # ``metadata.merchant`` (Task 5).
                    "merchant": merchant_name,
                },
                "product": product,
            }
        )
    return items


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

        for chunk in self._chunked(self._stream(conn, primary_stmt), self._resolved_chunk_size()):
            user_ids = [row.user_id for row in chunk if row.user_id is not None]
            excluded_user_ids = self._fetch_excluded_user_ids(conn, user_ids)
            eligible_chunk = [
                row for row in chunk if row.user_id is None or row.user_id not in excluded_user_ids
            ]
            order_ids = [row.id for row in eligible_chunk]
            items_by_order = self._fetch_grouped(conn, order_items, "order_id", order_ids)
            merchant_names = self._fetch_merchant_names(
                conn, [row.merchant_id for row in eligible_chunk if row.merchant_id]
            )

            variant_ids = {
                item.merchant_product_id for items in items_by_order.values() for item in items
            }
            product_info = self._fetch_product_info(conn, variant_ids)
            customer_contacts = self._fetch_customer_contacts(
                conn, [row.user_id for row in eligible_chunk if row.user_id is not None]
            )

            for row in eligible_chunk:
                yield self._build_one(
                    row,
                    items_by_order.get(row.id, []),
                    merchant_names,
                    product_info,
                    customer_contacts.get(row.user_id) if row.user_id is not None else None,
                )

    def _fetch_merchant_names(self, conn: Connection, merchant_ids: list[int]) -> dict[int, str]:
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

    def _fetch_customer_contacts(
        self,
        conn: Connection,
        user_ids: list[int],
    ) -> dict[int, _CustomerContact]:
        """Batch-load ``users`` + ``basic_profiles`` for a chunk of orders.

        Returns ``{user_id: {"customer_emails": [...], "customer_phones": [...],
        "customer_nric": str | None}}`` so the sales SourceRecord can carry the
        customer's contact channels at sale level — the Vehicle heuristic
        (``_propose_one_pending_sale``) reads ``raw_payload.customer_emails /
        customer_phones / customer_nric`` to find candidates.

        Emails and phones are deduped across the ``users`` and ``basic_profiles``
        rows (non-empty only); ``customer_nric`` comes from
        ``basic_profiles.nric`` (Task 4 emitted it per-line as
        ``metadata.nric`` — this standardizes the sale-level access path).
        """
        if not user_ids:
            return {}
        target = self._sidecar_conn or conn
        ids = list(set(user_ids))

        user_rows: dict[int, RowMapping] = {}
        for r in target.execute(select(users).where(users.c.id.in_(ids))):
            user_rows[int(r.id)] = cast(RowMapping, r)

        # A user may have multiple basic_profiles; keep the first (lowest id)
        # deterministically.
        profile_rows: dict[int, RowMapping] = {}
        profile_stmt = (
            select(basic_profiles)
            .where(basic_profiles.c.user_id.in_(ids))
            .order_by(basic_profiles.c.id)
        )
        for r in target.execute(profile_stmt):
            uid = int(r.user_id)
            if uid not in profile_rows:
                profile_rows[uid] = cast(RowMapping, r)

        contacts: dict[int, _CustomerContact] = {}
        for uid in ids:
            u = user_rows.get(uid)
            p = profile_rows.get(uid)
            emails: list[str] = []
            phones: list[str] = []
            for src in (u, p):
                if src is None:
                    continue
                email = src.email
                if email and email not in emails:
                    emails.append(email)
                mobile = src.mobile_number
                if mobile and mobile not in phones:
                    phones.append(mobile)
            nric: str | None = p.nric if p is not None and p.nric else None
            contacts[uid] = {
                "customer_emails": emails,
                "customer_phones": phones,
                "customer_nric": nric,
            }
        return contacts

    def _fetch_product_info(
        self, conn: Connection, merchant_product_ids: set[int]
    ) -> dict[int, dict[str, JsonValue]]:
        """Resolve merchant_product_id → {variant, product} bundle."""
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

        variant_stmt = select(product_variants).where(product_variants.c.id.in_(variant_ids))
        variants: dict[int, RowMapping] = {
            r.id: cast(RowMapping, r) for r in target.execute(variant_stmt)
        }

        product_ids = [v.product_id for v in variants.values() if v.product_id]
        products_map: dict[int, RowMapping] = {}
        if product_ids:
            product_stmt = select(products).where(products.c.id.in_(list(set(product_ids))))
            products_map = {r.id: cast(RowMapping, r) for r in target.execute(product_stmt)}

        bundle: dict[int, dict[str, JsonValue]] = {}
        for mp in mp_rows:
            variant = variants.get(mp.product_variant_id)
            if variant is None:
                continue
            product = products_map.get(variant.product_id)
            bundle[mp.merchant_product_id] = _variant_to_product(variant, product)
        return bundle

    def _build_one(
        self,
        row: RowMapping,
        line_rows: list[RowMapping],
        merchant_names: dict[int, str],
        product_info: dict[int, dict[str, JsonValue]],
        customer_contact: _CustomerContact | None,
    ) -> dict[str, JsonValue]:
        source_order_id = str(row.id)
        if customer_contact is None:
            customer_emails: list[str] = []
            customer_phones: list[str] = []
            customer_nric: str | None = None
        else:
            customer_emails = list(customer_contact.get("customer_emails") or [])
            customer_phones = list(customer_contact.get("customer_phones") or [])
            customer_nric = customer_contact.get("customer_nric")
            if not isinstance(customer_nric, str):
                customer_nric = None
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
                "line_items": _build_line_items(
                    line_rows,
                    source_order_id,
                    product_info,
                    merchant_names.get(row.merchant_id) if row.merchant_id else None,
                ),
                "customer_link": {
                    "identity_source_record_id": (
                        f"fundbox_consumer_backend-user-{row.user_id}"
                        if row.user_id is not None
                        else None
                    ),
                    "source_system_key": "fundbox_consumer_backend",
                },
                # Sale-level customer contact for the vehicle matching
                # heuristic (Task 6). Fundbox sales rows reference the customer
                # by ``user_id`` (an identity FK); the customer's
                # email/phone/nric live on the ``users``/``basic_profiles``
                # tables, joined here by ``orders.user_id`` and emitted at sale
                # level so ``_propose_one_pending_sale`` can read
                # ``raw_payload.customer_emails / customer_phones /
                # customer_nric``. Emails/phones are deduped non-empty values
                # across the two tables; ``customer_nric`` comes from
                # ``basic_profiles.nric``.
                "customer_nric": customer_nric,
                "customer_emails": cast(list[JsonValue], customer_emails),
                "customer_phones": cast(list[JsonValue], customer_phones),
            },
        )
