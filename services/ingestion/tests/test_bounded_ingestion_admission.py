"""Admission guards prove rejected deliveries cannot construct a connector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

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
from src.bounded_ingestion_models import AttemptContext, OccurrenceContext, RunScope
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
    ) -> AttemptContext | None:
        _ = (
            scope,
            occurrence,
            initial_checkpoint,
            worker_task_id,
            now,
            lease_token,
            lease_seconds,
        )
        self.admissions += 1
        return context() if self.admitted else None


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
        budget=BoundedIngestionBudget(max_unit_seconds=60, drain_reserve_seconds=120),
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
        budget=BoundedIngestionBudget(max_unit_seconds=60, drain_reserve_seconds=120),
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
