"""Retrospective source-availability label tests for the issue #149 correction."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from src.graph.queries.sales_prediction_gate import GATE_DEAL_VERSIONS_FOR_PARENTS
from src.sales_prediction_gate_labels import (
    SELECTOR_RETROSPECTIVE,
    build_labels,
    parse_deal_rows,
    validate_selector_version,
)
from src.sales_prediction_gate_models import (
    DealVersion,
    GateRelease,
    LabelEvidence,
    MappedState,
    StageEvent,
)
from src.sales_prediction_gate_report import build_gate_report
from src.sales_prediction_gate_retrospective import build_retrospective_labels

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
    entity: str | None = "fundbox",
    linked: tuple[str, ...] = ("private-person-1",),
    version: int = 1,
    observed_day: int = -1,
    superseded_day: int | None = None,
    lifecycle: str = "active",
    amount_state: str = "known",
    currency_status: str = "supported",
    timestamps_valid: bool = True,
) -> DealVersion:
    return DealVersion(
        parent_key=parent,
        version_key=f"private-version-{version}",
        source_record_version=version,
        entity_key=entity,
        observed_at=_BASE + timedelta(days=observed_day),
        ingested_at=_BASE + timedelta(days=observed_day),
        activated_at=_BASE + timedelta(days=observed_day),
        superseded_at=(
            _BASE + timedelta(days=superseded_day) if superseded_day is not None else None
        ),
        rejected_at=None,
        link_failed_at=None,
        linked_person_count=len(linked),
        active_person_count=len(linked),
        latest_linked_at=_BASE + timedelta(days=observed_day),
        timestamps_valid=timestamps_valid,
        amount_state=amount_state,
        currency_status=currency_status,
        lifecycle_status=lifecycle,
        linked_person_ids=linked,
        active_person_ids=linked,
    )


def _one(
    events: list[StageEvent],
    versions: list[DealVersion] | None = None,
    *,
    cutoff_days: int = 365,
    entity_keys: tuple[str, ...] = ("fundbox",),
) -> LabelEvidence:
    labels = build_retrospective_labels(
        _release(cutoff_days=cutoff_days),
        events,
        [_version()] if versions is None else versions,
        entity_keys,
    )
    assert len(labels) == 1
    return labels[0]


def test_retrospective_snapshot_uses_source_native_event_time() -> None:
    events = [
        _event("open", -200, available_day=200),
        _event("won", -180, available_day=200),
    ]
    operational = build_labels(_release(), events, [_version()], ("fundbox",))[0]
    retrospective = _one(events)

    assert operational.snapshot_at == _BASE + timedelta(days=200)
    assert retrospective.snapshot_at == _BASE + timedelta(days=-200)
    assert retrospective.month != operational.month
    assert retrospective.status == "positive"
    assert retrospective.mature is True


def test_retrospective_labels_from_stage_history_only() -> None:
    positive = _one([_event("open", -100), _event("won", -80)])
    negative = _one([_event("open", -100)])
    immature = _one([_event("open", 100)], cutoff_days=110)

    assert (positive.status, positive.reason) == ("positive", "first_won_in_horizon")
    assert (negative.status, negative.reason) == ("negative", "mature_no_first_won")
    assert (immature.status, immature.reason) == ("unknown", "immature_horizon")


def test_retrospective_horizon_is_open_at_s_plus_30_days() -> None:
    at_boundary = _one([_event("open", -100), _event("won", -70)])
    after_boundary = _one([_event("open", -100), _event("won", -69)])

    assert at_boundary.status == "positive"
    assert after_boundary.status == "negative"


def test_retrospective_pre_snapshot_non_open_state_is_ineligible_not_censored() -> None:
    label = _one(
        [
            _event("open", 0, head=1),
            _event("won", 0, head=2, identity="private-event-won-0-2"),
        ]
    )

    assert (label.status, label.reason) == ("ineligible", "not_open_retrospective")


def test_retrospective_currency_only_excludes_when_reconstructable() -> None:
    reconstructable_unsupported = _one(
        [_event("open", -100)],
        [
            _version(
                observed_day=-150,
                currency_status="unsupported",
            )
        ],
    )
    not_reconstructable = _one(
        [_event("open", -100)],
        [_version(observed_day=200, currency_status="unsupported")],
    )

    assert (reconstructable_unsupported.status, reconstructable_unsupported.reason) == (
        "ineligible",
        "unsupported_currency",
    )
    assert not_reconstructable.status == "negative"
    assert not_reconstructable.currency_status == "not_reconstructable"
    assert not_reconstructable.amount_reconstructable is False


def test_retrospective_amount_uses_latest_observed_version() -> None:
    label = _one(
        [_event("open", -100)],
        [
            _version(version=1, observed_day=-150, amount_state="known"),
            _version(version=2, observed_day=-120, amount_state="zero"),
            _version(version=3, observed_day=-10, amount_state="known"),
        ],
    )

    assert label.amount_state == "zero"
    assert label.amount_reconstructable is True


def test_retrospective_amount_ignores_rejected_versions() -> None:
    label = _one(
        [_event("open", -100)],
        [
            _version(observed_day=200, version=1),
            _version(observed_day=-150, lifecycle="rejected", amount_state="invalid", version=2),
        ],
    )

    assert label.amount_reconstructable is False
    assert label.amount_state == "not_reconstructable"
    assert label.status == "negative"


def test_retrospective_entity_resolution_from_live_versions() -> None:
    missing = _one([_event("open", -100)], [])
    ambiguous = _one(
        [_event("open", -100)],
        [_version(entity="fundbox"), _version(entity="eko", version=2)],
    )
    unsupported = _one([_event("open", -100)], [_version(entity="eko")])

    assert (missing.status, missing.reason) == ("censored", "missing_parent_at_snapshot")
    assert missing.history_determinate is False
    assert (ambiguous.status, ambiguous.reason) == ("censored", "selected_parent_ambiguity")
    assert (unsupported.status, unsupported.reason) == ("ineligible", "unsupported_entity")


def test_retrospective_linkage_is_reported_but_never_censors() -> None:
    unlinked = _one([_event("open", -100)], [_version(linked=())])
    multi_linked = _one(
        [_event("open", -100)],
        [_version(linked=("private-person-1", "private-person-2"))],
    )

    assert unlinked.status == "negative"
    assert unlinked.person_linked is False
    assert multi_linked.status == "negative"
    assert multi_linked.person_linked is False


def test_retrospective_deterministic_rerun_and_ordering() -> None:
    events = [
        _event("won", -80),
        _event("open", -100, identity="z"),
        _event("lost", -100, identity="a", head=2),
    ]
    first = build_retrospective_labels(_release(), events, [_version()], ("fundbox",))
    second = build_retrospective_labels(
        _release(), list(reversed(events)), [_version()], ("fundbox",)
    )

    assert first == second
    assert first[0].reason == "not_open_retrospective"


def test_retrospective_report_semantics_and_amount_metrics() -> None:
    labels = [
        replace(
            _one([_event("open", -100)], [_version(observed_day=-150)]),
            private_parent_key=("private", "deal-known"),
        ),
        replace(
            _one([_event("open", -100)], [_version(observed_day=200, version=2)]),
            private_parent_key=("private", "deal-absent"),
        ),
    ]
    report = build_gate_report(
        labels,
        _release(),
        ("fundbox",),
        generated_at="2026-08-19T00:00:00Z",
        selector_version=SELECTOR_RETROSPECTIVE,
        eligibility_version="crm-won-retrospective-eligibility-v1",
        restatement_version="authority-head-v1",
    )
    metrics = report.populations[0].metrics

    assert report.metadata.availability_semantics == "retrospective_source_native"
    assert report.metadata.report_schema_version == "issue-149-crm-won-gate-v2"
    assert metrics.amount_reconstructable_rate == 0.5
    assert metrics.amount_revision_availability == "retrospective_observed_at_versioned"


def test_selector_validation_rejects_unknown_versions() -> None:
    with pytest.raises(ValueError, match="open-episode-entry-v2"):
        validate_selector_version("open-episode-entry-v2")


def test_deal_version_rows_parse_lifecycle_and_person_ids() -> None:
    versions = parse_deal_rows(
        [
            {
                "parent_source_system": "private-source",
                "parent_source_record_id": "private-deal-1",
                "version_key": "element-1",
                "source_record_version": 1,
                "entity_key": "fundbox",
                "observed_at": "2025-01-01T00:00:00Z",
                "ingested_at": None,
                "activated_at": None,
                "superseded_at": None,
                "rejected_at": None,
                "link_failed_at": None,
                "latest_linked_at": None,
                "lifecycle_status": "superseded",
                "linked_person_ids": ("person-a", "person-b"),
                "active_person_ids": ("person-a",),
                "raw_payload": None,
            }
        ]
    )

    assert len(versions) == 1
    assert versions[0].lifecycle_status == "superseded"
    assert versions[0].linked_person_ids == ("person-a", "person-b")
    assert versions[0].active_person_ids == ("person-a",)


def test_gate_version_query_exposes_lifecycle_and_person_ids() -> None:
    assert "deal.lifecycle_status AS lifecycle_status" in GATE_DEAL_VERSIONS_FOR_PARENTS
    assert "collect(DISTINCT elementId(person)) AS linked_person_ids" in (
        GATE_DEAL_VERSIONS_FOR_PARENTS
    )
    assert "active_person_ids" in GATE_DEAL_VERSIONS_FOR_PARENTS
