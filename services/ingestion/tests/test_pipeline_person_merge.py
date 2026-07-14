"""Person-pair auto-merge: threshold, survivor selection, merge execution."""

from __future__ import annotations

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


class _MergeTx(_RecordingTx):
    def run(self, query: str, **params: object) -> _MergeResult:
        self._record(query, params)
        if "RETURN me.merge_event_id AS merge_event_id" in query:
            return _MergeResult([{"merge_event_id": "me-1"}])
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
    assert "SET absorbed.status = 'merged'" in text
    assert "cancelled_superseded" in text
    assert recomputed == ["p-live"]
