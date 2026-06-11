from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import pytest
from neo4j import AsyncManagedTransaction
from src.graph.queries import (
    CHECK_BOTH_PERSONS_ACTIVE,
    CHECK_EXISTING_LOCK,
    CHECK_NO_MATCH_LOCK,
    CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
    CREATE_PERSON_PAIR_LOCK,
    CREATE_UNMERGE_AUDIT,
    DELETE_LOCK,
    EXECUTE_MANUAL_MERGE,
    FLAG_AFFECTED_RECORDS_FOR_REVIEW,
    GET_UNMERGE_TARGET,
    REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    REVERT_MERGE,
    REVERT_PERSON_PAIR_CASE_CLOSURES,
    REVERT_RECORD_PERSON_CASE_REDIRECTS,
)
from src.repositories.neo4j import merge as merge_module
from src.repositories.neo4j.merge import (
    Neo4jMergeRepository,
    _create_lock_tx,
    _delete_lock_tx,
    _manual_merge_tx,
    _unmerge_tx,
)
from src.repositories.protocols.merge import GoldenProfileSelection, MergeOutcome

type RecordValue = str | bool | int | list[GoldenProfileSelection] | None
type Record = Mapping[str, RecordValue]
type Params = Mapping[str, RecordValue]
type WriteFn = Callable[..., Awaitable[object]]


class _AsyncResult:
    def __init__(self, record: Record | None) -> None:
        self._record = record

    async def single(self) -> Record | None:
        return self._record


@dataclass(frozen=True)
class _Call:
    query: str
    params: Params


class _Tx:
    def __init__(self, records: Sequence[Record | None]) -> None:
        self._records: list[Record | None] = list(records)
        self.calls: list[_Call] = []

    async def run(self, query: str, **params: RecordValue) -> _AsyncResult:
        self.calls.append(_Call(query=query, params=params))
        record = self._records.pop(0) if self._records else None
        return _AsyncResult(record)


class _Session:
    def __init__(self, tx_results: Sequence[object]) -> None:
        self._tx_results: list[object] = list(tx_results)
        self.write_calls: list[tuple[object, tuple[object, ...]]] = []

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def execute_write(self, fn: WriteFn, *args: object) -> object:
        self.write_calls.append((fn, args))
        return self._tx_results.pop(0)


class _SessionFactory:
    def __init__(self, sessions: Sequence[_Session]) -> None:
        self.sessions: list[_Session] = list(sessions)

    def __call__(self, *, write: bool = False) -> _Session:
        assert write is True
        return self.sessions.pop(0)


@pytest.mark.asyncio
async def test_manual_merge_returns_blocked_when_active_lock_exists() -> None:
    tx = _Tx([{"is_locked": True}])

    outcome = await _manual_merge_tx(
        cast(AsyncManagedTransaction, tx),
        "person-b",
        "person-a",
        "same customer",
        "admin@example.com",
    )

    assert outcome.blocked is True
    assert outcome.not_found is False
    assert outcome.merge_event_id is None
    assert [call.query for call in tx.calls] == [CHECK_NO_MATCH_LOCK]
    assert tx.calls[0].params == {"left": "person-a", "right": "person-b"}


@pytest.mark.asyncio
async def test_manual_merge_success_returns_merge_event_id() -> None:
    tx = _Tx(
        [
            {"is_locked": False},
            {"absorbed": "person-a", "survivor": "person-b"},
            {"merge_event_id": "merge-1"},
        ]
    )

    outcome = await _manual_merge_tx(
        cast(AsyncManagedTransaction, tx),
        "person-a",
        "person-b",
        "same customer",
        "admin@example.com",
    )

    assert outcome.merge_event_id == "merge-1"
    assert outcome.blocked is False
    assert outcome.not_found is False
    assert [call.query for call in tx.calls] == [
        CHECK_NO_MATCH_LOCK,
        CHECK_BOTH_PERSONS_ACTIVE,
        EXECUTE_MANUAL_MERGE,
        CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
        REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    ]
    assert tx.calls[2].params == {
        "from_id": "person-a",
        "to_id": "person-b",
        "reason": "same customer",
        "actor_id": "admin@example.com",
    }
    # Side-effects target the absorbed person and survivor, stamped with the event.
    assert tx.calls[4].params == {
        "absorbed_id": "person-a",
        "survivor_id": "person-b",
        "merge_event_id": "merge-1",
    }


def test_merge_event_queries_store_metadata_as_string_properties() -> None:
    assert "metadata: '{}'" in EXECUTE_MANUAL_MERGE
    assert "metadata: {" not in EXECUTE_MANUAL_MERGE
    assert "metadata: {" not in CREATE_UNMERGE_AUDIT


def test_manual_merge_copies_relationship_properties_before_deleting_relationships() -> None:
    # Properties must be captured into *_props while the old relationship still
    # exists — i.e. before the FOREACH that deletes it.
    def captured_before_delete(capture: str, delete: str) -> bool:
        return EXECUTE_MANUAL_MERGE.index(capture) < EXECUTE_MANUAL_MERGE.index(delete)

    assert captured_before_delete("properties(old_id) AS old_id_props", "DELETE old_id")
    assert "CREATE (survivor)-[new_id:IDENTIFIED_BY]->(id)" in EXECUTE_MANUAL_MERGE
    assert "SET new_id = old_id_props" in EXECUTE_MANUAL_MERGE
    assert captured_before_delete("properties(old_addr) AS old_addr_props", "DELETE old_addr")
    assert "CREATE (survivor)-[new_addr:LIVES_AT]->(addr)" in EXECUTE_MANUAL_MERGE
    assert "SET new_addr = old_addr_props" in EXECUTE_MANUAL_MERGE
    assert captured_before_delete("properties(old_fact) AS old_fact_props", "DELETE old_fact")
    assert "CREATE (survivor)-[new_fact:HAS_FACT]->(sr_fact)" in EXECUTE_MANUAL_MERGE
    assert "SET new_fact = old_fact_props" in EXECUTE_MANUAL_MERGE
    assert "old_id.is_verified" not in EXECUTE_MANUAL_MERGE
    assert "old_addr.is_active" not in EXECUTE_MANUAL_MERGE
    assert "old_fact.attribute_name" not in EXECUTE_MANUAL_MERGE


def test_revert_merge_restores_connections_moved_by_merge_event() -> None:
    assert "AFFECTED_RECORD" in REVERT_MERGE
    assert "DELETE survivor_link" in REVERT_MERGE
    assert "CREATE (sr)-[:LINKED_TO {linked_at: datetime()}]->(absorbed)" in REVERT_MERGE
    assert "DELETE survivor_id" in REVERT_MERGE
    assert "CREATE (absorbed)-[:IDENTIFIED_BY" in REVERT_MERGE
    assert "DELETE survivor_addr" in REVERT_MERGE
    assert "CREATE (absorbed)-[:LIVES_AT" in REVERT_MERGE
    assert "DELETE survivor_fact" in REVERT_MERGE
    assert "CREATE (absorbed)-[:HAS_FACT" in REVERT_MERGE
    assert "DELETE survivor_k_out" in REVERT_MERGE
    assert "CREATE (absorbed)-[:KNOWS" in REVERT_MERGE
    assert "DELETE survivor_k_in" in REVERT_MERGE
    assert "CREATE (k_other_in)-[:KNOWS" in REVERT_MERGE


def test_revert_merge_deduplicates_rows_between_relationship_rewrites() -> None:
    assert (
        REVERT_MERGE.count(
            "WITH DISTINCT absorbed, mi, current_survivor, current_survivor_id, affected_pks"
        )
        == 5
    )
    assert "WITH DISTINCT absorbed, mi, current_survivor_id\nDELETE mi" in REVERT_MERGE


@pytest.mark.asyncio
async def test_unmerge_reactivates_absorbed_flags_records_and_audits() -> None:
    tx = _Tx(
        [
            {"absorbed_id": "person-a", "survivor_id": "person-b"},
            {"removed_count": 1, "current_survivor_id": "person-b"},
            None,
            None,
        ]
    )

    result = await _unmerge_tx(
        cast(AsyncManagedTransaction, tx),
        "merge-1",
        "false merge",
        "admin@example.com",
    )

    assert result == ("person-a", "person-b")
    assert [call.query for call in tx.calls] == [
        GET_UNMERGE_TARGET,
        REVERT_MERGE,
        CREATE_UNMERGE_AUDIT,
        FLAG_AFFECTED_RECORDS_FOR_REVIEW,
        REVERT_RECORD_PERSON_CASE_REDIRECTS,
        REVERT_PERSON_PAIR_CASE_CLOSURES,
    ]
    assert tx.calls[1].params == {
        "absorbed_id": "person-a",
        "survivor_id": "person-b",
    }
    assert tx.calls[3].params == {"merge_event_id": "merge-1"}


@pytest.mark.asyncio
async def test_unmerge_returns_actual_current_survivor_for_recompute() -> None:
    tx = _Tx(
        [
            {"absorbed_id": "person-a", "survivor_id": "person-b"},
            {"removed_count": 1, "current_survivor_id": "person-c"},
            None,
            None,
        ]
    )

    result = await _unmerge_tx(
        cast(AsyncManagedTransaction, tx),
        "merge-1",
        "false merge",
        "admin@example.com",
    )

    assert result == ("person-a", "person-c")

    tx = _Tx(
        [
            {"absorbed_id": "person-a", "survivor_id": "person-b"},
            {"removed_count": 0},
        ]
    )

    result = await _unmerge_tx(
        cast(AsyncManagedTransaction, tx),
        "merge-1",
        "false merge",
        "admin@example.com",
    )

    assert result is None
    assert [call.query for call in tx.calls] == [GET_UNMERGE_TARGET, REVERT_MERGE]

    tx = _Tx([{"lock_id": "lock-1"}])

    result = await _create_lock_tx(
        cast(AsyncManagedTransaction, tx),
        "person-a",
        "person-b",
        "manual_no_match",
        "not same person",
        None,
        "admin@example.com",
    )

    assert result == ("conflict", "lock-1")
    assert [call.query for call in tx.calls] == [CHECK_EXISTING_LOCK]


@pytest.mark.asyncio
async def test_create_lock_creates_when_no_existing_lock() -> None:
    tx = _Tx([None, {"lock_id": "lock-2"}])

    result = await _create_lock_tx(
        cast(AsyncManagedTransaction, tx),
        "person-a",
        "person-b",
        "manual_no_match",
        "not same person",
        None,
        "admin@example.com",
    )

    assert result == ("ok", "lock-2")
    assert [call.query for call in tx.calls] == [CHECK_EXISTING_LOCK, CREATE_PERSON_PAIR_LOCK]


@pytest.mark.asyncio
async def test_delete_lock_returns_true_when_query_returns_row() -> None:
    tx = _Tx([{"deleted_lock_id": "lock-1"}])

    result = await _delete_lock_tx(cast(AsyncManagedTransaction, tx), "lock-1")

    assert result is True
    assert [call.query for call in tx.calls] == [DELETE_LOCK]


@pytest.mark.asyncio
async def test_repository_manual_merge_recomputes_survivor_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([MergeOutcome(merge_event_id="merge-1"), None])
    factory = _SessionFactory([session])

    monkeypatch.setattr(merge_module, "get_session", factory)

    outcome = await Neo4jMergeRepository().manual_merge(
        "person-a",
        "person-b",
        "same customer",
        "admin@example.com",
        [],
    )

    assert outcome.merge_event_id == "merge-1"
    assert session.write_calls == [
        (
            merge_module._manual_merge_tx,
            ("person-a", "person-b", "same customer", "admin@example.com"),
        ),
        (merge_module.recompute_golden_profile_tx, ("person-b",)),
    ]


@pytest.mark.asyncio
async def test_repository_manual_merge_applies_selected_golden_profile_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selections: list[GoldenProfileSelection] = [
        {
            "field_name": "preferred_email",
            "source_kind": "identifier",
            "selected_value": "customer@example.com",
            "source_record_pk": "sr-1",
            "identifier_type": "email",
        }
    ]
    session = _Session([MergeOutcome(merge_event_id="merge-1"), "ok", None])
    factory = _SessionFactory([session])

    monkeypatch.setattr(merge_module, "get_session", factory)

    outcome = await Neo4jMergeRepository().manual_merge(
        "person-a",
        "person-b",
        "same customer",
        "admin@example.com",
        selections,
    )

    assert outcome.merge_event_id == "merge-1"
    assert session.write_calls == [
        (
            merge_module._manual_merge_tx,
            ("person-a", "person-b", "same customer", "admin@example.com"),
        ),
        (merge_module.recompute_golden_profile_tx, ("person-b",)),
        (merge_module._apply_golden_profile_selections_tx, ("person-b", selections)),
    ]


def test_golden_profile_selection_validation_rejects_incompatible_identifier_field() -> None:
    selections: list[GoldenProfileSelection] = [
        {
            "field_name": "preferred_nric",
            "source_kind": "identifier",
            "selected_value": "customer@example.com",
            "source_record_pk": "sr-1",
            "identifier_type": "email",
        }
    ]

    assert merge_module.are_valid_golden_profile_selections(selections) is False


def test_golden_profile_selection_validation_rejects_fact_without_source_record() -> None:
    selections: list[GoldenProfileSelection] = [
        {
            "field_name": "preferred_full_name",
            "source_kind": "source_record_fact",
            "selected_value": "Jane Customer",
            "source_record_pk": None,
            "identifier_type": None,
        }
    ]

    assert merge_module.are_valid_golden_profile_selections(selections) is False


@pytest.mark.asyncio
async def test_repository_manual_merge_marks_invalid_selection_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selections: list[GoldenProfileSelection] = [
        {
            "field_name": "preferred_nric",
            "source_kind": "identifier",
            "selected_value": "customer@example.com",
            "source_record_pk": "sr-1",
            "identifier_type": "email",
        }
    ]
    session = _Session([])
    factory = _SessionFactory([session])

    monkeypatch.setattr(merge_module, "get_session", factory)

    outcome = await Neo4jMergeRepository().manual_merge(
        "person-a",
        "person-b",
        "same customer",
        "admin@example.com",
        selections,
    )

    assert outcome.not_found is True
    assert outcome.merge_event_id is None


@pytest.mark.asyncio
async def test_repository_manual_merge_skips_recompute_when_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([MergeOutcome(blocked=True)])
    factory = _SessionFactory([session])

    monkeypatch.setattr(merge_module, "get_session", factory)

    outcome = await Neo4jMergeRepository().manual_merge(
        "person-a",
        "person-b",
        "same customer",
        "admin@example.com",
        [],
    )

    assert outcome.blocked is True
    assert session.write_calls == [
        (
            merge_module._manual_merge_tx,
            ("person-a", "person-b", "same customer", "admin@example.com"),
        )
    ]


@pytest.mark.asyncio
async def test_repository_unmerge_recomputes_absorbed_and_survivor_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([("person-a", "person-b"), None, None])
    factory = _SessionFactory([session])

    monkeypatch.setattr(merge_module, "get_session", factory)

    result = await Neo4jMergeRepository().unmerge(
        "merge-1",
        "false merge",
        "admin@example.com",
    )

    assert result == ("person-a", "person-b")
    assert session.write_calls == [
        (merge_module._unmerge_tx, ("merge-1", "false merge", "admin@example.com")),
        (merge_module.recompute_golden_profile_tx, ("person-a",)),
        (merge_module.recompute_golden_profile_tx, ("person-b",)),
    ]
