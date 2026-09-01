"""Strict completed source-census validation for CRM projection admission and replay."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from neo4j import ManagedTransaction

from src.crm_tenant_projection_models import (
    CrmTenantProjectionConflictError,
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionMaterializationCommand,
)
from src.crm_tenant_projection_records import CrmTenantProjectionScope
from src.graph.crm_tenant_projection_values import (
    _json_object,
    _mapping_string,
    _nonnegative_int,
    _object_mapping,
    _required_mapping,
)
from src.graph.queries.crm_tenant_projection import READ_CENSUS
from src.standalone_crm_census_request_parser import parse_stored_census_request
from src.standalone_crm_census_requests import SourceSyncCensusRequest


@dataclass(frozen=True)
class _CensusUnitBoundary:
    state: Literal["completed", "no_work"]
    generation: int
    frozen_upper_id: int
    checkpoint_generation: int | None
    checkpoint_present: bool
    processed_rows: int
    skipped_rows: int


@dataclass(frozen=True)
class _CensusBoundary:
    contact: _CensusUnitBoundary
    lead: _CensusUnitBoundary


def _validate_source_census(
    tx: ManagedTransaction,
    command: CrmTenantProjectionMaterializationCommand,
) -> _CensusBoundary:
    return _read_source_census_boundary(
        tx,
        command.source_census_id,
        command.source_census_fingerprint,
        command.scope,
    )


def _read_source_census_boundary(
    tx: ManagedTransaction,
    census_id: str,
    census_fingerprint: str,
    scope: CrmTenantProjectionScope,
) -> _CensusBoundary:
    record = tx.run(READ_CENSUS, census_id=census_id).single()
    if record is None:
        raise CrmTenantProjectionConflictError("source census is missing")
    census = _required_mapping(record, "census")
    if (
        _mapping_string(census, "fingerprint") != census_fingerprint
        or _mapping_string(census, "source_key") != scope.source_key
        or _mapping_string(census, "source_instance_id") != scope.source_instance_id
        or _mapping_string(census, "control_instance_id") != scope.control_instance_id
        or _mapping_string(census, "census_kind") != "source_sync"
        or _mapping_string(census, "status") != "completed"
    ):
        raise CrmTenantProjectionConflictError("source census boundary conflicts")
    try:
        request = parse_stored_census_request(
            _json_object(json.loads(_mapping_string(census, "request_json")))
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CrmTenantProjectionIntegrityError("source census request is malformed") from exc
    if not isinstance(request, SourceSyncCensusRequest) or not {"contact", "lead"}.issubset(
        set(request.selected_kinds)
    ):
        raise CrmTenantProjectionConflictError("source census must select contact and lead")
    selected_kinds = set(request.selected_kinds)
    raw_units = record["units"]
    if not isinstance(raw_units, list):
        raise CrmTenantProjectionIntegrityError("source census units are malformed")
    units: dict[str, _CensusUnitBoundary] = {}
    for raw_unit in raw_units:
        unit = _object_mapping(raw_unit, "source census unit")
        kind = unit.get("stream_kind")
        if not isinstance(kind, str) or kind not in selected_kinds:
            raise CrmTenantProjectionIntegrityError("source census unit set is malformed")
        if kind in units:
            raise CrmTenantProjectionIntegrityError("source census unit is duplicated")
        units[kind] = _validate_census_unit(unit)
    if set(units) != selected_kinds:
        raise CrmTenantProjectionConflictError("source census selected unit set conflicts")
    _validate_census_terminal_accounting(
        census,
        units,
        record["publications"],
        record["fences"],
        record["active_scope_count"],
        len(selected_kinds),
    )
    contact = units.get("contact")
    lead = units.get("lead")
    if contact is None or lead is None:
        raise CrmTenantProjectionConflictError("source census lacks a contact or lead unit")
    return _CensusBoundary(contact, lead)


def _validate_census_unit(unit: Mapping[str, object]) -> _CensusUnitBoundary:
    state_value = unit.get("state")
    if state_value not in {"completed", "no_work"}:
        raise CrmTenantProjectionConflictError("source census selected unit is incomplete")
    state: Literal["completed", "no_work"] = (
        "completed" if state_value == "completed" else "no_work"
    )
    frozen = _nonnegative_int(unit.get("frozen_upper_id"), "source census frozen bound")
    generation = _nonnegative_int(unit.get("generation"), "source census unit generation")
    if generation < 1:
        raise CrmTenantProjectionIntegrityError("source census unit generation is malformed")
    checkpoint_rows = unit.get("checkpoints")
    if not isinstance(checkpoint_rows, list) or len(checkpoint_rows) > 1:
        raise CrmTenantProjectionIntegrityError("source census checkpoints are malformed")
    checkpoint_present = len(checkpoint_rows) == 1
    checkpoint = {} if not checkpoint_present else _object_mapping(checkpoint_rows[0], "checkpoint")
    checkpoint_generation_value = checkpoint.get("generation")
    checkpoint_generation = (
        None
        if checkpoint_generation_value is None
        else _nonnegative_int(checkpoint_generation_value, "source census checkpoint generation")
    )
    processed = _nonnegative_int(checkpoint.get("processed_rows", 0), "checkpoint processed rows")
    skipped = _nonnegative_int(checkpoint.get("skipped_rows", 0), "checkpoint skipped rows")
    if skipped > processed:
        raise CrmTenantProjectionIntegrityError("source census checkpoint accounting is malformed")
    if state == "completed" and (
        not checkpoint_present
        or checkpoint_generation != generation
        or checkpoint.get("frozen_upper_id") != frozen
        or checkpoint.get("last_committed_id") != frozen
    ):
        raise CrmTenantProjectionConflictError("source census checkpoint boundary is incomplete")
    if state == "no_work" and (
        checkpoint_present
        or checkpoint_generation is not None
        or processed != 0
        or skipped != 0
        or frozen != 0
    ):
        raise CrmTenantProjectionConflictError("no-work census checkpoint boundary is malformed")
    return _CensusUnitBoundary(
        state, generation, frozen, checkpoint_generation, checkpoint_present, processed, skipped
    )


def _validate_census_terminal_accounting(
    census: Mapping[str, object],
    units: dict[str, _CensusUnitBoundary],
    raw_publications: object,
    raw_fences: object,
    raw_active_scope_count: object,
    expected_units: int,
) -> None:
    completed = sum(unit.state == "completed" for unit in units.values())
    no_work = len(units) - completed
    processed = sum(unit.processed_rows for unit in units.values())
    skipped = sum(unit.skipped_rows for unit in units.values())
    if (
        _nonnegative_int(census.get("expected_units"), "source census expected units")
        != expected_units
        or _nonnegative_int(census.get("completed_units"), "source census completed units")
        != completed
        or _nonnegative_int(census.get("no_work_units"), "source census no-work units") != no_work
        or _nonnegative_int(census.get("failed_units"), "source census failed units") != 0
        or _nonnegative_int(census.get("cancelled_units"), "source census cancelled units") != 0
        or _nonnegative_int(census.get("processed_rows"), "source census processed rows")
        != processed
        or _nonnegative_int(census.get("skipped_rows"), "source census skipped rows") != skipped
    ):
        raise CrmTenantProjectionConflictError("source census terminal accounting conflicts")
    publications = _census_rows(raw_publications, "publications")
    if any(_mapping_string(row, "status") in {"pending", "publishing"} for row in publications):
        raise CrmTenantProjectionConflictError("source census publication is unresolved")
    fences = _census_rows(raw_fences, "fences")
    if any(_mapping_string(row, "status") != "retired" for row in fences):
        raise CrmTenantProjectionConflictError("source census fence is unresolved")
    if _nonnegative_int(raw_active_scope_count, "active scope"):
        raise CrmTenantProjectionConflictError("source census remains active")


def _census_rows(values: object, field: str) -> list[Mapping[str, object]]:
    if not isinstance(values, list):
        raise CrmTenantProjectionIntegrityError(f"source census {field} are malformed")
    return [_object_mapping(value, f"source census {field}") for value in values]
