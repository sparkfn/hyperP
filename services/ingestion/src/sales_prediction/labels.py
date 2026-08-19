"""Retrospective CRM-WON labels for the #125 dataset (issue #125.2).

Ports the #149 Gate 1 retrospective selector semantics exactly:

- Snapshot at source-native event_at of every deterministic open-episode
  entry (not operational available_at).
- Only historically reconstructable facts drive eligibility and labels.
- Current-state Person linkage is reported as an indicator, never censors
  or qualifies a retrospective snapshot.
- Decision order matches gate_retrospective.py: source authority, invalid
  event, entity resolution, entity eligibility, retrospective open check,
  currency, horizon maturity, first-won-in-horizon, mature negative.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta

from src.sales_prediction.contracts import HORIZON_DAYS
from src.sales_prediction.models import (
    DealVersion,
    LabelEvidence,
    LabelStatus,
    ReleaseSnapshot,
    StageEvent,
)

_HORIZON = timedelta(days=HORIZON_DAYS)
_LIVE_LIFECYCLE = frozenset({"active", "pending_review"})


def iterate_open_entries(
    events: tuple[StageEvent, ...],
) -> list[tuple[tuple[str, str], StageEvent, list[StageEvent]]]:
    """Return every deterministic open-episode entry with its parent timeline."""
    events_by_parent: defaultdict[tuple[str, str], list[StageEvent]] = defaultdict(list)
    for event in events:
        events_by_parent[event.parent_key].append(event)
    entries: list[tuple[tuple[str, str], StageEvent, list[StageEvent]]] = []
    for parent, timeline in sorted(events_by_parent.items()):
        ordered = sorted(timeline, key=_event_order)
        previous_state: str | None = None
        for event in ordered:
            is_entry = event.mapped_state == "open" and previous_state != "open"
            previous_state = event.mapped_state
            if not is_entry:
                continue
            entries.append((parent, event, ordered))
    return entries


def build_retrospective_labels(
    release: ReleaseSnapshot,
    events: tuple[StageEvent, ...],
    versions: tuple[DealVersion, ...],
    entity_keys: tuple[str, ...],
    *,
    invalid_event_parents: frozenset[tuple[str, str]] = frozenset(),
) -> list[LabelEvidence]:
    """Build one retrospective label per deterministic open-episode entry."""
    versions_by_parent: defaultdict[tuple[str, str], list[DealVersion]] = defaultdict(list)
    for version in versions:
        versions_by_parent[version.parent_key].append(version)
    output: list[LabelEvidence] = []
    for parent, event, ordered in iterate_open_entries(events):
        output.append(
            _label_entry(
                release,
                parent,
                event,
                ordered,
                versions_by_parent.get(parent, []),
                entity_keys,
                invalid_event=parent in invalid_event_parents,
            )
        )
    return sorted(output, key=lambda item: (item.entity_key, item.snapshot_at, item.reason))


def _label_entry(
    release: ReleaseSnapshot,
    parent: tuple[str, str],
    open_event: StageEvent,
    timeline: list[StageEvent],
    versions: list[DealVersion],
    entity_keys: tuple[str, ...],
    *,
    invalid_event: bool,
) -> LabelEvidence:
    """Label one open-episode entry using source-native snapshot time."""
    snapshot = open_event.event_at
    horizon = snapshot + _HORIZON
    live = _live_versions(versions)
    entity, entity_state = _live_entity(live)
    amount_version = _retrospective_amount_version(versions, snapshot)
    base = LabelEvidence(
        parent_key=parent,
        snapshot_at=snapshot,
        month=snapshot.strftime("%Y-%m"),
        entity_key=entity,
        status="ineligible",
        reason="unreachable",
        mature=horizon <= release.evidence_cutoff_at,
        person_linked=_current_state_linked(live),
        timestamp_valid=not invalid_event,
        history_determinate=(
            release.analytical_release_consistent
            and not invalid_event
            and entity_state == "resolved"
        ),
        amount_state=(
            amount_version.amount_state if amount_version is not None else "not_reconstructable"
        ),
        currency_status=(
            amount_version.currency_status if amount_version is not None else "not_reconstructable"
        ),
        amount_reconstructable=amount_version is not None,
    )
    if not release.enabled or not release.source_accounting_complete:
        return _with(base, status="censored", reason="source_authority_incomplete")
    if invalid_event:
        return _with(base, status="censored", reason="invalid_stage_timestamp")
    if entity_state == "missing":
        return _with(base, status="censored", reason="missing_parent_at_snapshot", det=False)
    if entity_state == "ambiguous":
        return _with(base, status="censored", reason="selected_parent_ambiguity", det=False)
    if entity not in entity_keys:
        return _with(base, status="ineligible", reason="unsupported_entity")
    known = [item for item in timeline if item.event_at <= snapshot]
    if (known[-1].mapped_state if known else None) != "open":
        return _with(base, status="ineligible", reason="not_open_retrospective")
    if amount_version is not None and amount_version.currency_status == "unsupported":
        return _with(base, status="ineligible", reason="unsupported_currency")
    if amount_version is not None and amount_version.currency_status == "invalid":
        return _with(base, status="censored", reason="invalid_currency")
    if horizon > release.evidence_cutoff_at:
        return _with(base, status="unknown", reason="immature_horizon")
    first_won = next(
        (
            item
            for item in timeline
            if item.mapped_state == "won" and snapshot < item.event_at <= horizon
        ),
        None,
    )
    if first_won is not None:
        return _with(base, status="positive", reason="first_won_in_horizon")
    return _with(base, status="negative", reason="mature_no_first_won")


def _with(
    base: LabelEvidence,
    *,
    status: LabelStatus,
    reason: str,
    det: bool | None = None,
) -> LabelEvidence:
    """Return a copy of base with updated label fields."""
    updates: dict[str, object] = {"status": status, "reason": reason}
    if det is not None:
        updates["history_determinate"] = det
    return replace(base, **updates)


def _live_versions(versions: list[DealVersion]) -> list[DealVersion]:
    return [
        item
        for item in versions
        if item.lifecycle_status in _LIVE_LIFECYCLE and item.timestamps_valid
    ]


def _live_entity(live: list[DealVersion]) -> tuple[str, str]:
    """Resolve the parent's population from its unique live-version entity key."""
    if not live:
        return "unresolved", "missing"
    keys = {item.entity_key for item in live}
    if len(keys) == 1:
        only = next(iter(keys))
        if only is not None:
            return only, "resolved"
    return "unresolved", "ambiguous"


def _current_state_linked(live: list[DealVersion]) -> bool:
    linked = {person_id for item in live for person_id in item.linked_person_ids}
    active = {person_id for item in live for person_id in item.active_person_ids}
    return len(linked) == 1 and len(active) == 1 and active.issubset(linked)


def _retrospective_amount_version(
    versions: list[DealVersion], snapshot: datetime
) -> DealVersion | None:
    candidates = [
        item
        for item in versions
        if item.lifecycle_status != "rejected"
        and item.timestamps_valid
        and item.observed_at is not None
        and item.observed_at <= snapshot
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (item.observed_at, item.source_record_version, item.version_key),
    )


def _event_order(item: StageEvent) -> tuple[datetime, int, str]:
    return item.event_at, item.authority_head_version, item.event_identity
