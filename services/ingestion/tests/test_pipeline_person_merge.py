"""Person-pair auto-merge: threshold, survivor selection, merge execution."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from _txmock import _RecordingTx
from src.graph import queries
from src.matching.heuristic import CONFIDENCE_AUTO_MERGE, CONFIDENCE_REVIEW
from src.matching.pair_score import PERSON_PAIR_AUTO_MERGE, PERSON_PAIR_REVIEW
from src.pipeline_person_merge import PairPersonAttrs, merge_person_pair, select_survivor


def test_record_match_thresholds_are_040_and_020() -> None:
    assert CONFIDENCE_AUTO_MERGE == 0.40
    assert CONFIDENCE_REVIEW == 0.20


def test_person_pair_auto_merge_threshold_is_040() -> None:
    assert PERSON_PAIR_AUTO_MERGE == 0.40
    assert PERSON_PAIR_REVIEW == 0.20


def test_person_merge_query_constants_exist() -> None:
    assert "left_status" in queries.FETCH_PAIR_MERGE_ATTRS
    assert "right_status" in queries.FETCH_PAIR_MERGE_ATTRS
    assert "cancelled_superseded" in queries.CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED
    assert "ABOUT_LEFT" in queries.REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT
    assert "ABOUT_RIGHT" in queries.REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT
    assert "source_record" in queries.REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED


def test_select_survivor_uses_completeness_then_age_then_id() -> None:
    complete = PairPersonAttrs("p-b", "active", 0.8, "2026-02-01T00:00:00Z")
    sparse = PairPersonAttrs("p-a", "active", 0.4, "2026-01-01T00:00:00Z")
    assert select_survivor(complete, sparse) == ("p-b", "p-a")

    older = PairPersonAttrs("p-b", "active", 0.5, "2026-01-01T00:00:00Z")
    newer = PairPersonAttrs("p-a", "active", 0.5, "2026-02-01T00:00:00Z")
    assert select_survivor(older, newer) == ("p-b", "p-a")

    left = PairPersonAttrs("p-b", "active", 0.5, "2026-01-01T00:00:00Z")
    right = PairPersonAttrs("p-a", "active", 0.5, "2026-01-01T00:00:00Z")
    assert select_survivor(left, right) == ("p-a", "p-b")


class _MergeResult:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self.records = records

    def single(self) -> dict[str, object] | None:
        return self.records[0] if self.records else None

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.records)


class _MergeTx(_RecordingTx):
    def run(self, query: str, **params: object) -> _MergeResult:
        self._record(query, params)
        if query is queries.GET_AFFECTED_SOURCE_RECORDS:
            return _MergeResult(
                [
                    {"source_record_pk": "sr-linked"},
                    {"source_record_pk": "sr-incoming-knows"},
                ]
            )
        if "RETURN me.merge_event_id AS merge_event_id" in query:
            return _MergeResult([{"merge_event_id": "me-1"}])
        if "collect(DISTINCT neighbor.person_id)" in query:
            return _MergeResult([{"person_ids": ["p-neighbor", "p-live"]}])
        return _MergeResult([])


def test_merge_person_pair_runs_rewires_and_recomputes_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recomputed: list[str] = []
    monkeypatch.setattr(
        "src.pipeline_person_merge.compute_golden_profile",
        lambda tx, person_id: recomputed.append(person_id),
    )
    tx = _MergeTx()
    result = merge_person_pair(
        tx,  # type: ignore[arg-type]
        absorbed_id="p-old",
        survivor_id="p-live",
        match_decision_id="md-1",
        reason="threshold met",
    )
    text = "\n".join(query for query, _ in tx.calls)
    assert result == "me-1"
    assert "TRIGGERED_BY" in text
    assert "LINKED_TO" in text
    assert "IDENTIFIED_BY" in text
    assert "LIVES_AT" in text
    assert "HAS_FACT" in text
    assert "KNOWS" in text
    assert "PURCHASED" in text
    assert "BOUGHT_VEHICLE" in text
    assert "OWNS_VEHICLE" in text
    assert "MOVED_RELATIONSHIP" in text
    assert "merge_origin_person_id" in text
    assert "SET absorbed.status = 'merged'" in text
    assert "cancelled_superseded" in text
    assert "collect(DISTINCT neighbor.person_id)" in text
    assert "analysis_input_revision" in text
    dirty_parameters = [
        parameters for query, parameters in tx.calls if query is queries.MARK_PROFILE_ANALYSIS_DIRTY
    ]
    assert dirty_parameters == [{"source_record_pks": [], "person_ids": ["p-live", "p-neighbor"]}]
    affected_record_parameters = [
        parameters
        for query, parameters in tx.calls
        if query is queries.LINK_MERGE_EVENT_AFFECTED_RECORD
    ]
    assert affected_record_parameters == [
        {"merge_event_id": "me-1", "source_record_pk": "sr-linked"},
        {"merge_event_id": "me-1", "source_record_pk": "sr-incoming-knows"},
    ]
    context_rewire_parameters = [
        parameters
        for query, parameters in tx.calls
        if query
        in {
            queries.REWIRE_PURCHASED,
            queries.REWIRE_BOUGHT_VEHICLE,
            queries.REWIRE_OWNS_VEHICLE,
        }
    ]
    assert context_rewire_parameters == [
        {"absorbed_id": "p-old", "survivor_id": "p-live", "merge_event_id": "me-1"},
        {"absorbed_id": "p-old", "survivor_id": "p-live", "merge_event_id": "me-1"},
        {"absorbed_id": "p-old", "survivor_id": "p-live", "merge_event_id": "me-1"},
    ]
    lineage_calls = [
        parameters for query, parameters in tx.calls if query is queries.PATH_COMPRESS_MERGED_INTO
    ]
    assert lineage_calls == [
        {"absorbed_id": "p-old", "survivor_id": "p-live", "merge_event_id": "me-1"}
    ]
    assert recomputed == ["p-live"]


def test_auto_merge_rewires_record_event_provenance_for_every_relationship() -> None:
    for query, relationship_type in (
        (queries.REWIRE_LINKED_TO, "LINKED_TO"),
        (queries.REWIRE_IDENTIFIED_BY, "IDENTIFIED_BY"),
        (queries.REWIRE_LIVES_AT, "LIVES_AT"),
        (queries.REWIRE_HAS_FACT, "HAS_FACT"),
        (queries.REWIRE_KNOWS_OUT, "KNOWS_OUT"),
        (queries.REWIRE_KNOWS_IN, "KNOWS_IN"),
        (queries.REWIRE_PURCHASED, "PURCHASED"),
        (queries.REWIRE_BOUGHT_VEHICLE, "BOUGHT_VEHICLE"),
        (queries.REWIRE_OWNS_VEHICLE, "OWNS_VEHICLE"),
    ):
        assert "moved:MOVED_RELATIONSHIP" in query
        assert f"moved.relationship_type = '{relationship_type}'" in query
        assert "moved.origin_person_id = coalesce(" in query
        assert "merge_origin_person_id" in query


def test_auto_merge_collision_rewires_keep_event_specific_snapshots() -> None:
    for query in (
        queries.REWIRE_IDENTIFIED_BY,
        queries.REWIRE_LIVES_AT,
        queries.REWIRE_PURCHASED,
        queries.REWIRE_BOUGHT_VEHICLE,
        queries.REWIRE_OWNS_VEHICLE,
    ):
        assert "moved.created_on_survivor = existing IS NULL" in query


def test_path_compression_records_event_specific_lineage_provenance() -> None:
    query = queries.PATH_COMPRESS_MERGED_INTO

    assert "moved_lineage:MOVED_MERGE_LINEAGE" in query
    assert "moved_lineage.prior_survivor_person_id = absorbed.person_id" in query
    assert "SET compressed = props" in query
