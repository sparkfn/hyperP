"""Point-in-time CRM-only feature builder for the #125 dataset (issue #125.2).

All features are computed at S = event_at of the open-episode entry.
No feature may use information from after S: stage events after S,
later deal versions, or post-cutoff records must not change the feature
vector. The leakage tests in test_sales_prediction_features.py prove this.

Feature families (v1):
  - deal age, transition/won/lost counts, episode index, days-since-prev
  - stage/category/source identity (from the open entry event)
  - amount, currency, amount-known, amount-nonzero (only when a non-rejected
    deal version was observed at or before S; else missing flags)
  - assignment and contact-count indicators
  - person-linked-at-s (version linkage timing indicator)
  - entity-version age/freshness
  - month sin/cos cyclical encoding
  - missingness/DQ count

Excluded (per issue #125): messages, calls, activities, raw text,
identifiers, protected traits, LLM prose. Person linkage is indicator only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from src.sales_prediction.models import DealVersion, LabelEvidence, StageEvent, SufficiencyBand

_MONTH_SIN = {m: math.sin(2 * math.pi * m / 12) for m in range(1, 13)}
_MONTH_COS = {m: math.cos(2 * math.pi * m / 12) for m in range(1, 13)}


@dataclass(frozen=True)
class FeatureSpec:
    """Ordered feature specification for the model artifact."""

    names: tuple[str, ...]
    numeric_indices: tuple[int, ...]
    categorical_indices: tuple[int, ...]
    vocabularies: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class FeatureVector:
    """One point-in-time feature row at snapshot S."""

    deal_age_days: float
    days_since_prev_event: float
    prior_transition_count: int
    prior_won_count: int
    prior_lost_count: int
    episode_index: int
    stage_id: str | None
    category_id: str | None
    source_semantic: str | None
    amount_value: float | None
    amount_state: str
    currency_status: str
    currency: str | None
    amount_known: int
    amount_nonzero: int
    assigned_known: int
    contact_count: int
    person_linked_at_s: int
    entity_version_age_days: float | None
    month_sin: float
    month_cos: float
    missingness_count: int


def build_feature_vector(
    open_event: StageEvent,
    timeline: list[StageEvent],
    versions: list[DealVersion],
    amount_version: DealVersion | None,
    snapshot: datetime,
    episode_index: int,
) -> FeatureVector:
    """Build a single point-in-time feature vector at S = snapshot."""
    prior_events = [e for e in timeline if e.event_at <= snapshot]
    first_event_at = min((e.event_at for e in prior_events), default=snapshot)
    deal_age = (snapshot - first_event_at).total_seconds() / 86400.0

    prev_events = [e for e in prior_events if e.event_at < snapshot]
    if prev_events:
        days_since_prev = (snapshot - prev_events[-1].event_at).total_seconds() / 86400.0
    else:
        days_since_prev = 0.0

    transitions = 0
    prior_won = 0
    prior_lost = 0
    prev_state: str | None = None
    for e in prev_events:
        if e.mapped_state == "won":
            prior_won += 1
        elif e.mapped_state == "lost":
            prior_lost += 1
        if prev_state is not None and e.mapped_state != prev_state:
            transitions += 1
        prev_state = e.mapped_state

    amount_value = amount_version.amount_value if amount_version is not None else None
    amount_state = (
        amount_version.amount_state if amount_version is not None else "not_reconstructable"
    )
    currency_status = (
        amount_version.currency_status if amount_version is not None else "not_reconstructable"
    )
    currency = amount_version.currency if amount_version is not None else None
    amount_known = 1 if amount_state == "known" else 0
    amount_nonzero = 1 if amount_value is not None and amount_value > 0 else 0
    assigned_known = 1 if amount_version is not None and amount_version.assigned_known else 0
    contact_count = amount_version.contact_count if amount_version is not None else 0

    person_linked = _person_linked_at_s(versions, snapshot)
    entity_version_age = _entity_version_age(amount_version, snapshot)

    month = snapshot.month
    missingness = _missingness_count(
        amount_state, currency_status, amount_value, entity_version_age
    )

    return FeatureVector(
        deal_age_days=deal_age,
        days_since_prev_event=days_since_prev,
        prior_transition_count=transitions,
        prior_won_count=prior_won,
        prior_lost_count=prior_lost,
        episode_index=episode_index,
        stage_id=open_event.stage_id,
        category_id=open_event.category_id,
        source_semantic=open_event.source_semantic,
        amount_value=amount_value,
        amount_state=amount_state,
        currency_status=currency_status,
        currency=currency,
        amount_known=amount_known,
        amount_nonzero=amount_nonzero,
        assigned_known=assigned_known,
        contact_count=contact_count,
        person_linked_at_s=person_linked,
        entity_version_age_days=entity_version_age,
        month_sin=_MONTH_SIN[month],
        month_cos=_MONTH_COS[month],
        missingness_count=missingness,
    )


def derive_sufficiency(label: LabelEvidence, features: FeatureVector) -> SufficiencyBand:
    """Classify a row's feature sufficiency for the evaluation report."""
    if label.status not in ("positive", "negative"):
        return "insufficient"
    missing = features.missingness_count
    if missing == 0:
        return "sufficient"
    if missing <= 2:
        return "limited"
    return "insufficient"


def _person_linked_at_s(versions: list[DealVersion], snapshot: datetime) -> int:
    """1 if exactly one person was linked at or before S on a live version."""
    live = [
        v
        for v in versions
        if v.lifecycle_status in ("active", "pending_review")
        and v.timestamps_valid
        and v.latest_linked_at is not None
        and v.latest_linked_at <= snapshot
    ]
    linked = {p for v in live for p in v.linked_person_ids}
    active = {p for v in live for p in v.active_person_ids}
    return 1 if len(linked) == 1 and len(active) == 1 and active.issubset(linked) else 0


def _entity_version_age(amount_version: DealVersion | None, snapshot: datetime) -> float | None:
    if amount_version is None or amount_version.observed_at is None:
        return None
    return (snapshot - amount_version.observed_at).total_seconds() / 86400.0


def _missingness_count(
    amount_state: str,
    currency_status: str,
    amount_value: float | None,
    entity_version_age: float | None,
) -> int:
    count = 0
    if amount_state in ("not_reconstructable", "missing", "unavailable"):
        count += 1
    if currency_status in ("not_reconstructable", "missing", "unavailable"):
        count += 1
    if amount_value is None:
        count += 1
    if entity_version_age is None:
        count += 1
    return count
