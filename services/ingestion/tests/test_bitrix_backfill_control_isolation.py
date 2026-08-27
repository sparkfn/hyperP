"""Namespace isolation for #272 Bitrix backfill topology."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, cast

import pytest
from src.bitrix_backfill_models import GenerationProvenance
from src.bitrix_ingestion_models import FenceContext
from src.graph.bitrix_backfill import BitrixBackfillRepository
from src.graph.client import Neo4jClient
from src.graph.queries.bitrix_backfill import (
    ALLOCATE_BITRIX_BACKFILL_GENERATION,
    ATTACH_BACKFILL_LOGICAL_RUN,
    GET_ACTIVE_BITRIX_SUCCESSOR_SCHEDULE,
    UPSERT_BITRIX_BACKFILL_COVERAGE,
)

T = TypeVar("T")


class _Result:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row


class _Transaction:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> _Result:
        self.calls.append((query, parameters))
        if query == ALLOCATE_BITRIX_BACKFILL_GENERATION:
            return _Result({"created": True})
        raise AssertionError("unexpected query")


class _Client:
    def __init__(self) -> None:
        self.transaction = _Transaction()

    def execute_write(self, work: Callable[[_Transaction], T]) -> T:
        return work(self.transaction)


def _provenance() -> GenerationProvenance:
    return GenerationProvenance("sha", "image", "config", "contract", "boundary")


def _fence(control_instance_id: str) -> FenceContext:
    return FenceContext(
        logical_run_id="logical",
        ingest_run_id="ingest",
        source_key="bitrix_chat",
        stream_key="crm_deals",
        stream_generation=1,
        fencing_token=1,
        attempt_generation=1,
        control_instance_id=control_instance_id,
    )


def test_same_generation_id_is_scoped_by_repository_control_instance() -> None:
    first = _Client()
    second = _Client()
    assert BitrixBackfillRepository(cast(Neo4jClient, first), "portal-one").allocate_generation(
        "generation-7", _provenance()
    )
    assert BitrixBackfillRepository(cast(Neo4jClient, second), "portal-two").allocate_generation(
        "generation-7", _provenance()
    )
    assert first.transaction.calls[0][1]["control_instance_id"] == "portal-one"
    assert second.transaction.calls[0][1]["control_instance_id"] == "portal-two"
    assert "control_instance_id: $control_instance_id" in ALLOCATE_BITRIX_BACKFILL_GENERATION


def test_mismatched_fence_is_rejected_before_known_owner_mutation() -> None:
    client = _Client()
    repository = BitrixBackfillRepository(cast(Neo4jClient, client), "portal-one")
    with pytest.raises(ValueError, match="does not match"):
        repository.materialize_known_owner_set(
            generation_id="generation-7",
            membership_set_id="owners",
            fence_context=_fence("portal-two"),
        )
    assert client.transaction.calls == []


def test_topology_queries_require_the_control_namespace() -> None:
    for query in (
        ATTACH_BACKFILL_LOGICAL_RUN,
        UPSERT_BITRIX_BACKFILL_COVERAGE,
        GET_ACTIVE_BITRIX_SUCCESSOR_SCHEDULE,
    ):
        assert "$control_instance_id" in query
