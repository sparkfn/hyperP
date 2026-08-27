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
            "control_instance_id": "legacy-default",
            "mode": "api",
            "dump_path": None,
            "entity_key": None,
            "stop_requested": True,
            "stop_reason": "operator request",
            "ingest_run_id": "attempt-2",
            "phase": "users",
            "cursor_json": '{"cursor":"next"}',
            "committed_count": 10,
            "duplicate_count": 2,
            "excluded_count": 1,
            "retry_count": 0,
            "checkpointed_at": "2026-08-05T10:00:00Z",
        },
    )

    state = logical_state(record)

    assert state.cursor == {"cursor": "next"}
    assert state.status == "paused_with_checkpoint"
    assert state.generation == 2
    assert state.committed_count == 10
    assert state.duplicate_count == 2
    assert state.excluded_count == 1
    assert state.retry_count == 0


def test_logical_control_queries_assert_control_instance_on_global_ids() -> None:
    from src.graph.queries.ingestion_control import (
        CLAIM_QUEUED_ATTEMPT,
        CREATE_RESUME_ATTEMPT,
        FAIL_LOGICAL_RUN,
        GET_ACTIVE_LOGICAL_RUN,
        REQUEST_LOGICAL_RUN_STOP,
    )

    for query in (
        CLAIM_QUEUED_ATTEMPT,
        CREATE_RESUME_ATTEMPT,
        FAIL_LOGICAL_RUN,
        GET_ACTIVE_LOGICAL_RUN,
        REQUEST_LOGICAL_RUN_STOP,
    ):
        assert "control_instance_id: $control_instance_id" in query


class _NoGraphClient:
    def execute_write(self, _work: object) -> object:
        raise AssertionError("graph access must not occur for a control-identity mismatch")


def test_logical_run_repository_rejects_mismatched_control_before_graph_access() -> None:
    from src.graph.ingestion_control import LogicalRunControl

    repository = LogicalRunControl(_NoGraphClient(), control_instance_id="portal-a")  # type: ignore[arg-type]
    with raises(ValueError, match="does not match repository identity"):
        repository.create_or_reuse(  # type: ignore[arg-type]
            source_key="bitrix_chat",
            control_instance_id="portal-b",
            mode="api",
            dump_path=None,
            entity_key=None,
            idempotency_key="key",
            worker_task_id="worker",
            configuration_fingerprint="fingerprint",
            connector_version="v1",
            checkpoint_schema_version=1,
            initial_checkpoint=None,
        )


class _LogicalRunRecord:
    def __getitem__(self, key: str) -> object:
        values = {
            "logical_run_id": "logical",
            "control_instance_id": "portal-a",
            "ingest_run_id": "run",
            "generation": 1,
            "worker_task_id": "worker",
            "logical_status": "queued",
            "created": True,
        }
        return values[key]


class _LogicalRunResult:
    def single(self) -> _LogicalRunRecord:
        return _LogicalRunRecord()


class _LogicalRunTransaction:
    def __init__(self) -> None:
        self.params: dict[str, object] | None = None

    def run(self, _query: str, **params: object) -> _LogicalRunResult:
        self.params = params
        return _LogicalRunResult()


class _LogicalRunClient:
    def __init__(self) -> None:
        self.transaction = _LogicalRunTransaction()

    def execute_write(self, work: object) -> object:
        return work(self.transaction)  # type: ignore[operator]


def test_logical_run_repository_uses_its_nondefault_identity_when_omitted() -> None:
    from src.graph.ingestion_control import CheckpointDescriptor, LogicalRunControl

    client = _LogicalRunClient()
    repository = LogicalRunControl(client, control_instance_id="portal-a")  # type: ignore[arg-type]
    repository.create_or_reuse(
        source_key="bitrix_chat",
        mode="api",
        dump_path=None,
        entity_key=None,
        idempotency_key="key",
        worker_task_id="worker",
        configuration_fingerprint="fingerprint",
        connector_version="v1",
        checkpoint_schema_version=1,
        initial_checkpoint=CheckpointDescriptor("phase", {}, {}, None, "v1", 1, "boundary"),
    )

    assert client.transaction.params is not None
    assert client.transaction.params["control_instance_id"] == "portal-a"
