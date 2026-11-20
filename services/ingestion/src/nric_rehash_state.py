"""Durable migration state, lease fencing, and checkpoint transactions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict

from neo4j import ManagedTransaction, Record
from neo4j.exceptions import DriverError, Neo4jError

from src.graph.client import Neo4jClient
from src.graph.queries import nric_rehash_consolidate as consolidate_queries
from src.graph.queries.nric_rehash import (
    NRIC_REHASH_ACQUIRE_LEASE,
    NRIC_REHASH_ADVANCE_BATCH,
    NRIC_REHASH_COMPLETE_AND_RELEASE,
    NRIC_REHASH_FENCE_BATCH,
    NRIC_REHASH_READ_REVISION,
    NRIC_REHASH_READ_STATE,
    NRIC_REHASH_RELEASE_LEASE,
)
from src.normalizers.nric import nric_digest
from src.nric_rehash_plan import NricRehashError, read_census, record_count

MIGRATION_KEY = "nric_hmac_rehash_v1"
REVISION_STREAM_KEY = "identity_link_revision_stream_v1"
KEY_CHECK_VALUE = "nric-rehash-key-check:v1"
LEASE_SECONDS = 300


class EvidenceCounts(TypedDict):
    asserts: int
    identified_by: int
    anchored_by: int
    moved: int
    has_identifier: int
    active_links: int


@dataclass(frozen=True)
class MigrationState:
    phase: str
    cursor: str | None = field(repr=False)
    key_version: str | None
    key_check: str | None = field(repr=False)
    baseline_total: int
    baseline_non_digest: int
    baseline_digest_shaped: int
    baseline_asserts: int
    baseline_identified_by: int
    baseline_anchored_by: int
    baseline_moved: int
    baseline_has_identifier: int
    baseline_active_links: int
    baseline_revision: int | None
    batches: int
    converted_in_place: int
    converted_consolidated: int
    removed_nodes: int


def key_check_for(key: str) -> str:
    """Return the stored non-reversible identity check for the configured key."""
    return nric_digest(KEY_CHECK_VALUE, key)


def read_migration_state(client: Neo4jClient) -> MigrationState | None:
    """Read the durable marker without acquiring its lease or modifying graph state."""
    return client.execute_read(_read_migration_state)


def _read_migration_state(tx: ManagedTransaction) -> MigrationState | None:
    record = tx.run(NRIC_REHASH_READ_STATE, migration_key=MIGRATION_KEY).single()
    if record is None:
        raise NricRehashError("neo4j_error")
    exists: object = record.get("exists")
    if not isinstance(exists, bool):
        raise NricRehashError("neo4j_error")
    return None if not exists else _state_from_record(record)


def _state_from_record(record: Record) -> MigrationState:
    phase = _optional_string(record, "phase")
    if phase is None:
        raise NricRehashError("drift_detected")
    return MigrationState(
        phase=phase,
        cursor=_optional_string(record, "cursor"),
        key_version=_optional_string(record, "key_version"),
        key_check=_optional_string(record, "key_check"),
        baseline_total=record_count(record, "baseline_total"),
        baseline_non_digest=record_count(record, "baseline_non_digest"),
        baseline_digest_shaped=record_count(record, "baseline_digest_shaped"),
        baseline_asserts=_required_baseline_count(record, "baseline_asserts"),
        baseline_identified_by=_required_baseline_count(record, "baseline_identified_by"),
        baseline_anchored_by=_required_baseline_count(record, "baseline_anchored_by"),
        baseline_moved=_required_baseline_count(record, "baseline_moved"),
        baseline_has_identifier=_required_baseline_count(record, "baseline_has_identifier"),
        baseline_active_links=_required_baseline_count(record, "baseline_active_links"),
        baseline_revision=_optional_count(record, "baseline_revision"),
        batches=record_count(record, "batches"),
        converted_in_place=record_count(record, "converted_in_place"),
        converted_consolidated=record_count(record, "converted_consolidated"),
        removed_nodes=record_count(record, "removed_nodes"),
    )


def _optional_string(record: Record, key: str) -> str | None:
    value: object = record.get(key)
    if value is None or isinstance(value, str):
        return value
    raise NricRehashError("drift_detected")


def _required_baseline_count(record: Record, key: str) -> int:
    value: object = record.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise NricRehashError("drift_detected")


def _optional_count(record: Record, key: str) -> int | None:
    value: object = record.get(key)
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise NricRehashError("neo4j_error")


def read_evidence_counts(tx: ManagedTransaction) -> EvidenceCounts:
    """Read evidence counters shared by baseline acquisition and final reporting."""
    record = tx.run(consolidate_queries.NRIC_REHASH_EVIDENCE_COUNTS).single()
    if record is None:
        raise NricRehashError("neo4j_error")
    return EvidenceCounts(
        asserts=record_count(record, "asserts"),
        identified_by=record_count(record, "identified_by"),
        anchored_by=record_count(record, "anchored_by"),
        moved=record_count(record, "moved"),
        has_identifier=record_count(record, "has_identifier"),
        active_links=record_count(record, "active_links"),
    )


def read_current_revision(client: Neo4jClient) -> int | None:
    """Read the existing counter without invoking its MERGE-bearing writer query."""
    return client.execute_read(_read_current_revision)


def _read_current_revision(tx: ManagedTransaction) -> int | None:
    record = tx.run(
        NRIC_REHASH_READ_REVISION,
        stream_key=REVISION_STREAM_KEY,
    ).single()
    if record is None:
        raise NricRehashError("neo4j_error")
    return _optional_count(record, "current_revision")


def acquire_lease(
    client: Neo4jClient,
    owner: str,
    key: str,
    key_version: str,
    *,
    restart_completed: bool,
) -> MigrationState:
    """Create the baseline once and acquire or resume the fenced migration."""

    def work(tx: ManagedTransaction) -> MigrationState:
        census = read_census(tx)
        _assert_census_values(census)
        evidence = read_evidence_counts(tx)
        baseline_revision = _read_current_revision(tx)
        check = key_check_for(key)
        record = tx.run(
            NRIC_REHASH_ACQUIRE_LEASE,
            migration_key=MIGRATION_KEY,
            key_version=key_version,
            key_check=check,
            baseline_total=census["total"],
            baseline_non_digest=census["non_digest"],
            baseline_digest_shaped=census["digest_shaped"],
            baseline_asserts=evidence["asserts"],
            baseline_identified_by=evidence["identified_by"],
            baseline_anchored_by=evidence["anchored_by"],
            baseline_moved=evidence["moved"],
            baseline_has_identifier=evidence["has_identifier"],
            baseline_active_links=evidence["active_links"],
            baseline_revision=baseline_revision,
            owner=owner,
            lease_seconds=LEASE_SECONDS,
            restart_completed=restart_completed,
        ).single()
        if record is None:
            raise NricRehashError("lease_unavailable")
        if record.get("key_matches") is not True:
            raise NricRehashError("key_changed")
        return _state_from_record(record)

    return client.execute_write(work)


def _assert_census_values(census: dict[str, int]) -> None:
    if census["missing_ids"]:
        raise NricRehashError("missing_id")
    if census["unexpected_scopes"]:
        raise NricRehashError("unexpected_scope")
    if census["missing_scopes"]:
        raise NricRehashError("null_scope_present")


def fence_batch(tx: ManagedTransaction, owner: str) -> None:
    """Lock first, then verify and extend the active owner lease in this transaction."""
    record = tx.run(
        NRIC_REHASH_FENCE_BATCH,
        migration_key=MIGRATION_KEY,
        owner=owner,
        lease_seconds=LEASE_SECONDS,
    ).single()
    if record is None:
        raise NricRehashError("lease_lost")


def advance_batch(
    tx: ManagedTransaction,
    owner: str,
    cursor: str,
    converted_in_place: int,
    converted_consolidated: int,
    removed_nodes: int,
) -> tuple[int, int, int, int]:
    """Persist a page cursor and counters under the same transaction fence."""
    record = tx.run(
        NRIC_REHASH_ADVANCE_BATCH,
        migration_key=MIGRATION_KEY,
        owner=owner,
        cursor=cursor,
        converted_in_place=converted_in_place,
        converted_consolidated=converted_consolidated,
        removed_nodes=removed_nodes,
        lease_seconds=LEASE_SECONDS,
    ).single()
    if record is None:
        raise NricRehashError("lease_lost")
    return (
        record_count(record, "batches"),
        record_count(record, "converted_in_place"),
        record_count(record, "converted_consolidated"),
        record_count(record, "removed_nodes"),
    )


def complete_and_release(client: Neo4jClient, owner: str) -> None:
    """Persist the completed phase and release only the current lease owner."""

    def work(tx: ManagedTransaction) -> None:
        record = tx.run(
            NRIC_REHASH_COMPLETE_AND_RELEASE,
            migration_key=MIGRATION_KEY,
            owner=owner,
        ).single()
        if record is None:
            raise NricRehashError("lease_lost")

    client.execute_write(work)


def release_lease(client: Neo4jClient, owner: str) -> None:
    """Best-effort release after failure without touching a replacement owner."""

    def work(tx: ManagedTransaction) -> None:
        record = tx.run(
            NRIC_REHASH_RELEASE_LEASE,
            migration_key=MIGRATION_KEY,
            owner=owner,
        ).single()
        if record is None:
            raise NricRehashError("lease_lost")

    try:
        client.execute_write(work)
    except (NricRehashError, Neo4jError, DriverError):
        return
