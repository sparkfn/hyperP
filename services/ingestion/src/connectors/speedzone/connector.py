"""Connector for SpeedZone POS identities (``source_key=speedzone_phppos``).

Preferred extraction: ``phppos_customers`` joined with ``phppos_people``
(gives NRIC via custom_field_1 and bitrix_user_id via custom_field_2).

Fallback: if ``phppos_customers`` is absent from the mounted database
(common in dev fixtures that ship only the config subset of phppos), the
connector ingests from ``phppos_people`` alone — dropping NRIC and
bitrix_user_id but still yielding phone/email/name/address identifiers.
This matches the product decision (2026-04-15): ingest whatever identity
signal is available; don't fail the whole task when a sibling table is
missing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.engine import Connection

from src.config import get_settings
from src.connectors.base import SourceConnector
from src.connectors.fundbox.builders import (
    IdentifierBag,
    build_envelope,
    format_address,
    serialize_row,
    to_iso,
)
from src.connectors.speedzone.db import get_engine
from src.connectors.speedzone.schema import customers, people
from src.models import JsonValue

logger = logging.getLogger(__name__)


class SpeedZoneConnector(SourceConnector):
    """Yields one source record per SpeedZone POS identity.

    Uses ``phppos_customers`` when available, otherwise falls back to
    ``phppos_people`` alone.
    """

    def get_source_key(self) -> str:
        return "speedzone_phppos"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        engine = get_engine()
        chunk_size = get_settings().speedzone_phppos_chunk_size
        existing_tables = set(inspect(engine).get_table_names())

        if "phppos_people" not in existing_tables:
            logger.warning(
                "SpeedZone: phppos_people table missing — skipping identity ingestion."
            )
            return

        use_customers = "phppos_customers" in existing_tables
        if not use_customers:
            logger.warning(
                "SpeedZone: phppos_customers table missing — ingesting from "
                "phppos_people only; NRIC and bitrix_user_id will be absent."
            )

        with engine.connect() as conn:
            conn = conn.execution_options(stream_results=True)
            if use_customers:
                yield from self._build_records_with_customers(conn, chunk_size)
            else:
                yield from self._build_records_people_only(conn, chunk_size)

    def _build_records_with_customers(
        self, conn: Connection, chunk_size: int
    ) -> Iterator[dict[str, JsonValue]]:
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
            yield self._build_envelope_with_customer(row)

    def _build_records_people_only(
        self, conn: Connection, chunk_size: int
    ) -> Iterator[dict[str, JsonValue]]:
        stmt = select(people).order_by(people.c.person_id)
        result = conn.execute(stmt).yield_per(chunk_size)
        for row in result:
            yield self._build_envelope_people_only(row)

    @staticmethod
    def _build_envelope_with_customer(row: Any) -> dict[str, JsonValue]:
        ids = IdentifierBag()
        ids.add("nric", row.nric_passport, verified=True)
        ids.add("email", row.email)
        ids.add("phone", row.phone_number)

        if row.bitrix_user_id:
            ids.add("external:bitrix", row.bitrix_user_id)

        address = format_address(row)
        return build_envelope(
            source_record_id=f"speedzone_phppos-customer-{row.customer_id}",
            observed_at=to_iso(row.last_modified or row.create_date),
            identifiers=ids.items,
            attributes={
                "full_name": row.full_name,
                "address": address,
            },
            raw_payload={"person": serialize_row(row)},
        )

    @staticmethod
    def _build_envelope_people_only(row: Any) -> dict[str, JsonValue]:
        ids = IdentifierBag()
        ids.add("email", row.email)
        ids.add("phone", row.phone_number)

        address = format_address(row)
        return build_envelope(
            source_record_id=f"speedzone_phppos-person-{row.person_id}",
            observed_at=to_iso(row.last_modified or row.create_date),
            identifiers=ids.items,
            attributes={
                "full_name": row.full_name,
                "address": address,
            },
            raw_payload={"person": serialize_row(row)},
        )
