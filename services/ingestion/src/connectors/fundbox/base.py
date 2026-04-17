"""Base class with shared streaming primitives for Fundbox connectors."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from sqlalchemy import Select, Table, select
from sqlalchemy.engine import Connection

from src.config import get_settings
from src.connectors.base import SourceConnector
from src.connectors.fundbox.db import get_engine
from src.models import JsonValue


class FundboxConnectorBase(SourceConnector):
    """Common behavior for all Fundbox connectors.

    Subclasses implement :meth:`build_records`, which receives an open
    SQLAlchemy ``Connection`` and yields raw envelope dicts. The base class
    handles connection lifecycle so each connector stays focused on its own
    extraction logic.
    """

    #: Streaming chunk size for primary-table reads. Override per connector
    #: if a particular slice needs different memory characteristics.
    chunk_size: int | None = None

    #: Sidecar connection used for batch lookups while the primary stream is
    #: still open. Set inside :meth:`fetch_records` and torn down on exit.
    _sidecar_conn: Connection | None = None

    def get_source_key(self) -> str:  # pragma: no cover - subclass contract
        raise NotImplementedError

    def build_records(self, conn: Connection) -> Iterator[dict[str, JsonValue]]:  # pragma: no cover
        raise NotImplementedError

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        # Two connections: one streams the primary entity, the other handles
        # sidecar batch lookups. This avoids interleaving queries on a single
        # streaming cursor (which pymysql/MySQL refuses).
        engine = get_engine()
        with engine.connect() as primary_conn, engine.connect() as sidecar_conn:
            primary_conn = primary_conn.execution_options(stream_results=True)
            self._sidecar_conn = sidecar_conn
            try:
                yield from self.build_records(primary_conn)
            finally:
                self._sidecar_conn = None

    # ---- helpers shared by subclasses --------------------------------------

    def _resolved_chunk_size(self) -> int:
        return self.chunk_size or get_settings().fundbox_consumer_backend_chunk_size

    def _stream(self, conn: Connection, stmt: Select) -> Iterator[Any]:
        """Yield rows from a SELECT in server-side chunks (bounded memory)."""
        result = conn.execute(stmt).yield_per(self._resolved_chunk_size())
        yield from result

    def _fetch_grouped(
        self,
        conn: Connection,
        table: Table,
        key_column: str,
        keys: Iterable[int],
    ) -> dict[int, list[Any]]:
        """Fetch rows whose ``key_column`` is in ``keys``, grouped by key.

        Used to batch-load sidecar tables (addresses, social_accounts, etc.)
        for a chunk of primary entities at a time. The chunked pattern keeps
        memory bounded regardless of total source size.
        """
        keys_list = list(keys)
        if not keys_list:
            return {}
        col = table.c[key_column]
        stmt = select(table).where(col.in_(keys_list))
        grouped: dict[int, list[Any]] = {}
        target = self._sidecar_conn or conn
        for row in target.execute(stmt):
            grouped.setdefault(row._mapping[key_column], []).append(row)
        return grouped

    def _fetch_scalar_map(
        self,
        conn: Connection,
        table: Table,
        key_column: str,
        value_column: str,
        keys: Iterable[int],
    ) -> dict[int, Any]:
        """Fetch a {key: value} map for ``keys`` from ``table``."""
        keys_list = list(keys)
        if not keys_list:
            return {}
        col = table.c[key_column]
        val = table.c[value_column]
        stmt = select(col, val).where(col.in_(keys_list))
        target = self._sidecar_conn or conn
        return {row[0]: row[1] for row in target.execute(stmt)}

    @staticmethod
    def _chunked(it: Iterable[Any], size: int) -> Iterator[list[Any]]:
        chunk: list[Any] = []
        for item in it:
            chunk.append(item)
            if len(chunk) >= size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk
