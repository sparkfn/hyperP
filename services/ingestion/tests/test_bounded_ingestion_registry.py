"""Trusted descriptor registration and retained bounded-dispatch fences."""

from __future__ import annotations

import pytest
from _bounded_ingestion_fixture import FixtureDescriptor, unit
from celery.exceptions import Reject
from src.connectors.registry import BoundedConnectorRegistry
from src.graph.queries.bounded_ingestion_control import CLAIM_BOUNDED_ATTEMPT
from src.graph.queries.ingestion_control import LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE
from src.ingestion_config import IngestionConfig, ScheduledIngestionConfig


def test_duplicate_malformed_and_mode_unsupported_descriptors_fail_closed() -> None:
    registry = BoundedConnectorRegistry()
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),))})
    registry.register(descriptor)

    with pytest.raises(ValueError, match="duplicated"):
        registry.register(descriptor)
    with pytest.raises(LookupError, match="requested mode"):
        descriptor.supports_delta = False
        registry.require("fixture", "delta")

    malformed = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),))})
    malformed.source_key = " "
    with pytest.raises(ValueError, match="empty"):
        BoundedConnectorRegistry().register(malformed)

    bad_limit = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),))})
    bad_limit.max_bytes_per_unit = 0
    with pytest.raises(ValueError, match="limits"):
        BoundedConnectorRegistry().register(bad_limit)

    unsupported = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),))})
    unsupported.supports_bootstrap = False
    unsupported.supports_delta = False
    unsupported.supports_one_time = False
    with pytest.raises(ValueError, match="supports no execution mode"):
        BoundedConnectorRegistry().register(unsupported)


def test_registry_never_constructs_a_connector_while_registering_or_rejecting() -> None:
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),))})
    registry = BoundedConnectorRegistry()

    registry.register(descriptor)
    assert registry.registered_sources() == ("fixture",)
    assert descriptor.create_calls == 0
    assert descriptor.fetch_calls == 0

    with pytest.raises(LookupError, match="no bounded descriptor"):
        registry.require("unknown-source", "bootstrap")
    assert descriptor.create_calls == 0
    assert descriptor.fetch_calls == 0


def test_claim_contract_keeps_active_source_reset_generation_occurrence_and_lease_fences() -> None:
    required_fragments = (
        "SourceSystem {source_key: $source_key, is_active: true}",
        "IngestionResetGeneration",
        "generation: $reset_generation",
        "status: 'active'",
        "logical.occurrence_starts_at",
        "logical.drain_starts_at",
        "logical.manual_pause",
        "logical.active_generation",
        "logical.bounded_fencing_token",
        "logical.worker_task_id",
        "logical.lease_token",
    )

    for fragment in required_fragments:
        assert fragment in CLAIM_BOUNDED_ATTEMPT


def test_scheduled_task_without_complete_bounded_context_rejects_before_runtime_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import tasks

    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(scheduled_ingestion=ScheduledIngestionConfig(enabled=True)),
    )
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: pytest.fail("incomplete bounded context must reject before runtime setup"),
    )

    with pytest.raises(Reject, match="complete bounded ingestion context"):
        tasks.run_ingestion_task.run("fixture", "api", scheduled_dispatch=True)


def test_bitrix_fence_contract_retains_all_attempt_and_stream_identity_dimensions() -> None:
    for fragment in (
        "source_key: $source_key",
        "control_instance_id: $control_instance_id",
        "stream_key: $stream_key",
        "logical_run_id: $logical_run_id",
        "ingest_run_id: $ingest_run_id",
        "attempt_generation: $attempt_generation",
        "stream_generation: $stream_generation",
        "fencing_token: $fencing_token",
    ):
        assert fragment in LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE
