"""Coordinator tests for bounded, resumable SourceRecord lifecycle migration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest
from src.graph import migrations
from src.graph.client import Neo4jClient


class _Result:
    def __init__(self, record: dict[str, object] | None) -> None:
        self._record = record

    def single(self) -> dict[str, object] | None:
        return self._record


@dataclass(frozen=True)
class _Call:
    query: str
    params: dict[str, object]


class _MigrationState:
    def __init__(
        self,
        *,
        phase: str = "prepare",
        completed: bool = False,
        busy_acquisitions: int = 0,
        legacy_prepare_batches: list[int] | None = None,
        prepare_batches: list[int] | None = None,
        migrate_batches: list[int | Exception] | None = None,
        cleanup_batches: list[int] | None = None,
        legacy_cleanup_batches: list[int] | None = None,
        total_records: int = 0,
        identity_count: int = 0,
    ) -> None:
        self.phase = phase
        self.completed = completed
        self.busy_acquisitions = busy_acquisitions
        self.legacy_prepare_batches = legacy_prepare_batches or [0]
        self.prepare_batches = prepare_batches or [0]
        self.migrate_batches = migrate_batches or [0]
        self.cleanup_batches = cleanup_batches or [0]
        self.legacy_cleanup_batches = legacy_cleanup_batches or [0]
        self.total_records = total_records
        self.identities_remaining = identity_count
        self.current_identity = False
        self.updated_records = 0
        self.calls: list[_Call] = []
        self.release_count = 0


class _Tx:
    def __init__(self, state: _MigrationState) -> None:
        self._state = state

    def run(self, query: str, **params: object) -> _Result:
        state = self._state
        state.calls.append(_Call(query=query, params=dict(params)))
        if query == migrations.ACQUIRE_SOURCE_RECORD_LIFECYCLE_MIGRATION:
            if state.completed:
                return _Result(
                    {
                        "completed": True,
                        "acquired": False,
                        "phase": state.phase,
                        "total_records": state.total_records,
                    }
                )
            if state.busy_acquisitions:
                state.busy_acquisitions -= 1
                return _Result(
                    {
                        "completed": False,
                        "acquired": False,
                        "phase": state.phase,
                        "total_records": state.total_records,
                    }
                )
            return _Result(
                {
                    "completed": False,
                    "acquired": True,
                    "phase": state.phase,
                    "total_records": state.total_records,
                }
            )
        if query == migrations.INITIALIZE_SOURCE_RECORD_LIFECYCLE_MIGRATION:
            return _Result({"phase": state.phase, "total_records": state.total_records})
        if query == migrations.PREPARE_LEGACY_SOURCE_RECORD_LIFECYCLE_BATCH:
            return _Result({"processed": state.legacy_prepare_batches.pop(0)})
        if query == migrations.PREPARE_SOURCE_RECORD_LIFECYCLE_BATCH:
            return _Result({"processed": state.prepare_batches.pop(0)})
        if query == migrations.CLAIM_SOURCE_RECORD_LIFECYCLE_IDENTITY:
            if not state.current_identity and state.identities_remaining:
                state.current_identity = True
            return _Result({"claimed": state.current_identity})
        if query == migrations.MIGRATE_SOURCE_RECORD_LIFECYCLE_BATCH:
            outcome = state.migrate_batches.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            state.updated_records += outcome
            return _Result({"updated": outcome})
        if query == migrations.COMPLETE_SOURCE_RECORD_LIFECYCLE_IDENTITY:
            assert state.current_identity
            state.current_identity = False
            state.identities_remaining -= 1
            return _Result({"completed_identity": True})
        if query == migrations.CLEAN_SOURCE_RECORD_LIFECYCLE_BATCH:
            return _Result({"processed": state.cleanup_batches.pop(0)})
        if query == migrations.CLEAN_LEGACY_SOURCE_RECORD_LIFECYCLE_BATCH:
            return _Result({"processed": state.legacy_cleanup_batches.pop(0)})
        if query == migrations.ADVANCE_SOURCE_RECORD_LIFECYCLE_MIGRATION:
            assert params["expected_phase"] == state.phase
            state.phase = cast(str, params["next_phase"])
            return _Result({"phase": state.phase})
        if query == migrations.COMPLETE_SOURCE_RECORD_LIFECYCLE_MIGRATION:
            assert state.phase == "cleanup"
            state.completed = True
            state.phase = "complete"
            return _Result({"updated_records": state.updated_records})
        if query == migrations.RELEASE_SOURCE_RECORD_LIFECYCLE_MIGRATION:
            state.release_count += 1
            return _Result({"released": True})
        raise AssertionError("unexpected migration query")


class _MigrationClient:
    def __init__(self, state: _MigrationState) -> None:
        self.state = state

    def execute_write(self, work: object, **_kwargs: object) -> object:
        return cast("object", work)(_Tx(self.state))  # type: ignore[operator]


def _client(state: _MigrationState) -> Neo4jClient:
    return cast(Neo4jClient, _MigrationClient(state))


def test_empty_graph_completes_all_bounded_phases() -> None:
    state = _MigrationState(total_records=0)

    assert migrations.migrate_source_record_lifecycle(_client(state)) == 0

    assert state.completed is True
    assert state.phase == "complete"
    assert state.release_count == 0
    assert any(
        call.query == migrations.INITIALIZE_SOURCE_RECORD_LIFECYCLE_MIGRATION
        for call in state.calls
    )
    assert any(
        call.query == migrations.CLAIM_SOURCE_RECORD_LIFECYCLE_IDENTITY
        for call in state.calls
    )
    assert not any(
        call.query == migrations.MIGRATE_SOURCE_RECORD_LIFECYCLE_BATCH
        for call in state.calls
    )


def test_completed_migration_rerun_is_a_noop() -> None:
    state = _MigrationState(completed=True, total_records=17)

    assert migrations.migrate_source_record_lifecycle(_client(state)) == 0

    assert [call.query for call in state.calls] == [
        migrations.ACQUIRE_SOURCE_RECORD_LIFECYCLE_MIGRATION
    ]


def test_concurrent_owner_is_waited_out_before_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _MigrationState(busy_acquisitions=1)
    sleeps: list[float] = []
    monkeypatch.setattr(migrations.time, "sleep", sleeps.append)

    assert migrations.migrate_source_record_lifecycle(_client(state)) == 0

    assert sleeps == [migrations.SOURCE_RECORD_LIFECYCLE_LOCK_POLL_SECONDS]
    acquire_calls = [
        call
        for call in state.calls
        if call.query == migrations.ACQUIRE_SOURCE_RECORD_LIFECYCLE_MIGRATION
    ]
    assert len(acquire_calls) == 2


def test_partial_failure_releases_lease_and_retry_resumes_persisted_phase() -> None:
    state = _MigrationState(
        prepare_batches=[2, 0],
        migrate_batches=[1, RuntimeError("simulated batch failure"), 1, 0],
        cleanup_batches=[2, 0],
        total_records=2,
        identity_count=1,
    )
    client = _client(state)

    with pytest.raises(RuntimeError, match="simulated batch failure"):
        migrations.migrate_source_record_lifecycle(client)

    assert state.phase == "migrate"
    assert state.updated_records == 1
    assert state.release_count == 1
    prepare_calls_before_retry = sum(
        call.query == migrations.PREPARE_SOURCE_RECORD_LIFECYCLE_BATCH for call in state.calls
    )

    assert migrations.migrate_source_record_lifecycle(client) == 1

    assert state.completed is True
    assert state.updated_records == 2
    assert state.release_count == 1
    assert (
        sum(call.query == migrations.PREPARE_SOURCE_RECORD_LIFECYCLE_BATCH for call in state.calls)
        == prepare_calls_before_retry
    )
