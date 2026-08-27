"""Focused regression coverage for dormant Bitrix stream admission control."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, cast

from neo4j import ManagedTransaction, Record
from pytest import raises
from src.bitrix_ingestion_models import FenceContext
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import BitrixStreamControl
from src.graph.ingestion_control_models import bitrix_stream_admission
from src.graph.queries.ingestion_control import (
    ADMIT_OR_COALESCE_BITRIX_STREAM,
    CREATE_BITRIX_INGESTION_STREAM_CONSTRAINTS,
)

T = TypeVar("T")


class _Result:
    def __init__(self, record: Record | None) -> None:
        self._record = record

    def single(self) -> Record | None:
        return self._record


class _Transaction:
    def __init__(self, record: Record) -> None:
        self.record = record
        self.query: str | None = None
        self.parameters: dict[str, object] | None = None

    def run(self, query: str, **parameters: object) -> _Result:
        self.query = query
        self.parameters = parameters
        return _Result(self.record)


class _Client:
    def __init__(self, record: Record) -> None:
        self.transaction = _Transaction(record)

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        return work(cast(ManagedTransaction, self.transaction))


def _record(
    *,
    outcome: str = "admitted",
    generation: int = 1,
    token: int = 1,
    stream_key: str = "crm_deals",
) -> Record:
    return cast(
        Record,
        {
            "admission_outcome": outcome,
            "source_key": "bitrix_chat",
            "control_instance_id": "legacy-default",
            "stream_key": stream_key,
            "logical_run_id": "logical-1",
            "ingest_run_id": "ingest-1",
            "attempt_generation": 2,
            "stream_generation": generation,
            "fencing_token": token,
            "worker_task_id": "task-1",
        },
    )


def test_admission_mapping_returns_a_typed_future_fence_context() -> None:
    admission = bitrix_stream_admission(_record(outcome="replaced", generation=3, token=5))

    assert admission.outcome == "replaced"
    assert admission.worker_task_id == "task-1"
    assert admission.fence_context == FenceContext(
        logical_run_id="logical-1",
        ingest_run_id="ingest-1",
        source_key="bitrix_chat",
        stream_key="crm_deals",
        stream_generation=3,
        fencing_token=5,
        attempt_generation=2,
    )


def test_admission_mapping_accepts_the_canonical_stage_history_stream() -> None:
    admission = bitrix_stream_admission(_record(stream_key="crm_stage_history"))

    assert admission.fence_context.stream_key == "crm_stage_history"


def test_admission_rejects_invalid_stream_control_records() -> None:
    with raises(ValueError, match="Unexpected Bitrix stream admission outcome"):
        bitrix_stream_admission(_record(outcome="fenced"))
    with raises(ValueError, match="positive integer"):
        bitrix_stream_admission(_record(generation=0))


def test_control_passes_a_typed_admission_request_without_dispatch_wiring() -> None:
    client = _Client(_record(outcome="coalesced"))
    control = BitrixStreamControl(cast(Neo4jClient, client))

    admission = control.admit_or_coalesce(
        stream_key="crm_deals",
        logical_run_id="logical-2",
        ingest_run_id="ingest-2",
        attempt_generation=4,
        worker_task_id="task-2",
    )

    assert admission.outcome == "coalesced"
    assert client.transaction.query == ADMIT_OR_COALESCE_BITRIX_STREAM
    assert client.transaction.parameters is not None
    assert client.transaction.parameters["source_key"] == "bitrix_chat"
    assert client.transaction.parameters["replace_active"] is False
    assert client.transaction.parameters["stream_key"] == "crm_deals"
    assert client.transaction.parameters["attempt_generation"] == 4


def test_control_rejects_invalid_admission_inputs_before_a_write() -> None:
    client = _Client(_record())
    control = BitrixStreamControl(cast(Neo4jClient, client))

    with raises(ValueError, match="attempt_generation"):
        control.admit_or_coalesce(
            stream_key="crm_deals",
            logical_run_id="logical-2",
            ingest_run_id="ingest-2",
            attempt_generation=0,
            worker_task_id="task-2",
        )

    assert client.transaction.query is None


def test_stream_control_schema_and_query_atomically_coalesce_or_replace() -> None:
    schema = "\n".join(CREATE_BITRIX_INGESTION_STREAM_CONSTRAINTS)

    assert "BitrixIngestionStream" in schema
    assert "(stream.source_key, stream.control_instance_id, stream.stream_key) IS UNIQUE" in schema
    assert "MERGE (stream:BitrixIngestionStream" in ADMIT_OR_COALESCE_BITRIX_STREAM
    assert "stream.logical_run_id = $logical_run_id," in ADMIT_OR_COALESCE_BITRIX_STREAM
    assert (
        "logical_run_id: $logical_run_id, control_instance_id: $control_instance_id"
        in ADMIT_OR_COALESCE_BITRIX_STREAM
    )
    assert "same_attempt" in ADMIT_OR_COALESCE_BITRIX_STREAM
    assert "stream.status IN ['completed', 'terminated', 'superseded']" in (
        ADMIT_OR_COALESCE_BITRIX_STREAM
    )
    assert "WHERE created OR same_attempt OR terminal_stream OR $replace_active" in (
        ADMIT_OR_COALESCE_BITRIX_STREAM
    )
    assert "terminal_stream OR $replace_active AS replace_existing" in (
        ADMIT_OR_COALESCE_BITRIX_STREAM
    )
    assert "WHEN replace_existing THEN current_stream_generation + 1" in (
        ADMIT_OR_COALESCE_BITRIX_STREAM
    )
    assert "WHEN replace_existing THEN current_fencing_token + 1" in (
        ADMIT_OR_COALESCE_BITRIX_STREAM
    )
    assert "WHEN replace_existing THEN 'replaced'" in ADMIT_OR_COALESCE_BITRIX_STREAM
    assert "ELSE 'coalesced'" in ADMIT_OR_COALESCE_BITRIX_STREAM
