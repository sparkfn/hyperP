"""Connector for Eko phppos sales (``source_key=eko_phppos:sales``).

Skips silently when the phppos sales tables are not present in the
mounted database.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import inspect, select
from sqlalchemy.engine import Connection

from src.config import get_settings
from src.connectors.base import SourceConnector
from src.connectors.eko.db import get_engine
from src.connectors.eko.schema import customers, employees
from src.connectors.phppos_sales_common import fetch_phppos_sales
from src.models import JsonValue


class EkoSalesConnector(SourceConnector):
    """Yields one sales SourceRecord per Eko phppos_sales row."""

    def get_source_key(self) -> str:
        return "eko_phppos:sales"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        engine = get_engine()
        chunk_size = get_settings().eko_phppos_chunk_size
        with engine.connect() as conn:
            conn = conn.execution_options(stream_results=True)
            excluded_customer_ids = self._fetch_employee_customer_ids(
                conn, set(inspect(engine).get_table_names())
            )
            yield from fetch_phppos_sales(
                engine=engine,
                conn=conn,
                source_system_key="eko_phppos",
                chunk_size=chunk_size,
                excluded_customer_ids=excluded_customer_ids,
            )

    @staticmethod
    def _fetch_employee_customer_ids(conn: Connection, existing_tables: set[str]) -> set[int]:
        if "phppos_employees" not in existing_tables or "phppos_customers" not in existing_tables:
            return set()
        stmt = (
            select(customers.c.id)
            .select_from(customers.join(employees, customers.c.person_id == employees.c.person_id))
            .where(customers.c.deleted == 0)
        )
        return {int(row[0]) for row in conn.execute(stmt) if row[0] is not None}
