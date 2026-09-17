"""Dependency-injected bounded dispatch used by Celery and conformance tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.bounded_ingestion_commit import BoundedCommitStore
from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedRunResult,
    OccurrenceContext,
    RunScope,
    utc_now,
)
from src.bounded_ingestion_runner import BoundedIngestionRunner, NeverShutdown, ShutdownSignal
from src.connectors.registry import BoundedConnectorRegistry
from src.resumable import CheckpointDescriptor


class BoundedDispatchControl(BoundedCommitStore, Protocol):
    def admit_or_resume(
        self,
        *,
        scope: RunScope,
        occurrence: OccurrenceContext,
        initial_checkpoint: CheckpointDescriptor,
        worker_task_id: str,
        now: datetime,
        lease_token: str | None = None,
        lease_seconds: float = 300.0,
    ) -> AttemptContext | None: ...


def dispatch_one(
    *,
    registry: BoundedConnectorRegistry,
    control: BoundedDispatchControl,
    scope: RunScope,
    occurrence: OccurrenceContext,
    worker_task_id: str,
    budget: BoundedIngestionBudget,
    clock: Callable[[], datetime] = utc_now,
    shutdown: ShutdownSignal | None = None,
) -> BoundedRunResult:
    drain_seconds = (occurrence.cutoff_at - occurrence.drain_starts_at).total_seconds()
    if drain_seconds < budget.drain_reserve_seconds:
        return _compatibility_block("occurrence_drain_reserve_too_small")
    try:
        descriptor = registry.require(scope.source_key, scope.mode)
    except LookupError as exc:
        return BoundedRunResult(
            "blocked",
            None,
            failure_category="capability",
            safe_message=str(exc),
        )
    if descriptor.connector_version != scope.connector_version:
        return _compatibility_block("connector_version_mismatch")
    if descriptor.checkpoint_schema_version != scope.checkpoint_schema_version:
        return _compatibility_block("checkpoint_schema_mismatch")
    initial_checkpoint = descriptor.initial_checkpoint(scope, occurrence)
    if initial_checkpoint.source_window != scope.source_window:
        return _compatibility_block("source_window_mismatch")
    if initial_checkpoint.connector_version != descriptor.connector_version:
        return _compatibility_block("initial_checkpoint_connector_mismatch")
    if initial_checkpoint.schema_version != descriptor.checkpoint_schema_version:
        return _compatibility_block("initial_checkpoint_schema_mismatch")
    context = control.admit_or_resume(
        scope=scope,
        occurrence=occurrence,
        initial_checkpoint=initial_checkpoint,
        worker_task_id=worker_task_id,
        now=clock(),
        lease_seconds=budget.drain_reserve_seconds,
    )
    if context is None:
        return BoundedRunResult(
            "blocked",
            None,
            failure_category="lease",
            safe_message="bounded attempt was not admitted",
        )
    runner = BoundedIngestionRunner(
        budget,
        clock,
        shutdown or NeverShutdown(),
    )
    return runner.run_one(descriptor, context, control)


def _compatibility_block(message: str) -> BoundedRunResult:
    return BoundedRunResult(
        "blocked",
        None,
        failure_category="capability",
        safe_message=message,
    )
