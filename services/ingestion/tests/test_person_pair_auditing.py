"""Person↔person review-case detection during ingestion."""

from __future__ import annotations

from collections.abc import Iterator

from src.graph import queries
from src.models import NormalizedIdentifier, QualityFlag
from src.pipeline_person_pairs import audit_person_pairs


def test_person_pair_query_constants_exist() -> None:
    assert "ABOUT_LEFT" in queries.CREATE_PERSON_PAIR_REVIEW_CASE
    assert "ABOUT_RIGHT" in queries.CREATE_PERSON_PAIR_REVIEW_CASE
    assert "pair_audit" in queries.CREATE_PERSON_PAIR_REVIEW_CASE
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
    ) -> None:
        self.fanout = fanout
        self.person_ids = person_ids
        self.is_locked = is_locked
        self.existing_case = existing_case
        self.create_calls: list[dict[str, object]] = []
        self._created = 0

    def run(self, query: str, **params: object) -> _Result:
        if "RETURN count(DISTINCT p) AS fanout" in query:
            return _Result([{"fanout": self.fanout}])
        if "collect(DISTINCT p.person_id) AS person_ids" in query:
            return _Result([{"person_ids": list(self.person_ids)}])
        if "RETURN count(lock) > 0 AS is_locked" in query:
            return _Result([{"is_locked": self.is_locked}])
        if (
            "queue_state IN ['open', 'assigned', 'deferred']" in query
            and "review_case_id" in query
            and "CREATE" not in query
        ):
            return _Result(
                [{"review_case_id": self.existing_case}] if self.existing_case else []
            )
        if "CREATE (rc:ReviewCase" in query:
            self.create_calls.append(dict(params))
            self._created += 1
            return _Result([{"review_case_id": f"rc-{self._created}"}])
        return _Result([])


def _nric() -> list[NormalizedIdentifier]:
    return [
        NormalizedIdentifier(
            identifier_type="nric",
            normalized_value="hash-shared",
            quality_flag=QualityFlag.VALID,
        )
    ]


def test_two_active_persons_open_one_ordered_pair_case() -> None:
    tx = _ScriptedTx(fanout=2, person_ids=["person-b", "person-a"])
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == ["rc-1"]
    assert len(tx.create_calls) == 1
    call = tx.create_calls[0]
    # Canonical ordering: left < right.
    assert call["left_person_id"] == "person-a"
    assert call["right_person_id"] == "person-b"
    assert "nric" in str(call["feature_snapshot"])


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
    tx = _ScriptedTx(fanout=3, person_ids=["person-c", "person-a", "person-b"])
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
