"""Typed boundary tests for durable logical-run checkpoint state."""

from __future__ import annotations

from typing import cast

from neo4j import Record
from pytest import raises
from src.graph.ingestion_control_models import (
    decode_json_object,
    encode_json,
    logical_state,
)


def test_checkpoint_json_round_trips_nested_values_deterministically() -> None:
    value = {"cursor": "next", "window": {"upper": 42}, "flags": [True, None]}

    encoded = encode_json(value)

    assert encoded == '{"cursor":"next","flags":[true,null],"window":{"upper":42}}'
    assert decode_json_object(encoded) == value


def test_checkpoint_json_rejects_non_standard_numbers() -> None:
    with raises(ValueError):
        encode_json({"cursor": float("nan")})
    with raises(ValueError):
        decode_json_object('{"cursor":NaN}')


def test_logical_state_decodes_the_current_checkpoint_cursor() -> None:
    record = cast(
        Record,
        {
            "logical_run_id": "logical-1",
            "status": "paused_with_checkpoint",
            "generation": 2,
            "source_key": "fundbox",
            "mode": "api",
            "dump_path": None,
            "entity_key": None,
            "stop_requested": True,
            "stop_reason": "operator request",
            "ingest_run_id": "attempt-2",
            "phase": "users",
            "cursor_json": '{"cursor":"next"}',
            "checkpointed_at": "2026-08-05T10:00:00Z",
        },
    )

    state = logical_state(record)

    assert state.cursor == {"cursor": "next"}
    assert state.status == "paused_with_checkpoint"
    assert state.generation == 2
