"""Neo4j repository for direct, on-demand profile analysis."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import NoReturn, Protocol

from neo4j import ManagedTransaction, Record

from src import profile_analysis_runtime_queries as queries
from src.profile_analysis_client import Neo4jClient
from src.profile_analysis_mapping import (
    GraphRow,
    build_profile_analysis_snapshot,
    optional_bool,
    required_bool,
    required_int,
    required_str,
)
from src.profile_analysis_mapping import (
    ProfileAnalysisMappingError as ProfileAnalysisMappingError,
)
from src.profile_analysis_models import (
    ProfileAnalysisAttempt,
    ProfileAnalysisStatus,
    ProfileAnalysisType,
)
from src.profile_analysis_snapshot import KnownSensitiveValue, ProfileAnalysisSnapshot

type SensitiveGraphValue = (
    str
    | int
    | float
    | Decimal
    | bool
    | None
    | list[SensitiveGraphValue]
    | dict[str, SensitiveGraphValue]
)
type SensitiveGraphRow = Record | Mapping[str, SensitiveGraphValue]


@dataclass(frozen=True, slots=True)
class DueProfileAnalysis:
    analysis_type: ProfileAnalysisType
    attempt_number: int

    def __post_init__(self) -> None:
        if self.attempt_number < 1:
            raise ValueError("attempt_number must be positive")


@dataclass(frozen=True, slots=True)
class ClaimedProfileAnalysisPerson:
    person_id: str
    input_revision: int
    due: tuple[DueProfileAnalysis, ...]


@dataclass(frozen=True, slots=True)
class ProfileAnalysisSnapshotBundle:
    snapshot: ProfileAnalysisSnapshot
    known_sensitive_values: tuple[KnownSensitiveValue, ...]


@dataclass(frozen=True, slots=True)
class ProfileAnalysisPersistenceResult:
    status: ProfileAnalysisStatus
    published: bool


class ProfileAnalysisRepository(Protocol):
    """Storage boundary used by direct, on-demand profile analysis."""

    def fetch_snapshot(self, person_id: str) -> ProfileAnalysisSnapshotBundle: ...

    def persist_attempt(
        self,
        attempt: ProfileAnalysisAttempt,
        *,
        claim_token: str,
    ) -> ProfileAnalysisPersistenceResult: ...

    def release_claim(self, *, person_id: str, claim_token: str) -> bool: ...

    def renew_claim(
        self,
        *,
        person_id: str,
        input_revision: int,
        claim_token: str,
        claim_until: datetime,
    ) -> bool: ...

    def claim_request(
        self,
        *,
        request_id: str,
        claim_token: str,
        now: datetime,
        claim_until: datetime,
    ) -> ClaimedProfileAnalysisPerson | None: ...

    def complete_request(
        self,
        *,
        request_id: str,
        claim_token: str,
        status: str,
    ) -> None: ...

    def obsolete_inactive_request(self, *, request_id: str) -> None: ...

    def request_is_waiting(self, *, request_id: str) -> bool: ...


def map_claimed_profile_analysis_people(
    rows: Iterable[GraphRow],
) -> tuple[ClaimedProfileAnalysisPerson, ...]:
    """Map candidate rows without allowing driver-dynamic values downstream."""
    claimed: list[ClaimedProfileAnalysisPerson] = []
    try:
        for row in rows:
            due: list[DueProfileAnalysis] = []
            if required_bool(row, "sales_due"):
                due.append(
                    DueProfileAnalysis(
                        ProfileAnalysisType.SALES,
                        required_int(row, "sales_attempt_number"),
                    )
                )
            if required_bool(row, "contact_due"):
                due.append(
                    DueProfileAnalysis(
                        ProfileAnalysisType.CONTACT_TRACING,
                        required_int(row, "contact_attempt_number"),
                    )
                )
            claimed.append(
                ClaimedProfileAnalysisPerson(
                    person_id=required_str(row, "person_id"),
                    input_revision=required_int(row, "input_revision"),
                    due=tuple(due),
                )
            )
    except (TypeError, ValueError):
        raise ProfileAnalysisMappingError("invalid profile analysis claim data") from None
    return tuple(claimed)


def map_profile_analysis_snapshot_rows(
    person_id: str,
    rows: Iterable[GraphRow],
) -> ProfileAnalysisSnapshotBundle:
    """Map rows and keep repository-only values outside the prompt snapshot."""
    return ProfileAnalysisSnapshotBundle(
        snapshot=build_profile_analysis_snapshot(person_id, rows),
        known_sensitive_values=(),
    )


class Neo4jProfileAnalysisRepository:
    """Concrete short-transaction Neo4j implementation."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def fetch_snapshot(self, person_id: str) -> ProfileAnalysisSnapshotBundle:
        def work(tx: ManagedTransaction) -> ProfileAnalysisSnapshotBundle:
            rows = tx.run(queries.FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS, person_id=person_id)
            mapped = map_profile_analysis_snapshot_rows(person_id, rows)
            sensitive_record = tx.run(
                queries.FETCH_PROFILE_ANALYSIS_SENSITIVE_VALUES,
                person_id=person_id,
            ).single()
            sensitive = map_known_sensitive_values(sensitive_record)
            return ProfileAnalysisSnapshotBundle(mapped.snapshot, sensitive)

        return self._client.execute_read(work)

    def persist_attempt(
        self,
        attempt: ProfileAnalysisAttempt,
        *,
        claim_token: str,
    ) -> ProfileAnalysisPersistenceResult:
        query = (
            queries.PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS
            if attempt.status is ProfileAnalysisStatus.SUCCEEDED
            else queries.PERSIST_PROFILE_ANALYSIS_ATTEMPT
        )

        def work(tx: ManagedTransaction) -> ProfileAnalysisPersistenceResult:
            record = tx.run(
                query,
                claim_token=claim_token,
                **attempt.to_cypher_parameters(),
            ).single()
            if record is None:
                raise RuntimeError("profile analysis Person was not found")
            status = ProfileAnalysisStatus(required_str(record, "status"))
            published = optional_bool(record, "publishable") or False
            return ProfileAnalysisPersistenceResult(status=status, published=published)

        return self._client.execute_write(work)

    def release_claim(self, *, person_id: str, claim_token: str) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                queries.RELEASE_PROFILE_ANALYSIS_CLAIM,
                person_id=person_id,
                claim_token=claim_token,
            ).single()
            return record is not None and required_bool(record, "released")

        return self._client.execute_write(work)

    def renew_claim(
        self,
        *,
        person_id: str,
        input_revision: int,
        claim_token: str,
        claim_until: datetime,
    ) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                queries.RENEW_PROFILE_ANALYSIS_CLAIM,
                person_id=person_id,
                input_revision=input_revision,
                claim_token=claim_token,
                claim_until=claim_until,
            ).single()
            return record is not None and required_bool(record, "renewed")

        return self._client.execute_write(work)

    def claim_request(
        self,
        *,
        request_id: str,
        claim_token: str,
        now: datetime,
        claim_until: datetime,
    ) -> ClaimedProfileAnalysisPerson | None:
        def work(tx: ManagedTransaction) -> ClaimedProfileAnalysisPerson | None:
            record = tx.run(
                queries.CLAIM_PROFILE_ANALYSIS_REQUEST,
                request_id=request_id,
                claim_token=claim_token,
                now=now,
                claim_until=claim_until,
            ).single()
            if record is None:
                return None
            people = map_claimed_profile_analysis_people((record,))
            if len(people) != 1 or len(people[0].due) != 1:
                raise ProfileAnalysisMappingError("invalid profile analysis request claim")
            return people[0]

        return self._client.execute_write(work)

    def complete_request(
        self,
        *,
        request_id: str,
        claim_token: str,
        status: str,
    ) -> None:
        if status not in {"succeeded", "failed", "obsolete"}:
            raise ValueError("invalid profile analysis request terminal status")

        def work(tx: ManagedTransaction) -> None:
            tx.run(
                queries.COMPLETE_PROFILE_ANALYSIS_REQUEST,
                request_id=request_id,
                claim_token=claim_token,
                status=status,
            ).consume()

        self._client.execute_write(work)

    def obsolete_inactive_request(self, *, request_id: str) -> None:
        def work(tx: ManagedTransaction) -> None:
            tx.run(
                queries.OBSOLETE_INACTIVE_PROFILE_ANALYSIS_REQUEST,
                request_id=request_id,
            ).consume()

        self._client.execute_write(work)

    def request_is_waiting(self, *, request_id: str) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                queries.PROFILE_ANALYSIS_REQUEST_WAITING,
                request_id=request_id,
            ).single()
            return record is not None and required_bool(record, "waiting")

        return self._client.execute_read(work)


def map_known_sensitive_values(
    record: SensitiveGraphRow | None,
) -> tuple[KnownSensitiveValue, ...]:
    """Fail closed while mapping repository-only direct values."""
    if record is None:
        _raise_sensitive_mapping_error()
    dynamic_values = record.get("known_sensitive_values")
    if not isinstance(dynamic_values, list):
        _raise_sensitive_mapping_error()
    values: list[KnownSensitiveValue] = []
    for value in dynamic_values:
        if isinstance(value, bool) or value is None:
            _raise_sensitive_mapping_error()
        if not isinstance(value, (str, int, float, Decimal)):
            _raise_sensitive_mapping_error()
        if isinstance(value, float) and not math.isfinite(value):
            _raise_sensitive_mapping_error()
        if isinstance(value, Decimal) and not value.is_finite():
            _raise_sensitive_mapping_error()
        values.append(value)
    return tuple(values)


def _raise_sensitive_mapping_error() -> NoReturn:
    raise ProfileAnalysisMappingError("invalid profile analysis sensitive value data")
