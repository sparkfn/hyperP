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
from datetime import date
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.engine import Connection

from src.config import get_settings
from src.connectors.base import SourceConnector
from src.connectors.fundbox.builders import (
    IdentifierBag,
    address_from_row,
    build_envelope,
    format_address,
    phone_region_hint,
    serialize_row,
    to_iso,
)
from src.connectors.speedzone.db import get_engine
from src.connectors.speedzone.schema import customers, employees, people
from src.models import JsonValue

logger = logging.getLogger(__name__)


def _raw_json_value(value: object) -> JsonValue:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _person_raw_payload(row: Any) -> dict[str, JsonValue]:
    payload = serialize_row(row)
    mapping = row._mapping if hasattr(row, "_mapping") else row
    for index in range(1, 11):
        key = f"custom_field_{index}_value"
        if key in mapping:
            payload[key] = _raw_json_value(mapping[key])
    return payload


def _date_string_to_iso(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    if not 1900 <= parsed.year <= 2100:
        return None
    return parsed.isoformat()


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
            logger.warning("SpeedZone: phppos_people table missing — skipping identity ingestion.")
            return

        use_customers = "phppos_customers" in existing_tables
        if not use_customers:
            logger.warning(
                "SpeedZone: phppos_customers table missing — ingesting from "
                "phppos_people only; NRIC and bitrix_user_id will be absent."
            )

        with engine.connect() as conn:
            conn = conn.execution_options(stream_results=True)
            excluded_person_ids = self._fetch_employee_person_ids(conn, existing_tables)
            if use_customers:
                yield from self._build_records_with_customers(conn, chunk_size, excluded_person_ids)
            else:
                yield from self._build_records_people_only(conn, chunk_size, excluded_person_ids)

    @staticmethod
    def _fetch_employee_person_ids(conn: Connection, existing_tables: set[str]) -> set[int]:
        if "phppos_employees" not in existing_tables:
            return set()
        result = conn.execute(select(employees.c.person_id))
        return {int(row[0]) for row in result if row[0] is not None}

    def _build_records_with_customers(
        self, conn: Connection, chunk_size: int, excluded_person_ids: set[int]
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
                customers.c.custom_field_1_value,
                customers.c.custom_field_2_value,
                customers.c.custom_field_3_value,
                customers.c.custom_field_4_value,
                customers.c.custom_field_5_value,
                customers.c.custom_field_6_value,
                customers.c.custom_field_7_value,
                customers.c.custom_field_8_value,
                customers.c.custom_field_9_value,
                customers.c.custom_field_10_value,
            )
            .select_from(customers.join(people, customers.c.person_id == people.c.person_id))
            .where(customers.c.deleted == 0)
            .order_by(customers.c.id)
        )

        result = conn.execute(stmt).yield_per(chunk_size)
        for row in result:
            if row.person_id in excluded_person_ids:
                continue
            yield self._build_envelope_with_customer(row)

    def _build_records_people_only(
        self, conn: Connection, chunk_size: int, excluded_person_ids: set[int]
    ) -> Iterator[dict[str, JsonValue]]:
        stmt = select(people).order_by(people.c.person_id)
        result = conn.execute(stmt).yield_per(chunk_size)
        for row in result:
            if row.person_id in excluded_person_ids:
                continue
            yield self._build_envelope_people_only(row)

    @staticmethod
    def _build_envelope_with_customer(row: Any) -> dict[str, JsonValue]:
        ids = IdentifierBag()
        ids.add("nric", row.custom_field_1_value, verified=True)
        ids.add("email", row.email)
        ids.add(
            "phone",
            row.phone_number,
            region_hint=phone_region_hint(row.phone_code, row.country),
        )

        if row.custom_field_2_value:
            ids.add("external:bitrix", row.custom_field_2_value)

        address = format_address(row)
        address_row = address_from_row(row)
        dob = _date_string_to_iso(row.custom_field_9_value)
        return build_envelope(
            # Keyed on person_id (not customers.id): phppos_sales.customer_id is a
            # person_id, so sales link to `-customer-{person_id}`. Both sides must
            # agree on person_id. person_id is unique among non-deleted customers.
            source_record_id=f"speedzone_phppos-customer-{row.person_id}",
            observed_at=to_iso(row.last_modified or row.create_date),
            identifiers=ids.items,
            attributes={
                "full_name": row.full_name,
                "dob": dob,
                "address": address,
            },
            raw_payload={"person": _person_raw_payload(row)},
            addresses=[address_row] if address_row is not None else None,
        )

    @staticmethod
    def _build_envelope_people_only(row: Any) -> dict[str, JsonValue]:
        ids = IdentifierBag()
        ids.add("email", row.email)
        ids.add(
            "phone",
            row.phone_number,
            region_hint=phone_region_hint(row.phone_code, row.country),
        )

        address = format_address(row)
        address_row = address_from_row(row)
        return build_envelope(
            source_record_id=f"speedzone_phppos-person-{row.person_id}",
            observed_at=to_iso(row.last_modified or row.create_date),
            identifiers=ids.items,
            attributes={
                "full_name": row.full_name,
                "address": address,
            },
            raw_payload={"person": serialize_row(row)},
            addresses=[address_row] if address_row is not None else None,
        )
