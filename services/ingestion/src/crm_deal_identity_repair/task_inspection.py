"""Fail-closed injected task/broker absence proofs for repair quiescence (#310)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RepairTaskIdentity:
    """The complete stable identity of one affected task delivery."""

    task_id: str
    task_name: str
    queue: str
    kwargs_digest: str

    def __post_init__(self) -> None:
        if not all((self.task_id, self.task_name, self.queue, self.kwargs_digest)):
            raise ValueError("repair task identity fields must be non-empty")


@dataclass(frozen=True)
class RepairObservedTask:
    """One task observation returned by an injected bounded inspector."""

    task_id: str | None
    task_name: str | None
    queue: str | None
    kwargs_digest: str | None

    def matches(self, expected: RepairTaskIdentity) -> bool:
        """A known ID or full routing identity is enough to prove an affected delivery exists."""
        if self.task_id == expected.task_id:
            return True
        return (
            self.task_name == expected.task_name
            and self.queue == expected.queue
            and self.kwargs_digest == expected.kwargs_digest
        )

    @property
    def identity_is_unknown(self) -> bool:
        """Partial records without a task ID cannot safely be classified as unrelated."""
        return self.task_id is None and not (
            self.task_name is not None
            and self.queue is not None
            and self.kwargs_digest is not None
        )


class RepairTaskInspector(Protocol):
    """Injected Celery inspection boundary; production wiring is deliberately absent."""

    def inspect(
        self,
        expected_workers: tuple[str, ...],
        tasks: tuple[RepairTaskIdentity, ...],
        timeout_seconds: float,
    ) -> "RepairTaskInspection": ...


class RepairBrokerInspector(Protocol):
    """Injected broker boundary that proves queue absence for captured task identities."""

    def has_queued_delivery(
        self, tasks: tuple[RepairTaskIdentity, ...], timeout_seconds: float
    ) -> bool | None: ...


@dataclass(frozen=True)
class RepairTaskInspection:
    """Bounded worker reply evidence. Any absence, error, timeout, or unknown fails closed."""

    responders: tuple[str, ...]
    active: tuple[RepairObservedTask, ...] = ()
    reserved: tuple[RepairObservedTask, ...] = ()
    scheduled: tuple[RepairObservedTask, ...] = ()
    queued: tuple[RepairObservedTask, ...] = ()
    unknown_task_ids: tuple[str, ...] = ()
    inspection_failed: bool = False
    timed_out: bool = False
    reply_errors: tuple[str, ...] = ()

    def proves_absence(
        self,
        *,
        expected_workers: tuple[str, ...],
        broker: RepairBrokerInspector,
        tasks: tuple[RepairTaskIdentity, ...],
        timeout_seconds: float,
    ) -> bool:
        """Require every worker reply and explicit no-delivery proof across all states."""
        if not expected_workers or timeout_seconds <= 0 or self.inspection_failed or self.timed_out:
            return False
        if self.reply_errors or self.unknown_task_ids:
            return False
        if len(set(expected_workers)) != len(expected_workers):
            return False
        if (
            set(self.responders) != set(expected_workers)
            or len(set(self.responders)) != len(self.responders)
        ):
            return False
        observations = self.active + self.reserved + self.scheduled + self.queued
        if any(observed.identity_is_unknown for observed in observations):
            return False
        if any(observed.matches(expected) for observed in observations for expected in tasks):
            return False
        queued = broker.has_queued_delivery(tasks, timeout_seconds)
        return queued is False
