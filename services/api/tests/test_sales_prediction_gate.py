"""Point-in-time label and privacy-safe sufficiency tests for issue #149."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

from src.graph.queries.sales_prediction_gate import (
    GATE_DEAL_VERSIONS_FOR_PARENTS,
    GATE_RELEASE,
    GATE_STAGE_EVENTS_PAGE,
)
from src.sales_prediction_discovery import build_parser, render_gate_markdown
from src.sales_prediction_gate_labels import build_labels
from src.sales_prediction_gate_models import (
    DealVersion,
    GateRelease,
    GateReport,
    LabelEvidence,
    MappedState,
    StageEvent,
)
from src.sales_prediction_gate_report import build_gate_report

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_PARENT = ("private-source", "private-deal-1")


def _release(*, cutoff_days: int = 365, complete: bool = True) -> GateRelease:
    return GateRelease(
        enabled=True,
        mapping_version="crm-stage-map-2026-08-18-v1",
        policy_version="crm-stage-lifecycle-policy-2026-08-18-v1",
        accepted_at=_BASE + timedelta(days=cutoff_days),
        evidence_cutoff_at=_BASE + timedelta(days=cutoff_days),
        source_accounting_complete=complete,
        analytical_release_consistent=complete,
        restated_event_count=0,
    )


def _event(
    state: str,
    day: int,
    *,
    available_day: int | None = None,
    identity: str | None = None,
    parent: tuple[str, str] = _PARENT,
    head: int = 1,
) -> StageEvent:
    return StageEvent(
        event_identity=identity or f"private-event-{state}-{day}-{head}",
        parent_key=parent,
        mapped_state=cast(MappedState, state),
        event_at=_BASE + timedelta(days=day),
        available_at=_BASE + timedelta(days=day if available_day is None else available_day),
        authority_head_version=head,
    )


def _version(
    *,
    parent: tuple[str, str] = _PARENT,
    entity: str = "fundbox",
    linked: int = 1,
    version: int = 1,
    activated_day: int = -1,
    superseded_day: int | None = None,
) -> DealVersion:
    return DealVersion(
        parent_key=parent,
        version_key=f"private-version-{version}",
        source_record_version=version,
        entity_key=entity,
        observed_at=_BASE + timedelta(days=-1),
        ingested_at=_BASE + timedelta(days=-1),
        activated_at=_BASE + timedelta(days=activated_day),
        superseded_at=(
            _BASE + timedelta(days=superseded_day) if superseded_day is not None else None
        ),
        rejected_at=None,
        link_failed_at=None,
        linked_person_count=linked,
        active_person_count=linked,
        latest_linked_at=_BASE + timedelta(days=-1),
        timestamps_valid=True,
        amount_state="known",
        currency_status="supported",
    )


def _one(events: list[StageEvent], *, cutoff_days: int = 365) -> LabelEvidence:
    labels = build_labels(_release(cutoff_days=cutoff_days), events, [_version()], ("fundbox",))
    assert len(labels) == 1
    return labels[0]


def test_gate_parser_does_not_require_discovery_cutoffs() -> None:
    args = build_parser().parse_args(
        [
            "--gate",
            "--entities",
            "fundbox",
            "--expected-mapping-version",
            "crm-stage-map-2026-08-18-v1",
            "--expected-policy-version",
            "crm-stage-lifecycle-policy-2026-08-18-v1",
            "--json-output",
            "private-output.json",
            "--markdown-output",
            "private-output.md",
        ]
    )

    assert args.as_of_at is None
    assert args.report_cutoff_at is None


def test_invalid_or_inverted_stage_timestamps_censor_the_parent_timeline() -> None:
    from src.sales_prediction_gate_labels import parse_stage_rows

    rows = [
        {
            "event_identity": "private-invalid-event",
            "parent_source_system": _PARENT[0],
            "parent_source_record_id": _PARENT[1],
            "mapped_state": "lost",
            "event_at": "2026-01-02T00:00:00Z",
            "available_at": "2026-01-01T00:00:00Z",
            "authority_head_version": 1,
        }
    ]

    events, invalid = parse_stage_rows(rows)

    assert events == []
    assert invalid == frozenset({_PARENT})


def test_cutoff_is_open_at_s_and_closed_at_s_plus_30_days() -> None:
    at_s = _one([_event("open", 0), _event("won", 0, head=2)])
    at_horizon = _one([_event("open", 0), _event("won", 30)])

    assert at_s.status == "ineligible"
    assert at_s.reason == "not_open_as_known"
    assert at_horizon.status == "positive"


def test_won_before_snapshot_and_repeated_won_do_not_create_a_second_positive() -> None:
    labels = build_labels(
        _release(),
        [
            _event("won", -10),
            _event("lost", -5),
            _event("open", 0),
            _event("won", 10),
        ],
        [_version()],
        ("fundbox",),
    )

    assert [item.status for item in labels] == ["negative"]


def test_lost_then_reopen_creates_a_new_snapshot_and_revert_uses_later_state() -> None:
    labels = build_labels(
        _release(),
        [
            _event("open", 0),
            _event("lost", 5),
            _event("open", 10),
            _event("lost", 10, head=2),
            _event("open", 11),
            _event("won", 20),
        ],
        [_version()],
        ("fundbox",),
    )

    assert len(labels) == 3
    assert labels[1].status == "ineligible"
    assert labels[1].reason == "not_open_as_known"
    assert labels[0].status == "positive"
    assert labels[2].status == "positive"


def test_mature_and_immature_non_won_horizons_are_distinct() -> None:
    mature = _one([_event("open", 0)], cutoff_days=30)
    immature = _one([_event("open", 1)], cutoff_days=30)

    assert mature.status == "negative"
    assert immature.status == "unknown"


def test_late_pre_snapshot_disqualifier_censors_instead_of_leaking() -> None:
    label = _one(
        [
            _event("lost", -1, available_day=1),
            _event("open", 0),
        ]
    )

    assert label.status == "censored"
    assert label.reason == "censored_retrospective_disqualifier"


def test_missing_ambiguous_or_late_parent_and_linkage_fail_closed() -> None:
    open_event = [_event("open", 0)]
    missing = build_labels(_release(), open_event, [], ("fundbox",))[0]
    ambiguous = build_labels(
        _release(), open_event, [_version(version=2), _version(version=2)], ("fundbox",)
    )[0]
    late_link = replace(_version(), latest_linked_at=_BASE + timedelta(days=1))

    assert (missing.status, missing.reason) == ("censored", "missing_parent_at_snapshot")
    assert (ambiguous.status, ambiguous.reason) == ("censored", "selected_parent_ambiguity")
    assert build_labels(_release(), open_event, [late_link], ("fundbox",))[0].status == ("censored")


def test_incomplete_release_conflict_censors_all_candidates() -> None:
    label = build_labels(_release(complete=False), [_event("open", 0)], [_version()], ("fundbox",))[
        0
    ]

    assert label.reason == "source_authority_incomplete"


def test_stable_event_order_and_deterministic_rerun() -> None:
    events = [
        _event("won", 20),
        _event("open", 0, identity="z"),
        _event("lost", 0, identity="a", head=2),
    ]

    first = build_labels(_release(), events, [_version()], ("fundbox",))
    second = build_labels(_release(), list(reversed(events)), [_version()], ("fundbox",))

    assert first == second
    assert first[0].reason == "not_open_as_known"


def _aggregate_labels(
    *, positives: int, negatives: int, months: int, entity: str = "fundbox"
) -> list[LabelEvidence]:
    rows: list[LabelEvidence] = []
    total = positives + negatives
    for index in range(total):
        month = (index % months) + 1
        rows.append(
            LabelEvidence(
                private_parent_key=("private", f"deal-{index}"),
                snapshot_at=datetime(2026, month, 1, tzinfo=UTC),
                month=f"2026-{month:02d}",
                entity_key=entity,
                status="positive" if index < positives else "negative",
                reason="test",
                mature=True,
                person_linked=True,
                timestamp_valid=True,
                history_determinate=True,
                amount_state="known",
                currency_status="supported",
            )
        )
    return rows


def _report(labels: list[LabelEvidence]) -> GateReport:
    return build_gate_report(
        labels,
        _release(),
        ("fundbox",),
        generated_at="2026-08-18T00:00:00Z",
        selector_version="open-episode-entry-v1",
        eligibility_version="crm-won-eligibility-v1",
        restatement_version="authority-head-v1",
    )


def test_threshold_boundaries_for_go_rules_only_and_insufficient() -> None:
    assert (
        _report(_aggregate_labels(positives=200, negatives=1800, months=6))
        .populations[0]
        .recommendation
        == "go"
    )
    assert (
        _report(_aggregate_labels(positives=50, negatives=450, months=3))
        .populations[0]
        .recommendation
        == "rules_only"
    )
    assert (
        _report(_aggregate_labels(positives=49, negatives=451, months=3))
        .populations[0]
        .recommendation
        == "collect_more_data"
    )


def test_gate_queries_page_stage_events_and_bound_deal_parent_batches() -> None:
    assert "projection.event_identity > $after_event_identity" in GATE_STAGE_EVENTS_PAGE
    assert "ORDER BY event_identity" in GATE_STAGE_EVENTS_PAGE
    assert "LIMIT $limit" in GATE_STAGE_EVENTS_PAGE
    assert "UNWIND $parents AS parent" in GATE_DEAL_VERSIONS_FOR_PARENTS
    assert "WITH DISTINCT" not in GATE_DEAL_VERSIONS_FOR_PARENTS


def test_release_query_binds_persisted_acceptance_without_live_source_reconciliation() -> None:
    assert "release.boundary_digest IS NOT NULL AS boundary_bound" in GATE_RELEASE
    assert "release.reconciliation_digest IS NOT NULL AS reconciliation_bound" in GATE_RELEASE
    assert "StageHistoryOccurrence" not in GATE_RELEASE
    assert "StageHistoryRetry" not in GATE_RELEASE
    assert "CrmHistoryInvalidationIntent" not in GATE_RELEASE


def test_immature_unknown_is_not_counted_as_data_quality_censoring() -> None:
    labels = _aggregate_labels(positives=50, negatives=450, months=3)
    labels.append(
        replace(
            labels[0],
            private_parent_key=("private", "immature"),
            status="unknown",
            reason="immature_horizon",
            mature=False,
        )
    )

    decision = _report(labels).populations[0]

    assert decision.metrics.data_quality_unknown_censored_rate == 0
    assert decision.recommendation == "rules_only"


def test_selected_parent_ambiguity_blocks_a_population_even_below_ten_percent() -> None:
    labels = _aggregate_labels(positives=50, negatives=450, months=3)
    labels.append(
        replace(
            labels[0],
            private_parent_key=("private", "ambiguous"),
            status="censored",
            reason="selected_parent_ambiguity",
            mature=False,
            person_linked=False,
            history_determinate=False,
        )
    )

    decision = _report(labels).populations[0]

    assert decision.metrics.data_quality_unknown_censored_rate < 0.10
    assert decision.recommendation == "collect_more_data"


def test_markdown_never_renders_private_ids_or_boundary_values() -> None:
    report = _report(_aggregate_labels(positives=50, negatives=450, months=3))
    rendered = render_gate_markdown(report)

    assert "private" not in rendered
    assert "deal-1" not in rendered
    assert "boundary_digest" not in rendered
    assert "accepted_release_derived_not_rendered" in rendered
    assert "private-source" not in rendered
