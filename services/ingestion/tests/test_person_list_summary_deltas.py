"""Unit tests for the maintained Person-list summary delta primitives (#489)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from src.graph.person_list_summary_deltas import (
    DealContribution,
    PersonContribution,
    PersonListSummaryDeltaCollector,
    PersonListSummarySnapshot,
    apply_captured_batch,
    capture,
    plan_deltas,
)
from src.graph.queries.person_list_summary import (
    APPLY_PERSON_LIST_SUMMARY_DELTAS,
    CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS,
    CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID,
    CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS,
)


class _Rows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def single(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


class _Tx:
    """Records the counter queries and answers them with canned rows."""

    def __init__(
        self,
        *,
        persons: list[dict[str, object]] | None = None,
        deals: list[dict[str, object]] | None = None,
        element_deals: list[dict[str, object]] | None = None,
        apply_row: dict[str, object] | None = None,
    ) -> None:
        self.persons = persons or []
        self.deals = deals or []
        self.element_deals = element_deals or []
        self.apply_row = apply_row or {
            "ready": True,
            "core_applied": True,
            "all_deals_applied": True,
            "monthly_applied": True,
            "current_month_key": "2026-11",
            "active_person_count": 0,
            "high_risk_count": 0,
            "high_value_count": 0,
            "no_contact_count": 0,
            "all_deals_count": 0,
            "deals_this_month_count": 0,
        }
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **params: object) -> _Rows:
        self.calls.append((query, dict(params)))
        if query == CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS:
            return _Rows(self.persons)
        if query == CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS:
            return _Rows(self.deals)
        if query == CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID:
            return _Rows(self.element_deals)
        if query == APPLY_PERSON_LIST_SUMMARY_DELTAS:
            return _Rows([self.apply_row])
        raise AssertionError(f"unexpected counter query: {query}")


def _person(
    *,
    qualifying: bool = True,
    high_risk: bool = False,
    high_value: bool = False,
    no_contact: bool = False,
) -> PersonContribution:
    return PersonContribution(
        qualifying_status=qualifying,
        high_risk=high_risk,
        high_value=high_value,
        no_contact=no_contact,
    )


def _deal(*, qualifying: bool = True, observed_at: str | None = None) -> DealContribution:
    return DealContribution(qualifying=qualifying, observed_at=observed_at)


def test_plan_deltas_counts_each_core_flag_once_per_person() -> None:
    before = PersonListSummarySnapshot(
        persons={"p-1": _person()},
        deals={},
    )
    after = PersonListSummarySnapshot(
        persons={
            "p-1": _person(high_risk=True, no_contact=True),
            "p-2": _person(high_value=True),
        },
        deals={},
    )

    plan = plan_deltas(before, after)

    assert plan.active_person_count == 1
    assert plan.high_risk_count == 1
    assert plan.high_value_count == 1
    assert plan.no_contact_count == 1


def test_plan_deltas_ignores_persons_that_left_the_qualifying_set() -> None:
    before = PersonListSummarySnapshot(persons={"p-1": _person(high_risk=True)}, deals={})
    after = PersonListSummarySnapshot(persons={"p-1": _person(qualifying=False)}, deals={})

    plan = plan_deltas(before, after)

    assert plan.active_person_count == -1
    assert plan.high_risk_count == -1


def test_plan_deltas_counts_a_shared_distinct_deal_once() -> None:
    before = PersonListSummarySnapshot(
        persons={},
        deals={"deal-1": _deal(observed_at="2026-11-02T00:00:00Z")},
    )
    after = PersonListSummarySnapshot(
        persons={},
        deals={"deal-1": _deal(observed_at="2026-11-02T00:00:00Z")},
    )

    plan = plan_deltas(before, after)

    assert plan.all_deals_count == 0
    assert plan.before_observed_at == ("2026-11-02T00:00:00Z",)
    assert plan.after_observed_at == ("2026-11-02T00:00:00Z",)


def test_plan_deltas_carries_only_qualifying_observed_at_strings() -> None:
    before = PersonListSummarySnapshot(
        persons={},
        deals={
            "deal-1": _deal(qualifying=False, observed_at=None),
            "deal-2": _deal(observed_at="2026-11-02T00:00:00Z"),
        },
    )
    after = PersonListSummarySnapshot(persons={}, deals={})

    plan = plan_deltas(before, after)

    assert plan.all_deals_count == -1
    assert plan.before_observed_at == ("2026-11-02T00:00:00Z",)
    assert plan.after_observed_at == ()


def test_capture_uses_bounded_sorted_selectors() -> None:
    tx = _Tx(
        persons=[
            {
                "person_id": "p-1",
                "qualifying_status": True,
                "high_risk": False,
                "high_value": False,
                "no_contact": True,
            }
        ],
        deals=[
            {
                "source_record_pk": "sr-1",
                "resolved_element_id": "4:abc:1",
                "qualifying": True,
                "observed_at": "2026-11-02T00:00:00Z",
            }
        ],
        element_deals=[
            {
                "deal_element_id": "4:abc:2",
                "resolved_element_id": "4:abc:2",
                "qualifying": True,
                "observed_at": "2026-11-03T00:00:00Z",
            }
        ],
    )

    snapshot = capture(tx, ["p-2", "p-1", "p-1"], ["sr-1"], ["4:abc:2"])

    assert snapshot.persons["p-1"].no_contact is True
    # A resolved deal is keyed by its element id, whichever selector found it.
    assert snapshot.deals["4:abc:1"].qualifying is True
    assert snapshot.deals["4:abc:2"].observed_at == "2026-11-03T00:00:00Z"
    queries = {query for query, _ in tx.calls}
    assert queries == {
        CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS,
        CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS,
        CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID,
    }
    person_call = next(
        params for query, params in tx.calls if query == CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS
    )
    assert person_call["person_ids"] == ["p-1", "p-2"]
    pk_call = next(
        params for query, params in tx.calls if query == CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS
    )
    assert pk_call["source_record_pks"] == ["sr-1"]
    element_call = next(
        params
        for query, params in tx.calls
        if query == CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID
    )
    assert element_call["deal_element_ids"] == ["4:abc:2"]


def test_apply_captured_batch_applies_once_for_a_bounded_batch() -> None:
    tx = _Tx()

    result = apply_captured_batch(
        tx,
        [
            {"deal_element_id": "4:abc:1", "qualifying": True, "observed_at": "2026-11-02"},
            {"deal_element_id": "4:abc:2", "qualifying": False, "observed_at": None},
        ],
    )

    assert result is not None
    apply_calls = [
        params for query, params in tx.calls if query == APPLY_PERSON_LIST_SUMMARY_DELTAS
    ]
    assert len(apply_calls) == 1
    assert apply_calls[0]["all_deals_delta"] == -1
    assert apply_calls[0]["before_observed_at"] == ["2026-11-02"]
    assert apply_calls[0]["after_observed_at"] == []


def test_apply_captured_batch_without_entries_writes_nothing() -> None:
    tx = _Tx()

    assert apply_captured_batch(tx, []) is None
    assert apply_captured_batch(tx, None) is None
    assert [query for query, _ in tx.calls] == []


def test_apply_captured_batch_rejects_a_malformed_entry() -> None:
    tx = _Tx()

    with pytest.raises(RuntimeError, match="element id"):
        apply_captured_batch(tx, [{"deal_element_id": "", "qualifying": True, "observed_at": None}])
    with pytest.raises(RuntimeError, match="boolean qualifying"):
        apply_captured_batch(tx, [{"deal_element_id": "4:abc:1", "qualifying": "yes"}])
    with pytest.raises(RuntimeError, match="observed_at"):
        apply_captured_batch(
            tx, [{"deal_element_id": "4:abc:1", "qualifying": True, "observed_at": 7}]
        )


def test_collector_keeps_the_first_capture_and_finalizes_once() -> None:
    tx = _Tx(
        persons=[
            {
                "person_id": "p-1",
                "qualifying_status": True,
                "high_risk": False,
                "high_value": False,
                "no_contact": False,
            }
        ],
    )
    collector = PersonListSummaryDeltaCollector()

    collector.capture_before(tx, person_ids=["p-1"])
    collector.capture_before(tx, person_ids=["p-1"])

    # The second capture did not read the node again: the first snapshot wins.
    assert [query for query, _ in tx.calls] == [CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS]

    collector.finalize(tx)

    apply_calls = [
        params for query, params in tx.calls if query == APPLY_PERSON_LIST_SUMMARY_DELTAS
    ]
    assert len(apply_calls) == 1


def test_collector_mark_new_records_an_explicit_zero_snapshot() -> None:
    tx = _Tx()
    collector = PersonListSummaryDeltaCollector()

    collector.mark_new(person_ids=["p-new"], source_record_pks=["sr-new"])

    collector.finalize(tx)

    apply_calls = [
        params for query, params in tx.calls if query == APPLY_PERSON_LIST_SUMMARY_DELTAS
    ]
    assert len(apply_calls) == 1
    assert apply_calls[0]["active_person_delta"] == 0
    assert apply_calls[0]["all_deals_delta"] == 0


def test_collector_rejects_a_second_finalize() -> None:
    tx = _Tx()
    collector = PersonListSummaryDeltaCollector()

    collector.finalize(tx)

    with pytest.raises(RuntimeError, match="already finalized"):
        collector.finalize(tx)


_DORMANT_REPAIR_MODULES = (
    "src/graph/crm_deal_identity_repair_mutation.py",
    "src/graph/crm_deal_identity_repair_verification.py",
    "src/graph/crm_deal_identity_repair_rollback.py",
)


def test_dormant_repair_entries_invalidate_the_counter_before_their_work() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    for relative in _DORMANT_REPAIR_MODULES:
        source = (root / "services" / "ingestion" / relative).read_text(encoding="utf-8")
        assert "invalidate_person_list_summary(self._client)" in source
        invalidate_index = source.index("invalidate_person_list_summary(self._client)")
        write_index = source.index("execute_write")
        assert invalidate_index < write_index
