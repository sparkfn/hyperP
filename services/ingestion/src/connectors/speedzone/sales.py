"""Connector for SpeedZone phppos sales (``source_key=speedzone_phppos:sales``).

Skips silently when the phppos sales tables are not present in the
mounted database — per product decision (2026-04-15), identity ingestion
for these sources must continue even when sales data is unavailable.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import inspect

from src.config import get_settings
from src.connectors.base import SourceConnector
from src.connectors.phppos_sales_common import fetch_employee_person_ids, fetch_phppos_sales
from src.connectors.speedzone.db import get_engine
from src.connectors.speedzone.schema import customers, employees
from src.models import JsonValue


class SpeedZoneSalesConnector(SourceConnector):
    """Yields one sales SourceRecord per SpeedZone phppos_sales row."""

    def get_source_key(self) -> str:
        return "speedzone_phppos:sales"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        engine = get_engine()
        chunk_size = get_settings().speedzone_phppos_chunk_size
        with engine.connect() as conn:
            conn = conn.execution_options(stream_results=True)
            excluded_person_ids = fetch_employee_person_ids(
                conn,
                customers_t=customers,
                employees_t=employees,
                existing_tables=set(inspect(engine).get_table_names()),
            )
            yield from fetch_phppos_sales(
                engine=engine,
                conn=conn,
                source_system_key="speedzone_phppos",
                chunk_size=chunk_size,
                excluded_person_ids=excluded_person_ids,
                # SpeedZone customer ``custom_field_8/10_value`` holds the bike
                # plate, emitted as the line ``lta_tag`` for vehicle extraction.
                extract_bike_plate=True,
            )
