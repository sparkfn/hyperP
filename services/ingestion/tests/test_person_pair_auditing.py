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
        self.create_calls: list[dict[str, object]] = []
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
    # No snapshot rows -> heuristic finds no shared evidence -> confidence 0.0.
    assert call["confidence"] == 0.0


def test_pair_case_carries_heuristic_confidence() -> None:
    # Both persons (content-dispatched mock) share a verified phone and the same
    # name, so the reused record-engine scorer yields a real confidence:
    # verified phone (+0.35) + high name similarity (+0.20) = 0.55.
    tx = _ScriptedTx(
        fanout=2,
        person_ids=["person-a", "person-b"],
        idents=[
            {"identifier_type": "phone", "normalized_value": "+6580000000", "is_verified": True}
        ],
        facts=[{"attribute_name": "full_name", "attribute_value": "Alice Tan"}],
    )
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == ["rc-1"]
    call = tx.create_calls[0]
    confidence = call["confidence"]
    assert isinstance(confidence, float)
    assert confidence > 0.5
    # Heuristic signals are merged into the persisted feature snapshot, and the
    # bridging-identifier provenance is preserved alongside them.
    snap = str(call["feature_snapshot"])
    assert "phone_exact_match" in snap
    assert "heuristic_band" in snap
    assert "bridging_identifier_value" in snap


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
