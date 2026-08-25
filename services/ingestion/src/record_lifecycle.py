"""Typed coordination helpers for immutable SourceRecord lifecycle transitions."""

from __future__ import annotations

from dataclasses import dataclass

from neo4j import ManagedTransaction, Record

from src.graph import queries
from src.graph.crm_deal_count import recompute_source_person_crm_deal_counts
from src.models import SourceLifecycleState, SourceRecordLifecycleStatus, SourceVersionState
from src.source_instances import effective_source_instance_id


@dataclass(frozen=True)
class DuplicateVersion:
    source_record_pk: str


@dataclass(frozen=True)
class PlannedVersion:
    version: int
    active_source_record_pk: str | None
    prior_person_ids: tuple[str, ...]
    pending_to_reject: str | None
    active_normalized_payload: str | None = None
    active_raw_payload: str | None = None


class SourceLifecycleConflict(RuntimeError):  # noqa: N818 - required domain term
    """Raised when graph state changed before a planned transition completed."""


class SourceLifecycleDataError(ValueError):
    """Raised when lifecycle rows violate the graph boundary contract."""


def classify_incoming_hash(
    state: SourceLifecycleState, record_hash: str
) -> DuplicateVersion | None:
    """Return the matching open version, preferring the active version."""
    if state.active is not None and state.active.record_hash == record_hash:
        return DuplicateVersion(state.active.source_record_pk)
    if state.pending is not None and state.pending.record_hash == record_hash:
        return DuplicateVersion(state.pending.source_record_pk)
    return None


def plan_incoming_version(
    state: SourceLifecycleState, record_hash: str
) -> DuplicateVersion | PlannedVersion:
    """Classify a duplicate or describe the next immutable version to stage."""
    duplicate = classify_incoming_hash(state, record_hash)
    if duplicate is not None:
        return duplicate
    return PlannedVersion(
        version=state.next_version,
        active_source_record_pk=(
            state.active.source_record_pk if state.active is not None else None
        ),
        prior_person_ids=(state.active.linked_person_ids if state.active is not None else ()),
        pending_to_reject=(state.pending.source_record_pk if state.pending is not None else None),
        active_normalized_payload=(
            state.active.normalized_payload if state.active is not None else None
        ),
        active_raw_payload=(state.active.raw_payload if state.active is not None else None),
    )


def _row_value(row: Record, key: str) -> object:
    try:
        value: object = row[key]
    except KeyError as exc:
        raise SourceLifecycleDataError(f"missing lifecycle row field: {key}") from exc
    return value


def _parse_string(row: Record, key: str) -> str:
    value = _row_value(row, key)
    if not isinstance(value, str):
        raise SourceLifecycleDataError(f"{key} must be a string")
    return value


def _parse_optional_string(row: Record, key: str) -> str | None:
    try:
        value: object = row[key]
    except KeyError:
        return None
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceLifecycleDataError(f"{key} must be a string or null")
    return value


def _parse_version(row: Record) -> int:
    value = _row_value(row, "source_record_version")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SourceLifecycleDataError("source_record_version must be a positive integer")
    return value


def _parse_max_version(row: Record) -> int | None:
    value = _row_value(row, "max_source_record_version")
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SourceLifecycleDataError(
            "max_source_record_version must be a positive integer or null"
        )
    return value


def _parse_person_ids(row: Record) -> tuple[str, ...]:
    value = _row_value(row, "linked_person_ids")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SourceLifecycleDataError("linked_person_ids must be a list of strings")
    return tuple(value)


def _parse_version_state(row: Record) -> SourceVersionState | None:
    pk = _row_value(row, "source_record_pk")
    if pk is None:
        empty_keys = ("source_record_version", "record_hash", "lifecycle_status")
        if any(_row_value(row, key) is not None for key in empty_keys):
            raise SourceLifecycleDataError("null source_record_pk row contains version data")
        person_ids = _row_value(row, "linked_person_ids")
        if not isinstance(person_ids, list) or person_ids:
            raise SourceLifecycleDataError(
                "null source_record_pk row must have empty linked_person_ids"
            )
        return None
    status_value = _parse_string(row, "lifecycle_status")
    try:
        status = SourceRecordLifecycleStatus(status_value)
    except ValueError as exc:
        raise SourceLifecycleDataError(f"unexpected lifecycle_status: {status_value}") from exc
    if status not in {
        SourceRecordLifecycleStatus.ACTIVE,
        SourceRecordLifecycleStatus.PENDING_REVIEW,
    }:
        raise SourceLifecycleDataError(f"unexpected lifecycle_status: {status_value}")
    return SourceVersionState(
        source_record_pk=_parse_string(row, "source_record_pk"),
        source_record_version=_parse_version(row),
        record_hash=_parse_string(row, "record_hash"),
        lifecycle_status=status,
        linked_person_ids=_parse_person_ids(row),
        normalized_payload=_parse_optional_string(row, "normalized_payload"),
        raw_payload=_parse_optional_string(row, "raw_payload"),
    )


def load_locked_source_state(
    tx: ManagedTransaction,
    source_system: str,
    source_record_id: str,
    source_instance_id: str | None = None,
) -> SourceLifecycleState:
    """Lock one source identity and parse its open versions."""
    result = tx.run(
        queries.LOCK_AND_GET_SOURCE_STATE,
        source_system=source_system,
        source_instance_id=effective_source_instance_id(source_instance_id),
        source_record_id=source_record_id,
    )
    active: SourceVersionState | None = None
    pending: SourceVersionState | None = None
    max_version: int | None = None
    saw_row = False
    for row in result:
        row_max_version = _parse_max_version(row)
        if saw_row and row_max_version != max_version:
            raise SourceLifecycleDataError("inconsistent max_source_record_version rows")
        max_version = row_max_version
        saw_row = True
        version = _parse_version_state(row)
        if version is None:
            continue
        if max_version is None or version.source_record_version > max_version:
            raise SourceLifecycleDataError(
                "max_source_record_version is lower than an open source version"
            )
        if version.lifecycle_status is SourceRecordLifecycleStatus.ACTIVE:
            if active is not None:
                raise SourceLifecycleDataError("multiple active source versions")
            active = version
        else:
            if pending is not None:
                raise SourceLifecycleDataError("multiple pending source versions")
            pending = version
    return SourceLifecycleState(
        active=active,
        pending=pending,
        next_version=(max_version + 1 if max_version is not None else 1),
    )


def reject_replaced_pending(tx: ManagedTransaction, source_record_pk: str) -> None:
    """Reject a pending version that a newer incoming version replaces."""
    result = tx.run(
        queries.REJECT_PENDING_SOURCE_RECORD,
        source_record_pk=source_record_pk,
        reason="rejected_by_newer_version",
    )
    if result.single() is None:
        raise SourceLifecycleConflict(f"pending source record {source_record_pk} was not rejected")


def activate_staged_version(
    tx: ManagedTransaction,
    *,
    source_system: str,
    source_record_id: str,
    old_source_record_pk: str | None,
    source_instance_id: str | None = None,
    new_source_record_pk: str,
) -> None:
    """Activate a staged first or replacement version using guarded queries."""
    if old_source_record_pk is None:
        result = tx.run(
            queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION,
            source_record_pk=new_source_record_pk,
            source_system=source_system,
            source_instance_id=effective_source_instance_id(source_instance_id),
            source_record_id=source_record_id,
        )
    else:
        result = tx.run(
            queries.ACTIVATE_SOURCE_RECORD_VERSION,
            old_source_record_pk=old_source_record_pk,
            new_source_record_pk=new_source_record_pk,
        )
    if result.single() is None:
        raise SourceLifecycleConflict(f"source record {new_source_record_pk} was not activated")
    source_record_pks = [new_source_record_pk]
    if old_source_record_pk is not None:
        source_record_pks.append(old_source_record_pk)
    recompute_source_person_crm_deal_counts(tx, source_record_pks)
