"""Orchestrate bounded in-place NRIC rehash passes and aggregate verification."""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field, replace
from typing import Literal, TypedDict

from neo4j import ManagedTransaction, Record

from src.graph import queries
from src.graph.client import Neo4jClient
from src.normalizers.nric import encrypt_nric
from src.nric_rehash_classify import (
    NricIdentifierRow,
    classify_nric_identifier,
    identifier_row_from_record,
)
from src.nric_rehash_plan import (
    NricRehashError,
    PlanReport,
    VerifyReport,
    build_plan_report,
    build_verify_report,
    record_count,
)
from src.nric_rehash_state import (
    MigrationState,
    acquire_lease,
    advance_batch,
    complete_and_release,
    fence_batch,
    key_check_for,
    read_current_revision,
    read_migration_state,
    release_lease,
)


class CountSummary(TypedDict):
    total: int
    non_digest: int
    digest_shaped: int


class FinalCountSummary(TypedDict):
    identifier_count: int
    non_digest_count: int


class ApplyReport(TypedDict):
    command: Literal["apply"]
    status: Literal["ok", "blocked"]
    phase: str
    changes: int
    batches: int
    converted_in_place: int
    converted_consolidated: int
    removed_nodes: int
    already_digest: int
    unprocessable: dict[str, int]
    digest_metadata_gap: int
    baseline: CountSummary
    final: FinalCountSummary
    count_equation_ok: bool
    conversion_equation_ok: bool
    revision_unchanged: bool


@dataclass(frozen=True)
class BatchOutcome:
    cursor: str | None = field(repr=False)
    row_count: int
    batches: int
    converted_in_place: int


@dataclass(frozen=True)
class LockedIdentifier:
    row: NricIdentifierRow
    element_id: str = field(repr=False)


def run_nric_rehash_apply(
    client: Neo4jClient,
    *,
    batch_size: int,
    key: str,
    key_version: str,
    expected_database: str,
    acknowledge_offline: bool,
) -> tuple[ApplyReport, int]:
    """Apply up to two fenced keyset passes and return aggregate-only output."""
    if not acknowledge_offline:
        raise NricRehashError("offline_ack_required")

    state = read_migration_state(client)
    _require_same_key(state, key_version, key_check_for(key))
    if client.execute_read(_read_database_name) != expected_database:
        raise NricRehashError("database_mismatch")
    plan = build_plan_report(client, batch_size, key, key_version)
    _assert_census_preconditions(plan)
    if state is not None and state.phase == "convert_complete" and plan["convertible"] == 0:
        return _final_report(client, state, plan, key_version, changes=0)
    if state is not None and state.phase not in {"convert", "convert_complete"}:
        raise NricRehashError("drift_detected")

    owner = uuid.uuid4().hex
    state = acquire_lease(
        client,
        owner,
        key,
        key_version,
        restart_completed=state is not None and state.phase == "convert_complete",
    )
    return _run_acquired_passes(client, state, owner, batch_size, key, key_version)


def _run_acquired_passes(
    client: Neo4jClient,
    state: MigrationState,
    owner: str,
    batch_size: int,
    key: str,
    key_version: str,
) -> tuple[ApplyReport, int]:
    initial_converted = state.converted_in_place
    lease_active = True
    try:
        for pass_number in range(2):
            state = _run_keyset_pass(client, state, owner, batch_size, key, key_version)
            complete_and_release(client, owner)
            lease_active = False
            state = replace(state, phase="convert_complete", cursor=None)
            plan = build_plan_report(client, batch_size, key, key_version)
            _assert_census_preconditions(plan)
            if plan["convertible"] == 0:
                return _final_report(
                    client,
                    state,
                    plan,
                    key_version,
                    changes=state.converted_in_place - initial_converted,
                )
            if pass_number == 1:
                raise NricRehashError("residual_plaintext")
            state = acquire_lease(
                client,
                owner,
                key,
                key_version,
                restart_completed=True,
            )
            lease_active = True
    finally:
        if lease_active:
            release_lease(client, owner)
    raise NricRehashError("residual_plaintext")


def _require_same_key(
    state: MigrationState | None,
    key_version: str,
    key_check: str,
) -> None:
    if state is not None and (state.key_version != key_version or state.key_check != key_check):
        raise NricRehashError("key_changed")


def _assert_census_preconditions(plan: PlanReport) -> None:
    census = plan["census"]
    if census["missing_ids"]:
        raise NricRehashError("missing_id")
    if census["unexpected_scopes"]:
        raise NricRehashError("unexpected_scope")
    if census["missing_scopes"]:
        raise NricRehashError("null_scope_present")


def _read_database_name(tx: ManagedTransaction) -> str:
    record = tx.run(queries.NRIC_REHASH_DATABASE_NAME).single()
    if record is None:
        raise NricRehashError("neo4j_error")
    name: object = record.get("name")
    if isinstance(name, str):
        return name
    raise NricRehashError("neo4j_error")


def _run_keyset_pass(
    client: Neo4jClient,
    state: MigrationState,
    owner: str,
    batch_size: int,
    key: str,
    key_version: str,
) -> MigrationState:
    while True:
        outcome = _process_batch(client, state, owner, batch_size, key, key_version)
        if outcome.row_count == 0:
            return state
        state = replace(
            state,
            cursor=outcome.cursor,
            batches=outcome.batches,
            converted_in_place=outcome.converted_in_place,
        )
        print(
            json.dumps(
                {
                    "event": "batch",
                    "batches": state.batches,
                    "converted_in_place": state.converted_in_place,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        if outcome.row_count < batch_size:
            return state


def _process_batch(
    client: Neo4jClient,
    state: MigrationState,
    owner: str,
    batch_size: int,
    key: str,
    key_version: str,
) -> BatchOutcome:
    def work(tx: ManagedTransaction) -> BatchOutcome:
        fence_batch(tx, owner)
        rows = tuple(
            _locked_identifier_from_record(record)
            for record in tx.run(
                queries.NRIC_REHASH_PAGE_AND_LOCK_IDENTIFIERS,
                cursor=state.cursor,
                batch_size=batch_size,
            )
        )
        _validate_locked_page(rows, state.cursor)
        if not rows:
            return BatchOutcome(state.cursor, 0, state.batches, state.converted_in_place)
        converted = _convert_rows(tx, rows, key, key_version)
        last_identifier_id = rows[-1].row.identifier_id
        if last_identifier_id is None:
            raise NricRehashError("missing_id")
        batches, total_converted = advance_batch(
            tx,
            owner,
            last_identifier_id,
            converted,
        )
        return BatchOutcome(last_identifier_id, len(rows), batches, total_converted)

    return client.execute_write(work)


def _locked_identifier_from_record(record: Record) -> LockedIdentifier:
    element_id: object = record.get("element_id")
    if not isinstance(element_id, str) or not element_id:
        raise NricRehashError("drift_detected")
    return LockedIdentifier(identifier_row_from_record(record), element_id)


def _validate_locked_page(rows: tuple[LockedIdentifier, ...], cursor: str | None) -> None:
    previous_id = cursor
    for locked in rows:
        row = locked.row
        identifier_id = row.identifier_id
        if identifier_id is None:
            raise NricRehashError("missing_id")
        if previous_id is not None and identifier_id <= previous_id:
            raise NricRehashError("drift_detected")
        if row.identifier_scope is None:
            raise NricRehashError("null_scope_present")
        if row.identifier_scope != "global":
            raise NricRehashError("unexpected_scope")
        previous_id = identifier_id


def _convert_rows(
    tx: ManagedTransaction,
    rows: tuple[LockedIdentifier, ...],
    key: str,
    key_version: str,
) -> int:
    converted = 0
    for locked in rows:
        row = locked.row
        classification = classify_nric_identifier(row, key, key_version)
        if classification.blocker == "missing_id":
            raise NricRehashError("missing_id")
        if classification.blocker == "unexpected_scope":
            raise NricRehashError("unexpected_scope")
        if classification.category != "convertible":
            continue
        canonical = classification.canonical
        digest = classification.digest
        masked = classification.masked
        checksum_valid = classification.checksum_valid
        if canonical is None or digest is None or masked is None or checksum_valid is None:
            raise NricRehashError("drift_detected")
        collision = tx.run(queries.NRIC_REHASH_FIND_DIGEST_TARGET, digest=digest).single()
        if collision is not None:
            raise NricRehashError("collision_unsupported")
        result = tx.run(
            queries.NRIC_REHASH_UPDATE_IDENTIFIER,
            element_id=locked.element_id,
            digest=digest,
            hashed_value=digest,
            key_version=key_version,
            masked=masked,
            checksum_valid=checksum_valid,
            ciphertext=encrypt_nric(canonical, key),
        ).single()
        if result is None or record_count(result, "updated") != 1:
            raise NricRehashError("drift_detected")
        converted += 1
    return converted


def _final_report(
    client: Neo4jClient,
    state: MigrationState,
    plan: PlanReport,
    key_version: str,
    *,
    changes: int,
) -> tuple[ApplyReport, int]:
    verification: VerifyReport = build_verify_report(client, key_version)
    current_revision = read_current_revision(client)
    revision_unchanged = current_revision == state.baseline_revision
    count_equation_ok = (
        verification["identifier_count"] == state.baseline_total - state.removed_nodes
    )
    conversion_equation_ok = state.baseline_non_digest == (
        state.converted_in_place + state.converted_consolidated + verification["non_digest_count"]
    )
    unprocessable = plan["unprocessable"]
    blocked = _verification_is_blocked(
        plan,
        verification,
        count_equation_ok,
        conversion_equation_ok,
        revision_unchanged,
    )
    report = ApplyReport(
        command="apply",
        status="blocked" if blocked else "ok",
        phase=state.phase,
        changes=changes,
        batches=state.batches,
        converted_in_place=state.converted_in_place,
        converted_consolidated=state.converted_consolidated,
        removed_nodes=state.removed_nodes,
        already_digest=plan["already_digest"],
        unprocessable=dict(unprocessable),
        digest_metadata_gap=plan["digest_metadata_gap"],
        baseline={
            "total": state.baseline_total,
            "non_digest": state.baseline_non_digest,
            "digest_shaped": state.baseline_digest_shaped,
        },
        final={
            "identifier_count": verification["identifier_count"],
            "non_digest_count": verification["non_digest_count"],
        },
        count_equation_ok=count_equation_ok,
        conversion_equation_ok=conversion_equation_ok,
        revision_unchanged=revision_unchanged,
    )
    return report, 1 if blocked else 0


def _verification_is_blocked(
    plan: PlanReport,
    verification: VerifyReport,
    count_equation_ok: bool,
    conversion_equation_ok: bool,
    revision_unchanged: bool,
) -> bool:
    return (
        plan["convertible"] > 0
        or sum(plan["unprocessable"].values()) > 0
        or not verification["ok"]
        or not count_equation_ok
        or not conversion_equation_ok
        or not revision_unchanged
    )
