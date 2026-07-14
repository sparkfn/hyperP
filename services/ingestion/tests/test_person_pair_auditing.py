"""Person↔person review-case detection during ingestion."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from src.graph import queries
from src.models import (
    EngineType,
    MatchDecision,
    MatchResult,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
)
from src.pipeline_person_pairs import audit_person_pairs


def test_person_pair_query_constants_exist() -> None:
    assert "ABOUT_LEFT" in queries.CREATE_PERSON_PAIR_REVIEW_CASE
    assert "ABOUT_RIGHT" in queries.CREATE_PERSON_PAIR_REVIEW_CASE
    assert "pair_audit" in queries.CREATE_PERSON_PAIR_REVIEW_CASE
    # Confidence is now the heuristic score, passed as a parameter (not a literal).
    assert "confidence: $confidence" in queries.CREATE_PERSON_PAIR_REVIEW_CASE
    assert "queue_state IN ['open', 'assigned', 'deferred']" in queries.CHECK_OPEN_PERSON_PAIR_CASE
    assert "IDENTIFIED_BY" in queries.FIND_PERSONS_SHARING_IDENTIFIER


class _Result:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._records)

    def single(self) -> dict[str, object] | None:
        return self._records[0] if self._records else None


class _ScriptedTx:
    """Dispatches results by query content; records the create calls."""

    def __init__(
        self,
        *,
        fanout: int,
        person_ids: list[str],
        is_locked: bool = False,
        existing_case: str | None = None,
        idents: list[dict[str, object]] | None = None,
        facts: list[dict[str, object]] | None = None,
        addrs: list[dict[str, object]] | None = None,
        pair_attrs: dict[str, object] | None = None,
    ) -> None:
        self.fanout = fanout
        self.person_ids = person_ids
        self.is_locked = is_locked
        self.existing_case = existing_case
        # Candidate-snapshot rows returned for every person (content-dispatched,
        # so both sides of a pair see the same rows — enough to exercise scoring).
        self.idents = idents or []
        self.facts = facts or []
        self.addrs = addrs or []
        self.pair_attrs = pair_attrs or {
            "left_status": "active",
            "left_completeness": 0.0,
            "left_created_at": "2026-01-01T00:00:00Z",
            "right_status": "active",
            "right_completeness": 0.0,
            "right_created_at": "2026-01-01T00:00:00Z",
        }
        self.create_calls: list[dict[str, object]] = []
        self.match_decision_calls: list[dict[str, object]] = []
        self._created = 0

    def run(self, query: str, **params: object) -> _Result:
        if "RETURN count(DISTINCT p) AS fanout" in query:
            return _Result([{"fanout": self.fanout}])
        if "RETURN id.identifier_type AS identifier_type" in query:
            return _Result(list(self.idents))
        if "RETURN f.attribute_name AS attribute_name" in query:
            return _Result(list(self.facts))
        if "addr.address_id AS address_id" in query:
            return _Result(list(self.addrs))
        if "collect(DISTINCT p.person_id) AS person_ids" in query:
            return _Result([{"person_ids": list(self.person_ids)}])
        if "RETURN count(lock) > 0 AS is_locked" in query:
            return _Result([{"is_locked": self.is_locked}])
        if "left_status" in query and "right_status" in query:
            attrs = dict(self.pair_attrs)
            attrs["left_person_id"] = params["left_person_id"]
            attrs["right_person_id"] = params["right_person_id"]
            return _Result([attrs])
        if (
            "queue_state IN ['open', 'assigned', 'deferred']" in query
            and "review_case_id" in query
            and "CREATE" not in query
        ):
            return _Result([{"review_case_id": self.existing_case}] if self.existing_case else [])
        if "CREATE (rc:ReviewCase" in query:
            self.create_calls.append(dict(params))
            self._created += 1
            return _Result([{"review_case_id": f"rc-{self._created}"}])
        if "RETURN md.match_decision_id AS match_decision_id" in query:
            self.match_decision_calls.append(dict(params))
            return _Result([{"match_decision_id": "md-1"}])
        return _Result([])


def _nric() -> list[NormalizedIdentifier]:
    return [
        NormalizedIdentifier(
            identifier_type="nric",
            normalized_value="hash-shared",
            quality_flag=QualityFlag.VALID,
        )
    ]


def test_pair_below_020_does_not_open_review() -> None:
    tx = _ScriptedTx(fanout=2, person_ids=["person-b", "person-a"])
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == []
    assert tx.create_calls == []


def test_pair_at_040_auto_merges_instead_of_opening_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.pipeline_person_pairs as module

    merges: list[tuple[str, str]] = []

    def fake_merge(
        tx: object, *, absorbed_id: str, survivor_id: str, match_decision_id: str, reason: str
    ) -> str:
        merges.append((absorbed_id, survivor_id))
        return "me-1"

    monkeypatch.setattr(module, "merge_person_pair", fake_merge)
    tx = _ScriptedTx(
        fanout=2,
        person_ids=["person-a", "person-b"],
        idents=[
            {"identifier_type": "phone", "normalized_value": "+6580000000", "is_verified": False},
            {"identifier_type": "email", "normalized_value": "a@example.com", "is_verified": False},
        ],
        pair_attrs={
            "left_status": "active",
            "left_completeness": 0.8,
            "left_created_at": "2026-01-01T00:00:00Z",
            "right_status": "active",
            "right_completeness": 0.2,
            "right_created_at": "2026-01-01T00:00:00Z",
        },
    )
    assert audit_person_pairs(tx, _nric()) == []  # type: ignore[arg-type]
    assert tx.create_calls == []
    assert merges == [("person-b", "person-a")]


def test_pair_at_020_opens_review_case() -> None:
    tx = _ScriptedTx(
        fanout=2,
        person_ids=["person-a", "person-b"],
        idents=[
            {"identifier_type": "phone", "normalized_value": "+6580000000", "is_verified": False}
        ],
    )
    assert audit_person_pairs(tx, _nric()) == ["rc-1"]  # type: ignore[arg-type]
    assert tx.create_calls[0]["confidence"] == 0.20


def _score(confidence: float, *, hard_conflict: bool = False) -> MatchResult:
    return MatchResult(
        decision=MatchDecision.REVIEW,
        confidence=confidence,
        reasons=["scripted score"],
        engine_type=EngineType.HEURISTIC,
        feature_snapshot={"dob_conflict": hard_conflict},
    )


def test_relationship_pair_at_020_auto_merges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.pipeline_person_pairs as module

    merges: list[tuple[str, str]] = []
    monkeypatch.setattr(module, "score_person_pair", lambda *_args: _score(0.20))
    monkeypatch.setattr(
        module,
        "merge_person_pair",
        lambda _tx, *, absorbed_id, survivor_id, **_kwargs: (
            merges.append((absorbed_id, survivor_id)) or "me-relationship"
        ),
    )
    tx = _ScriptedTx(fanout=2, person_ids=["person-a", "person-b"])

    assert audit_person_pairs(tx, _nric(), RecordType.RELATIONSHIP) == []  # type: ignore[arg-type]
    assert merges == [("person-b", "person-a")]
    assert tx.create_calls == []


@pytest.mark.parametrize(
    ("confidence", "expected_cases"),
    [(0.099, []), (0.10, ["rc-1"]), (0.199, ["rc-1"])],
)
def test_relationship_pair_review_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    confidence: float,
    expected_cases: list[str],
) -> None:
    import src.pipeline_person_pairs as module

    monkeypatch.setattr(module, "score_person_pair", lambda *_args: _score(confidence))
    tx = _ScriptedTx(fanout=2, person_ids=["person-a", "person-b"])

    assert audit_person_pairs(  # type: ignore[arg-type]
        tx, _nric(), RecordType.RELATIONSHIP
    ) == expected_cases


def test_relationship_pair_hard_conflict_vetoes_auto_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.pipeline_person_pairs as module

    monkeypatch.setattr(
        module,
        "score_person_pair",
        lambda *_args: _score(0.20, hard_conflict=True),
    )
    tx = _ScriptedTx(fanout=2, person_ids=["person-a", "person-b"])

    assert audit_person_pairs(  # type: ignore[arg-type]
        tx, _nric(), RecordType.RELATIONSHIP
    ) == ["rc-1"]
    assert tx.match_decision_calls == []
    assert tx.create_calls[0]["confidence"] == 0.20


def test_existing_open_case_suppresses_duplicate() -> None:
    tx = _ScriptedTx(fanout=2, person_ids=["person-a", "person-b"], existing_case="rc-old")
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == []
    assert tx.create_calls == []


def test_active_no_match_lock_suppresses_case() -> None:
    tx = _ScriptedTx(fanout=2, person_ids=["person-a", "person-b"], is_locked=True)
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == []
    assert tx.create_calls == []


def test_fanout_over_cap_skips_identifier() -> None:
    # nric cap is 5; fanout 6 exceeds it.
    tx = _ScriptedTx(fanout=6, person_ids=["a", "b", "c", "d", "e", "f"])
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == []
    assert tx.create_calls == []


def test_three_persons_produce_three_pairwise_cases() -> None:
    tx = _ScriptedTx(
        fanout=3,
        person_ids=["person-c", "person-a", "person-b"],
        idents=[
            {"identifier_type": "phone", "normalized_value": "+6580000000", "is_verified": False}
        ],
    )
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == ["rc-1", "rc-2", "rc-3"]
    pairs = {(c["left_person_id"], c["right_person_id"]) for c in tx.create_calls}
    assert pairs == {
        ("person-a", "person-b"),
        ("person-a", "person-c"),
        ("person-b", "person-c"),
    }


def test_unusable_identifier_skipped() -> None:
    bad = [
        NormalizedIdentifier(
            identifier_type="nric",
            normalized_value="hash-shared",
            quality_flag=QualityFlag.INVALID_FORMAT,
        )
    ]
    tx = _ScriptedTx(fanout=2, person_ids=["person-a", "person-b"])
    created = audit_person_pairs(tx, bad)  # type: ignore[arg-type]
    assert created == []


def test_pipeline_imports_audit_person_pairs() -> None:
    import src.pipeline as pipeline_module

    assert hasattr(pipeline_module, "audit_person_pairs")
