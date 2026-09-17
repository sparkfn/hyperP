"""Reusable in-memory fixtures for durable bounded-ingestion conformance tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from neo4j import ManagedTransaction
from pydantic.types import JsonValue
from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedUnit,
    OccurrenceContext,
    RetryObligation,
    RunScope,
    UnitApplyResult,
    Usage,
)
from src.resumable import CheckpointDescriptor, IngestionUnit


class SimulatedKillError(RuntimeError):
    """A process loss injected at a named bounded-commit boundary."""


@dataclass
class FakeClock:
    """A deterministic UTC clock whose value can be moved by a test."""

    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class FakeShutdown:
    """Cooperative SIGTERM state used by the runner."""

    is_requested: bool = False

    def requested(self) -> bool:
        return self.is_requested


class _NoopWriter:
    def apply(
        self,
        _tx: ManagedTransaction,
        _context: AttemptContext,
        unit: BoundedUnit,
    ) -> UnitApplyResult:
        return UnitApplyResult(tuple("committed" for _ in unit.unit.records))


class FixtureConnector:
    """A connector that returns one scripted unit for the durable checkpoint."""

    def __init__(self, descriptor: FixtureDescriptor) -> None:
        self._descriptor = descriptor

    def validate_checkpoint(self, _checkpoint: CheckpointDescriptor) -> str:
        return self._descriptor.compatibility

    def fetch_one_unit(
        self,
        checkpoint: CheckpointDescriptor,
        _context: AttemptContext,
    ) -> BoundedUnit:
        self._descriptor.fetch_calls += 1
        if self._descriptor.fetch_failure is not None:
            raise self._descriptor.fetch_failure
        page = checkpoint.cursor.get("page")
        if not isinstance(page, int):
            raise AssertionError("fixture checkpoint page must be an integer")
        try:
            return self._descriptor.units[page]
        except KeyError as exc:
            raise AssertionError(f"no fixture unit exists at page {page}") from exc

    def close(self) -> None:
        self._descriptor.close_calls += 1


class FixtureDescriptor:
    """Trusted test descriptor with bounded limits and visible factory side effects."""

    source_key = "fixture"
    connector_version = "fixture-v1"
    configuration_version = "fixture-config-v1"
    checkpoint_schema_version = 1
    supports_bootstrap = True
    supports_delta = True
    supports_one_time = True
    max_records_per_unit = 3
    max_source_requests_per_unit = 1
    max_bytes_per_unit = 1_000
    max_extraction_calls_per_unit = 1
    writer = _NoopWriter()

    def __init__(
        self,
        units: dict[int, BoundedUnit],
        *,
        compatibility: str = "compatible",
        fetch_failure: Exception | None = None,
    ) -> None:
        self.units = units
        self.compatibility = compatibility
        self.fetch_failure = fetch_failure
        self.create_calls = 0
        self.fetch_calls = 0
        self.close_calls = 0
        self.initial_checkpoint_calls = 0

    def initial_checkpoint(
        self,
        scope: RunScope,
        _occurrence: OccurrenceContext | None,
    ) -> CheckpointDescriptor:
        self.initial_checkpoint_calls += 1
        return checkpoint(0, source_window=scope.source_window)

    def create(self, _context: AttemptContext) -> FixtureConnector:
        self.create_calls += 1
        return FixtureConnector(self)


@dataclass
class MemoryControl:
    """A graph-control fake with receipts, output versions, retries, and watermarks."""

    reserve_allowed: bool = True
    commit_allowed: bool = True
    crash_after_writer: bool = False
    retry_by_replay: dict[str, tuple[RetryObligation, ...]] = field(default_factory=dict)
    reservations: list[Usage] = field(default_factory=list)
    pauses: list[tuple[str, datetime]] = field(default_factory=list)
    failures: list[tuple[str, str, datetime]] = field(default_factory=list)
    receipts: dict[str, UnitApplyResult] = field(default_factory=dict)
    active_versions: dict[str, str] = field(default_factory=dict)
    checkpoint_pages: list[int] = field(default_factory=list)
    finalized: int = 0
    terminal_watermark: bool = False
    writer_invocations: int = 0

    def reserve_usage(
        self,
        _context: AttemptContext,
        requested: Usage,
        _budget: BoundedIngestionBudget,
    ) -> bool:
        self.reservations.append(requested)
        return self.reserve_allowed

    def commit_unit(
        self,
        _context: AttemptContext,
        unit: BoundedUnit,
        _writer: _NoopWriter,
    ) -> UnitApplyResult | None:
        if not self.commit_allowed:
            return None
        existing = self.receipts.get(unit.replay_id)
        if existing is not None:
            return existing
        output_before = dict(self.active_versions)
        checkpoint_before = list(self.checkpoint_pages)
        self.writer_invocations += 1
        for record in unit.unit.records:
            identity = record.get("id")
            version = record.get("version")
            if not isinstance(identity, str) or not isinstance(version, str):
                raise AssertionError("fixture records require string id and version")
            self.active_versions[identity] = version
        page = unit.unit.checkpoint_after.cursor.get("page")
        if not isinstance(page, int):
            raise AssertionError("fixture checkpoint page must be an integer")
        self.checkpoint_pages.append(page)
        if self.crash_after_writer:
            self.active_versions = output_before
            self.checkpoint_pages = checkpoint_before
            raise SimulatedKillError("killed after writer before checkpoint commit")
        result = UnitApplyResult(
            dispositions=tuple("committed" for _ in unit.unit.records),
            retry_obligations=self.retry_by_replay.get(unit.replay_id, ()),
        )
        self.receipts[unit.replay_id] = result
        return result

    def pause(
        self,
        _context: AttemptContext,
        reason: str,
        next_eligible_at: datetime,
    ) -> bool:
        self.pauses.append((reason, next_eligible_at))
        return True

    def fail(
        self,
        _context: AttemptContext,
        category: str,
        message: str,
        next_eligible_at: datetime,
    ) -> bool:
        self.failures.append((category, message, next_eligible_at))
        return True

    def finalize(self, _context: AttemptContext) -> bool:
        self.finalized += 1
        if self.retry_backlog:
            return False
        self.terminal_watermark = True
        return True

    @property
    def retry_backlog(self) -> int:
        return sum(len(result.retry_obligations) for result in self.receipts.values())


def checkpoint(
    page: int,
    *,
    source_window: dict[str, JsonValue] | None = None,
) -> CheckpointDescriptor:
    """Build the stable page checkpoint used by the synthetic source."""

    return CheckpointDescriptor(
        phase="records",
        cursor={"page": page},
        source_window=source_window or {"snapshot": "fixture-snapshot-1"},
        last_committed_record_id=None,
        connector_version="fixture-v1",
        schema_version=1,
        replay_boundary="page",
    )


def unit(
    page: int,
    records: tuple[tuple[str, str], ...],
    *,
    terminal: bool = False,
    replay_id: str | None = None,
    usage: Usage | None = None,
    source_window: dict[str, JsonValue] | None = None,
) -> BoundedUnit:
    """Create one page that retains a deterministic replay identity."""

    before = checkpoint(page, source_window=source_window)
    after = checkpoint(page + 1, source_window=source_window)
    payload: tuple[dict[str, JsonValue], ...] = tuple(
        {"id": identity, "version": version} for identity, version in records
    )
    return BoundedUnit(
        unit=IngestionUnit(before, after, payload),
        replay_id=replay_id or f"page-{page}",
        usage=usage or Usage(records=len(payload), source_requests=1, pages=1, bytes_read=100),
        terminal=terminal,
    )


def occurrence(
    *,
    starts_at: datetime = datetime(2026, 9, 17, 1, tzinfo=UTC),
    drain_after: timedelta = timedelta(hours=13, minutes=55),
) -> OccurrenceContext:
    """Return the approved Thursday 09:00--23:00 Asia/Singapore occurrence."""

    cutoff_at = starts_at + timedelta(hours=14)
    return OccurrenceContext(
        occurrence_id="thu-2026-09-17",
        starts_at=starts_at,
        drain_starts_at=starts_at + drain_after,
        cutoff_at=cutoff_at,
        next_eligible_at=starts_at + timedelta(days=7),
    )


def scope(
    *,
    reset_generation: int = 1,
    source_window: dict[str, JsonValue] | None = None,
) -> RunScope:
    """Return one exact source/control/environment scope for a fixture run."""

    return RunScope(
        environment="test",
        reset_generation=reset_generation,
        source_key="fixture",
        control_instance_id="fixture-control",
        entity_key="fixture-entity",
        stream_key="fixture-stream",
        mode="bootstrap",
        configuration_fingerprint="fixture-fingerprint",
        connector_version="fixture-v1",
        checkpoint_schema_version=1,
        source_window=source_window or {"snapshot": "fixture-snapshot-1"},
    )


def context(
    page: int = 0,
    *,
    attempt_generation: int = 1,
    fence: int = 1,
    occurrence_context: OccurrenceContext | None = None,
    run_scope: RunScope | None = None,
    usage: Usage | None = None,
    reserved_usage: Usage | None = None,
) -> AttemptContext:
    """Return a fresh bounded attempt sharing the immutable fixture run scope."""

    selected_scope = run_scope or scope()
    return AttemptContext(
        logical_run_id="logical-fixture",
        ingest_run_id=f"attempt-{attempt_generation}",
        worker_task_id=f"task-{attempt_generation}",
        attempt_generation=attempt_generation,
        fencing_token=fence,
        lease_token=f"lease-{attempt_generation}",
        scope=selected_scope,
        occurrence=occurrence_context or occurrence(),
        checkpoint=checkpoint(page, source_window=selected_scope.source_window),
        usage=usage or Usage(),
        reserved_usage=reserved_usage or Usage(),
    )
