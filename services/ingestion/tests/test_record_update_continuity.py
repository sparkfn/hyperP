from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from neo4j import ManagedTransaction
from src.graph import queries
from src.models import MatchDecision, MatchResult, RecordType, SourceRecordEnvelope
from src.pipeline import IngestPipeline
from src.pipeline_writes import persist_match_decision, retire_identity_projections
from src.record_lifecycle import PlannedVersion


class _Rows:
    def __iter__(self) -> Iterator[dict[str, str]]:
        return iter(
            [
                {"person_id": "person-b"},
                {"person_id": "person-a"},
                {"person_id": "person-a"},
            ]
        )


class _Tx:
    def run(self, query: str, **params: object) -> _Rows:
        assert query == queries.RETIRE_IDENTITY_PROJECTIONS
        assert params == {"source_record_pk": "old-sr"}
        return _Rows()


def test_retire_identity_projections_returns_sorted_distinct_owners() -> None:
    assert retire_identity_projections(_Tx(), "old-sr") == ("person-a", "person-b")  # type: ignore[arg-type]


def test_create_source_record_stores_authoritative_lifecycle_identity() -> None:
    query = queries.CREATE_SOURCE_RECORD
    assert "lifecycle_status:     $lifecycle_status" in query
    assert "source_version_key:   $source_version_key" in query
    assert "expected_active_source_record_pk: $expected_active_source_record_pk" in query
    assert "is_latest:             $is_latest" in query
    assert "Entity {entity_key: $entity_key}" in query
    assert "WITH ss, entity, $entity_key AS requested_entity_key" in query
    assert "WHERE requested_entity_key IS NULL OR entity IS NOT NULL" in query
    assert "MERGE (sr)-[:OWNED_BY]->(entity)" in query


def test_retirement_is_strictly_source_scoped_and_keeps_history() -> None:
    query = queries.RETIRE_IDENTITY_PROJECTIONS
    assert "MATCH (source:SourceRecord {source_record_pk: $source_record_pk})" in query
    assert query.count("source_record_pk: $source_record_pk") == 1
    assert query.count("rel.source_record_pk = source.source_record_pk") == 3
    assert "IDENTIFIED_BY" in query
    assert "LIVES_AT" in query
    assert "HAS_FACT" in query
    assert "DELETE" not in query


def test_person_assertions_are_keyed_by_immutable_source_provenance() -> None:
    identifier = queries.LINK_PERSON_TO_IDENTIFIER
    address = queries.LINK_PERSON_TO_ADDRESS
    bankruptcy = queries.MERGE_BANKRUPTCY_CASE
    for query in (identifier, address, bankruptcy):
        merge_clause = query.split("ON CREATE", maxsplit=1)[0]
        assert "source_record_pk: $source_record_pk" in merge_clause
        assert "source_system_key: $source_system_key" in merge_clause
    assert "RETURN DISTINCT candidate.person_id" in queries.FIND_CANDIDATES_BY_IDENTIFIER
    assert "RETURN DISTINCT candidate.person_id" in queries.FIND_CANDIDATES_BY_ADDRESS
    batch = queries.FIND_CANDIDATES_BY_IDENTIFIERS_BATCH
    assert "fanout_rel.is_active = true" in batch
    assert "fanout_rel.quality_flag" not in batch
    assert "rel.quality_flag IN ['valid', 'partial_parse']" in batch


def test_auto_merge_rewires_each_source_assertion_independently() -> None:
    for query, relationship in (
        (queries.REWIRE_IDENTIFIED_BY, "IDENTIFIED_BY"),
        (queries.REWIRE_LIVES_AT, "LIVES_AT"),
    ):
        merge_clause = query.split("ON CREATE", maxsplit=1)[0]
        assert f"MERGE (survivor)-[rel:{relationship} {{" in merge_clause
        assert "source_system_key: props.source_system_key" in merge_clause
        assert "source_record_pk: props.source_record_pk" in merge_clause


def test_activation_synchronizes_legacy_latest_flags() -> None:
    assert "old.is_latest = false" in queries.ACTIVATE_SOURCE_RECORD_VERSION
    assert "new.is_latest = true" in queries.ACTIVATE_SOURCE_RECORD_VERSION
    assert "pending.is_latest = true" in queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION


class _DecisionResult:
    def __init__(self, row: dict[str, str] | None = None) -> None:
        self.row = row

    def single(self) -> dict[str, str] | None:
        return self.row


class _DecisionTx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **params: object) -> _DecisionResult:
        self.calls.append((query, params))
        if query == queries.CREATE_MATCH_DECISION:
            return _DecisionResult({"match_decision_id": "decision-1"})
        return _DecisionResult()


def test_review_decision_links_proposed_person_as_right_side() -> None:
    tx = _DecisionTx()
    result = MatchResult(
        decision=MatchDecision.REVIEW,
        matched_person_id="prior",
        proposed_person_id="destination",
    )

    persist_match_decision(cast(ManagedTransaction, tx), result, "source-1")

    right_calls = [call for call in tx.calls if call[0] == queries.LINK_MATCH_DECISION_RIGHT_PERSON]
    assert right_calls[0][1]["person_id"] == "destination"


def _envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="bitrix",
        source_record_id="contact-1",
        source_record_version="2",
        record_type=RecordType.IDENTITY,
        observed_at="2026-07-13T00:00:00Z",
        record_hash="new-hash",
    )


def test_multi_owner_changed_update_stays_pending_without_active_side_effects() -> None:
    pipeline = IngestPipeline(cast(object, MagicMock()))
    tx = cast(ManagedTransaction, MagicMock())
    plan = PlannedVersion(
        version=2,
        active_source_record_pk="old-sr",
        prior_person_ids=("person-b", "person-a"),
        pending_to_reject="older-pending",
    )
    match_engine = MagicMock()
    pipeline._match_engine = match_engine

    with ExitStack() as stack:
        find = stack.enter_context(patch("src.pipeline.find_candidates", return_value=[]))
        stack.enter_context(patch("src.pipeline.upsert_nodes"))
        persist = stack.enter_context(
            patch("src.pipeline.persist_source_record", return_value="new-sr")
        )
        stack.enter_context(patch("src.pipeline.persist_match_decision", return_value="decision-1"))
        review = stack.enter_context(
            patch("src.pipeline.create_review_case_if_needed", return_value="review-1")
        )
        link = stack.enter_context(patch("src.pipeline.link_record_to_graph"))
        retire = stack.enter_context(patch("src.pipeline.retire_identity_projections"))
        activate = stack.enter_context(patch("src.pipeline.activate_staged_version"))
        bankruptcy = stack.enter_context(patch("src.pipeline.materialize_bankruptcy_case"))
        golden = stack.enter_context(patch("src.pipeline.compute_golden_profile"))
        audit = stack.enter_context(patch("src.pipeline.audit_person_pairs"))
        dirty = stack.enter_context(patch("src.pipeline.mark_profile_analysis_dirty"))
        vehicles = stack.enter_context(patch.object(pipeline, "_write_chat_vehicle_observations"))
        reject = stack.enter_context(patch("src.pipeline.reject_replaced_pending"))

        result = pipeline._execute_ingest(tx, _envelope(), [], [], [], lifecycle_plan=plan)

    assert result.match_decision is MatchDecision.REVIEW
    assert result.person_id == "person-a"
    find.assert_called_once()
    match_engine.evaluate.assert_not_called()
    assert persist.call_args.kwargs["lifecycle_status"].value == "pending_review"
    assert persist.call_args.kwargs["expected_active_source_record_pk"] == "old-sr"
    review.assert_called_once()
    assert link.call_args.kwargs["attach_evidence"] is False
    retire.assert_not_called()
    activate.assert_not_called()
    bankruptcy.assert_not_called()
    golden.assert_not_called()
    audit.assert_not_called()
    dirty.assert_not_called()
    vehicles.assert_not_called()
    reject.assert_called_once_with(tx, "older-pending")


@pytest.mark.parametrize(
    ("matched", "additional", "prior", "old_pk", "expected_recomputed"),
    [
        ("person-first", [], (), None, {"person-first"}),
        ("person-a", [], ("person-a",), "old-sr", {"person-a"}),
        (
            "person-new",
            [],
            ("person-old",),
            "old-sr",
            {"person-old", "person-new"},
        ),
        (
            "person-a",
            ["person-b", "person-c"],
            ("person-a",),
            "old-sr",
            {"person-a", "person-b", "person-c"},
        ),
    ],
)
def test_accepted_update_retires_activates_and_recomputes_complete_affected_set(
    matched: str,
    additional: list[str],
    prior: tuple[str, ...],
    old_pk: str | None,
    expected_recomputed: set[str],
) -> None:
    pipeline = IngestPipeline(cast(object, MagicMock()))
    pipeline._match_engine = MagicMock(
        evaluate=MagicMock(
            return_value=MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                matched_person_id=matched,
                additional_linked_person_ids=additional,
            )
        )
    )
    tx = cast(ManagedTransaction, MagicMock())
    plan = PlannedVersion(2, old_pk, prior, None)

    with ExitStack() as stack:
        stack.enter_context(patch("src.pipeline.find_candidates", return_value=[]))
        stack.enter_context(patch("src.pipeline.upsert_nodes"))
        stack.enter_context(patch("src.pipeline.persist_source_record", return_value="new-sr"))
        stack.enter_context(patch("src.pipeline.persist_match_decision", return_value="decision-1"))
        stack.enter_context(patch("src.pipeline.create_review_case_if_needed", return_value=None))
        link = stack.enter_context(patch("src.pipeline.link_record_to_graph"))
        retire = stack.enter_context(
            patch("src.pipeline.retire_identity_projections", return_value=prior)
        )
        activate = stack.enter_context(patch("src.pipeline.activate_staged_version"))
        stack.enter_context(patch("src.pipeline.materialize_bankruptcy_case"))
        golden = stack.enter_context(patch("src.pipeline.compute_golden_profile"))
        stack.enter_context(patch("src.pipeline.audit_person_pairs"))
        dirty = stack.enter_context(patch("src.pipeline.mark_profile_analysis_dirty"))
        stack.enter_context(patch("src.pipeline.record_auto_merge_event"))
        stack.enter_context(patch.object(pipeline, "_write_chat_vehicle_observations"))

        pipeline._execute_ingest(tx, _envelope(), [], [], [], lifecycle_plan=plan)

    if old_pk is None:
        retire.assert_not_called()
    else:
        retire.assert_called_once_with(tx, old_pk)
    activate.assert_called_once()
    assert activate.call_args.kwargs["old_source_record_pk"] == old_pk
    assert all(call.kwargs["attach_evidence"] for call in link.call_args_list)
    assert {call.args[1] for call in golden.call_args_list} == expected_recomputed
    dirty.assert_called_once_with(
        tx,
        source_record_pks=("new-sr", old_pk or ""),
        person_ids=expected_recomputed,
    )


class _LifecycleRows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)


class _DuplicateTx:
    def __init__(self, status: str) -> None:
        self.status = status
        self.calls: list[str] = []

    def run(self, query: str, **_params: object) -> _LifecycleRows:
        self.calls.append(query)
        if query != queries.LOCK_AND_GET_SOURCE_STATE:
            raise AssertionError("duplicate ingestion must stop after the lifecycle lock read")
        return _LifecycleRows(
            [
                {
                    "source_record_pk": f"{self.status}-sr",
                    "source_record_version": 1,
                    "record_hash": "new-hash",
                    "lifecycle_status": self.status,
                    "linked_person_ids": ["person-a"],
                    "max_source_record_version": 1,
                }
            ]
        )


class _Session:
    def __init__(self, tx: _DuplicateTx) -> None:
        self.tx = tx

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute_write(self, callback: object) -> object:
        return callback(cast(ManagedTransaction, self.tx))  # type: ignore[operator]


class _Client:
    def __init__(self, tx: _DuplicateTx) -> None:
        self.tx = tx

    def session(self) -> _Session:
        return _Session(self.tx)


@pytest.mark.parametrize("status", ["active", "pending_review"])
def test_duplicate_open_hash_returns_existing_record_without_writes(status: str) -> None:
    tx = _DuplicateTx(status)
    pipeline = IngestPipeline(cast(object, _Client(tx)))

    result = pipeline.ingest(_envelope())

    assert result.skipped_duplicate is True
    assert result.source_record_pk == f"{status}-sr"
    assert tx.calls == [queries.LOCK_AND_GET_SOURCE_STATE]
