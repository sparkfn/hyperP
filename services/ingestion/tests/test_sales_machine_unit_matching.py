from __future__ import annotations

from collections.abc import Iterator
from typing import cast
from unittest.mock import patch

from neo4j import ManagedTransaction

from src.matching.machine_unit_heuristic import (
    MACHINE_UNIT_BOUGHT_CONFIDENCE,
    MACHINE_UNIT_OWNS_CONFIDENCE,
)
from src.pipeline_sales import _propose_one_pending_sale, propose_machine_unit_matches_for_pending_sales


class _Result:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self._row = row
        self._rows = rows if rows is not None else ([] if row is None else [row])

    def single(self) -> dict[str, object] | None:
        return self._row

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)


class _Tx:
    def __init__(self, candidates: list[dict[str, object]] | None = None) -> None:
        self._candidates: list[dict[str, object]] = candidates or []
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> _Result:
        self.calls.append((query, dict(kwargs)))
        # FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES: unique fragment
        if "INVOLVES_UNIT {source_record_pk: $sales_source_record_pk}" in query:
            return _Result(rows=self._candidates)
        # FIND_PENDING_CUSTOMER_SALES: has $limit parameter
        if "LIMIT $limit" in query:
            return _Result(
                rows=[{"source_record_pk": "sr-pending", "source_system_key": "sys", "raw_payload": "{}"}]
            )
        return _Result()


class _Session:
    def __init__(self, tx: _Tx) -> None:
        self.tx = tx

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute_write(self, callback: object) -> object:
        return callback(cast(ManagedTransaction, self.tx))  # type: ignore[arg-type]


class _Client:
    """Fake Neo4jClient that hands out sessions in order."""

    def __init__(self, *txs: _Tx) -> None:
        self._sessions = [_Session(tx) for tx in txs]

    def session(self) -> _Session:
        return self._sessions.pop(0)


# ---------------------------------------------------------------------------
# _propose_one_pending_sale
# ---------------------------------------------------------------------------

def test_propose_no_candidates_returns_false() -> None:
    tx = _Tx(candidates=[])
    with (
        patch("src.pipeline_sales.persist_match_decision") as mock_persist,
        patch("src.pipeline_sales.create_review_case_if_needed") as mock_create,
    ):
        result = _propose_one_pending_sale(cast(ManagedTransaction, tx), "sr-1")

    assert result is False
    mock_persist.assert_not_called()
    mock_create.assert_not_called()
    assert not any("pending_review" in q for q, _ in tx.calls)


def test_propose_owns_unit_candidate_creates_review_case() -> None:
    candidate_row: dict[str, object] = {
        "person_id": "person-1",
        "machine_unit_id": "unit-1",
        "rel_type": "OWNS_UNIT",
        "is_active": True,
        "conflict_flag": False,
        "last_confirmed_at": "2026-06-01T00:00:00+00:00",
    }
    tx = _Tx(candidates=[candidate_row])

    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-1") as mock_persist,
        patch("src.pipeline_sales.create_review_case_if_needed", return_value="rc-1") as mock_create,
    ):
        result = _propose_one_pending_sale(cast(ManagedTransaction, tx), "sr-1")

    assert result is True
    mock_persist.assert_called_once()
    match_result_arg = mock_persist.call_args[0][1]
    assert match_result_arg.confidence == MACHINE_UNIT_OWNS_CONFIDENCE
    assert match_result_arg.matched_person_id == "person-1"
    mock_create.assert_called_once()

    pending_review_calls = [(q, k) for q, k in tx.calls if "pending_review" in q]
    assert len(pending_review_calls) == 1
    assert pending_review_calls[0][1]["source_record_pk"] == "sr-1"


def test_propose_bought_unit_candidate_uses_lower_confidence() -> None:
    candidate_row: dict[str, object] = {
        "person_id": "person-2",
        "machine_unit_id": "unit-2",
        "rel_type": "BOUGHT_UNIT",
        "is_active": False,
        "conflict_flag": False,
        "last_confirmed_at": "2026-06-01T00:00:00+00:00",
    }
    tx = _Tx(candidates=[candidate_row])

    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-2") as mock_persist,
        patch("src.pipeline_sales.create_review_case_if_needed", return_value="rc-2"),
    ):
        _propose_one_pending_sale(cast(ManagedTransaction, tx), "sr-2")

    match_result_arg = mock_persist.call_args[0][1]
    assert match_result_arg.confidence == MACHINE_UNIT_BOUGHT_CONFIDENCE


def test_propose_selects_best_among_multiple_candidates() -> None:
    """OWNS_UNIT (active, no conflict) beats BOUGHT_UNIT even with a more recent date."""
    candidates: list[dict[str, object]] = [
        {
            "person_id": "person-bought",
            "machine_unit_id": "unit-1",
            "rel_type": "BOUGHT_UNIT",
            "is_active": False,
            "conflict_flag": False,
            "last_confirmed_at": "2026-06-10T00:00:00+00:00",
        },
        {
            "person_id": "person-owns",
            "machine_unit_id": "unit-1",
            "rel_type": "OWNS_UNIT",
            "is_active": True,
            "conflict_flag": False,
            "last_confirmed_at": "2026-05-01T00:00:00+00:00",
        },
    ]
    tx = _Tx(candidates=candidates)

    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-x") as mock_persist,
        patch("src.pipeline_sales.create_review_case_if_needed", return_value="rc-x"),
    ):
        _propose_one_pending_sale(cast(ManagedTransaction, tx), "sr-x")

    match_result_arg = mock_persist.call_args[0][1]
    assert match_result_arg.matched_person_id == "person-owns"


# ---------------------------------------------------------------------------
# propose_machine_unit_matches_for_pending_sales
# ---------------------------------------------------------------------------

def test_propose_orchestration_returns_count_of_created_cases() -> None:
    """One pending sale, one candidate → one ReviewCase proposed → returns 1."""
    candidate_row: dict[str, object] = {
        "person_id": "person-1",
        "machine_unit_id": "unit-1",
        "rel_type": "OWNS_UNIT",
        "is_active": True,
        "conflict_flag": False,
        "last_confirmed_at": "2026-06-01T00:00:00+00:00",
    }
    # Two sessions: first for _get_pending_pks, second for _propose_one_pending_sale
    get_tx = _Tx()
    propose_tx = _Tx(candidates=[candidate_row])
    client = _Client(get_tx, propose_tx)

    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-1"),
        patch("src.pipeline_sales.create_review_case_if_needed", return_value="rc-1"),
    ):
        count = propose_machine_unit_matches_for_pending_sales(client)

    assert count == 1


def test_propose_orchestration_no_pending_returns_zero() -> None:
    class _EmptyTx(_Tx):
        def run(self, query: str, **kwargs: object) -> _Result:
            self.calls.append((query, dict(kwargs)))
            return _Result(rows=[])

    client = _Client(_EmptyTx())
    count = propose_machine_unit_matches_for_pending_sales(client)
    assert count == 0
