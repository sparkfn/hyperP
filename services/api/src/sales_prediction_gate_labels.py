"""Deterministic point-in-time CRM-WON labels for Gate 1."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TypedDict, cast

from src.sales_prediction_gate_models import (
    DealVersion,
    GateRelease,
    LabelEvidence,
    MappedState,
    StageEvent,
)

_HORIZON = timedelta(days=30)
_SUPPORTED_CURRENCIES = frozenset({"SGD", "USD", "MYR"})

SELECTOR_OPERATIONAL = "open-episode-entry-v1"
SELECTOR_RETROSPECTIVE = "retrospective-source-availability-v1"
SUPPORTED_SELECTOR_VERSIONS = frozenset({SELECTOR_OPERATIONAL, SELECTOR_RETROSPECTIVE})


class _LabelBase(TypedDict):
    private_parent_key: tuple[str, str]
    snapshot_at: datetime
    month: str
    entity_key: str
    mature: bool
    person_linked: bool
    timestamp_valid: bool
    history_determinate: bool
    amount_state: str
    currency_status: str
    amount_reconstructable: bool


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def parse_stage_rows(
    rows: Sequence[Mapping[str, object]],
) -> tuple[list[StageEvent], frozenset[tuple[str, str]]]:
    events: list[StageEvent] = []
    invalid: set[tuple[str, str]] = set()
    for row in rows:
        parent = _parent_key(row)
        event_at = parse_timestamp(row.get("event_at"))
        available_at = parse_timestamp(row.get("available_at"))
        state = row.get("mapped_state")
        identity = row.get("event_identity")
        head_version = row.get("authority_head_version")
        if (
            parent is None
            or event_at is None
            or available_at is None
            or available_at < event_at
            or state not in {"open", "won", "lost"}
            or not isinstance(identity, str)
            or not identity
            or isinstance(head_version, bool)
            or not isinstance(head_version, int)
            or head_version < 0
        ):
            if parent is not None:
                invalid.add(parent)
            continue
        events.append(
            StageEvent(
                event_identity=identity,
                parent_key=parent,
                mapped_state=cast(MappedState, state),
                event_at=event_at,
                available_at=available_at,
                authority_head_version=head_version,
            )
        )
    return events, frozenset(invalid)


def parse_deal_rows(rows: Sequence[Mapping[str, object]]) -> list[DealVersion]:
    versions: list[DealVersion] = []
    for row in rows:
        parent = _parent_key(row)
        version_key = row.get("version_key")
        version = row.get("source_record_version")
        if (
            parent is None
            or not isinstance(version_key, str)
            or not version_key
            or isinstance(version, bool)
            or not isinstance(version, int)
        ):
            continue
        amount_state, currency_status = _payload_states(row.get("raw_payload"))
        timestamp_fields = (
            "observed_at",
            "ingested_at",
            "activated_at",
            "superseded_at",
            "rejected_at",
            "link_failed_at",
            "latest_linked_at",
        )
        timestamps_valid = all(
            row.get(field) in {None, ""} or parse_timestamp(row.get(field)) is not None
            for field in timestamp_fields
        )
        lifecycle = row.get("lifecycle_status")
        versions.append(
            DealVersion(
                parent_key=parent,
                version_key=version_key,
                source_record_version=version,
                entity_key=_optional_text(row.get("entity_key")),
                observed_at=parse_timestamp(row.get("observed_at")),
                ingested_at=parse_timestamp(row.get("ingested_at")),
                activated_at=parse_timestamp(row.get("activated_at")),
                superseded_at=parse_timestamp(row.get("superseded_at")),
                rejected_at=parse_timestamp(row.get("rejected_at")),
                link_failed_at=parse_timestamp(row.get("link_failed_at")),
                linked_person_count=_non_negative(row.get("linked_person_count")),
                active_person_count=_non_negative(row.get("active_person_count")),
                latest_linked_at=parse_timestamp(row.get("latest_linked_at")),
                timestamps_valid=timestamps_valid,
                amount_state=amount_state,
                currency_status=currency_status,
                lifecycle_status=(
                    lifecycle if isinstance(lifecycle, str) and lifecycle else "unknown"
                ),
                linked_person_ids=_text_tuple(row.get("linked_person_ids")),
                active_person_ids=_text_tuple(row.get("active_person_ids")),
            )
        )
    return versions


def validate_selector_version(selector_version: str) -> None:
    """Reject selector versions the gate cannot label deterministically."""
    if selector_version not in SUPPORTED_SELECTOR_VERSIONS:
        raise ValueError(f"unsupported Gate 1 selector version: {selector_version}")


def iterate_open_entries(
    events: list[StageEvent],
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


def build_labels(
    release: GateRelease,
    events: list[StageEvent],
    versions: list[DealVersion],
    entity_keys: tuple[str, ...],
    *,
    invalid_event_parents: frozenset[tuple[str, str]] = frozenset(),
) -> list[LabelEvidence]:
    """Build one operational-as-known label for every deterministic open-episode entry."""
    versions_by_parent: defaultdict[tuple[str, str], list[DealVersion]] = defaultdict(list)
    for version in versions:
        versions_by_parent[version.parent_key].append(version)
    output: list[LabelEvidence] = []
    for parent, event, ordered in iterate_open_entries(events):
        output.append(
            _label_open_entry(
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


def _label_open_entry(
    release: GateRelease,
    parent: tuple[str, str],
    open_event: StageEvent,
    timeline: list[StageEvent],
    versions: list[DealVersion],
    entity_keys: tuple[str, ...],
    *,
    invalid_event: bool,
) -> LabelEvidence:
    snapshot = max(open_event.event_at, open_event.available_at)
    horizon = snapshot + _HORIZON
    selected, selection_reason = _select_version(versions, snapshot)
    entity = selected.entity_key if selected is not None and selected.entity_key else "unassigned"
    base: _LabelBase = {
        "private_parent_key": parent,
        "snapshot_at": snapshot,
        "month": snapshot.strftime("%Y-%m"),
        "entity_key": entity,
        "mature": horizon <= release.evidence_cutoff_at,
        "person_linked": _person_linked(selected, snapshot),
        "timestamp_valid": not invalid_event and (selected is None or selected.timestamps_valid),
        "history_determinate": release.analytical_release_consistent and not invalid_event,
        "amount_state": selected.amount_state if selected is not None else "unavailable",
        "currency_status": selected.currency_status if selected is not None else "unavailable",
        "amount_reconstructable": selected is not None,
    }
    if not release.enabled or not release.source_accounting_complete:
        return LabelEvidence(status="censored", reason="source_authority_incomplete", **base)
    if invalid_event:
        return LabelEvidence(status="censored", reason="invalid_stage_timestamp", **base)
    if selected is None:
        base["history_determinate"] = False
        return LabelEvidence(status="censored", reason=selection_reason, **base)
    if entity not in entity_keys:
        return LabelEvidence(status="ineligible", reason="unsupported_entity", **base)
    if selected.currency_status == "unsupported":
        return LabelEvidence(status="ineligible", reason="unsupported_currency", **base)
    if selected.currency_status == "invalid":
        return LabelEvidence(status="censored", reason="invalid_currency", **base)
    if not _person_linked(selected, snapshot):
        base["history_determinate"] = False
        return LabelEvidence(status="censored", reason="ambiguous_or_late_person_link", **base)
    if any(
        item.event_at <= snapshot < item.available_at and item.mapped_state != "open"
        for item in timeline
    ):
        return LabelEvidence(
            status="censored", reason="censored_retrospective_disqualifier", **base
        )
    known_state = _state_as_known(timeline, snapshot)
    if known_state != "open":
        return LabelEvidence(status="ineligible", reason="not_open_as_known", **base)

    first_won = next((item for item in timeline if item.mapped_state == "won"), None)
    if first_won is not None and snapshot < first_won.event_at <= horizon:
        if first_won.available_at > release.evidence_cutoff_at:
            return LabelEvidence(status="censored", reason="won_unavailable_by_cutoff", **base)
        return LabelEvidence(status="positive", reason="first_won_in_horizon", **base)
    if horizon > release.evidence_cutoff_at:
        return LabelEvidence(status="unknown", reason="immature_horizon", **base)
    return LabelEvidence(status="negative", reason="mature_no_first_won", **base)


def _select_version(
    versions: list[DealVersion], snapshot: datetime
) -> tuple[DealVersion | None, str]:
    candidates = [item for item in versions if _available_version(item, snapshot)]
    if not candidates:
        if any(not item.timestamps_valid for item in versions):
            return None, "invalid_parent_timestamp"
        return None, "missing_parent_at_snapshot"
    if len(candidates) != 1:
        return None, "selected_parent_ambiguity"
    return candidates[0], "selected"


def _available_version(item: DealVersion, snapshot: datetime) -> bool:
    if not item.timestamps_valid:
        return False
    required = (item.observed_at, item.ingested_at, item.activated_at)
    if any(value is None or value > snapshot for value in required):
        return False
    terminal = (item.superseded_at, item.rejected_at, item.link_failed_at)
    return all(value is None or value > snapshot for value in terminal)


def _person_linked(item: DealVersion | None, snapshot: datetime) -> bool:
    return bool(
        item is not None
        and item.linked_person_count == 1
        and item.active_person_count == 1
        and item.latest_linked_at is not None
        and item.latest_linked_at <= snapshot
    )


def _state_as_known(timeline: list[StageEvent], snapshot: datetime) -> str | None:
    known = [
        item for item in timeline if item.event_at <= snapshot and item.available_at <= snapshot
    ]
    return max(known, key=_event_order).mapped_state if known else None


def _event_order(item: StageEvent) -> tuple[datetime, int, str]:
    return item.event_at, item.authority_head_version, item.event_identity


def _parent_key(row: Mapping[str, object]) -> tuple[str, str] | None:
    system = row.get("parent_source_system")
    record = row.get("parent_source_record_id")
    if not isinstance(system, str) or not system or not isinstance(record, str) or not record:
        return None
    return system, record


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _text_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, tuple | list):
        return tuple(item for item in value if isinstance(item, str) and item)
    return ()


def _non_negative(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _payload_states(raw: object) -> tuple[str, str]:
    if not isinstance(raw, str):
        return "missing", "missing"
    try:
        payload: object = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return "invalid", "invalid"
    if not isinstance(payload, dict):
        return "invalid", "invalid"
    nested = payload.get("deal")
    deal = nested if isinstance(nested, dict) else {}
    amount = _first(payload, deal, "amount", "opportunity", "OPPORTUNITY")
    currency = _first(payload, deal, "currency", "currency_id", "CURRENCY_ID")
    return _amount_state(amount), _currency_status(currency)


def _first(first: dict[str, object], second: dict[str, object], *keys: str) -> object:
    for container in (first, second):
        for key in keys:
            if key in container:
                return container[key]
    return None


def _amount_state(value: object) -> str:
    if value is None or value == "":
        return "missing"
    if isinstance(value, bool):
        return "invalid"
    try:
        number = float(value) if isinstance(value, str | int | float) else math.nan
    except ValueError:
        return "invalid"
    if not math.isfinite(number) or number < 0:
        return "invalid"
    return "zero" if number == 0 else "known"


def _currency_status(value: object) -> str:
    if value is None or value == "":
        return "missing"
    if not isinstance(value, str) or len(value.strip()) != 3 or not value.strip().isalpha():
        return "invalid"
    return "supported" if value.strip().upper() in _SUPPORTED_CURRENCIES else "unsupported"
