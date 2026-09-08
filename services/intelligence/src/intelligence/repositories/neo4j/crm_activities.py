"""Read-session-only Neo4j repository for activity archive source evidence."""

from __future__ import annotations

from collections.abc import Mapping

from neo4j import READ_ACCESS, Driver, GraphDatabase

from intelligence.crm.activities.model_parsing import record_from_mapping
from intelligence.crm.activities.models import ArchivePage, ArchiveRecord, ArchiveRequest
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

    def page(self, request: ArchiveRequest, after_source_record_pk: str) -> ArchivePage:
        return self._read(
            READ_SELECTED_PAGE,
            {
                "source_instance_id": request.source_instance_id,
                "source_key": request.source_key,
                "after_source_record_pk": after_source_record_pk,
                "limit": request.page_size,
            },
        )

    def by_identities(self, request: ArchiveRequest, identities: tuple[str, ...]) -> ArchivePage:
        if not identities:
            return ArchivePage((), 0)
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
        if row is None:
            raise RuntimeError(f"CRM activities {preflight_name} preflight returned no count")
        values: Mapping[str, object] = row.data()
        count = values.get("invalid_count")
        if not isinstance(count, int):
            raise RuntimeError(f"CRM activities {preflight_name} preflight count is invalid")
        if isinstance(count, bool) or count < 0:
            raise RuntimeError(f"CRM activities {preflight_name} preflight count is invalid")
        return count

    def _read(self, query: str, parameters: Mapping[str, object]) -> ArchivePage:
        with self._driver.session(
            database=self._database,
            default_access_mode=READ_ACCESS,
        ) as session:
            result = session.run(query, dict(parameters))
            parsed: list[tuple[ArchiveRecord, int]] = []
            for row in result:
                value = _row_mapping(row.data())
                parsed.append((record_from_mapping(value), _duplicate_delivery_count(value)))
        records = tuple(item[0] for item in parsed)
        if tuple(sorted(records, key=lambda item: item.source_record_pk)) != records:
            raise RuntimeError("archive query returned a non-keyset page")
        return ArchivePage(records, sum(item[1] for item in parsed))


def _row_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    """Copy the Neo4j boundary into concrete object-valued mapping evidence."""
    return {key: item for key, item in value.items()}


def _duplicate_delivery_count(value: Mapping[str, object]) -> int:
    count = value.get("duplicate_delivery_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise RuntimeError("archive query duplicate delivery count is invalid")
    return count
