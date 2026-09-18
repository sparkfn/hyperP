"""Durable scheduler coordination built on #430's fenced graph control store."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from neo4j import ManagedTransaction, Record, Result
from pydantic.types import JsonValue

from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.bounded_ingestion_models import BoundedMode, OccurrenceContext, Usage
from src.bounded_ingestion_window import occurrence_to_payload
from src.graph.client import Neo4jClient
from src.graph.queries.scheduled_ingestion import (
    BLOCK_SCHEDULED_GROUP_WORKFLOW,
    CLAIM_SCHEDULED_CHILD_PUBLICATION,
    CLAIM_SCHEDULED_MAINTENANCE,
    CONFIRM_SCHEDULED_CHILD_PUBLICATION,
    CONFIRM_SCHEDULED_MAINTENANCE,
    ENSURE_SCHEDULED_GROUP_WORKFLOW,
    ENSURE_SCHEDULED_OCCURRENCE_AUTHORITY,
    GET_ACTIVE_SCHEDULE_RESET,
    REBIND_SCHEDULED_GROUP_OCCURRENCE,
    RECONCILE_SCHEDULED_CHILD_COMPLETION,
)


class _QueryTransaction(Protocol):
    def run(self, query: str, **parameters: object) -> Result: ...


def _run(tx: ManagedTransaction, query: str, **parameters: object) -> Result:
    return cast(_QueryTransaction, tx).run(query, **parameters)


@dataclass(frozen=True)
class ScheduledChildContext:
    """The complete bounded payload the scheduler may publish for one child."""

    child_key: str
    source_key: str
    entity_key: str | None
    control_instance_id: str
    mode: BoundedMode
    configuration_fingerprint: str
    connector_version: str
    configuration_version: str
    checkpoint_schema_version: int
    source_window: dict[str, JsonValue]
    stream_key: str | None = None
    reserved_usage: Usage = Usage()

    def __post_init__(self) -> None:
        required = (
            self.child_key,
            self.source_key,
            self.control_instance_id,
            self.configuration_fingerprint,
            self.connector_version,
            self.configuration_version,
        )
        if not all(value.strip() for value in required):
            raise ValueError("scheduled child context has an empty identity field")
        if self.checkpoint_schema_version < 1:
            raise ValueError("scheduled checkpoint schema version must be positive")


@dataclass(frozen=True)
class ScheduledWorkflow:
    """A bounded durable group workflow, not a Celery-chain result."""

    workflow_id: str
    status: str
    manual_pause: bool
    current_child_index: int
    completed_prefix: int
    pending_publication_id: str | None
    occurrence_id: str | None
    next_eligible_at: str | None


@dataclass(frozen=True)
class ScheduledMaintenanceContext:
    """The bounded reservation one scheduler-owned maintenance obligation charges."""

    reserved_usage: Usage = Usage()


@dataclass(frozen=True)
class ScheduledPublication:
    """One stable, re-publishable child intent protected by a graph claim."""

    workflow_id: str
    publication_id: str


@dataclass(frozen=True)
class ScheduledProgress:
    """Durable completion reconciliation outcome for the current child."""

    completed: bool
    current_child_index: int
    status: str


@dataclass(frozen=True)
class ScheduledMaintenanceObligation:
    """A scheduler-owned gate for lifecycle or KNOWS task publication."""

    obligation_id: str
    publication_state: str


class ScheduledIngestionControl:
    """Own group progress, publication intent, and scheduler-side reservations."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def active_reset_generation(self, environment: str) -> int | None:
        def read(tx: ManagedTransaction) -> int | None:
            record = _run(tx, GET_ACTIVE_SCHEDULE_RESET, environment=environment).single()
            if record is None:
                return None
            return _positive_int(record, "generation")

        return self._client.execute_read(read)

    def ensure_workflow(
        self,
        *,
        environment: str,
        reset_generation: int,
        group_key: str,
        control_instance_id: str,
        child_keys: tuple[str, ...],
        occurrence: OccurrenceContext,
        now: datetime,
    ) -> ScheduledWorkflow | None:
        scope_key = _workflow_scope_key(
            environment,
            reset_generation,
            group_key,
            control_instance_id,
        )
        children_json = json.dumps(child_keys, separators=(",", ":"))
        params = {
            "scope_key": scope_key,
            "environment": environment,
            "reset_generation": reset_generation,
            "group_key": group_key,
            "control_instance_id": control_instance_id,
            "children_json": children_json,
            "child_count": len(child_keys),
            "occurrence_id": occurrence.occurrence_id,
            "timezone": occurrence.timezone,
            "starts_at": occurrence.starts_at.isoformat(),
            "drain_starts_at": occurrence.drain_starts_at.isoformat(),
            "cutoff_at": occurrence.cutoff_at.isoformat(),
        }

        def write(tx: ManagedTransaction) -> ScheduledWorkflow | None:
            record = _run(tx, ENSURE_SCHEDULED_GROUP_WORKFLOW, **params).single()
            return _workflow(record) if record is not None else None

        workflow = self._client.execute_write(write)
        if (
            workflow is None
            or workflow.manual_pause
            or workflow.occurrence_id == occurrence.occurrence_id
        ):
            return workflow
        self._rebind(scope_key, environment, reset_generation, occurrence, now)
        return self._client.execute_write(write)

    def reconcile_current_child(
        self,
        *,
        environment: str,
        reset_generation: int,
        group_key: str,
        control_instance_id: str,
    ) -> ScheduledProgress | None:
        scope_key = _workflow_scope_key(
            environment,
            reset_generation,
            group_key,
            control_instance_id,
        )

        def write(tx: ManagedTransaction) -> ScheduledProgress | None:
            record = _run(
                tx,
                RECONCILE_SCHEDULED_CHILD_COMPLETION,
                scope_key=scope_key,
                environment=environment,
                reset_generation=reset_generation,
            ).single()
            if record is None:
                return None
            return ScheduledProgress(
                completed=_bool(record, "completed"),
                current_child_index=_non_negative_int(record, "current_child_index"),
                status=_text(record, "workflow_status"),
            )

        return self._client.execute_write(write)

    def claim_current_child(
        self,
        *,
        environment: str,
        reset_generation: int,
        group_key: str,
        occurrence: OccurrenceContext,
        now: datetime,
        child_index: int,
        child: ScheduledChildContext,
        budget: BoundedIngestionBudget,
    ) -> ScheduledPublication | None:
        scope_key = _workflow_scope_key(
            environment,
            reset_generation,
            group_key,
            child.control_instance_id,
        )
        publication_id = f"{scope_key}:{occurrence.occurrence_id}:{child_index}"
        usage = child.reserved_usage
        authority_key = _occurrence_authority_key(
            environment,
            reset_generation,
            occurrence.occurrence_id,
        )
        if not self.ensure_occurrence_authority(
            environment=environment,
            reset_generation=reset_generation,
            occurrence=occurrence,
            budget=budget,
        ):
            return None

        def write(tx: ManagedTransaction) -> ScheduledPublication | None:
            record = _run(
                tx,
                CLAIM_SCHEDULED_CHILD_PUBLICATION,
                scope_key=scope_key,
                occurrence_authority_key=authority_key,
                participant_key=f"{authority_key}|source|{scope_key}",
                environment=environment,
                reset_generation=reset_generation,
                occurrence_id=occurrence.occurrence_id,
                now=now.isoformat(),
                child_index=child_index,
                publication_id=publication_id,
                child_key=child.child_key,
                source_key=child.source_key,
                entity_key=child.entity_key,
                mode=child.mode,
                stream_key=child.stream_key,
                configuration_fingerprint=child.configuration_fingerprint,
                source_window_fingerprint=_source_window_fingerprint(child.source_window),
                reserved_records=usage.records,
                reserved_source_requests=usage.source_requests,
                reserved_pages=usage.pages,
                reserved_bytes=usage.bytes_read,
                reserved_extraction_calls=usage.extraction_calls,
            ).single()
            if record is None:
                return None
            return ScheduledPublication(
                workflow_id=_text(record, "workflow_id"),
                publication_id=_text(record, "publication_id"),
            )

        return self._client.execute_write(write)

    def ensure_occurrence_authority(
        self,
        *,
        environment: str,
        reset_generation: int,
        occurrence: OccurrenceContext,
        budget: BoundedIngestionBudget,
    ) -> bool:
        authority_key = _occurrence_authority_key(
            environment,
            reset_generation,
            occurrence.occurrence_id,
        )

        def write(tx: ManagedTransaction) -> bool:
            return (
                _run(
                    tx,
                    ENSURE_SCHEDULED_OCCURRENCE_AUTHORITY,
                    occurrence_authority_key=authority_key,
                    environment=environment,
                    reset_generation=reset_generation,
                    occurrence_id=occurrence.occurrence_id,
                    max_records=budget.max_records,
                    max_source_requests=budget.max_source_requests,
                    max_pages=budget.max_pages,
                    max_bytes=budget.max_bytes,
                    max_extraction_calls=budget.max_extraction_calls,
                ).single()
                is not None
            )

        return self._client.execute_write(write)

    def confirm_child_publication(
        self,
        *,
        environment: str,
        reset_generation: int,
        group_key: str,
        control_instance_id: str,
        publication_id: str,
    ) -> bool:
        scope_key = _workflow_scope_key(
            environment,
            reset_generation,
            group_key,
            control_instance_id,
        )

        def write(tx: ManagedTransaction) -> bool:
            return (
                _run(
                    tx,
                    CONFIRM_SCHEDULED_CHILD_PUBLICATION,
                    scope_key=scope_key,
                    publication_id=publication_id,
                ).single()
                is not None
            )

        return self._client.execute_write(write)

    def block_workflow(
        self,
        *,
        environment: str,
        reset_generation: int,
        group_key: str,
        control_instance_id: str,
        reason: str,
        next_eligible_at: datetime,
    ) -> bool:
        scope_key = _workflow_scope_key(
            environment,
            reset_generation,
            group_key,
            control_instance_id,
        )

        def write(tx: ManagedTransaction) -> bool:
            return (
                _run(
                    tx,
                    BLOCK_SCHEDULED_GROUP_WORKFLOW,
                    scope_key=scope_key,
                    environment=environment,
                    reset_generation=reset_generation,
                    reason=reason[:200],
                    next_eligible_at=next_eligible_at.isoformat(),
                ).single()
                is not None
            )

        return self._client.execute_write(write)

    def claim_maintenance_obligation(
        self,
        *,
        environment: str,
        reset_generation: int,
        kind: str,
        phase: str | None,
        occurrence: OccurrenceContext,
        bucket: str,
        context: ScheduledMaintenanceContext,
        budget: BoundedIngestionBudget,
    ) -> ScheduledMaintenanceObligation | None:
        """Claim one hourly maintenance publication or coalesce a duplicate tick."""
        scope_key = _maintenance_scope_key(
            environment,
            reset_generation,
            kind,
            phase,
            occurrence,
            bucket,
        )
        authority_key = _occurrence_authority_key(
            environment,
            reset_generation,
            occurrence.occurrence_id,
        )
        if not self.ensure_occurrence_authority(
            environment=environment,
            reset_generation=reset_generation,
            occurrence=occurrence,
            budget=budget,
        ):
            return None
        usage = context.reserved_usage
        params = {
            "scope_key": scope_key,
            "obligation_id": f"{scope_key}:publication",
            "participant_key": f"{authority_key}|maintenance|{kind}|{phase or '-'}",
            "occurrence_authority_key": authority_key,
            "environment": environment,
            "reset_generation": reset_generation,
            "kind": kind,
            "phase": phase,
            "occurrence_id": occurrence.occurrence_id,
            "occurrence_json": json.dumps(occurrence_to_payload(occurrence), sort_keys=True),
            "bucket": bucket,
            "reserved_records": usage.records,
            "reserved_source_requests": usage.source_requests,
            "reserved_pages": usage.pages,
            "reserved_bytes": usage.bytes_read,
            "reserved_extraction_calls": usage.extraction_calls,
        }

        def write(tx: ManagedTransaction) -> ScheduledMaintenanceObligation | None:
            record = _run(tx, CLAIM_SCHEDULED_MAINTENANCE, **params).single()
            if record is None:
                return None
            return ScheduledMaintenanceObligation(
                obligation_id=_text(record, "obligation_id"),
                publication_state=_text(record, "publication_state"),
            )

        return self._client.execute_write(write)

    def confirm_maintenance_obligation(
        self,
        *,
        environment: str,
        reset_generation: int,
        kind: str,
        phase: str | None,
        occurrence: OccurrenceContext,
        bucket: str,
    ) -> bool:
        scope_key = _maintenance_scope_key(
            environment,
            reset_generation,
            kind,
            phase,
            occurrence,
            bucket,
        )

        def write(tx: ManagedTransaction) -> bool:
            return _run(tx, CONFIRM_SCHEDULED_MAINTENANCE, scope_key=scope_key).single() is not None

        return self._client.execute_write(write)

    def _rebind(
        self,
        scope_key: str,
        environment: str,
        reset_generation: int,
        occurrence: OccurrenceContext,
        now: datetime,
    ) -> None:
        payload = occurrence_to_payload(occurrence)

        def write(tx: ManagedTransaction) -> None:
            _run(
                tx,
                REBIND_SCHEDULED_GROUP_OCCURRENCE,
                scope_key=scope_key,
                environment=environment,
                reset_generation=reset_generation,
                occurrence_id=payload["occurrence_id"],
                timezone=payload["timezone"],
                starts_at=payload["starts_at"],
                drain_starts_at=payload["drain_starts_at"],
                cutoff_at=payload["cutoff_at"],
                next_eligible_at=payload["next_eligible_at"],
                now=now.isoformat(),
            ).single()

        self._client.execute_write(write)


def _workflow_scope_key(
    environment: str,
    reset_generation: int,
    group_key: str,
    control_instance_id: str,
) -> str:
    return f"scheduled-group|{environment}|{reset_generation}|{control_instance_id}|{group_key}"


def _occurrence_authority_key(
    environment: str,
    reset_generation: int,
    occurrence_id: str,
) -> str:
    return f"scheduled-occurrence|{environment}|{reset_generation}|{occurrence_id}"


def _maintenance_scope_key(
    environment: str,
    reset_generation: int,
    kind: str,
    phase: str | None,
    occurrence: OccurrenceContext,
    bucket: str,
) -> str:
    return (
        f"scheduled-maintenance|{environment}|{reset_generation}|{kind}|{phase or '-'}|"
        f"{occurrence.occurrence_id}|{bucket}"
    )


def _source_window_fingerprint(source_window: dict[str, JsonValue]) -> str:
    encoded = json.dumps(
        source_window,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _workflow(record: Record) -> ScheduledWorkflow:
    return ScheduledWorkflow(
        workflow_id=_text(record, "workflow_id"),
        status=_text(record, "workflow_status"),
        manual_pause=_bool(record, "manual_pause"),
        current_child_index=_non_negative_int(record, "current_child_index"),
        completed_prefix=_non_negative_int(record, "completed_prefix"),
        pending_publication_id=_optional_text(record["pending_publication_id"]),
        occurrence_id=_optional_text(record["occurrence_id"]),
        next_eligible_at=_optional_text(record["next_eligible_at"]),
    )


def _text(record: Record, key: str) -> str:
    value: object = record[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"scheduled control returned invalid {key}")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _bool(record: Record, key: str) -> bool:
    value: object = record[key]
    if not isinstance(value, bool):
        raise ValueError(f"scheduled control returned invalid {key}")
    return value


def _positive_int(record: Record, key: str) -> int:
    value: object = record[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"scheduled control returned invalid {key}")
    return value


def _non_negative_int(record: Record, key: str) -> int:
    value: object = record[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"scheduled control returned invalid {key}")
    return value
