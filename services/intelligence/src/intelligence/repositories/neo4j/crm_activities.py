"""Read-session-only Neo4j repository for activity archive source evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from neo4j import READ_ACCESS, Driver, GraphDatabase

from intelligence.crm.activities.model_parsing import record_from_mapping
from intelligence.crm.activities.models import ArchiveRecord, ArchiveRequest
from intelligence.graph.queries.crm_activities import (
    PREFLIGHT_REFERENCE_FANOUT,
    PREFLIGHT_STRUCTURAL_INVALID,
    READ_BY_IDENTITIES,
    READ_SELECTED_PAGE,
)


class Neo4jCrmActivitiesRepository:
    """A deliberately narrow driver wrapper: only parameterized read sessions exist."""

    def __init__(self, uri: str, user: str, password: str, database: str | None = None) -> None:
        if not uri or not user or not password:
            raise ValueError("Neo4j archive configuration is incomplete")
        self._driver: Driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database or None

    def close(self) -> None:
        self._driver.close()

    def page(
        self, request: ArchiveRequest, after_source_record_pk: str
    ) -> tuple[ArchiveRecord, ...]:
        return self._read(
            READ_SELECTED_PAGE,
            {
                "source_instance_id": request.source_instance_id,
                "source_key": request.source_key,
                "after_source_record_pk": after_source_record_pk,
                "limit": request.page_size,
            },
        )

    def by_identities(
        self, request: ArchiveRequest, identities: tuple[str, ...]
    ) -> tuple[ArchiveRecord, ...]:
        if not identities:
            return ()
        if len(identities) > request.page_size:
            raise ValueError("identity verification page exceeds the configured page size")
        return self._read(
            READ_BY_IDENTITIES,
            {
                "source_instance_id": request.source_instance_id,
                "source_key": request.source_key,
                "source_record_pks": list(identities),
            },
        )

    def structural_invalid_count(self, request: ArchiveRequest) -> int:
        return self._preflight_invalid_count(
            PREFLIGHT_STRUCTURAL_INVALID,
            {
                "source_instance_id": request.source_instance_id,
                "source_key": request.source_key,
            },
            "structural",
        )

    def reference_fanout_invalid_count(self, request: ArchiveRequest) -> int:
        return self._preflight_invalid_count(
            PREFLIGHT_REFERENCE_FANOUT,
            {
                "source_instance_id": request.source_instance_id,
                "source_key": request.source_key,
                "max_references_per_record": request.max_references_per_record,
            },
            "reference fan-out",
        )

    def _preflight_invalid_count(
        self,
        query: str,
        parameters: Mapping[str, object],
        preflight_name: str,
    ) -> int:
        with self._driver.session(
            database=self._database,
            default_access_mode=READ_ACCESS,
        ) as session:
            row = session.run(query, dict(parameters)).single()
        if row is None or not isinstance(row.get("invalid_count"), int):
            raise RuntimeError(f"CRM activities {preflight_name} preflight returned no count")
        count = row.get("invalid_count")
        if isinstance(count, bool) or count < 0:
            raise RuntimeError(f"CRM activities {preflight_name} preflight count is invalid")
        return count

    def _read(self, query: str, parameters: Mapping[str, object]) -> tuple[ArchiveRecord, ...]:
        with self._driver.session(
            database=self._database,
            default_access_mode=READ_ACCESS,
        ) as session:
            result = session.run(query, dict(parameters))
            rows = tuple(
                record_from_mapping(cast(Mapping[str, object], row.data())) for row in result
            )
        if tuple(sorted(rows, key=lambda item: item.source_record_pk)) != rows:
            raise RuntimeError("archive query returned a non-keyset page")
        return rows
