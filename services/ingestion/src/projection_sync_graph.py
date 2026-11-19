"""Neo4j reads for the read projection (#491).

Every read here is read-only. The projection never writes to Neo4j; the only
writes in the spike go to the Postgres projection.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from neo4j import ManagedTransaction, Record

from src.graph.client import Neo4jClient
from src.graph.queries.projection_sync import (
    PROJECTION_ALL_PERSON_IDS,
    PROJECTION_CHANGED_PERSON_IDS,
    PROJECTION_EXISTING_PERSON_IDS,
    PROJECTION_IDENTITY_LINK_REVISIONS,
    PROJECTION_IDENTITY_LINK_STATE,
    PROJECTION_NEIGHBOUR_PERSON_IDS,
    PROJECTION_PERSON_ADDRESSES,
    PROJECTION_PERSON_CONNECTIONS,
    PROJECTION_PERSON_COUNT,
    PROJECTION_PERSON_DETAIL_ROWS,
    PROJECTION_PERSON_IDENTIFIERS,
    PROJECTION_PERSON_LIST_ROWS,
    PROJECTION_PERSON_SOURCE_RECORDS,
    PROJECTION_SUMMARY_COUNTERS,
)

logger = logging.getLogger(__name__)


def record_to_row(record: Record) -> dict[str, object]:
    """Return a Neo4j record as a plain row mapping."""
    return {key: record[key] for key in record.keys()}


class ProjectionGraph:
    """Bounded Neo4j reads backing one projection run."""

    def __init__(self, client: Neo4jClient) -> None:
        """Bind the graph reader to a Neo4j client."""
        self._client = client

    def _rows(self, query: str, params: dict[str, object]) -> list[dict[str, object]]:
        """Run a read query and return its rows as plain mappings."""

        def _work(tx: ManagedTransaction) -> list[dict[str, object]]:
            result = tx.run(query, params)
            return [record_to_row(record) for record in result]

        return self._client.execute_read(_work)

    def all_person_ids(self, after_person_id: str, page_size: int) -> list[str]:
        """Return a keyset page of every person id."""
        rows = self._rows(
            PROJECTION_ALL_PERSON_IDS,
            {"after_person_id": after_person_id, "page_size": page_size},
        )
        return [str(row["person_id"]) for row in rows]

    def changed_person_ids(
        self,
        mark: str,
        after_person_id: str,
        page_size: int,
    ) -> list[str]:
        """Return a keyset page of person ids changed since the mark."""
        rows = self._rows(
            PROJECTION_CHANGED_PERSON_IDS,
            {
                "mark": mark,
                "after_person_id": after_person_id,
                "page_size": page_size,
            },
        )
        return [str(row["person_id"]) for row in rows]

    def neighbour_person_ids(self, person_ids: Sequence[str]) -> list[str]:
        """Return the one-hop neighbours of the given persons."""
        if not person_ids:
            return []
        rows = self._rows(PROJECTION_NEIGHBOUR_PERSON_IDS, {"person_ids": list(person_ids)})
        return [str(row["person_id"]) for row in rows]

    def person_list_rows(self, person_ids: Sequence[str]) -> list[dict[str, object]]:
        """Return the list-view rows of the given persons."""
        return self._rows(PROJECTION_PERSON_LIST_ROWS, {"person_ids": list(person_ids)})

    def person_detail_rows(self, person_ids: Sequence[str]) -> list[dict[str, object]]:
        """Return the detail-view rows of the given persons."""
        return self._rows(PROJECTION_PERSON_DETAIL_ROWS, {"person_ids": list(person_ids)})

    def person_identifier_rows(self, person_ids: Sequence[str]) -> list[dict[str, object]]:
        """Return the identifier rows of the given persons."""
        return self._rows(PROJECTION_PERSON_IDENTIFIERS, {"person_ids": list(person_ids)})

    def person_address_rows(self, person_ids: Sequence[str]) -> list[dict[str, object]]:
        """Return the address rows of the given persons."""
        return self._rows(PROJECTION_PERSON_ADDRESSES, {"person_ids": list(person_ids)})

    def person_connection_rows(self, person_ids: Sequence[str]) -> list[dict[str, object]]:
        """Return the connection rows of the given persons."""
        return self._rows(PROJECTION_PERSON_CONNECTIONS, {"person_ids": list(person_ids)})

    def person_source_record_rows(self, person_ids: Sequence[str]) -> list[dict[str, object]]:
        """Return the source-record rows of the given persons."""
        return self._rows(PROJECTION_PERSON_SOURCE_RECORDS, {"person_ids": list(person_ids)})

    def person_count(self) -> int:
        """Return the number of persons in the graph."""
        rows = self._rows(PROJECTION_PERSON_COUNT, {})
        return int(str(rows[0]["person_count"])) if rows else 0

    def existing_person_ids(self, person_ids: Sequence[str]) -> list[str]:
        """Return the subset of the given person ids that still exists."""
        if not person_ids:
            return []
        rows = self._rows(PROJECTION_EXISTING_PERSON_IDS, {"person_ids": list(person_ids)})
        return [str(row["person_id"]) for row in rows]

    def summary_counters(self, bound: str) -> dict[str, object]:
        """Return the six person-list summary counters."""
        rows = self._rows(PROJECTION_SUMMARY_COUNTERS, {"bound": bound})
        if not rows:
            return {}
        return rows[0]

    def identity_link_state(self) -> dict[str, object]:
        """Return the read-only identity-link counter and migration state."""
        rows = self._rows(PROJECTION_IDENTITY_LINK_STATE, {})
        if not rows:
            return {}
        return rows[0]

    def identity_link_revisions(
        self,
        after_revision: int,
        page_size: int,
    ) -> list[dict[str, object]]:
        """Return a page of identity-link revisions after the given revision."""
        return self._rows(
            PROJECTION_IDENTITY_LINK_REVISIONS,
            {"after_revision": after_revision, "page_size": page_size},
        )
