"""Contracts for connector-neutral checkpointed ingestion primitives."""

from __future__ import annotations

from dataclasses import replace

from pytest import raises
from src.resumable import CheckpointDescriptor, IngestionUnit, checkpoint_can_advance


def test_checkpoint_can_advance_for_durable_dispositions() -> None:
    assert checkpoint_can_advance(
        ("committed", "duplicate", "excluded", "policy_dropped", "durable_retry")
    )


def test_checkpoint_can_advance_past_an_empty_source_page() -> None:
    assert checkpoint_can_advance(()) is True


def test_checkpoint_descriptor_rejects_an_invalid_contract() -> None:
    with raises(ValueError, match="phase"):
        CheckpointDescriptor(
            phase="",
            cursor={},
            source_window={},
            last_committed_record_id=None,
            connector_version="v1",
            schema_version=1,
            replay_boundary="page",
        )


def test_ingestion_unit_rejects_cross_phase_boundaries() -> None:
    before = CheckpointDescriptor(
        phase="page",
        cursor={},
        source_window={},
        last_committed_record_id=None,
        connector_version="v1",
        schema_version=1,
        replay_boundary="page",
    )
    with raises(ValueError, match="phases"):
        IngestionUnit(before, replace(before, phase="next"), ())


def test_checkpoint_descriptor_rejects_boolean_schema_version() -> None:
    with raises(ValueError, match="schema version"):
        CheckpointDescriptor(
            phase="page",
            cursor={},
            source_window={},
            last_committed_record_id=None,
            connector_version="v1",
            schema_version=True,
            replay_boundary="page",
        )
