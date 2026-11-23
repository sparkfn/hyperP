"""Orchestrate bounded in-place NRIC rehash passes and aggregate verification."""

from __future__ import annotations

import uuid
from dataclasses import replace

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient
from src.graph.queries.nric_rehash import NRIC_REHASH_DATABASE_NAME
from src.nric_rehash_passes import run_conversion_pass
from src.nric_rehash_plan import NricRehashError, PlanReport, build_plan_report
from src.nric_rehash_report import ApplyReport, build_final_report
from src.nric_rehash_state import (
    MigrationState,
    acquire_lease,
    complete_and_release,
    key_check_for,
    read_migration_state,
    release_lease,
)


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
            state = run_conversion_pass(client, state, owner, batch_size, key, key_version)
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
    record = tx.run(NRIC_REHASH_DATABASE_NAME).single()
    if record is None:
        raise NricRehashError("neo4j_error")
    name: object = record.get("name")
    if isinstance(name, str):
        return name
    raise NricRehashError("neo4j_error")
