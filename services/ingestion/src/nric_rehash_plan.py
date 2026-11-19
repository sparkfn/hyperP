"""Aggregate planning and verification helpers for NRIC rehashing."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import partial
from typing import Literal, Protocol, TypedDict, TypeVar

from neo4j import ManagedTransaction, Record

from src.graph import queries
from src.nric_rehash_classify import (
    NricClassification,
    NricIdentifierRow,
    classify_nric_identifier,
    identifier_row_from_record,
)

T = TypeVar("T")


class ReadClient(Protocol):
    """Minimal Neo4j client surface used by plan and verify commands."""

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T: ...


class NricRehashError(Exception):
    """Sanitized domain error identified by a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PlanReport(TypedDict):
    command: Literal["plan"]
    status: Literal["ok", "blocked"]
    census: dict[str, int]
    convertible: int
    already_digest: int
    checksum_invalid_convertible: int
    null_scope: int
    digest_metadata_gap: int
    unprocessable: dict[str, int]
    blockers: list[str]
    pages: int


class VerifyReport(TypedDict):
    identifier_count: int
    non_digest_count: int
    missing_metadata_count: int
    duplicate_key_groups: int
    duplicate_extra_owners: int
    duplicate_scoped_targets: int
    ok: bool


def build_plan_report(
    client: ReadClient,
    batch_size: int,
    key: str,
    key_version: str,
) -> PlanReport:
    """Read and classify the NRIC census, emitting aggregate-safe counters."""
    census = client.execute_read(read_census)
    blockers: set[str] = set()
    if census["missing_ids"]:
        blockers.add("missing_id")
    if census["unexpected_scopes"]:
        blockers.add("unexpected_scope")
    if census["missing_scopes"]:
        blockers.add("null_scope")

    counters = _plan_pages(client, census, batch_size, key, key_version, blockers)
    unprocessable: dict[str, int] = {
        "masked": counters["masked"],
        "empty": counters["empty"],
        "unknown_format": counters["unknown_format"],
    }
    status: Literal["ok", "blocked"] = "blocked" if blockers else "ok"
    return PlanReport(
        command="plan",
        status=status,
        census=census,
        convertible=counters["convertible"],
        already_digest=counters["already_digest"],
        checksum_invalid_convertible=counters["checksum_invalid_convertible"],
        null_scope=census["missing_scopes"],
        digest_metadata_gap=counters["digest_metadata_gap"],
        unprocessable=unprocessable,
        blockers=sorted(blockers),
        pages=counters["pages"],
    )


def _plan_pages(
    client: ReadClient,
    census: dict[str, int],
    batch_size: int,
    key: str,
    key_version: str,
    blockers: set[str],
) -> dict[str, int]:
    counters = {
        "convertible": 0,
        "already_digest": 0,
        "checksum_invalid_convertible": 0,
        "digest_metadata_gap": 0,
        "masked": 0,
        "empty": 0,
        "unknown_format": 0,
        "pages": 0,
    }
    if census["missing_ids"] or census["unexpected_scopes"]:
        return counters

    after_id: str | None = None
    while True:
        rows = client.execute_read(
            partial(read_identifier_page, after_id=after_id, batch_size=batch_size)
        )
        counters["pages"] += 1
        _validate_page_order(rows, after_id)
        for row in rows:
            classified = classify_nric_identifier(row, key, key_version)
            _accumulate_classification(classified, counters, blockers)
        if not rows or len(rows) < batch_size:
            return counters
        after_id = rows[-1].identifier_id


def _accumulate_classification(
    classified: NricClassification,
    counters: dict[str, int],
    blockers: set[str],
) -> None:
    if classified.blocker is not None:
        blockers.add(classified.blocker)
    elif classified.category == "convertible":
        counters["convertible"] += 1
        if classified.checksum_valid is False:
            counters["checksum_invalid_convertible"] += 1
    elif classified.category == "already_digest":
        counters["already_digest"] += 1
        if classified.digest_metadata_gap:
            counters["digest_metadata_gap"] += 1
    elif classified.reason is not None:
        counters[classified.reason] += 1
        blockers.add(classified.reason)


def _validate_page_order(rows: Sequence[NricIdentifierRow], after_id: str | None) -> None:
    previous_id = after_id
    for row in rows:
        identifier_id = row.identifier_id
        if identifier_id is None or (previous_id is not None and identifier_id <= previous_id):
            raise NricRehashError("paging_violation")
        previous_id = identifier_id


def read_identifier_page(
    tx: ManagedTransaction,
    after_id: str | None,
    batch_size: int,
) -> tuple[NricIdentifierRow, ...]:
    """Read one keyset page and validate each raw record at the boundary."""
    return tuple(
        identifier_row_from_record(record)
        for record in tx.run(
            queries.NRIC_IDENTIFIER_PAGE,
            after_id=after_id,
            batch_size=batch_size,
        )
    )


def read_census(tx: ManagedTransaction) -> dict[str, int]:
    """Read aggregate NRIC counts without modifying graph state."""
    record = tx.run(queries.NRIC_IDENTIFIER_CENSUS).single()
    if record is None:
        raise NricRehashError("neo4j_error")
    return {
        "total": record_count(record, "total"),
        "digest_shaped": record_count(record, "digest_shaped"),
        "non_digest": record_count(record, "non_digest"),
        "missing_ids": record_count(record, "missing_ids"),
        "missing_scopes": record_count(record, "missing_scopes"),
        "unexpected_scopes": record_count(record, "unexpected_scopes"),
    }


def record_count(record: Record, key: str) -> int:
    """Read a required aggregate count or raise a sanitized result-shape error."""
    value: object = record.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise NricRehashError("neo4j_error")


def build_verify_report(client: ReadClient, key_version: str) -> VerifyReport:
    """Run aggregate-only graph invariant queries without exposing key data."""
    return client.execute_read(_verify_reader(key_version))


def _verify_reader(key_version: str) -> Callable[[ManagedTransaction], VerifyReport]:
    def read_verification(tx: ManagedTransaction) -> VerifyReport:
        identifiers = tx.run(
            queries.NRIC_VERIFY_IDENTIFIERS,
            key_version=key_version,
        ).single()
        duplicate_keys = tx.run(queries.NRIC_VERIFY_DUPLICATE_PERSON_KEYS).single()
        duplicate_targets = tx.run(queries.NRIC_VERIFY_DUPLICATE_SCOPED_TARGETS).single()
        if identifiers is None or duplicate_keys is None or duplicate_targets is None:
            raise NricRehashError("neo4j_error")
        counts: tuple[int, int, int, int, int, int] = (
            record_count(identifiers, "identifier_count"),
            record_count(identifiers, "non_digest_count"),
            record_count(identifiers, "missing_metadata_count"),
            record_count(duplicate_keys, "duplicate_key_groups"),
            record_count(duplicate_keys, "duplicate_extra_owners"),
            record_count(duplicate_targets, "duplicate_scoped_targets"),
        )
        return VerifyReport(
            identifier_count=counts[0],
            non_digest_count=counts[1],
            missing_metadata_count=counts[2],
            duplicate_key_groups=counts[3],
            duplicate_extra_owners=counts[4],
            duplicate_scoped_targets=counts[5],
            ok=not any(counts[1:]),
        )

    return read_verification
