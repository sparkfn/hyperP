"""#272 API admission happens before creation or Celery publication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from unittest.mock import patch

import pytest
from fastapi import HTTPException, Request
from src.repositories.neo4j.ingest import _create_run_tx, _ingest_records_tx
from src.repositories.protocols.ingest import (
    BitrixApiAdmissionError,
    IngestRepository,
    IngestRunCreationResult,
    IngestRunResponse,
)
from src.routes.ingest import create_ingest_run
from src.types_requests import IngestRecord, IngestRunCreateRequest


class _Result:
    def __init__(self, row: object) -> None:
        self._row = row

    async def single(self) -> object:
        return self._row

    def __aiter__(self) -> object:
        async def rows() -> object:
            if isinstance(self._row, list):
                for row in self._row:
                    yield row

        return rows()


class _Transaction:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    async def run(self, query: str, **_params: object) -> _Result:
        self.queries.append(query)
        return _Result(self.rows.pop(0))


def _constraint_inventory() -> list[dict[str, object]]:
    from src.repositories.neo4j.ingestion_control_schema import _REQUIRED_SPECS

    return [
        {
            "name": spec.name,
            "type": "UNIQUENESS",
            "entityType": "NODE",
            "labelsOrTypes": [spec.label],
            "properties": list(spec.properties),
        }
        for spec in _REQUIRED_SPECS
    ]


@pytest.mark.asyncio
async def test_bitrix_repository_admission_precedes_run_creation() -> None:
    transaction = _Transaction([None])

    with pytest.raises(BitrixApiAdmissionError):
        await _create_run_tx(
            cast(object, transaction),
            "bitrix_chat",
            "manual",
            "batch",
            None,
            {},
            "idem",
        )

    assert len(transaction.queries) == 1
    assert transaction.queries[0].startswith("SHOW CONSTRAINTS")


@dataclass
class _BlockedRepository:
    async def create_run(
        self,
        source_key: str,
        run_type: str,
        mode: str,
        dump_path: str | None,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> IngestRunCreationResult | None:
        del source_key, run_type, mode, dump_path, metadata, idempotency_key
        raise BitrixApiAdmissionError("blocked")


@pytest.mark.asyncio
async def test_blocked_api_run_never_calls_celery() -> None:
    request = Request({"type": "http", "headers": []})
    body = IngestRunCreateRequest(run_type="manual", mode="batch")
    repo = cast(IngestRepository, _BlockedRepository())

    with (
        patch("src.routes.ingest.enqueue_ingestion_run") as enqueue,
        pytest.raises(HTTPException) as exc_info,
    ):
        await create_ingest_run(
            "bitrix_chat",
            body,
            request,
            "idempotency",
            cast(object, None),
            repo,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"]["code"] == "control_not_ready"
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_ready_api_run_publishes_legacy_payload_without_control_kwarg() -> None:
    @dataclass
    class _ReadyRepository:
        async def create_run(
            self,
            source_key: str,
            run_type: str,
            mode: str,
            dump_path: str | None,
            metadata: dict[str, str],
            idempotency_key: str,
        ) -> IngestRunCreationResult | None:
            del source_key, run_type, mode, dump_path, metadata, idempotency_key
            return IngestRunCreationResult(
                run=IngestRunResponse("run-1", "started", "batch"),
                created=True,
            )

    request = Request({"type": "http", "headers": []})
    body = IngestRunCreateRequest(run_type="manual", mode="batch")
    with patch("src.routes.ingest.enqueue_ingestion_run") as enqueue:
        await create_ingest_run(
            "bitrix_chat",
            body,
            request,
            "idempotency",
            cast(object, None),
            cast(IngestRepository, _ReadyRepository()),
        )

    enqueue.assert_called_once_with(
        "bitrix_chat",
        "batch",
        dump_path=None,
        ingest_run_id="run-1",
    )


def test_api_admission_requires_exact_registry_relationship_and_dispatch_cardinality() -> None:
    from src.graph.queries.ingestion import CHECK_BITRIX_API_ADMISSION, CREATE_INGEST_RUN

    for query in (CHECK_BITRIX_API_ADMISSION, CREATE_INGEST_RUN):
        assert "size(migrations) = 1" in query
        assert "size(instances) = 1" in query
        assert "relationship_counts = [1]" in query
        assert "canonical_relationship_counts = [1]" in query
        assert "size([(instance)-[:INSTANCE_OF]->(:SourceSystem) | 1])" in query
        assert "size(dispatches) <= 1" in query
        assert "collect(DISTINCT dispatch)" in query


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguity", ["duplicate_instance_relationship", "duplicate_dispatch"])
async def test_ambiguous_bitrix_admission_never_reaches_run_creation(
    ambiguity: str,
) -> None:
    """A no-row exact-cardinality admission blocks writes for either ambiguity shape."""
    transaction = _Transaction([_constraint_inventory(), None])

    with pytest.raises(BitrixApiAdmissionError):
        await _create_run_tx(
            cast(object, transaction),
            "bitrix_chat",
            "manual",
            "batch",
            None,
            {},
            f"idem-{ambiguity}",
        )

    assert len(transaction.queries) == 2
    assert transaction.queries[0].startswith("SHOW CONSTRAINTS")
    assert "relationship_counts = [1]" in transaction.queries[1]
    assert "size(dispatches) <= 1" in transaction.queries[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_missing_or_malformed_schema_blocks_api_run_before_admission_or_write(
    malformed: bool,
) -> None:
    inventory = _constraint_inventory()
    if malformed:
        inventory[0]["properties"] = ["worker_task_id", "control_instance_id"]
    else:
        inventory.pop()
    transaction = _Transaction([inventory])

    with pytest.raises(BitrixApiAdmissionError, match="schema is not ready"):
        await _create_run_tx(
            cast(object, transaction),
            "bitrix_chat",
            "manual",
            "batch",
            None,
            {},
            "idem",
        )

    assert len(transaction.queries) == 1
    assert transaction.queries[0].startswith("SHOW CONSTRAINTS")


@pytest.mark.asyncio
async def test_missing_schema_blocks_inline_domain_writes() -> None:
    transaction = _Transaction([[]])
    record = IngestRecord(
        source_record_id="record-1",
        source_record_version="1",
        record_type="identity",
        observed_at="2026-08-27T00:00:00Z",
        record_hash="sha256:record",
        raw_payload={},
    )

    with pytest.raises(BitrixApiAdmissionError, match="schema is not ready"):
        await _ingest_records_tx(
            cast(object, transaction),
            "bitrix_chat",
            "manual",
            None,
            [record],
        )

    assert len(transaction.queries) == 1
    assert transaction.queries[0].startswith("SHOW CONSTRAINTS")


@pytest.mark.asyncio
async def test_alternate_name_for_retired_identity_blocks_api_run_before_admission() -> None:
    inventory = _constraint_inventory()
    inventory.append(
        {
            "name": "unexpected_control_identity",
            "type": "UNIQUENESS",
            "entityType": "NODE",
            "labelsOrTypes": ["IngestRun"],
            "properties": ["source_key", "idempotency_key"],
        }
    )
    transaction = _Transaction([inventory])

    with pytest.raises(BitrixApiAdmissionError, match="schema is not ready"):
        await _create_run_tx(
            cast(object, transaction),
            "bitrix_chat",
            "manual",
            "batch",
            None,
            {},
            "idem",
        )

    assert transaction.queries == [transaction.queries[0]]
    assert transaction.queries[0].startswith("SHOW CONSTRAINTS")
