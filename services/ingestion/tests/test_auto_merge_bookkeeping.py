from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from neo4j import ManagedTransaction
from src.graph import queries
from src.models import MatchDecision, MatchResult
from src.pipeline_writes import record_auto_merge_event

type ParamValue = str | float | int | list[str] | None
type Params = Mapping[str, ParamValue]


class _Result:
    def __init__(self, record: dict[str, str] | None) -> None:
        self._record = record

    def single(self) -> dict[str, str] | None:
        return self._record


@dataclass(frozen=True)
class _Call:
    query: str
    params: Params


class _Tx:
    def __init__(self) -> None:
        self.calls: list[_Call] = []

    def run(self, query: str, **params: ParamValue) -> _Result:
        self.calls.append(_Call(query=query, params=params))
        if query == queries.CREATE_MERGE_EVENT_AUTO_MERGE:
            return _Result({"merge_event_id": "merge-1"})
        return _Result(None)


def test_record_auto_merge_event_links_decision_and_affected_record() -> None:
    tx = _Tx()
    match_result = MatchResult(
        decision=MatchDecision.MERGE,
        confidence=0.99,
        reasons=["verified identifier", "trusted source"],
        candidate_count=1,
        matched_person_id="person-survivor",
    )

    record_auto_merge_event(
        cast(ManagedTransaction, tx),
        match_result=match_result,
        match_decision_id="decision-1",
        person_id="person-survivor",
        source_record_pk="sr-1",
    )

    assert [call.query for call in tx.calls] == [
        queries.CREATE_MERGE_EVENT_AUTO_MERGE,
        queries.LINK_MERGE_EVENT_TRIGGERED_BY,
        queries.LINK_MERGE_EVENT_AFFECTED_RECORD,
    ]
    assert tx.calls[0].params == {
        "from_person_id": "person-survivor",
        "to_person_id": "person-survivor",
        "reason": "verified identifier; trusted source",
    }
    assert tx.calls[1].params == {
        "merge_event_id": "merge-1",
        "match_decision_id": "decision-1",
    }
    assert tx.calls[2].params == {
        "merge_event_id": "merge-1",
        "source_record_pk": "sr-1",
    }
