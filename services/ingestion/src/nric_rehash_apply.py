"""Orchestrate bounded in-place NRIC rehash passes and aggregate verification."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient
from src.graph.queries.nric_rehash import NRIC_REHASH_DATABASE_NAME
from src.nric_rehash_anchor import AnchorScan, run_anchor_pass, scan_anchor_state
from src.nric_rehash_handoff import (
    handoff_summary,
    validate_handoff_dir,
    write_handoff_file,
)
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
    handoff_dir: Path,
) -> tuple[ApplyReport, int]:
    """Apply conversion and anchor passes and return aggregate-only output."""
    if not acknowledge_offline:
        raise NricRehashError("offline_ack_required")
    validate_handoff_dir(handoff_dir)
    state, plan = _read_apply_preflight(client, batch_size, key, key_version, expected_database)
    if state is None or state.phase == "convert":
        return _run_conversion_to_anchor(
            client, batch_size, key, key_version, handoff_dir, restart_completed=False
        )
    return _resume_existing_state(client, state, plan, batch_size, key, key_version, handoff_dir)


def _read_apply_preflight(
    client: Neo4jClient,
    batch_size: int,
    key: str,
    key_version: str,
    expected_database: str,
) -> tuple[MigrationState | None, PlanReport]:
    state = read_migration_state(client)
    _require_same_key(state, key_version, key_check_for(key))
    if client.execute_read(_read_database_name) != expected_database:
        raise NricRehashError("database_mismatch")
    plan = build_plan_report(client, batch_size, key, key_version)
    _assert_census_preconditions(plan)
    if state is not None and state.phase not in {
        "convert",
        "convert_complete",
        "anchor",
        "anchor_complete",
    }:
        raise NricRehashError("drift_detected")
    return state, plan


def _resume_existing_state(
    client: Neo4jClient,
    state: MigrationState,
    plan: PlanReport,
    batch_size: int,
    key: str,
    key_version: str,
    handoff_dir: Path,
) -> tuple[ApplyReport, int]:
    if plan["convertible"] > 0:
        return _run_conversion_to_anchor(
            client, batch_size, key, key_version, handoff_dir, restart_completed=True
        )
    if state.phase == "anchor":
        return _run_resumed_anchor_phase(
            client, state, plan, batch_size, key, key_version, handoff_dir
        )
    anchor_scan = scan_anchor_state(client, batch_size, key_version)
    if anchor_scan.anchorable_remaining > 0:
        return _run_resumed_anchor_phase(
            client, state, plan, batch_size, key, key_version, handoff_dir
        )
    return _build_final_report(
        client,
        state,
        plan,
        key_version,
        changes=0,
        batch_size=batch_size,
        handoff_dir=handoff_dir,
        anchor_scan=anchor_scan,
    )


def _run_resumed_anchor_phase(
    client: Neo4jClient,
    state: MigrationState,
    plan: PlanReport,
    batch_size: int,
    key: str,
    key_version: str,
    handoff_dir: Path,
) -> tuple[ApplyReport, int]:
    owner = uuid.uuid4().hex
    state = acquire_lease(
        client,
        owner,
        key,
        key_version,
        restart_completed=False,
        target_phase="anchor",
    )
    state, anchored = _run_anchor_phase(client, state, owner, batch_size, key_version)
    return _build_final_report(
        client,
        state,
        plan,
        key_version,
        changes=anchored,
        batch_size=batch_size,
        handoff_dir=handoff_dir,
    )


def _run_conversion_to_anchor(
    client: Neo4jClient,
    batch_size: int,
    key: str,
    key_version: str,
    handoff_dir: Path,
    *,
    restart_completed: bool,
) -> tuple[ApplyReport, int]:
    owner = uuid.uuid4().hex
    state = acquire_lease(
        client,
        owner,
        key,
        key_version,
        restart_completed=restart_completed,
        target_phase="convert",
    )
    state, converted, plan = _run_conversion_passes(
        client, state, owner, batch_size, key, key_version
    )
    return _finish_conversion_with_anchor(
        client, state, owner, plan, key, key_version, converted, batch_size, handoff_dir
    )


def _run_conversion_passes(
    client: Neo4jClient,
    state: MigrationState,
    owner: str,
    batch_size: int,
    key: str,
    key_version: str,
) -> tuple[MigrationState, int, PlanReport]:
    initial_converted = state.converted_in_place + state.converted_consolidated
    lease_active = True
    try:
        for pass_number in range(2):
            state = run_conversion_pass(client, state, owner, batch_size, key, key_version)
            complete_and_release(client, owner, phase="convert_complete")
            lease_active = False
            state = replace(state, phase="convert_complete", cursor=None)
            plan = build_plan_report(client, batch_size, key, key_version)
            _assert_census_preconditions(plan)
            if plan["convertible"] == 0:
                changes = (
                    state.converted_in_place + state.converted_consolidated - initial_converted
                )
                return state, changes, plan
            if pass_number == 1:
                raise NricRehashError("residual_plaintext")
            state = acquire_lease(
                client,
                owner,
                key,
                key_version,
                restart_completed=True,
                target_phase="convert",
            )
            lease_active = True
    finally:
        if lease_active:
            release_lease(client, owner)
    raise NricRehashError("residual_plaintext")


def _finish_conversion_with_anchor(
    client: Neo4jClient,
    state: MigrationState,
    owner: str,
    plan: PlanReport,
    key: str,
    key_version: str,
    converted: int,
    batch_size: int,
    handoff_dir: Path,
) -> tuple[ApplyReport, int]:
    state, anchored = _start_anchor_phase(client, state, owner, batch_size, key, key_version)
    return _build_final_report(
        client,
        state,
        plan,
        key_version,
        changes=converted + anchored,
        batch_size=batch_size,
        handoff_dir=handoff_dir,
    )


def _start_anchor_phase(
    client: Neo4jClient,
    state: MigrationState,
    owner: str,
    batch_size: int,
    key: str,
    key_version: str,
) -> tuple[MigrationState, int]:
    state = acquire_lease(
        client,
        owner,
        key,
        key_version,
        restart_completed=False,
        target_phase="anchor",
    )
    return _run_anchor_phase(client, state, owner, batch_size, key_version)


def _run_anchor_phase(
    client: Neo4jClient,
    state: MigrationState,
    owner: str,
    batch_size: int,
    key_version: str,
) -> tuple[MigrationState, int]:
    initial_anchored = state.anchored
    lease_active = True
    try:
        state = run_anchor_pass(client, state, owner, batch_size, key_version)
        complete_and_release(client, owner, phase="anchor_complete")
        lease_active = False
        state = replace(state, phase="anchor_complete", anchor_cursor=None)
        return state, state.anchored - initial_anchored
    finally:
        if lease_active:
            release_lease(client, owner)


def _build_final_report(
    client: Neo4jClient,
    state: MigrationState,
    plan: PlanReport,
    key_version: str,
    *,
    changes: int,
    batch_size: int,
    handoff_dir: Path,
    anchor_scan: AnchorScan | None = None,
) -> tuple[ApplyReport, int]:
    scan = anchor_scan or scan_anchor_state(client, batch_size, key_version)
    basename = write_handoff_file(handoff_dir, scan.handoff_entries)
    return build_final_report(
        client,
        state,
        plan,
        key_version,
        changes=changes,
        anchored=state.anchored,
        already_anchored=scan.already_anchored,
        anchorable_remaining=scan.anchorable_remaining,
        handoff=handoff_summary(scan.handoff_entries, basename),
    )


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
