"""Repository-mediated access to accepted CRM stage evidence (issue #125).

The dataset builder never queries the live graph ad hoc: everything flows
through this repository, which reads only the persisted, enabled
``CrmStageAnalyticalRelease`` and its active projections — the same fail-closed
contract as the #149 Gate 1 runner, including keyset pagination, deal-version
deduplication with change detection, and a release-stability recheck after the
paged read completes.
"""

from __future__ import annotations

from typing import Protocol

from src.graph.client import Neo4jClient
from src.graph.queries.sales_prediction import (
    SALES_PREDICTION_DEAL_VERSIONS_FOR_PARENTS,
    SALES_PREDICTION_RELEASE,
    SALES_PREDICTION_STAGE_EVENTS_PAGE,
)
from src.sales_prediction.evidence import parse_deal_rows, parse_stage_rows, parse_timestamp
from src.sales_prediction.models import DealVersion, ReleaseSnapshot, SalesEvidence, StageEvent

type SalesScalar = str | int | float | bool | tuple[str, ...] | None
type SalesRow = dict[str, SalesScalar]
type SalesParameter = str | int | list[dict[str, str]] | None

_PAGE_SIZE = 2_000


class SalesEvidenceRepository(Protocol):
    """Point-in-time evidence access against the accepted release."""

    def load_evidence(
        self,
        *,
        expected_mapping_version: str,
        expected_policy_version: str,
    ) -> SalesEvidence:
        """Read all stage events and deal versions from the accepted release."""
        ...


class Neo4jSalesEvidenceRepository:
    """Sync Neo4j implementation of the sales evidence repository."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def load_evidence(
        self,
        *,
        expected_mapping_version: str,
        expected_policy_version: str,
    ) -> SalesEvidence:
        """Read the accepted release and its full evidence, fail-closed."""
        release = _parse_release(
            self._query_rows(SALES_PREDICTION_RELEASE),
            expected_mapping_version,
            expected_policy_version,
        )
        stage_rows, deal_rows = self._paged_evidence()
        final_release = _parse_release(
            self._query_rows(SALES_PREDICTION_RELEASE),
            expected_mapping_version,
            expected_policy_version,
        )
        if final_release != release:
            raise ValueError("accepted CRM stage release changed during sales dataset read")
        events, invalid_parents = parse_stage_rows(stage_rows)
        versions = parse_deal_rows(deal_rows)
        return SalesEvidence(
            release=release,
            events=tuple(events),
            versions=tuple(versions),
            invalid_event_parents=invalid_parents,
        )

    def _paged_evidence(self) -> tuple[list[SalesRow], list[SalesRow]]:
        stage_rows: list[SalesRow] = []
        deal_rows_by_version: dict[str, SalesRow] = {}
        after_event_identity: str | None = None
        while True:
            page = self._query_rows(
                SALES_PREDICTION_STAGE_EVENTS_PAGE,
                {"after_event_identity": after_event_identity, "limit": _PAGE_SIZE},
            )
            if not page:
                break
            stage_rows.extend(page)
            parents = _page_parents(page)
            for row in self._query_rows(
                SALES_PREDICTION_DEAL_VERSIONS_FOR_PARENTS, {"parents": parents}
            ):
                version_key = _required_text(row, "version_key")
                previous = deal_rows_by_version.get(version_key)
                if previous is not None and previous != row:
                    raise ValueError("deal version changed during sales dataset read")
                deal_rows_by_version[version_key] = row
            next_cursor = _required_text(page[-1], "event_identity")
            if after_event_identity is not None and next_cursor <= after_event_identity:
                raise ValueError("sales dataset stage pagination did not advance")
            after_event_identity = next_cursor
            if len(page) < _PAGE_SIZE:
                break
        return stage_rows, list(deal_rows_by_version.values())

    def _query_rows(
        self, query: str, parameters: dict[str, SalesParameter] | None = None
    ) -> list[SalesRow]:
        with self._client.session() as session:
            result = session.run(query, parameters or {})
            rows: list[SalesRow] = []
            for record in result:
                row: SalesRow = {}
                for key, value in zip(record.keys(), record.values(), strict=True):
                    row[str(key)] = _to_scalar(value)
                rows.append(row)
            return rows


def _page_parents(page: list[SalesRow]) -> list[dict[str, str]]:
    parents = {
        (
            _required_text(row, "parent_source_system"),
            _required_text(row, "parent_source_record_id"),
        )
        for row in page
    }
    return [
        {"source_system": source_system, "source_record_id": source_record_id}
        for source_system, source_record_id in sorted(parents)
    ]


def _required_text(row: SalesRow, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"sales dataset query row has invalid {key}")
    return value


def _to_scalar(value: object) -> SalesScalar:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(str(item) for item in value)
    raise ValueError(f"sales dataset query returned a non-scalar value: {type(value).__name__}")


def _parse_release(
    rows: list[SalesRow], expected_mapping: str, expected_policy: str
) -> ReleaseSnapshot:
    if len(rows) != 1:
        raise ValueError("accepted CRM stage release query returned an invalid row count")
    row = rows[0]
    mapping = row.get("mapping_version")
    policy = row.get("policy_version")
    accepted = parse_timestamp(row.get("accepted_at"))
    max_event = parse_timestamp(row.get("max_event_at"))
    max_available = parse_timestamp(row.get("max_available_at"))
    if mapping != expected_mapping or policy != expected_policy:
        raise ValueError("accepted CRM stage release version does not match sales dataset inputs")
    if accepted is None or max_event is None or max_available is None:
        raise ValueError("accepted CRM stage release has no valid evidence cutoff")
    projection_count = _integer(row, "projection_count")
    source_complete = (
        bool(row.get("enabled"))
        and row.get("boundary_bound") is True
        and row.get("reconciliation_bound") is True
        and row.get("mapping_bound") is True
    )
    release_consistent = (
        bool(row.get("enabled"))
        and projection_count > 0
        and projection_count == _integer(row, "distinct_projection_count")
        and _integer(row, "invalid_projection_timestamp_count") == 0
        and _integer(row, "wrong_mapping_count") == 0
        and _integer(row, "wrong_policy_count") == 0
    )
    return ReleaseSnapshot(
        enabled=bool(row.get("enabled")),
        mapping_version=expected_mapping,
        policy_version=expected_policy,
        accepted_at=accepted,
        evidence_cutoff_at=min(accepted, max_event, max_available),
        source_accounting_complete=source_complete,
        analytical_release_consistent=release_consistent,
        restated_event_count=_integer(row, "restated_event_count"),
    )


def _integer(row: SalesRow, key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"sales dataset release row has invalid {key}")
    return value


def evidence_summary(
    events: tuple[StageEvent, ...], versions: tuple[DealVersion, ...]
) -> dict[str, int]:
    """Aggregate-only evidence counts safe to render outside restricted storage."""
    return {
        "stage_event_count": len(events),
        "deal_version_count": len(versions),
        "parent_count": len({event.parent_key for event in events}),
    }
