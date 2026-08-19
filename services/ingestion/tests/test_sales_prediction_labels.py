"""Retrospective label semantics tests for the #125 dataset (issue #125.2).

Ports the #149 Gate 1 retrospective selector decision order so the dataset
is built from exactly the same label semantics the gate decision used.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.sales_prediction.labels import build_retrospective_labels, iterate_open_entries
from src.sales_prediction.models import DealVersion, ReleaseSnapshot, StageEvent

MAPPING = "crm-stage-map-2026-08-18-v1"
POLICY = "crm-stage-lifecycle-policy-2026-08-18-v1"
CUTOFF = datetime(2026, 8, 1, tzinfo=UTC)
ACCEPTED = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


def _release(**overrides: object) -> ReleaseSnapshot:
    defaults: dict[str, object] = {
        "enabled": True,
        "mapping_version": MAPPING,
        "policy_version": POLICY,
        "accepted_at": ACCEPTED,
        "evidence_cutoff_at": CUTOFF,
        "source_accounting_complete": True,
        "analytical_release_consistent": True,
        "restated_event_count": 0,
    }
    defaults.update(overrides)
    return ReleaseSnapshot(**defaults)  # type: ignore[arg-type]


def _event(
    identity: str,
    parent: tuple[str, str] = ("bitrix_chat", "deal-1"),
    state: str = "open",
    event_at: datetime | None = None,
) -> StageEvent:
    return StageEvent(
        event_identity=identity,
        parent_key=parent,
        mapped_state=state,  # type: ignore[arg-type]
        event_at=event_at or datetime(2026, 1, 10, 8, 0, tzinfo=UTC),
        available_at=event_at or datetime(2026, 1, 10, 8, 5, tzinfo=UTC),
        authority_head_version=1,
    )


def _version(
    parent: tuple[str, str] = ("bitrix_chat", "deal-1"),
    *,
    entity_key: str = "eko",
    observed_at: datetime | None = None,
    lifecycle_status: str = "active",
    amount_state: str = "known",
    currency_status: str = "supported",
) -> DealVersion:
    return DealVersion(
        parent_key=parent,
        version_key="4:abc:1",
        source_record_version=1,
        entity_key=entity_key,
        observed_at=observed_at or datetime(2026, 1, 5, tzinfo=UTC),
        ingested_at=datetime(2026, 1, 5, tzinfo=UTC),
        activated_at=datetime(2026, 1, 5, tzinfo=UTC),
        superseded_at=None,
        rejected_at=None,
        link_failed_at=None,
        linked_person_count=1,
        active_person_count=1,
        latest_linked_at=datetime(2026, 1, 6, tzinfo=UTC),
        timestamps_valid=True,
        amount_state=amount_state,
        currency_status=currency_status,
        lifecycle_status=lifecycle_status,
    )


def _labels(
    events: list[StageEvent],
    versions: list[DealVersion] | None = None,
    entity_keys: tuple[str, ...] = ("eko",),
    **overrides: object,
) -> list:
    return build_retrospective_labels(
        _release(**overrides),
        tuple(events),
        tuple(versions or []),
        entity_keys,
    )


def test_iterate_open_entries_finds_entry_points() -> None:
    events = [
        _event("e1", state="open", event_at=datetime(2026, 1, 1, tzinfo=UTC)),
        _event("e2", state="open", event_at=datetime(2026, 1, 5, tzinfo=UTC)),
        _event("e3", state="won", event_at=datetime(2026, 1, 15, tzinfo=UTC)),
        _event("e4", state="open", event_at=datetime(2026, 2, 1, tzinfo=UTC)),
    ]
    entries = iterate_open_entries(tuple(events))
    # Two open entries: e1 (first open) and e4 (open after won)
    assert len(entries) == 2
    assert entries[0][1].event_identity == "e1"
    assert entries[1][1].event_identity == "e4"


def test_positive_label_when_won_within_horizon() -> None:
    snapshot = datetime(2026, 1, 10, tzinfo=UTC)
    events = [
        _event("e1", state="open", event_at=snapshot),
        _event("e2", state="won", event_at=snapshot + timedelta(days=20)),
    ]
    labels = _labels(events, [_version()])
    assert len(labels) == 1
    assert labels[0].status == "positive"
    assert labels[0].reason == "first_won_in_horizon"
    assert labels[0].entity_key == "eko"


def test_negative_label_when_mature_no_won() -> None:
    snapshot = datetime(2026, 1, 10, tzinfo=UTC)
    events = [
        _event("e1", state="open", event_at=snapshot),
        _event("e2", state="lost", event_at=snapshot + timedelta(days=20)),
    ]
    labels = _labels(events, [_version()])
    assert len(labels) == 1
    assert labels[0].status == "negative"
    assert labels[0].reason == "mature_no_first_won"


def test_unknown_label_when_horizon_exceeds_cutoff() -> None:
    snapshot = datetime(2026, 7, 15, tzinfo=UTC)
    events = [_event("e1", state="open", event_at=snapshot)]
    labels = _labels(events, [_version()])
    assert labels[0].status == "unknown"
    assert labels[0].reason == "immature_horizon"


def test_censored_when_source_authority_incomplete() -> None:
    events = [_event("e1")]
    labels = _labels(events, [_version()], source_accounting_complete=False)
    assert labels[0].status == "censored"
    assert labels[0].reason == "source_authority_incomplete"


def test_ineligible_for_unsupported_entity() -> None:
    events = [_event("e1")]
    versions = [_version(entity_key="fundbox")]
    labels = _labels(events, versions, entity_keys=("eko",))
    assert labels[0].status == "ineligible"
    assert labels[0].reason == "unsupported_entity"


def test_censored_for_missing_parent() -> None:
    events = [_event("e1")]
    labels = _labels(events, [])
    assert labels[0].status == "censored"
    assert labels[0].reason == "missing_parent_at_snapshot"


def test_ineligible_for_not_open_retrospective() -> None:
    snapshot = datetime(2026, 1, 10, tzinfo=UTC)
    events = [
        _event("e1", state="won", event_at=datetime(2026, 1, 5, tzinfo=UTC)),
        _event("e2", state="open", event_at=snapshot),
    ]
    labels = _labels(events, [_version()])
    # last known state before snapshot is "won", not "open"
    assert labels[0].status == "ineligible"
    assert labels[0].reason == "not_open_retrospective"


def test_won_outside_horizon_is_negative() -> None:
    snapshot = datetime(2026, 1, 10, tzinfo=UTC)
    events = [
        _event("e1", state="open", event_at=snapshot),
        _event("e2", state="won", event_at=snapshot + timedelta(days=45)),
    ]
    labels = _labels(events, [_version()])
    assert labels[0].status == "negative"
