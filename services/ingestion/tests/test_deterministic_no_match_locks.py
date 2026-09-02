from __future__ import annotations

from collections.abc import Iterator

from src.matching.deterministic import (
    _FIND_ACTIVE_NO_MATCH_LOCKS,
    evaluate_deterministic,
    prefetch_no_match_lock_owners,
)
from src.models import (
    MatchDecision,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
)


class _Result:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self.records = records

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.records)

    def single(self) -> dict[str, object] | None:
        return self.records[0] if self.records else None


class _Tx:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self.records = records
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **params: object) -> _Result:
        self.calls.append((query, params))
        return _Result(self.records)


def _identifier(
    identifier_type: str,
    normalized_value: str,
    quality_flag: QualityFlag,
) -> NormalizedIdentifier:
    return NormalizedIdentifier(
        identifier_type=identifier_type,
        normalized_value=normalized_value,
        is_verified=False,
        quality_flag=quality_flag,
    )


def test_prefetch_no_match_locks_batches_candidates_and_usable_identifiers() -> None:
    tx = _Tx(
        [
            {"candidate_person_id": "candidate-1", "owner_person_id": "owner-2"},
            {"candidate_person_id": "candidate-1", "owner_person_id": "owner-3"},
            {"candidate_person_id": "candidate-2", "owner_person_id": "owner-4"},
        ]
    )

    owners = prefetch_no_match_lock_owners(
        tx,  # type: ignore[arg-type]
        ["candidate-1", "candidate-2"],
        [
            _identifier("phone", "+6512345678", QualityFlag.VALID),
            _identifier("email", "partial@example.net", QualityFlag.PARTIAL_PARSE),
            _identifier("email", "invalid@example.net", QualityFlag.INVALID_FORMAT),
        ],
    )

    assert owners == {"candidate-1": "owner-2", "candidate-2": "owner-4"}
    assert len(tx.calls) == 1
    query, params = tx.calls[0]
    assert query == _FIND_ACTIVE_NO_MATCH_LOCKS
    assert params["candidate_inputs"] == [
        {"input_index": 0, "person_id": "candidate-1"},
        {"input_index": 1, "person_id": "candidate-2"},
    ]
    assert params["identifier_inputs"] == [
        {
            "input_index": 0,
            "identifier_type": "phone",
            "normalized_value": "+6512345678",
        },
        {
            "input_index": 1,
            "identifier_type": "email",
            "normalized_value": "partial@example.net",
        },
    ]


def test_prefetched_lock_blocks_without_per_candidate_queries() -> None:
    tx = _Tx([])

    result = evaluate_deterministic(
        tx,  # type: ignore[arg-type]
        "candidate-1",
        [_identifier("phone", "+6512345678", QualityFlag.VALID)],
        [],
        RecordType.CONVERSATION,
        no_match_lock_owners={"candidate-1": "owner-1"},
    )

    assert result is not None
    assert result.decision is MatchDecision.NO_MATCH
    assert tx.calls == []


def test_batched_lock_query_preserves_active_lock_semantics() -> None:
    assert "owner:Person {status: 'active'}" in _FIND_ACTIVE_NO_MATCH_LOCKS
    assert "coalesce(rel.is_active, true) = true" in _FIND_ACTIVE_NO_MATCH_LOCKS
    assert "lock.expires_at IS NULL OR lock.expires_at > datetime()" in _FIND_ACTIVE_NO_MATCH_LOCKS
