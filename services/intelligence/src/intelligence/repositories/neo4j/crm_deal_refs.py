"""Parameterised, read-only Neo4j implementation for CRM deal-reference export."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from os import environ
from typing import cast

from neo4j import Driver as Neo4jDriver
from neo4j import GraphDatabase, Record

from intelligence.graph.queries.crm_deal_refs import (
    LIST_DEAL_REFERENCE_PAGE,
    LIST_IDENTITY_REVISION_PAGE,
    READ_IDENTITY_BOUNDARY,
    VALIDATE_SOURCE_INSTANCE,
)
from intelligence.repositories.protocols.crm_deal_refs import (
    CrmDealRefsRepository,
    DealReferenceRow,
    IdentityBoundary,
    IdentityRevisionRow,
)

_IDENTITY_ID_BATCH_SIZE = 500


class Neo4jCrmDealRefsRepository(CrmDealRefsRepository):
    """A small sync driver wrapper whose queries have no write clauses."""

    def __init__(self, driver: Neo4jDriver, database: str) -> None:
        self._driver = driver
        self._database = database

    @classmethod
    def from_environment(cls) -> Neo4jCrmDealRefsRepository:
        """Read credentials only from the process environment, never from command arguments."""
        uri = _required_environment("INTELLIGENCE_NEO4J_URI")
        user = _required_environment("INTELLIGENCE_NEO4J_USER")
        password = _required_environment("INTELLIGENCE_NEO4J_PASSWORD")
        database = _required_environment("INTELLIGENCE_NEO4J_DATABASE")
        return cls(GraphDatabase.driver(uri, auth=(user, password)), database)

    def close(self) -> None:
        """Close the driver after the bounded child handler completes."""
        self._driver.close()

    def validate_source_instance(self, source_instance_id: str) -> None:
        rows = self._read(VALIDATE_SOURCE_INSTANCE, {"source_instance_id": source_instance_id})
        if len(rows) != 1 or rows[0].get("source_instance_id") != source_instance_id:
            raise ValueError("source instance is not an active Bitrix source")

    def read_identity_boundary(self) -> IdentityBoundary:
        rows = self._read(READ_IDENTITY_BOUNDARY, {})
        if len(rows) != 1:
            raise RuntimeError("identity boundary query returned an invalid result")
        revision = rows[0].get("current_revision")
        baseline_ready = rows[0].get("baseline_ready")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise RuntimeError("identity revision ceiling is invalid")
        if not isinstance(baseline_ready, bool):
            raise RuntimeError("identity baseline readiness is invalid")
        return {"current_revision": revision, "baseline_ready": baseline_ready}

    def iter_deal_reference_pages(
        self,
        source_instance_id: str,
        as_of: str,
        page_size: int,
        max_records: int,
        max_raw_payload_chars: int,
    ) -> Iterator[tuple[DealReferenceRow, ...]]:
        after_id: str | None = None
        after_version: int | None = None
        after_pk: str | None = None
        count = 0
        while True:
            limit = min(page_size, max_records - count + 1)
            page = self._read(
                LIST_DEAL_REFERENCE_PAGE,
                {
                    "source_instance_id": source_instance_id,
                    "as_of": as_of,
                    "after_source_record_id": after_id,
                    "after_source_record_version": after_version,
                    "after_source_record_pk": after_pk,
                    "limit": limit,
                    "max_raw_payload_chars": max_raw_payload_chars,
                },
            )
            typed = tuple(_deal_row(row) for row in page)
            count += len(typed)
            if count > max_records:
                raise RuntimeError("selected deal references exceed max-records")
            if typed:
                yield typed
            if len(typed) < limit:
                return
            final = typed[-1]
            after_id = _text(final["source_record_id"], "source_record_id")
            after_version = _positive_int(final["source_record_version"], "source_record_version")
            after_pk = _text(final["source_record_pk"], "source_record_pk")

    def iter_identity_revision_pages(
        self,
        source_instance_id: str,
        source_entity_ids: tuple[str, ...],
        as_of: str,
        through_revision: int,
        page_size: int,
        max_records: int,
    ) -> Iterator[tuple[IdentityRevisionRow, ...]]:
        if not source_entity_ids:
            return
        count = 0
        for offset in range(0, len(source_entity_ids), _IDENTITY_ID_BATCH_SIZE):
            after: int | None = None
            source_ids = source_entity_ids[offset : offset + _IDENTITY_ID_BATCH_SIZE]
            while True:
                limit = min(page_size, max_records - count + 1)
                page = self._read(
                    LIST_IDENTITY_REVISION_PAGE,
                    {
                        "source_instance_id": source_instance_id,
                        "source_entity_ids": list(source_ids),
                        "identity_policy_version": "crm_deal_identity_v2",
                        "as_of": as_of,
                        "through_revision": through_revision,
                        "after_global_revision": after,
                        "limit": limit,
                    },
                )
                typed = tuple(_identity_row(row) for row in page)
                count += len(typed)
                if count > max_records:
                    raise RuntimeError("selected identity revisions exceed max-records")
                if typed:
                    yield typed
                if len(typed) < limit:
                    break
                after = _positive_int(typed[-1]["global_revision"], "global_revision")

    def _read(self, query: str, parameters: Mapping[str, object]) -> list[dict[str, object]]:
        with self._driver.session(database=self._database, default_access_mode="READ") as session:
            result = session.run(query, dict(parameters))
            return [_record_to_dict(record) for record in result]


def _required_environment(name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} must be configured")
    return value


def _record_to_dict(record: Record) -> dict[str, object]:
    return {key: record[key] for key in record.keys()}


def _deal_row(value: dict[str, object]) -> DealReferenceRow:
    required = {
        "source_record_id",
        "source_record_version",
        "source_record_pk",
        "record_hash",
        "source_entity_type",
        "source_entity_id",
        "identity_policy_version",
        "record_entity_key",
        "owned_entity_keys",
        "owned_entity_count",
        "stage_id",
        "observed_at",
        "ingested_at",
        "lifecycle_status",
        "link_status",
        "raw_payload",
        "raw_payload_oversize",
    }
    if set(value) != required:
        raise RuntimeError("deal-reference query returned an invalid row shape")
    return cast(DealReferenceRow, value)


def _identity_row(value: dict[str, object]) -> IdentityRevisionRow:
    required = {
        "event_id",
        "global_revision",
        "source_instance_id",
        "source_entity_id",
        "identity_policy_version",
        "link_status",
        "hyperp_person_id",
        "person_status",
        "resolution_kind",
        "resolution_revision",
        "effective_at",
        "created_at",
    }
    if set(value) != required:
        raise RuntimeError("identity-revision query returned an invalid row shape")
    return cast(IdentityRevisionRow, value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{field} is invalid")
    return value


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RuntimeError(f"{field} is invalid")
    return value
