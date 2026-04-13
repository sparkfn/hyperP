"""Connector for SpeedZone POS customers (``source_key=speedzone``)."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import select
from sqlalchemy.engine import Connection, Row

from src.config import get_settings
from src.connectors.base import SourceConnector
from src.connectors.fundbox.builders import (
    IdentifierBag,
    build_envelope,
    serialize_row,
    to_iso,
)
from src.connectors.speedzone.db import get_engine
from src.connectors.speedzone.schema import customers, people
from src.models import JsonValue


class SpeedZoneConnector(SourceConnector):
    """Yields one source record per active SpeedZone POS customer.

    Streams ``phppos_people`` joined with ``phppos_customers`` in chunks.
    Only non-deleted customers are emitted.  The PHP POS schema stores person
    details (name, phone, email, address) in ``phppos_people`` and
    customer-specific data (NRIC via custom_field_1, bitrix ID via
    custom_field_2) in ``phppos_customers``.
    """

    def get_source_key(self) -> str:
        return "speedzone"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        engine = get_engine()
        chunk_size = get_settings().speedzone_chunk_size
        with engine.connect() as conn:
            conn = conn.execution_options(stream_results=True)
            yield from self._build_records(conn, chunk_size)

    def _build_records(self, conn: Connection, chunk_size: int) -> Iterator[dict[str, JsonValue]]:
        stmt = (
            select(
                people.c.person_id,
                people.c.first_name,
                people.c.last_name,
                people.c.full_name,
                people.c.phone_number,
                people.c.email,
                people.c.address_1,
                people.c.address_2,
                people.c.city,
                people.c.state,
                people.c.zip,
                people.c.country,
                people.c.comments,
                people.c.create_date,
                people.c.last_modified,
                people.c.title,
                people.c.phone_code,
                customers.c.id.label("customer_id"),
                customers.c.account_number,
                customers.c.company_name,
                customers.c.custom_field_1_value.label("nric_passport"),
                customers.c.custom_field_2_value.label("bitrix_user_id"),
            )
            .select_from(customers.join(people, customers.c.person_id == people.c.person_id))
            .where(customers.c.deleted == 0)
            .order_by(customers.c.id)
        )

        result = conn.execute(stmt).yield_per(chunk_size)
        for row in result:
            yield self._build_one(row)

    @staticmethod
    def _build_one(row: Row[tuple[object, ...]]) -> dict[str, JsonValue]:
        ids = IdentifierBag()
        ids.add("nric", row.nric_passport, verified=True)
        ids.add("email", row.email)
        ids.add("phone", row.phone_number)

        if row.bitrix_user_id:
            ids.add("external:bitrix", row.bitrix_user_id)

        address_parts: list[object] = [
            row.address_1,
            row.address_2,
            row.city,
            row.state,
            row.zip,
            row.country,
        ]
        address = ", ".join(str(p).strip() for p in address_parts if p and str(p).strip()) or None

        return build_envelope(
            source_record_id=f"speedzone-customer-{row.customer_id}",
            observed_at=to_iso(row.last_modified or row.create_date),
            identifiers=ids.items,
            attributes={
                "full_name": row.full_name,
                "address": address,
            },
            raw_payload={
                "person": serialize_row(row),
            },
        )
