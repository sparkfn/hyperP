"""Admission guards prove rejected deliveries cannot construct a connector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import src.bounded_ingestion_task_runtime as task_runtime
from _bounded_ingestion_fixture import (
    FixtureDescriptor,
    MemoryControl,
    context,
    occurrence,
    scope,
    unit,
)
from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.bounded_ingestion_dispatch import dispatch_one
from src.bounded_ingestion_models import (
    AttemptContext,
    OccurrenceContext,
    RunScope,
    SourceBackoffError,
)
from src.connectors.registry import BoundedConnectorRegistry
from src.resumable import CheckpointDescriptor


@dataclass(frozen=True)
class _Settings:
    deployment_environment: str = "test"


class _AdmissionControl(MemoryControl):
    def __init__(self, admitted: bool) -> None:
        super().__init__()
        self.admitted = admitted
        self.admissions = 0

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
        max_graph_writers: int = 1,
    ) -> AttemptContext | None:
        _ = (
            scope,
            occurrence,
            initial_checkpoint,
            worker_task_id,
            now,
            lease_token,
            lease_seconds,
            max_graph_writers,
        )
        self.admissions += 1
        return context() if self.admitted else None


class _RetryAwareAdmissionControl(MemoryControl):
    """Persist only the retry not-before needed to model a scheduled rebind."""

    def __init__(self) -> None:
        super().__init__()
        self.next_eligible_at: datetime | None = None
        self.admission_occurrences: list[datetime] = []

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
        max_graph_writers: int = 1,
    ) -> AttemptContext | None:
        _ = (
            scope,
            initial_checkpoint,
            worker_task_id,
            now,
            lease_token,
            lease_seconds,
            max_graph_writers,
        )
        self.admission_occurrences.append(occurrence.starts_at)
        if self.next_eligible_at is not None and occurrence.starts_at < self.next_eligible_at:
            return None
        return context(occurrence_context=occurrence)

    def pause(
        self,
        attempt: AttemptContext,
        reason: str,
        next_eligible_at: datetime,
    ) -> bool:
        self.next_eligible_at = next_eligible_at
        return super().pause(attempt, reason, next_eligible_at)


def _registry(descriptor: FixtureDescriptor) -> BoundedConnectorRegistry:
    registry = BoundedConnectorRegistry()
    registry.register(descriptor)
    return registry


def test_stale_or_manual_admission_rejection_happens_before_source_factory_or_fetch() -> None:
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = _AdmissionControl(admitted=False)

    result = dispatch_one(
        registry=_registry(descriptor),
        control=control,
        scope=scope(),
        occurrence=occurrence(),
        worker_task_id="task-1",
        budget=BoundedIngestionBudget(max_unit_seconds=60, drain_reserve_seconds=155),
        clock=lambda: datetime(2026, 9, 17, 1, tzinfo=UTC),
    )

    assert result.status == "blocked"
    assert result.failure_category == "lease"
    assert control.admissions == 1
    assert descriptor.create_calls == 0
    assert descriptor.fetch_calls == 0
    assert control.writer_invocations == 0


@pytest.mark.parametrize(
    ("scope_kwargs", "expected_message"),
    [
        ({"connector_version": "other-v1"}, "connector_version_mismatch"),
        ({"checkpoint_schema_version": 2}, "checkpoint_schema_mismatch"),
    ],
)
def test_descriptor_compatibility_mismatch_blocks_before_admission_and_side_effects(
    scope_kwargs: dict[str, object],
    expected_message: str,
) -> None:
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = _AdmissionControl(admitted=True)
    run_scope = scope()
    for key, value in scope_kwargs.items():
        object.__setattr__(run_scope, key, value)

    result = dispatch_one(
        registry=_registry(descriptor),
        control=control,
        scope=run_scope,
        occurrence=occurrence(),
        worker_task_id="task-1",
        budget=BoundedIngestionBudget(max_unit_seconds=60, drain_reserve_seconds=155),
        clock=lambda: datetime(2026, 9, 17, 1, tzinfo=UTC),
    )

    assert result.status == "blocked"
    assert result.safe_message == expected_message
    assert control.admissions == 0
    assert descriptor.initial_checkpoint_calls == 0
    assert descriptor.create_calls == 0


@pytest.mark.parametrize(
    (
        "reset_generation",
        "connector_version",
        "configuration_version",
        "schema_version",
        "fingerprint",
    ),
    [
        (0, "fixture-v1", "fixture-config-v1", 1, "fixture-fingerprint"),
        (1, "other-v1", "fixture-config-v1", 1, "fixture-fingerprint"),
        (1, "fixture-v1", "other-config-v1", 1, "fixture-fingerprint"),
        (1, "fixture-v1", "fixture-config-v1", 2, "fixture-fingerprint"),
        (1, "fixture-v1", "fixture-config-v1", 1, "wrong-fingerprint"),
    ],
)
def test_task_scope_rejects_generation_config_connector_schema_and_fingerprint_mismatches(
    monkeypatch: pytest.MonkeyPatch,
    reset_generation: int,
    connector_version: str,
    configuration_version: str,
    schema_version: int,
    fingerprint: str,
) -> None:
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    registry = _registry(descriptor)
    monkeypatch.setattr(task_runtime, "get_settings", lambda: _Settings())

    with pytest.raises(ValueError):
        task_runtime._scope(
            source_key="fixture",
            entity_key="fixture-entity",
            control_instance_id="fixture-control",
            environment="test",
            reset_generation=reset_generation,
            bounded_mode="bootstrap",
            configuration_fingerprint=fingerprint,
            connector_version=connector_version,
            configuration_version=configuration_version,
            checkpoint_schema_version=schema_version,
            source_window={"snapshot": "fixture-snapshot-1"},
            stream_key="fixture-stream",
            descriptor_registry=registry,
        )

    assert descriptor.create_calls == 0
    assert descriptor.fetch_calls == 0


def test_task_scope_rejects_wrong_environment_before_connector_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    monkeypatch.setattr(task_runtime, "get_settings", lambda: _Settings())

    with pytest.raises(ValueError, match="environment"):
        task_runtime._scope(
            source_key="fixture",
            entity_key="fixture-entity",
            control_instance_id="fixture-control",
            environment="other",
            reset_generation=1,
            bounded_mode="bootstrap",
            configuration_fingerprint="fixture-fingerprint",
            connector_version="fixture-v1",
            configuration_version="fixture-config-v1",
            checkpoint_schema_version=1,
            source_window={"snapshot": "fixture-snapshot-1"},
            stream_key="fixture-stream",
            descriptor_registry=_registry(descriptor),
        )

    assert descriptor.create_calls == 0


def test_dispatch_rejects_descriptor_lifecycle_larger_than_drain_reserve() -> None:
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = _AdmissionControl(admitted=True)
    insufficient = BoundedIngestionBudget(
        max_unit_seconds=60,
        max_graph_transaction_seconds=10,
        drain_reserve_seconds=94,
    )

    result = dispatch_one(
        registry=_registry(descriptor),
        control=control,
        scope=scope(),
        occurrence=occurrence(),
        worker_task_id="task-1",
        budget=insufficient,
        clock=lambda: datetime(2026, 9, 17, 1, tzinfo=UTC),
    )

    assert result.status == "blocked"
    assert result.safe_message == "descriptor_lifecycle_exceeds_drain_reserve"
    assert control.admissions == 0
    assert descriptor.initial_checkpoint_calls == 0
    assert descriptor.create_calls == 0


def test_dispatch_accepts_exact_source_close_and_three_graph_transition_reserve() -> None:
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = _AdmissionControl(admitted=True)
    exact = BoundedIngestionBudget(
        max_unit_seconds=60,
        max_graph_transaction_seconds=10,
        drain_reserve_seconds=95,
    )

    result = dispatch_one(
        registry=_registry(descriptor),
        control=control,
        scope=scope(),
        occurrence=occurrence(),
        worker_task_id="task-1",
        budget=exact,
        clock=lambda: datetime(2026, 9, 17, 1, tzinfo=UTC),
    )

    assert result.status == "completed"
    assert control.admissions == 1
    assert descriptor.initial_checkpoint_calls == 1
    assert descriptor.create_calls == 1


def test_later_source_retry_blocks_intervening_weekly_rebinds_before_source_calls() -> None:
    first = occurrence()
    retry_at = first.next_eligible_at + timedelta(days=10)
    descriptor = FixtureDescriptor(
        {0: unit(0, (("identity-1", "v1"),))},
        fetch_failure=SourceBackoffError(retry_at),
    )
    control = _RetryAwareAdmissionControl()
    budget = BoundedIngestionBudget(max_unit_seconds=60, drain_reserve_seconds=155)

    def clock() -> datetime:
        return first.starts_at

    initial = dispatch_one(
        registry=_registry(descriptor),
        control=control,
        scope=scope(),
        occurrence=first,
        worker_task_id="first",
        budget=budget,
        clock=clock,
    )

    assert initial.status == "paused_with_checkpoint"
    assert control.next_eligible_at == first.next_eligible_at + timedelta(days=14)
    assert descriptor.fetch_calls == 1

    for number, starts_at in enumerate(
        (first.next_eligible_at, first.next_eligible_at + timedelta(days=7)),
        start=1,
    ):
        blocked = dispatch_one(
            registry=_registry(descriptor),
            control=control,
            scope=scope(),
            occurrence=occurrence(starts_at=starts_at),
            worker_task_id=f"intervening-{number}",
            budget=budget,
            clock=lambda starts_at=starts_at: starts_at,
        )
        assert blocked.status == "blocked"
        assert blocked.safe_message == "bounded attempt was not admitted"

    assert control.admission_occurrences == [
        first.starts_at,
        first.next_eligible_at,
        first.next_eligible_at + timedelta(days=7),
    ]
    assert descriptor.fetch_calls == 1
