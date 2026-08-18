from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import TypeVar, cast

import pytest
from neo4j import ManagedTransaction, Record
from src.crm_stage_mapping import (
    CrmStageLifecyclePolicy,
    CrmStageMappingEntry,
    CrmStageMappingPolicy,
    CrmStageTuple,
    mapping_policy_digest,
)
from src.graph.client import Neo4jClient
from src.graph.crm_stage_backfill import CrmStageBackfillRepository
from src.graph.queries.crm_stage_backfill import (
    CLEAR_CRM_STAGE_PROJECTION_ROLLBACK_PROBES,
    COUNT_CRM_STAGE_PROJECTION_ROLLBACK_PROBE_LEAKS,
    CRM_STAGE_CURRENT_EFFECTIVE_ROWS,
    GET_ACTIVE_CRM_STAGE_PROJECTION_IDENTITIES_PAGE,
    PUBLISH_CRM_STAGE_INVALIDATIONS,
    RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS,
    SET_CRM_STAGE_PROJECTION_ROLLBACK_PROBES,
    UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS,
)

T = TypeVar("T")
_NOW = datetime(2026, 8, 18, 7, 0, tzinfo=UTC)


class _Result:
    def __init__(self, rows: list[Record]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[Record]:
        return iter(self._rows)

    def single(self) -> Record | None:
        if len(self._rows) > 1:
            raise AssertionError("single() used for a multi-row scripted result")
        return self._rows[0] if self._rows else None


class _Transaction:
    def __init__(self, client: _Client) -> None:
        self._client = client

    def run(self, query: str, **parameters: object) -> _Result:
        self._client.calls.append((query, parameters))
        queue = self._client.responses.get(query)
        if queue is None or not queue:
            raise AssertionError("unexpected CRM stage repository query")
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, list):
            return _Result(response)
        return _Result([] if response is None else [response])


class _Client:
    def __init__(
        self, responses: dict[str, list[Record | list[Record] | Exception | None]]
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.committed_write_transactions: list[tuple[str, ...]] = []

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        return work(cast(ManagedTransaction, _Transaction(self)))

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        start = len(self.calls)
        result = work(cast(ManagedTransaction, _Transaction(self)))
        self.committed_write_transactions.append(tuple(query for query, _ in self.calls[start:]))
        return result


def _record(**values: object) -> Record:
    return cast(Record, values)


def _source(event_identity: str, stage_id: str, semantic: str) -> Record:
    return _record(
        event_identity=event_identity,
        authority_decision_id=f"decision-{event_identity}",
        authority_head_version=1,
        authority_token=1,
        available_at=_NOW,
        parent_source_system="bitrix_chat",
        parent_source_record_id=f"deal-{event_identity}",
        entity_type_id="2",
        category_id="0",
        stage_id=stage_id,
        source_semantic=semantic,
        event_at=_NOW,
    )


def _policy() -> CrmStageMappingPolicy:
    entries = (
        CrmStageMappingEntry(CrmStageTuple("2", "0", "S1", "P"), "open", "pending"),
        CrmStageMappingEntry(CrmStageTuple("2", "0", "S2", "P"), "excluded", "ignored"),
        CrmStageMappingEntry(CrmStageTuple("2", "0", "S3", "S"), "won", "success"),
    )
    lifecycle = CrmStageLifecyclePolicy(
        first_won="earliest_effective_won",
        repeated_won="retain_all_first_is_conversion",
        reopen="open_after_won_reopens",
        revert="later_effective_state_wins",
        category_migration="preserve_event_category",
        equal_time="authority_sequence_then_history_id",
    )
    provisional = CrmStageMappingPolicy.__new__(CrmStageMappingPolicy)
    object.__setattr__(provisional, "mapping_version", "mapping-v1")
    object.__setattr__(provisional, "policy_version", "policy-v1")
    object.__setattr__(provisional, "entries", entries)
    object.__setattr__(provisional, "lifecycle", lifecycle)
    object.__setattr__(provisional, "digest", "")
    return CrmStageMappingPolicy(
        mapping_version="mapping-v1",
        policy_version="policy-v1",
        entries=entries,
        lifecycle=lifecycle,
        digest=mapping_policy_digest(provisional),
    )


def _rebuild_responses(
    *, publish_failure: Exception | None = None
) -> dict[str, list[Record | list[Record] | Exception | None]]:
    return {
        CRM_STAGE_CURRENT_EFFECTIVE_ROWS: [
            [_source("event-1", "S1", "P"), _source("event-2", "S2", "P")],
            [_source("event-3", "S3", "S")],
            [],
        ],
        UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS: [
            _record(projection_count=1),
            _record(projection_count=1),
        ],
        RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS: [
            _record(retired_count=2, last_event_identity="stale-event-2"),
            _record(retired_count=0, last_event_identity=None),
        ],
        PUBLISH_CRM_STAGE_INVALIDATIONS: (
            [publish_failure]
            if publish_failure is not None
            else [
                _record(published_count=2, last_intent_id="intent-2"),
                _record(published_count=1, last_intent_id="intent-3"),
            ]
        ),
    }


def test_rebuild_pages_source_rows_and_publishes_only_after_all_projection_batches() -> None:
    client = _Client(_rebuild_responses())
    repository = CrmStageBackfillRepository(
        cast(Neo4jClient, client), entity_type_id=2, rebuild_batch_size=2
    )

    result = repository.rebuild(_policy())

    assert result.projection_count == 2
    assert result.retired_count == 2
    assert result.published_invalidation_count == 3
    page_parameters = [
        parameters
        for query, parameters in client.calls
        if query == CRM_STAGE_CURRENT_EFFECTIVE_ROWS
    ]
    assert [item["after_event_identity"] for item in page_parameters] == [
        None,
        "event-2",
        "event-3",
    ]
    assert all(item["limit"] == 2 for item in page_parameters)
    upserts = [
        parameters
        for query, parameters in client.calls
        if query == UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS
    ]
    assert [
        [row["event_identity"] for row in cast(list[dict[str, object]], item["rows"])]
        for item in upserts
    ] == [
        ["event-1"],
        ["event-3"],
    ]
    rebuild_ids = {cast(str, item["rebuild_id"]) for item in upserts}
    retire_parameters = next(
        parameters
        for query, parameters in client.calls
        if query == RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS
    )
    assert rebuild_ids == {retire_parameters["rebuild_id"]}
    retire_batches = [
        parameters
        for query, parameters in client.calls
        if query == RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS
    ]
    assert [item["after_event_identity"] for item in retire_batches] == [
        None,
        "stale-event-2",
    ]
    publication_batches = [
        parameters for query, parameters in client.calls if query == PUBLISH_CRM_STAGE_INVALIDATIONS
    ]
    assert [item["after_intent_id"] for item in publication_batches] == [None, "intent-2"]
    assert all(item["limit"] == 2 for item in retire_batches + publication_batches)
    assert client.committed_write_transactions == [
        (UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS,),
        (UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS,),
        (RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS,),
        (RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS,),
        (PUBLISH_CRM_STAGE_INVALIDATIONS,),
        (PUBLISH_CRM_STAGE_INVALIDATIONS,),
    ]


def test_rebuild_batch_failure_never_starts_retirement_or_publication() -> None:
    responses = _rebuild_responses()
    responses[UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS][1] = RuntimeError("upsert failed")
    client = _Client(responses)
    repository = CrmStageBackfillRepository(
        cast(Neo4jClient, client), entity_type_id=2, rebuild_batch_size=2
    )

    with pytest.raises(RuntimeError, match="upsert failed"):
        repository.rebuild(_policy())

    queries = [query for query, _ in client.calls]
    assert RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS not in queries
    assert PUBLISH_CRM_STAGE_INVALIDATIONS not in queries


def test_rebuild_failure_does_not_commit_invalidation_publication() -> None:
    client = _Client(_rebuild_responses(publish_failure=RuntimeError("publish failed")))
    repository = CrmStageBackfillRepository(
        cast(Neo4jClient, client), entity_type_id=2, rebuild_batch_size=2
    )

    with pytest.raises(RuntimeError, match="publish failed"):
        repository.rebuild(_policy())

    assert (PUBLISH_CRM_STAGE_INVALIDATIONS,) not in client.committed_write_transactions
    assert client.committed_write_transactions[-1] == (RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS,)


def test_rebuild_rerun_uses_a_new_marker_and_preserves_aggregate_counts() -> None:
    responses = _rebuild_responses()
    for query, values in _rebuild_responses().items():
        responses[query].extend(values)
    client = _Client(responses)
    repository = CrmStageBackfillRepository(
        cast(Neo4jClient, client), entity_type_id=2, rebuild_batch_size=2
    )

    first = repository.rebuild(_policy())
    second = repository.rebuild(_policy())

    assert first == second
    upsert_parameters = [
        parameters
        for query, parameters in client.calls
        if query == UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS
    ]
    first_id = upsert_parameters[0]["rebuild_id"]
    second_id = upsert_parameters[2]["rebuild_id"]
    assert first_id != second_id
    assert {item["rebuild_id"] for item in upsert_parameters[:2]} == {first_id}
    assert {item["rebuild_id"] for item in upsert_parameters[2:]} == {second_id}


def test_rollback_rehearsal_pages_candidates_and_clears_each_batch_atomically() -> None:
    client = _Client(
        {
            GET_ACTIVE_CRM_STAGE_PROJECTION_IDENTITIES_PAGE: [
                [_record(event_identity="event-1"), _record(event_identity="event-2")],
                [_record(event_identity="event-3")],
                [],
            ],
            SET_CRM_STAGE_PROJECTION_ROLLBACK_PROBES: [
                _record(candidate_count=2),
                _record(candidate_count=1),
            ],
            CLEAR_CRM_STAGE_PROJECTION_ROLLBACK_PROBES: [
                _record(cleared_count=2),
                _record(cleared_count=1),
            ],
            COUNT_CRM_STAGE_PROJECTION_ROLLBACK_PROBE_LEAKS: [_record(leaked_probe_count=0)],
        }
    )
    repository = CrmStageBackfillRepository(
        cast(Neo4jClient, client), entity_type_id=2, rollback_batch_size=2
    )

    candidate_count, leaked_count = repository.rehearse_rollback("mapping-v1", "probe-1")

    assert (candidate_count, leaked_count) == (3, 0)
    page_parameters = [
        parameters
        for query, parameters in client.calls
        if query == GET_ACTIVE_CRM_STAGE_PROJECTION_IDENTITIES_PAGE
    ]
    assert [item["after_event_identity"] for item in page_parameters] == [
        None,
        "event-2",
        "event-3",
    ]
    assert client.committed_write_transactions == [
        (
            SET_CRM_STAGE_PROJECTION_ROLLBACK_PROBES,
            CLEAR_CRM_STAGE_PROJECTION_ROLLBACK_PROBES,
        ),
        (
            SET_CRM_STAGE_PROJECTION_ROLLBACK_PROBES,
            CLEAR_CRM_STAGE_PROJECTION_ROLLBACK_PROBES,
        ),
    ]
