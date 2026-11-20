"""Orchestrate bounded in-place NRIC rehash passes and aggregate verification."""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field, replace

from neo4j import ManagedTransaction, Record

from src.graph import queries
from src.graph.client import Neo4jClient
from src.normalizers.nric import encrypt_nric
from src.nric_rehash_classify import (
    NricIdentifierRow,
    classify_nric_identifier,
    identifier_row_from_record,
)
from src.nric_rehash_consolidate import consolidate_identifier
from src.nric_rehash_plan import (
    NricRehashError,
    PlanReport,
    build_plan_report,
    record_count,
)
from src.nric_rehash_report import ApplyReport, build_final_report
from src.nric_rehash_state import (
    MigrationState,
    acquire_lease,
    advance_batch,
    complete_and_release,
    fence_batch,
    key_check_for,
    read_migration_state,
    release_lease,
)


@dataclass(frozen=True)
class BatchOutcome:
    cursor: str | None = field(repr=False)
    row_count: int
    batches: int
    converted_in_place: int
    converted_consolidated: int
    removed_nodes: int


@dataclass(frozen=True)
class ConversionOutcome:
    converted_in_place: int
    converted_consolidated: int
    removed_nodes: int


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
        return build_final_report(client, state, plan, key_version, changes=0)
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
    initial_converted = state.converted_in_place + state.converted_consolidated
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
                return build_final_report(
                    client,
                    state,
                    plan,
                    key_version,
                    changes=(
                        state.converted_in_place + state.converted_consolidated - initial_converted
                    ),
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
            converted_consolidated=outcome.converted_consolidated,
            removed_nodes=outcome.removed_nodes,
        )
        print(
            json.dumps(
                {
                    "event": "batch",
                    "batches": state.batches,
                    "converted_in_place": state.converted_in_place,
                    "converted_consolidated": state.converted_consolidated,
                    "removed_nodes": state.removed_nodes,
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
            return BatchOutcome(
                state.cursor,
                0,
                state.batches,
                state.converted_in_place,
                state.converted_consolidated,
                state.removed_nodes,
            )
        converted = _convert_rows(tx, rows, key, key_version)
        last_identifier_id = rows[-1].row.identifier_id
        if last_identifier_id is None:
            raise NricRehashError("missing_id")
        batches, total_in_place, total_consolidated, total_removed = advance_batch(
            tx,
            owner,
            last_identifier_id,
            converted.converted_in_place,
            converted.converted_consolidated,
            converted.removed_nodes,
        )
        return BatchOutcome(
            last_identifier_id,
            len(rows),
            batches,
            total_in_place,
            total_consolidated,
            total_removed,
        )

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
) -> ConversionOutcome:
    converted_in_place = 0
    converted_consolidated = 0
    for locked in rows:
        row = locked.row
        classification = classify_nric_identifier(row, key, key_version)
        if classification.blocker in ("missing_id", "unexpected_scope"):
            raise NricRehashError(classification.blocker)
        if classification.category != "convertible":
            continue
        canonical = classification.canonical
        digest = classification.digest
        masked = classification.masked
        checksum_valid = classification.checksum_valid
        plaintext_value = row.normalized_value
        if (
            canonical is None
            or digest is None
            or masked is None
            or checksum_valid is None
            or plaintext_value is None
        ):
            raise NricRehashError("drift_detected")
        target = tx.run(queries.NRIC_REHASH_FIND_DIGEST_TARGET, digest=digest).single()
        if target is None:
            _convert_in_place(
                tx, locked, canonical, digest, key, key_version, masked, checksum_valid
            )
            converted_in_place += 1
            continue
        _consolidate_row(
            tx,
            locked,
            target,
            plaintext_value,
            canonical,
            digest,
            key,
            key_version,
            masked,
            checksum_valid,
        )
        converted_consolidated += 1
    return ConversionOutcome(converted_in_place, converted_consolidated, converted_consolidated)


def _consolidate_row(
    tx: ManagedTransaction,
    locked: LockedIdentifier,
    target: Record,
    plaintext_value: str,
    canonical: str,
    digest: str,
    key: str,
    key_version: str,
    masked: str,
    checksum_valid: bool,
) -> None:
    survivor_element_id: object = target.get("element_id")
    if not isinstance(survivor_element_id, str) or not survivor_element_id:
        raise NricRehashError("drift_detected")
    consolidate_identifier(
        tx,
        loser_element_id=locked.element_id,
        survivor_element_id=survivor_element_id,
        plaintext_value=plaintext_value,
        canonical=canonical,
        digest=digest,
        key=key,
        key_version=key_version,
        masked=masked,
        checksum_valid=checksum_valid,
    )


def _convert_in_place(
    tx: ManagedTransaction,
    locked: LockedIdentifier,
    canonical: str,
    digest: str,
    key: str,
    key_version: str,
    masked: str,
    checksum_valid: bool,
) -> None:
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
