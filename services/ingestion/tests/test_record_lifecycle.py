"""Contract tests for immutable SourceRecord version lifecycle primitives."""

import re
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from typing import cast

import pytest
from neo4j import ManagedTransaction
from src.graph import queries
from src.graph.schema_init import LIFECYCLE_CONSTRAINTS
from src.models import SourceLifecycleState, SourceRecordLifecycleStatus, SourceVersionState
from src.record_lifecycle import (
    DuplicateVersion,
    PlannedVersion,
    SourceLifecycleConflict,
    SourceLifecycleDataError,
    activate_staged_version,
    classify_incoming_hash,
    load_locked_source_state,
    plan_incoming_version,
    reject_replaced_pending,
)


class _FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def single(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


class _FakeTx:
    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = iter(results)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> _FakeResult:
        self.calls.append((query, kwargs))
        return next(self._results)


def _version(
    pk: str,
    version: int,
    record_hash: str,
    status: SourceRecordLifecycleStatus,
    person_ids: tuple[str, ...] = (),
) -> SourceVersionState:
    return SourceVersionState(pk, version, record_hash, status, person_ids)


def test_source_record_lifecycle_status_values_are_closed() -> None:
    assert [status.value for status in SourceRecordLifecycleStatus] == [
        "active",
        "pending_review",
        "superseded",
        "rejected",
        "link_failed",
    ]


def test_source_version_state_is_frozen() -> None:
    state = SourceVersionState(
        source_record_pk="record-1",
        source_record_version=1,
        record_hash="hash-1",
        lifecycle_status=SourceRecordLifecycleStatus.ACTIVE,
        linked_person_ids=("person-1",),
    )

    with pytest.raises(FrozenInstanceError):
        state.source_record_version = 2  # type: ignore[misc]


def test_lock_and_get_source_state_locks_and_reads_open_versions() -> None:
    query = queries.LOCK_AND_GET_SOURCE_STATE

    assert "MERGE (lock:SourceRecordIdentityLock" in query
    assert "source_system: $source_system" in query
    assert "source_record_id: $source_record_id" in query
    assert "SET lock.locked_at = datetime()" in query
    assert "['active', 'pending_review']" in query
    assert "source_record_version" in query
    assert "linked_person_ids" in query
    assert "max(toInteger(history.source_record_version)) AS max_source_record_version" in query
    assert "max_source_record_version" in query
    assert "ORDER BY toInteger(sr.source_record_version) DESC" in query


def test_legacy_latest_record_is_effective_active_without_reclassifying_explicit_status() -> None:
    query = queries.LOCK_AND_GET_SOURCE_STATE
    assert "sr.lifecycle_status IS NULL AND sr.is_latest = true" in query
    assert "coalesce(sr.is_latest, true)" not in query
    assert "WHEN sr.lifecycle_status IS NULL THEN 'active'" in query
    assert "ELSE sr.lifecycle_status" in query
    assert "coalesce(sr.lifecycle_status" not in query


def test_effective_legacy_active_row_drives_duplicate_and_replacement_continuity() -> None:
    tx = _FakeTx(
        [
            _FakeResult(
                [
                    {
                        "source_record_pk": "legacy-sr",
                        "source_record_version": 4,
                        "record_hash": "legacy-hash",
                        "lifecycle_status": "active",
                        "linked_person_ids": ["legacy-owner"],
                        "max_source_record_version": 4,
                    }
                ]
            )
        ]
    )

    state = load_locked_source_state(cast(ManagedTransaction, tx), "legacy", "record-1")

    assert classify_incoming_hash(state, "legacy-hash") == DuplicateVersion("legacy-sr")
    assert plan_incoming_version(state, "changed-hash") == PlannedVersion(
        version=5,
        active_source_record_pk="legacy-sr",
        prior_person_ids=("legacy-owner",),
        pending_to_reject=None,
    )


def test_activate_source_record_version_is_compare_and_transition() -> None:
    query = queries.ACTIVATE_SOURCE_RECORD_VERSION

    assert "old.lifecycle_status = 'active'" in query
    assert "old.lifecycle_status IS NULL AND old.is_latest = true" in query
    assert "coalesce(old.is_latest, true)" not in query
    assert re.search(
        r"MATCH \(new:SourceRecord \{[^}]*lifecycle_status: 'pending_review'[^}]*\}\)",
        query,
    )
    assert "old.lifecycle_status = 'superseded'" in query
    assert "new.lifecycle_status = 'active'" in query
    assert "MERGE (old)-[:PREVIOUS_VERSION_OF]->(new)" in query
    assert "RETURN new.source_record_pk AS source_record_pk" in query


def test_all_ingestion_legacy_active_predicates_require_explicit_latest_marker() -> None:
    lifecycle_queries = (
        queries.LOCK_AND_GET_SOURCE_STATE,
        queries.ACTIVATE_SOURCE_RECORD_VERSION,
        queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION,
        queries.CHECK_SOURCE_RECORD_EXISTS,
        queries.GET_LATEST_SOURCE_RECORD,
    )

    for query in lifecycle_queries:
        assert "coalesce(sr.is_latest, true)" not in query
        assert "coalesce(old.is_latest, true)" not in query
        assert "coalesce(active.is_latest, true)" not in query

    assert "sr.lifecycle_status IS NULL AND sr.is_latest = true" in (
        queries.LOCK_AND_GET_SOURCE_STATE
    )
    assert "old.lifecycle_status IS NULL AND old.is_latest = true" in (
        queries.ACTIVATE_SOURCE_RECORD_VERSION
    )
    assert "active.lifecycle_status IS NULL AND active.is_latest = true" in (
        queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION
    )


@pytest.mark.parametrize(
    ("lifecycle_status", "is_latest", "effective_active"),
    [
        ("active", None, True),
        (None, True, True),
        (None, False, False),
        (None, None, False),
    ],
)
def test_effective_active_compatibility_contract(
    lifecycle_status: str | None,
    is_latest: bool | None,
    effective_active: bool,
) -> None:
    assert (lifecycle_status == "active" or (lifecycle_status is None and is_latest is True)) is (
        effective_active
    )


def test_unmarked_legacy_history_does_not_block_first_version_activation() -> None:
    query = queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION

    assert "active.lifecycle_status IS NULL AND active.is_latest = true" in query
    assert "coalesce(active.is_latest, true)" not in query


def test_all_lifecycle_transitions_return_source_record_pk() -> None:
    assert "RETURN pending.source_record_pk AS source_record_pk" in (
        queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION
    )
    assert "RETURN pending.source_record_pk AS source_record_pk" in (
        queries.REJECT_PENDING_SOURCE_RECORD
    )
    assert "RETURN sr.source_record_pk AS source_record_pk" in (
        queries.MARK_SOURCE_RECORD_LINK_FAILED
    )


def test_reject_replaced_pending_cancels_its_open_review_cases_atomically() -> None:
    query = queries.REJECT_PENDING_SOURCE_RECORD

    assert "OPTIONAL MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)" in query
    assert "[:ABOUT_LEFT|ABOUT_RIGHT]->(pending)" in query
    assert "rc.queue_state IN ['open', 'assigned', 'deferred']" in query
    assert "stale_case.queue_state = 'cancelled'" in query
    assert "stale_case.resolution = 'cancelled_superseded'" in query
    assert "stale_case.resolution_reason = $reason" in query
    assert "stale_case.resolved_at = datetime()" in query
    assert "stale_case.updated_at = datetime()" in query


def test_lifecycle_schema_constraints_are_idempotent_and_unique() -> None:
    schema = "\n".join(LIFECYCLE_CONSTRAINTS)

    assert "CREATE CONSTRAINT source_record_identity_lock_unique IF NOT EXISTS" in schema
    assert "REQUIRE (lock.source_system, lock.source_record_id) IS UNIQUE" in schema
    assert "CREATE CONSTRAINT source_record_version_key_unique IF NOT EXISTS" in schema
    assert "REQUIRE sr.source_version_key IS UNIQUE" in schema


@pytest.mark.parametrize("matching_slot", ["active", "pending"])
def test_classify_incoming_hash_finds_each_open_version(matching_slot: str) -> None:
    active_hash = "same" if matching_slot == "active" else "active-hash"
    pending_hash = "same" if matching_slot == "pending" else "pending-hash"
    state = SourceLifecycleState(
        active=_version("active-pk", 2, active_hash, SourceRecordLifecycleStatus.ACTIVE),
        pending=_version("pending-pk", 3, pending_hash, SourceRecordLifecycleStatus.PENDING_REVIEW),
        next_version=4,
    )

    expected_pk = "active-pk" if matching_slot == "active" else "pending-pk"
    assert classify_incoming_hash(state, "same") == DuplicateVersion(expected_pk)


def test_plan_incoming_version_preserves_replacement_context() -> None:
    state = SourceLifecycleState(
        active=_version(
            "active-pk",
            4,
            "old-hash",
            SourceRecordLifecycleStatus.ACTIVE,
            ("person-2", "person-1"),
        ),
        pending=_version(
            "pending-pk", 5, "pending-hash", SourceRecordLifecycleStatus.PENDING_REVIEW
        ),
        next_version=6,
    )

    assert plan_incoming_version(state, "new-hash") == PlannedVersion(
        version=6,
        active_source_record_pk="active-pk",
        prior_person_ids=("person-2", "person-1"),
        pending_to_reject="pending-pk",
    )


def test_load_locked_source_state_parses_typed_rows_and_next_version() -> None:
    tx = _FakeTx(
        [
            _FakeResult(
                [
                    {
                        "source_record_pk": "pending-pk",
                        "source_record_version": 7,
                        "record_hash": "pending-hash",
                        "lifecycle_status": "pending_review",
                        "linked_person_ids": [],
                        "max_source_record_version": 7,
                    },
                    {
                        "source_record_pk": "active-pk",
                        "source_record_version": 4,
                        "record_hash": "active-hash",
                        "lifecycle_status": "active",
                        "linked_person_ids": ["person-1", "person-2"],
                        "max_source_record_version": 7,
                    },
                ]
            )
        ]
    )

    state = load_locked_source_state(cast(ManagedTransaction, tx), "pos", "customer-1")

    assert state == SourceLifecycleState(
        active=_version(
            "active-pk",
            4,
            "active-hash",
            SourceRecordLifecycleStatus.ACTIVE,
            ("person-1", "person-2"),
        ),
        pending=_version(
            "pending-pk", 7, "pending-hash", SourceRecordLifecycleStatus.PENDING_REVIEW
        ),
        next_version=8,
    )
    assert tx.calls == [
        (
            queries.LOCK_AND_GET_SOURCE_STATE,
            {"source_system": "pos", "source_record_id": "customer-1"},
        )
    ]


def test_load_locked_source_state_accepts_no_open_versions() -> None:
    tx = _FakeTx([_FakeResult([])])

    assert load_locked_source_state(cast(ManagedTransaction, tx), "pos", "missing") == (
        SourceLifecycleState(active=None, pending=None, next_version=1)
    )


def test_load_locked_source_state_accepts_optional_match_placeholder() -> None:
    tx = _FakeTx(
        [
            _FakeResult(
                [
                    {
                        "source_record_pk": None,
                        "source_record_version": None,
                        "record_hash": None,
                        "lifecycle_status": None,
                        "linked_person_ids": [],
                        "max_source_record_version": None,
                    }
                ]
            )
        ]
    )

    assert load_locked_source_state(cast(ManagedTransaction, tx), "pos", "missing") == (
        SourceLifecycleState(active=None, pending=None, next_version=1)
    )


def test_load_locked_source_state_counts_rejected_version_after_active() -> None:
    row: dict[str, object] = {
        "source_record_pk": "active-pk",
        "source_record_version": 1,
        "record_hash": "active-hash",
        "lifecycle_status": "active",
        "linked_person_ids": [],
        "max_source_record_version": 2,
    }

    state = load_locked_source_state(
        cast(ManagedTransaction, _FakeTx([_FakeResult([row])])), "pos", "customer-1"
    )

    assert state.next_version == 3


def test_load_locked_source_state_counts_historical_versions_without_open_state() -> None:
    row: dict[str, object] = {
        "source_record_pk": None,
        "source_record_version": None,
        "record_hash": None,
        "lifecycle_status": None,
        "linked_person_ids": [],
        "max_source_record_version": 5,
    }

    assert load_locked_source_state(
        cast(ManagedTransaction, _FakeTx([_FakeResult([row])])), "pos", "customer-1"
    ) == SourceLifecycleState(active=None, pending=None, next_version=6)


def test_load_locked_source_state_uses_max_with_superseded_history_and_active_latest() -> None:
    row: dict[str, object] = {
        "source_record_pk": "active-pk",
        "source_record_version": 4,
        "record_hash": "active-hash",
        "lifecycle_status": "active",
        "linked_person_ids": [],
        "max_source_record_version": 4,
    }

    state = load_locked_source_state(
        cast(ManagedTransaction, _FakeTx([_FakeResult([row])])), "pos", "customer-1"
    )

    assert state.active is not None
    assert state.active.source_record_version == 4
    assert state.next_version == 5


@pytest.mark.parametrize("bad_max", [True, "2", -1])
def test_load_locked_source_state_rejects_malformed_max_version(bad_max: object) -> None:
    row: dict[str, object] = {
        "source_record_pk": None,
        "source_record_version": None,
        "record_hash": None,
        "lifecycle_status": None,
        "linked_person_ids": [],
        "max_source_record_version": bad_max,
    }

    with pytest.raises(SourceLifecycleDataError, match="max_source_record_version"):
        load_locked_source_state(
            cast(ManagedTransaction, _FakeTx([_FakeResult([row])])), "pos", "customer-1"
        )


def test_load_locked_source_state_rejects_inconsistent_max_versions() -> None:
    rows = [
        {
            "source_record_pk": "active-pk",
            "source_record_version": 1,
            "record_hash": "active-hash",
            "lifecycle_status": "active",
            "linked_person_ids": [],
            "max_source_record_version": 2,
        },
        {
            "source_record_pk": "pending-pk",
            "source_record_version": 2,
            "record_hash": "pending-hash",
            "lifecycle_status": "pending_review",
            "linked_person_ids": [],
            "max_source_record_version": 3,
        },
    ]

    with pytest.raises(SourceLifecycleDataError, match="inconsistent max_source_record_version"):
        load_locked_source_state(
            cast(ManagedTransaction, _FakeTx([_FakeResult(rows)])), "pos", "customer-1"
        )


@pytest.mark.parametrize(
    "linked_person_ids",
    ["bad", ["unexpected-person"], [123]],
)
def test_load_locked_source_state_rejects_malformed_placeholder_person_ids(
    linked_person_ids: object,
) -> None:
    tx = _FakeTx(
        [
            _FakeResult(
                [
                    {
                        "source_record_pk": None,
                        "source_record_version": None,
                        "record_hash": None,
                        "lifecycle_status": None,
                        "linked_person_ids": linked_person_ids,
                        "max_source_record_version": None,
                    }
                ]
            )
        ]
    )

    with pytest.raises(SourceLifecycleDataError, match="null source_record_pk"):
        load_locked_source_state(cast(ManagedTransaction, tx), "pos", "missing")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_record_pk", 123, "source_record_pk must be a string"),
        ("source_record_version", "2", "source_record_version must be a positive integer"),
        ("record_hash", None, "record_hash must be a string"),
        ("lifecycle_status", 123, "lifecycle_status must be a string"),
        ("linked_person_ids", "bad", "linked_person_ids must be a list of strings"),
        ("linked_person_ids", [123], "linked_person_ids must be a list of strings"),
    ],
)
def test_load_locked_source_state_rejects_malformed_open_version_fields(
    field: str, value: object, message: str
) -> None:
    row: dict[str, object] = {
        "source_record_pk": "active-pk",
        "source_record_version": 2,
        "record_hash": "active-hash",
        "lifecycle_status": "active",
        "linked_person_ids": ["person-1"],
        "max_source_record_version": 2,
    }
    row[field] = value

    with pytest.raises(SourceLifecycleDataError, match=message):
        load_locked_source_state(
            cast(ManagedTransaction, _FakeTx([_FakeResult([row])])), "pos", "customer-1"
        )


def test_load_locked_source_state_rejects_missing_row_field() -> None:
    row: dict[str, object] = {
        "source_record_pk": "active-pk",
        "source_record_version": 2,
        "record_hash": "active-hash",
        "lifecycle_status": "active",
        "max_source_record_version": 2,
    }

    with pytest.raises(SourceLifecycleDataError, match="missing lifecycle row field"):
        load_locked_source_state(
            cast(ManagedTransaction, _FakeTx([_FakeResult([row])])), "pos", "customer-1"
        )


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [
                {
                    "source_record_pk": "one",
                    "source_record_version": 1,
                    "record_hash": "one-hash",
                    "lifecycle_status": "active",
                    "linked_person_ids": [],
                    "max_source_record_version": 2,
                },
                {
                    "source_record_pk": "two",
                    "source_record_version": 2,
                    "record_hash": "two-hash",
                    "lifecycle_status": "active",
                    "linked_person_ids": [],
                    "max_source_record_version": 2,
                },
            ],
            "multiple active",
        ),
        (
            [
                {
                    "source_record_pk": "bad",
                    "source_record_version": 1,
                    "record_hash": "bad-hash",
                    "lifecycle_status": "superseded",
                    "linked_person_ids": [],
                    "max_source_record_version": 1,
                }
            ],
            "unexpected lifecycle_status",
        ),
    ],
)
def test_load_locked_source_state_rejects_inconsistent_rows(
    rows: list[dict[str, object]], message: str
) -> None:
    tx = _FakeTx([_FakeResult(rows)])

    with pytest.raises(SourceLifecycleDataError, match=message):
        load_locked_source_state(cast(ManagedTransaction, tx), "pos", "customer-1")


def test_reject_replaced_pending_uses_reason_and_requires_transition() -> None:
    successful = _FakeTx([_FakeResult([{"source_record_pk": "pending-pk"}])])
    reject_replaced_pending(cast(ManagedTransaction, successful), "pending-pk")
    assert successful.calls == [
        (
            queries.REJECT_PENDING_SOURCE_RECORD,
            {"source_record_pk": "pending-pk", "reason": "rejected_by_newer_version"},
        )
    ]

    with pytest.raises(SourceLifecycleConflict):
        reject_replaced_pending(cast(ManagedTransaction, _FakeTx([_FakeResult([])])), "pending-pk")


def test_activate_staged_version_selects_first_version_query() -> None:
    tx = _FakeTx([_FakeResult([{"source_record_pk": "new-pk"}])])

    activate_staged_version(
        cast(ManagedTransaction, tx),
        source_system="pos",
        source_record_id="customer-1",
        old_source_record_pk=None,
        new_source_record_pk="new-pk",
    )

    assert tx.calls == [
        (
            queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION,
            {
                "source_record_pk": "new-pk",
                "source_system": "pos",
                "source_record_id": "customer-1",
            },
        )
    ]


def test_activate_staged_version_selects_replacement_query_and_detects_conflict() -> None:
    tx = _FakeTx([_FakeResult([])])

    with pytest.raises(SourceLifecycleConflict):
        activate_staged_version(
            cast(ManagedTransaction, tx),
            source_system="pos",
            source_record_id="customer-1",
            old_source_record_pk="old-pk",
            new_source_record_pk="new-pk",
        )

    assert tx.calls == [
        (
            queries.ACTIVATE_SOURCE_RECORD_VERSION,
            {"old_source_record_pk": "old-pk", "new_source_record_pk": "new-pk"},
        )
    ]
