"""Execute exactly one bounded unit with source work outside the graph transaction."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
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
        early = self._early_pause(context, control, now)
        if early is not None:
            return early
        reservation = _reservation(descriptor)
        if not self._budget.permits(context.reserved_usage, reservation):
            return self._pause_next_occurrence(context, control, "budget")
        if not control.reserve_usage(context, reservation, self._budget):
            return self._pause_next_occurrence(context, control, "budget")
        return self._fetch_and_commit(descriptor, context, control)

    def _early_pause(
        self,
        context: AttemptContext,
        control: BoundedCommitStore,
        now: datetime,
    ) -> BoundedRunResult | None:
        if self._shutdown.requested():
            control.pause(context, "shutdown", now)
            return BoundedRunResult("paused_with_checkpoint", context, "shutdown")
        occurrence = context.occurrence
        if occurrence is None:
            return None
        if not occurrence.can_start(now):
            return self._pause_next_occurrence(
                context,
                control,
                "schedule_window_closed",
            )
        if not occurrence.can_finish(now, self._budget.max_unit_seconds):
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
        try:
            connector = descriptor.create(context)
            compatibility = connector.validate_checkpoint(context.checkpoint)
            if compatibility != "compatible":
                return self._fail(
                    context,
                    control,
                    "checkpoint",
                    f"checkpoint_{compatibility}",
                )
            unit = connector.fetch_one_unit(context.checkpoint, context)
        except SourceBackoffError as exc:
            next_eligible = _backoff_eligibility(context, exc.retry_at)
            control.pause(context, "source_backoff", next_eligible)
            return BoundedRunResult(
                "paused_with_checkpoint",
                context,
                "source_backoff",
                safe_message=exc.safe_message,
            )
        except Exception as exc:
            return self._fail(context, control, "source", type(exc).__name__)
        finally:
            if connector is not None:
                connector.close()
        after_fetch = self._clock()
        if self._shutdown.requested():
            control.pause(context, "shutdown", after_fetch)
            return BoundedRunResult("paused_with_checkpoint", context, "shutdown")
        occurrence = context.occurrence
        if occurrence is not None and after_fetch >= occurrence.cutoff_at:
            control.fail(
                context,
                "overrun",
                "bounded unit crossed its absolute cutoff",
                occurrence.next_eligible_at,
            )
            return BoundedRunResult(
                "failed",
                context,
                failure_category="overrun",
                safe_message="bounded unit crossed its absolute cutoff",
            )
        validation = _validate_unit(descriptor, context, unit)
        if validation is not None:
            return self._fail(context, control, "source", validation)
        try:
            result = control.commit_unit(context, unit, descriptor.writer)
        except Exception as exc:
            control.fail(
                context,
                "writer",
                type(exc).__name__,
                _next_occurrence(context),
            )
            return BoundedRunResult(
                "failed",
                context,
                failure_category="writer",
                safe_message=type(exc).__name__,
            )
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
        control.pause(context, reason, next_eligible)
        return BoundedRunResult(
            "paused_with_checkpoint",
            context,
            reason,
            unit_usage=unit.usage,
        )

    def _pause_next_occurrence(
        self,
        context: AttemptContext,
        control: BoundedCommitStore,
        reason: PauseReason,
    ) -> BoundedRunResult:
        control.pause(context, reason, _next_occurrence(context))
        return BoundedRunResult("paused_with_checkpoint", context, reason)

    def _fail(
        self,
        context: AttemptContext,
        control: BoundedCommitStore,
        category: FailureCategory,
        safe_message: str,
    ) -> BoundedRunResult:
        failure = category
        control.fail(
            context,
            failure,
            safe_message,
            _next_occurrence(context),
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
