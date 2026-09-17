"""Execute exactly one bounded unit with source work outside the graph transaction."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Protocol

from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.bounded_ingestion_commit import BoundedCommitStore
from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedConnector,
    BoundedConnectorDescriptor,
    BoundedRunResult,
    BoundedUnit,
    FailureCategory,
    PauseReason,
    RetryObligation,
    SourceBackoffError,
    Usage,
)


class ShutdownSignal(Protocol):
    def requested(self) -> bool: ...


class NeverShutdown:
    def requested(self) -> bool:
        return False


class BoundedIngestionRunner:
    def __init__(
        self,
        budget: BoundedIngestionBudget,
        clock: Callable[[], datetime],
        shutdown: ShutdownSignal,
    ) -> None:
        self._budget = budget
        self._clock = clock
        self._shutdown = shutdown

    def run_one(
        self,
        descriptor: BoundedConnectorDescriptor,
        context: AttemptContext,
        control: BoundedCommitStore,
    ) -> BoundedRunResult:
        now = self._clock()
        worst_case_seconds = (
            self._budget.max_unit_seconds
            + self._budget.max_graph_transaction_seconds
            + descriptor.max_close_seconds
        )
        early = self._early_pause(context, control, now, worst_case_seconds)
        if early is not None:
            return early
        reservation = _reservation(descriptor)
        if not self._budget.permits(context.reserved_usage, reservation):
            return self._pause_next_occurrence(context, control, "budget")
        if not control.reserve_usage(context, reservation, self._budget):
            return self._pause_next_occurrence(context, control, "budget")
        lifecycle_start = self._clock()
        delayed = self._early_pause(
            context,
            control,
            lifecycle_start,
            worst_case_seconds,
        )
        if delayed is not None:
            return delayed
        deadline = lifecycle_start + timedelta(
            seconds=self._budget.max_unit_seconds + descriptor.max_close_seconds
        )
        if context.occurrence is not None:
            deadline = min(
                deadline,
                context.occurrence.cutoff_at
                - timedelta(seconds=self._budget.max_graph_transaction_seconds),
            )
        context = replace(
            context,
            operation_deadline_at=deadline,
            cancellation=self._shutdown,
        )
        return self._fetch_and_commit(descriptor, context, control)

    def _early_pause(
        self,
        context: AttemptContext,
        control: BoundedCommitStore,
        now: datetime,
        worst_case_seconds: float,
    ) -> BoundedRunResult | None:
        if self._shutdown.requested():
            return self._pause_at(context, control, "shutdown", now)
        occurrence = context.occurrence
        if occurrence is None:
            return None
        if not occurrence.can_start(now):
            return self._pause_next_occurrence(
                context,
                control,
                "schedule_window_closed",
            )
        if not occurrence.can_finish(now, worst_case_seconds):
            return self._pause_next_occurrence(
                context,
                control,
                "schedule_window_closed",
            )
        return None

    def _fetch_and_commit(
        self,
        descriptor: BoundedConnectorDescriptor,
        context: AttemptContext,
        control: BoundedCommitStore,
    ) -> BoundedRunResult:
        connector: BoundedConnector | None = None
        source_backoff: SourceBackoffError | None = None
        backoff_observed_at: datetime | None = None
        source_failure: Exception | None = None
        compatibility_failure: str | None = None
        unit: BoundedUnit | None = None
        cleanup_failure: Exception | None = None
        close_budget_insufficient = False
        try:
            connector = descriptor.create(context)
            compatibility = connector.validate_checkpoint(context.checkpoint)
            if compatibility != "compatible":
                compatibility_failure = f"checkpoint_{compatibility}"
            else:
                unit = connector.fetch_one_unit(context.checkpoint, context)
        except SourceBackoffError as exc:
            source_backoff = exc
            backoff_observed_at = self._clock()
        except Exception as exc:
            source_failure = exc
        finally:
            if connector is not None:
                close_now = self._clock()
                close_budget_insufficient = not _can_close(
                    context,
                    close_now,
                    descriptor.max_close_seconds,
                )
                if self._shutdown.requested() or close_budget_insufficient:
                    try:
                        connector.cancel()
                    except Exception as exc:
                        cleanup_failure = exc
                try:
                    connector.close()
                except Exception as exc:
                    cleanup_failure = cleanup_failure or exc
        after_lifecycle = self._clock()
        if cleanup_failure is not None:
            return self._fail(context, control, "source", "connector_cleanup_failed")
        if close_budget_insufficient or not _within_operation_deadline(context, after_lifecycle):
            return self._fail(context, control, "overrun", "connector_lifecycle_deadline_exceeded")
        if self._shutdown.requested():
            return self._pause_at(context, control, "shutdown", after_lifecycle)
        if source_backoff is not None:
            assert backoff_observed_at is not None
            return self._handle_backoff(
                context, control, descriptor, source_backoff, backoff_observed_at
            )
        if source_failure is not None:
            return self._fail(context, control, "source", type(source_failure).__name__)
        if compatibility_failure is not None:
            return self._fail(context, control, "checkpoint", compatibility_failure)
        if unit is None:
            return self._fail(context, control, "source", "connector_returned_no_unit")
        occurrence = context.occurrence
        if occurrence is not None and not occurrence.can_finish(
            after_lifecycle,
            self._budget.max_graph_transaction_seconds,
        ):
            return self._fail(context, control, "overrun", "transaction_deadline_exceeded")
        validation = _validate_unit(descriptor, context, unit)
        if validation is not None:
            return self._fail(context, control, "source", validation)
        try:
            result = control.commit_unit(context, unit, descriptor.writer)
        except Exception as exc:
            return self._fail(context, control, "writer", type(exc).__name__)
        if result is None:
            return BoundedRunResult(
                "failed",
                context,
                failure_category="lease",
                safe_message="bounded write fence was lost",
            )
        if unit.terminal and not result.retry_obligations:
            if not control.finalize(context):
                return BoundedRunResult(
                    "failed",
                    context,
                    failure_category="lease",
                    safe_message="bounded completion fence was lost",
                )
            return BoundedRunResult("completed", context, unit_usage=unit.usage)
        reason: PauseReason = "source_backoff" if result.retry_obligations else "budget"
        next_eligible = _result_eligibility(context, result.retry_obligations)
        paused = self._pause_at(context, control, reason, next_eligible)
        return replace(paused, unit_usage=unit.usage)

    def _handle_backoff(
        self,
        context: AttemptContext,
        control: BoundedCommitStore,
        descriptor: BoundedConnectorDescriptor,
        error: SourceBackoffError,
        observed_at: datetime,
    ) -> BoundedRunResult:
        next_eligible = _backoff_eligibility(context, error.retry_at)
        occurrence = context.occurrence
        within_window = occurrence is None or error.retry_at < occurrence.drain_starts_at
        backoff_seconds = (error.retry_at - observed_at).total_seconds()
        if within_window and backoff_seconds > descriptor.max_retry_backoff_seconds:
            return self._fail(context, control, "source", "source_backoff_limit_exceeded")
        return replace(
            self._pause_at(context, control, "source_backoff", next_eligible),
            safe_message=error.safe_message,
        )

    def _pause_next_occurrence(
        self,
        context: AttemptContext,
        control: BoundedCommitStore,
        reason: PauseReason,
    ) -> BoundedRunResult:
        return self._pause_at(
            context,
            control,
            reason,
            _next_occurrence(context),
        )

    def _pause_at(
        self,
        context: AttemptContext,
        control: BoundedCommitStore,
        reason: PauseReason,
        next_eligible_at: datetime,
    ) -> BoundedRunResult:
        if not control.pause(context, reason, next_eligible_at):
            return BoundedRunResult(
                "failed",
                context,
                failure_category="lease",
                safe_message="bounded pause state was not persisted",
            )
        return BoundedRunResult("paused_with_checkpoint", context, reason)

    def _fail(
        self,
        context: AttemptContext,
        control: BoundedCommitStore,
        category: FailureCategory,
        safe_message: str,
    ) -> BoundedRunResult:
        failure = category
        if not control.fail(
            context,
            failure,
            safe_message,
            _next_occurrence(context),
        ):
            return BoundedRunResult(
                "failed",
                context,
                failure_category="lease",
                safe_message="bounded failure state was not persisted",
            )
        return BoundedRunResult(
            "failed",
            context,
            failure_category=failure,
            safe_message=safe_message,
        )


def _reservation(descriptor: BoundedConnectorDescriptor) -> Usage:
    return Usage(
        records=descriptor.max_records_per_unit,
        source_requests=descriptor.max_source_requests_per_unit,
        pages=1,
        bytes_read=descriptor.max_bytes_per_unit,
        extraction_calls=descriptor.max_extraction_calls_per_unit,
    )


def _validate_unit(
    descriptor: BoundedConnectorDescriptor,
    context: AttemptContext,
    unit: BoundedUnit,
) -> str | None:
    if unit.unit.checkpoint_before != context.checkpoint:
        return "checkpoint_before_mismatch"
    if len(unit.unit.records) > descriptor.max_records_per_unit:
        return "oversized_record_page"
    if unit.usage.records != len(unit.unit.records):
        return "record_usage_mismatch"
    if unit.usage.source_requests > descriptor.max_source_requests_per_unit:
        return "source_request_limit_exceeded"
    if unit.usage.bytes_read > descriptor.max_bytes_per_unit:
        return "response_size_limit_exceeded"
    if unit.usage.extraction_calls > descriptor.max_extraction_calls_per_unit:
        return "extraction_limit_exceeded"
    if unit.unit.checkpoint_after.cursor == context.checkpoint.cursor:
        return "repeated_cursor"
    return None


def _result_eligibility(
    context: AttemptContext,
    obligations: tuple[RetryObligation, ...],
) -> datetime:
    retry_times = [
        obligation.eligible_at for obligation in obligations if obligation.eligible_at is not None
    ]
    return min(retry_times) if retry_times else _next_occurrence(context)


def _next_occurrence(context: AttemptContext) -> datetime:
    occurrence = context.occurrence
    if occurrence is None:
        raise ValueError("one-time bounded runs require explicit continuation policy")
    return occurrence.next_eligible_at


def _backoff_eligibility(context: AttemptContext, retry_at: datetime) -> datetime:
    occurrence = context.occurrence
    if occurrence is None:
        return retry_at
    if retry_at < occurrence.drain_starts_at:
        return retry_at
    return occurrence.next_eligible_at


def _within_operation_deadline(context: AttemptContext, now: datetime) -> bool:
    deadline = context.operation_deadline_at
    return deadline is None or now <= deadline


def _can_close(context: AttemptContext, now: datetime, close_seconds: float) -> bool:
    try:
        context.require_operation_budget(now, close_seconds)
    except TimeoutError:
        return False
    return True
