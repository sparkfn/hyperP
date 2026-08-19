"""Retrospective source-availability labels for Gate 1 (issue #149 correction).

The retrospective selector snapshots at source-native stage history time
(`event_at`) instead of operational availability time
(`max(event_at, available_at)`). A one-time historical capture collapses all
operational availability into the capture month, which would otherwise make
every historical horizon immature and mislabel matured historical winners.

Only historically reconstructable facts drive eligibility and labels:

- stage history states, event times, and derived ages/transition counts;
- entity population, from the parent's unique live-version entity key;
- amount and currency, only when a non-rejected deal version was observed at
  or before the snapshot.

Non-backdatable current-state facts are excluded from label determination:
current Person linkage is reported as a current-state population metric and
never censors or qualifies a retrospective snapshot.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from src.sales_prediction_gate_labels import (
    _HORIZON,
    _LabelBase,
    iterate_open_entries,
)
from src.sales_prediction_gate_models import (
    DealVersion,
    GateRelease,
    LabelEvidence,
    StageEvent,
)

_LIVE_LIFECYCLE = frozenset({"active", "pending_review"})


def build_retrospective_labels(
    release: GateRelease,
    events: list[StageEvent],
    versions: list[DealVersion],
    entity_keys: tuple[str, ...],
    *,
    invalid_event_parents: frozenset[tuple[str, str]] = frozenset(),
) -> list[LabelEvidence]:
    """Build one retrospective label for every deterministic open-episode entry."""
    versions_by_parent: defaultdict[tuple[str, str], list[DealVersion]] = defaultdict(list)
    for version in versions:
        versions_by_parent[version.parent_key].append(version)
    output: list[LabelEvidence] = []
    for parent, event, ordered in iterate_open_entries(events):
        output.append(
            label_retrospective_entry(
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


def label_retrospective_entry(
    release: GateRelease,
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
    base: _LabelBase = {
        "private_parent_key": parent,
        "snapshot_at": snapshot,
        "month": snapshot.strftime("%Y-%m"),
        "entity_key": entity,
        "mature": horizon <= release.evidence_cutoff_at,
        "person_linked": _current_state_linked(live),
        "timestamp_valid": not invalid_event,
        "history_determinate": (
            release.analytical_release_consistent
            and not invalid_event
            and entity_state == "resolved"
        ),
        "amount_state": (
            amount_version.amount_state if amount_version is not None else "not_reconstructable"
        ),
        "currency_status": (
            amount_version.currency_status if amount_version is not None else "not_reconstructable"
        ),
        "amount_reconstructable": amount_version is not None,
    }
    if not release.enabled or not release.source_accounting_complete:
        return LabelEvidence(status="censored", reason="source_authority_incomplete", **base)
    if invalid_event:
        return LabelEvidence(status="censored", reason="invalid_stage_timestamp", **base)
    if entity_state == "missing":
        base["history_determinate"] = False
        return LabelEvidence(status="censored", reason="missing_parent_at_snapshot", **base)
    if entity_state == "ambiguous":
        base["history_determinate"] = False
        return LabelEvidence(status="censored", reason="selected_parent_ambiguity", **base)
    if entity not in entity_keys:
        return LabelEvidence(status="ineligible", reason="unsupported_entity", **base)
    known = [item for item in timeline if item.event_at <= snapshot]
    if (known[-1].mapped_state if known else None) != "open":
        return LabelEvidence(status="ineligible", reason="not_open_retrospective", **base)
    if amount_version is not None and amount_version.currency_status == "unsupported":
        return LabelEvidence(status="ineligible", reason="unsupported_currency", **base)
    if amount_version is not None and amount_version.currency_status == "invalid":
        return LabelEvidence(status="censored", reason="invalid_currency", **base)
    if horizon > release.evidence_cutoff_at:
        return LabelEvidence(status="unknown", reason="immature_horizon", **base)
    first_won = next(
        (
            item
            for item in timeline
            if item.mapped_state == "won" and snapshot < item.event_at <= horizon
        ),
        None,
    )
    if first_won is not None:
        return LabelEvidence(status="positive", reason="first_won_in_horizon", **base)
    return LabelEvidence(status="negative", reason="mature_no_first_won", **base)


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
