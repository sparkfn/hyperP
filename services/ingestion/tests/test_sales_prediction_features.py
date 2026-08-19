"""Point-in-time feature leakage tests for the #125 dataset (issue #125.2).

Injects future stage events, future close, later amount edits, later
assignment/contact changes, and post-cutoff records, then proves the
snapshot features and label are unchanged (S-safety).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.sales_prediction.features import build_feature_vector, derive_sufficiency
from src.sales_prediction.models import DealVersion, LabelEvidence, StageEvent

SNAPSHOT = datetime(2026, 1, 10, 8, 0, tzinfo=UTC)


def _event(
    identity: str,
    state: str = "open",
    event_at: datetime | None = None,
) -> StageEvent:
    return StageEvent(
        event_identity=identity,
        parent_key=("bitrix_chat", "deal-1"),
        mapped_state=state,  # type: ignore[arg-type]
        event_at=event_at or SNAPSHOT,
        available_at=event_at or SNAPSHOT,
        authority_head_version=1,
    )


def _version(
    *,
    observed_at: datetime | None = None,
    amount_value: float | None = 1200.0,
    amount_state: str = "known",
    currency_status: str = "supported",
    assigned_known: bool = True,
    contact_count: int = 2,
    latest_linked_at: datetime | None = None,
) -> DealVersion:
    return DealVersion(
        parent_key=("bitrix_chat", "deal-1"),
        version_key="4:abc:1",
        source_record_version=1,
        entity_key="eko",
        observed_at=observed_at or datetime(2026, 1, 5, tzinfo=UTC),
        ingested_at=datetime(2026, 1, 5, tzinfo=UTC),
        activated_at=datetime(2026, 1, 5, tzinfo=UTC),
        superseded_at=None,
        rejected_at=None,
        link_failed_at=None,
        linked_person_count=1,
        active_person_count=1,
        latest_linked_at=latest_linked_at or datetime(2026, 1, 6, tzinfo=UTC),
        timestamps_valid=True,
        amount_state=amount_state,
        currency_status=currency_status,
        lifecycle_status="active",
        amount_value=amount_value,
        currency="SGD",
        assigned_known=assigned_known,
        contact_count=contact_count,
        linked_person_ids=("p1",),
        active_person_ids=("p1",),
    )


def _features(
    timeline: list[StageEvent],
    versions: list[DealVersion],
    amount_version: DealVersion | None = None,
) -> object:
    open_event = _event("e1", state="open", event_at=SNAPSHOT)
    return build_feature_vector(
        open_event=open_event,
        timeline=timeline,
        versions=versions,
        amount_version=amount_version or (versions[0] if versions else None),
        snapshot=SNAPSHOT,
        episode_index=1,
    )


def _label(status: str = "positive") -> LabelEvidence:
    return LabelEvidence(
        parent_key=("bitrix_chat", "deal-1"),
        snapshot_at=SNAPSHOT,
        month="2026-01",
        entity_key="eko",
        status=status,  # type: ignore[arg-type]
        reason="first_won_in_horizon",
        mature=True,
        person_linked=True,
        timestamp_valid=True,
        history_determinate=True,
        amount_state="known",
        currency_status="supported",
        amount_reconstructable=True,
    )


def test_future_stage_event_does_not_change_features() -> None:
    base_timeline = [_event("e1", state="open", event_at=SNAPSHOT)]
    future_timeline = base_timeline + [
        _event("e2", state="won", event_at=SNAPSHOT + timedelta(days=60)),
    ]
    v = _version()
    base_feats = _features(base_timeline, [v])
    future_feats = _features(future_timeline, [v])
    assert base_feats == future_feats


def test_future_amount_edit_does_not_change_features() -> None:
    timeline = [_event("e1", state="open", event_at=SNAPSHOT)]
    old_version = _version(amount_value=1200.0, observed_at=datetime(2026, 1, 5, tzinfo=UTC))
    future_version = _version(amount_value=9999.0, observed_at=SNAPSHOT + timedelta(days=10))
    base_feats = _features(timeline, [old_version], amount_version=old_version)
    future_feats = _features(timeline, [old_version, future_version], amount_version=old_version)
    assert base_feats == future_feats


def test_future_assignment_change_does_not_change_features() -> None:
    timeline = [_event("e1", state="open", event_at=SNAPSHOT)]
    original = _version(assigned_known=True, contact_count=2)
    future = _version(
        assigned_known=False,
        contact_count=5,
        observed_at=SNAPSHOT + timedelta(days=10),
    )
    base_feats = _features(timeline, [original], amount_version=original)
    future_feats = _features(timeline, [original, future], amount_version=original)
    assert base_feats == future_feats


def test_deal_age_uses_first_event_at_or_before_s() -> None:
    first = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    timeline = [
        _event("e0", state="open", event_at=first),
        _event("e1", state="open", event_at=SNAPSHOT),
    ]
    feats = _features(timeline, [_version()])
    assert feats.deal_age_days == 9.0  # type: ignore[attr-defined]


def test_transition_counts_only_prior_events() -> None:
    timeline = [
        _event("e0", state="open", event_at=datetime(2026, 1, 1, tzinfo=UTC)),
        _event("e1", state="won", event_at=datetime(2026, 1, 5, tzinfo=UTC)),
        _event("e2", state="open", event_at=SNAPSHOT),
        _event("e3", state="won", event_at=SNAPSHOT + timedelta(days=10)),
    ]
    feats = _features(timeline, [_version()])
    assert feats.prior_won_count == 1  # type: ignore[attr-defined]
    assert feats.prior_transition_count == 1  # type: ignore[attr-defined]


def test_sufficiency_band_classification() -> None:
    full_label = _label("positive")
    full_feats = _features([_event("e1")], [_version()])
    assert derive_sufficiency(full_label, full_feats) == "sufficient"

    missing_version = _version(
        amount_state="not_reconstructable",
        currency_status="not_reconstructable",
        amount_value=None,
        latest_linked_at=None,
    )
    missing_feats = _features([_event("e1")], [missing_version], amount_version=None)
    assert derive_sufficiency(full_label, missing_feats) == "insufficient"
