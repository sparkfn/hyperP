"""Tests for the pair-audit match recalculation Celery task."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from src.graph import queries
from src.matching.heuristic import MatchResult
from src.models import MatchDecision
from src.tasks import _parse_feature_snapshot, _recalculate_pair_audit_match_in_tx


class _FakeResult:
    def __init__(self, record: Mapping[str, object] | None = None) -> None:
        self._record = record

    def single(self) -> Mapping[str, object] | None:
        return self._record


class _FakeTx:
    def __init__(self, get_record: Mapping[str, object] | None, update_record: bool) -> None:
        self.get_record = get_record
        self.update_record = update_record
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **params: object) -> _FakeResult:
        self.calls.append((query, params))
        if query is queries.GET_PERSON_PAIR_REVIEW_CASE:
            return _FakeResult(self.get_record)
        if query is queries.UPDATE_PAIR_AUDIT_MATCH_DECISION:
            record: Mapping[str, object] | None = None
            if self.update_record:
                params_dict = params
                assert isinstance(params_dict, dict)
                record = {"review_case_id": params_dict["review_case_id"]}
            return _FakeResult(record)
        return _FakeResult(None)


def test_parse_feature_snapshot_parses_json_dict() -> None:
    assert _parse_feature_snapshot('{"heuristic_band": "review", "phone_exact_match": true}') == {
        "heuristic_band": "review",
        "phone_exact_match": True,
    }


def test_parse_feature_snapshot_returns_empty_on_bad_json() -> None:
    assert _parse_feature_snapshot("not-json") == {}
    assert _parse_feature_snapshot("") == {}


def test_recalculation_updates_match_decision_with_merged_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_score(_tx: object, left: str, right: str) -> MatchResult:
        assert left == "left-person"
        assert right == "right-person"
        return MatchResult(
            decision=MatchDecision.REVIEW,
            confidence=0.75,
            reasons=["Phone exact match (+0.40)", "Name exact match (+0.35)"],
            feature_snapshot={
                "phone_exact_match": True,
                "name_exact_match": True,
            },
        )

    monkeypatch.setattr("src.tasks.score_person_pair", _fake_score)

    tx = _FakeTx(
        get_record={
            "left_person_id": "left-person",
            "right_person_id": "right-person",
            "feature_snapshot": json.dumps(
                {"bridging_identifier_type": "phone", "heuristic_band": "no_match"}
            ),
        },
        update_record=True,
    )

    result = _recalculate_pair_audit_match_in_tx(tx, "rc-1")  # type: ignore[arg-type]

    assert result == "rc-1"
    assert len(tx.calls) == 2
    get_query, get_params = tx.calls[0]
    assert get_query == queries.GET_PERSON_PAIR_REVIEW_CASE
    assert get_params["review_case_id"] == "rc-1"

    update_query, update_params = tx.calls[1]
    assert update_query == queries.UPDATE_PAIR_AUDIT_MATCH_DECISION
    assert update_params["review_case_id"] == "rc-1"
    assert update_params["confidence"] == 0.75
    assert update_params["decision"] == "review"
    assert update_params["reasons"] == [
        "Phone exact match (+0.40)",
        "Name exact match (+0.35)",
    ]
    assert update_params["engine_version"] == "v0.1.0"
    assert update_params["policy_version"] == "v0.1.0"

    snapshot = json.loads(str(update_params["feature_snapshot"]))
    assert snapshot["bridging_identifier_type"] == "phone"
    assert snapshot["heuristic_band"] == "review"
    assert snapshot["phone_exact_match"] is True


def test_recalculation_noop_when_case_not_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _noop_score(_tx: object, _left: str, _right: str) -> MatchResult:
        return MatchResult(decision=MatchDecision.NO_MATCH)

    monkeypatch.setattr("src.tasks.score_person_pair", _noop_score)

    tx = _FakeTx(get_record=None, update_record=True)
    result = _recalculate_pair_audit_match_in_tx(tx, "rc-missing")  # type: ignore[arg-type]

    assert result == "rc-missing"
    assert len(tx.calls) == 1
