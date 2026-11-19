"""Unit tests for the maintained Person-list summary drift accounting (#489)."""

from __future__ import annotations

from src.graph.person_list_summary import (
    PersonListSummaryCounts,
    PersonListSummaryPreviousState,
    PersonListSummaryPublication,
    exceeds_person_list_summary_drift_threshold,
    person_list_summary_alarming_fields,
    person_list_summary_field_drifts,
    person_list_summary_month_rolled_over,
)
from src.graph.queries.person_list_summary import RECOMPUTE_PERSON_LIST_SUMMARY


def _previous(
    *,
    active_person_count: object = 40,
    high_risk_count: object = 7,
    high_value_count: object = 5,
    no_contact_count: object = 3,
    deals_this_month_count: object = 9,
    all_deals_count: object = 24,
    month: object = "2026-11",
) -> PersonListSummaryPreviousState:
    return PersonListSummaryPreviousState(
        active_person_count=active_person_count,
        high_risk_count=high_risk_count,
        high_value_count=high_value_count,
        no_contact_count=no_contact_count,
        deals_this_month_count=deals_this_month_count,
        all_deals_count=all_deals_count,
        month=month,
        ready=True,
    )


def _new(
    *,
    active_person_count: int = 40,
    high_risk_count: int = 7,
    high_value_count: int = 5,
    no_contact_count: int = 3,
    deals_this_month_count: int = 9,
    all_deals_count: int = 24,
) -> PersonListSummaryCounts:
    return PersonListSummaryCounts(
        active_person_count=active_person_count,
        high_risk_count=high_risk_count,
        high_value_count=high_value_count,
        no_contact_count=no_contact_count,
        deals_this_month_count=deals_this_month_count,
        all_deals_count=all_deals_count,
    )


def _publication(
    previous: PersonListSummaryPreviousState,
    new: PersonListSummaryCounts,
    *,
    new_month: str = "2026-11",
) -> PersonListSummaryPublication:
    return PersonListSummaryPublication(
        previous=previous,
        new=new,
        new_month=new_month,
        activated=False,
    )


def test_drift_threshold_is_one_per_mille_of_the_person_count() -> None:
    assert exceeds_person_list_summary_drift_threshold(1, 1000) is True
    assert exceeds_person_list_summary_drift_threshold(1, 1001) is False
    assert exceeds_person_list_summary_drift_threshold(5, 1000) is True
    assert exceeds_person_list_summary_drift_threshold(0, 1000) is False


def test_zero_denominator_requires_exactly_zero_drift() -> None:
    assert exceeds_person_list_summary_drift_threshold(0, 0) is False
    assert exceeds_person_list_summary_drift_threshold(1, 0) is True


def test_field_drift_reports_the_absolute_change_per_field() -> None:
    publication = _publication(
        _previous(high_risk_count=7),
        _new(high_risk_count=9),
    )

    reports = person_list_summary_field_drifts(publication)

    assert [report.field for report in reports] == [
        "active_person_count",
        "high_risk_count",
        "high_value_count",
        "no_contact_count",
        "deals_this_month_count",
        "all_deals_count",
    ]
    high_risk = reports[1]
    assert high_risk.drift == 2
    assert high_risk.valid_previous is True
    assert high_risk.previous == 7
    assert high_risk.current == 9


def test_field_drift_reports_an_unknown_previous_value() -> None:
    publication = _publication(
        _previous(high_risk_count="seven"),
        _new(high_risk_count=9),
    )

    high_risk = person_list_summary_field_drifts(publication)[1]

    assert high_risk.drift is None
    assert high_risk.valid_previous is False
    assert high_risk.previous == "seven"


def test_alarming_fields_skip_the_excluded_month_scoped_field() -> None:
    publication = _publication(
        _previous(high_risk_count=7),
        _new(high_risk_count=9),
    )

    drifts = person_list_summary_field_drifts(publication)

    alarming = person_list_summary_alarming_fields(
        drifts,
        denominator=1000,
        excluded_fields=frozenset({"deals_this_month_count"}),
    )
    assert [report.field for report in alarming] == ["high_risk_count"]


def test_month_rollover_needs_a_valid_earlier_marker() -> None:
    assert (
        person_list_summary_month_rolled_over(
            _publication(_previous(month="2026-10"), _new(), new_month="2026-11")
        )
        is True
    )
    assert (
        person_list_summary_month_rolled_over(
            _publication(_previous(month="2026-11"), _new(), new_month="2026-11")
        )
        is False
    )
    assert (
        person_list_summary_month_rolled_over(
            _publication(_previous(month=None), _new(), new_month="2026-11")
        )
        is False
    )
    assert (
        person_list_summary_month_rolled_over(
            _publication(_previous(month="not-a-month"), _new(), new_month="2026-11")
        )
        is False
    )


def test_recompute_query_uses_two_realtime_samples_and_returns_prior_readiness() -> None:
    query = RECOMPUTE_PERSON_LIST_SUMMARY
    lock = query.index("SET c.updated_at = datetime()")
    scan = query.index("datetime.truncate('month', datetime.realtime()) AS scan_month")
    final = query.index("datetime.realtime() AS current_time")

    assert lock < scan < final
    assert query.count("datetime.realtime()") == 2
    assert "right('0' + toString(current_time.month), 2)" in query
    assert "previous_ready AS previous_ready" in query
    assert "substring(" not in query
