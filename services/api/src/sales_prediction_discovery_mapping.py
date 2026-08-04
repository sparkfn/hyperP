"""Private-row parsing and privacy-safe aggregation for issue #124 discovery."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime

type DiscoveryScalar = str | int | float | bool | None
type DiscoveryRow = dict[str, DiscoveryScalar]

_ACCEPTED_STATES = frozenset({"accepted_at_cutoff", "accepted_inferred_from_ingestion"})


def aggregate_deals(rows: list[DiscoveryRow], as_of_at: str) -> list[DiscoveryRow]:
    selected = _select_logical_versions(rows, as_of_at)
    counts: defaultdict[tuple[DiscoveryScalar, ...], int] = defaultdict(int)
    for row, state in selected:
        payload = _json_object(row.get("raw_payload"))
        raw_stage_id = payload.get("stage_id") if payload is not None else None
        raw_contact_count = payload.get("contact_count") if payload is not None else None
        raw_ambiguous = (
            payload.get("crm_contact_resolution_required") if payload is not None else None
        )
        stage_id = _string(raw_stage_id)
        contact_count = _integer(raw_contact_count)
        ambiguous = _boolean(raw_ambiguous) if payload is not None else None
        key = (
            row.get("entity_key"),
            stage_id or "",
            state,
            _integer(row.get("linked_person_count")) or 0,
            contact_count,
            ambiguous,
            "current_graph_projection",
            row.get("observed_at") is None,
            raw_stage_id is None or (stage_id is not None and not stage_id.strip()),
            raw_stage_id is not None and stage_id is None,
            raw_contact_count is not None and (contact_count is None or contact_count < 0),
            raw_ambiguous is not None and ambiguous is None,
            payload is None,
        )
        counts[key] += 1
    columns = (
        "entity_key",
        "stage_id",
        "historical_state",
        "linked_person_count",
        "contact_count",
        "ambiguous_contacts",
        "person_linkage_basis",
        "missing_observed_at",
        "missing_stage_id",
        "invalid_stage_id",
        "invalid_contact_count",
        "invalid_ambiguous_contacts",
        "invalid_raw_payload",
    )
    return _count_rows(counts, columns, "deal_count")


def aggregate_interactions(rows: list[DiscoveryRow], as_of_at: str) -> list[DiscoveryRow]:
    selected = _select_logical_versions(rows, as_of_at)
    groups: dict[tuple[DiscoveryScalar, ...], dict[str, DiscoveryScalar]] = {}
    for row, state in selected:
        record_type = _string(row.get("record_type")) or ""
        key = (row.get("entity_key"), record_type, state)
        group = groups.setdefault(
            key,
            {
                "entity_key": row.get("entity_key"),
                "record_type": record_type,
                "historical_state": state,
                "record_count": 0,
                "missing_observed_at_count": 0,
                "confidence_present_count": 0,
                "duration_present_count": 0,
                "invalid_duration_count": 0,
                "invalid_raw_payload_count": 0,
            },
        )
        _increment(group, "record_count")
        if row.get("observed_at") is None:
            _increment(group, "missing_observed_at_count")
        confidence = row.get("extraction_confidence")
        if isinstance(confidence, int | float) and not isinstance(confidence, bool):
            _increment(group, "confidence_present_count")
        if record_type == "call":
            payload = _json_object(row.get("raw_payload"))
            if payload is None:
                _increment(group, "invalid_raw_payload_count")
            else:
                duration = payload.get("duration_seconds")
                if _integer(duration) is not None:
                    _increment(group, "duration_present_count")
                elif duration is not None:
                    _increment(group, "invalid_duration_count")
    return [groups[key] for key in sorted(groups, key=_sort_key)]


def aggregate_sales(rows: list[DiscoveryRow], as_of_at: str) -> list[DiscoveryRow]:
    selected = _select_logical_versions(rows, as_of_at)
    counts: defaultdict[tuple[DiscoveryScalar, ...], int] = defaultdict(int)
    for row, state in selected:
        payload = _json_object(row.get("raw_payload"))
        order = _nested_object(payload, "order")
        raw_status = order.get("status") if order is not None else None
        raw_ordered_at = order.get("ordered_at") if order is not None else None
        raw_currency = order.get("currency") if order is not None else None
        status = _string(raw_status)
        ordered_at = _string(raw_ordered_at)
        currency = _string(raw_currency)
        invalid_ordered_at = raw_ordered_at is not None and (
            ordered_at is None or _optional_datetime(ordered_at) is None
        )
        key = (
            row.get("entity_key"),
            row.get("source_key"),
            status or "",
            state,
            _integer(row.get("linked_person_count")) or 0,
            "current_graph_projection",
            raw_ordered_at is None,
            invalid_ordered_at,
            raw_currency is None or (currency is not None and not currency.strip()),
            raw_currency is not None and currency is None,
            raw_status is None or (status is not None and not status.strip()),
            raw_status is not None and status is None,
            payload is None or order is None,
        )
        counts[key] += 1
    columns = (
        "entity_key",
        "source_key",
        "order_status",
        "historical_state",
        "linked_person_count",
        "person_linkage_basis",
        "missing_ordered_at",
        "invalid_ordered_at",
        "missing_currency",
        "invalid_currency",
        "missing_order_status",
        "invalid_order_status",
        "invalid_raw_payload",
    )
    return _count_rows(counts, columns, "order_count")


def _select_logical_versions(
    rows: list[DiscoveryRow], as_of_at: str
) -> list[tuple[DiscoveryRow, str]]:
    cutoff = _datetime(as_of_at)
    grouped: defaultdict[tuple[str, str], list[tuple[DiscoveryRow, str]]] = defaultdict(list)
    for row in rows:
        source_key = _required_string(row, "source_key")
        logical_id = _required_string(row, "logical_record_id")
        grouped[(source_key, logical_id)].append((row, _historical_state(row, cutoff)))
    selected: list[tuple[DiscoveryRow, str]] = []
    for candidates in grouped.values():
        accepted = [candidate for candidate in candidates if candidate[1] in _ACCEPTED_STATES]
        if len(accepted) > 1:
            row, _ = max(accepted, key=lambda candidate: _version(candidate[0]))
            selected.append((row, "multiple_accepted_versions_at_cutoff"))
        else:
            pool = accepted or candidates
            selected.append(max(pool, key=lambda candidate: _version(candidate[0])))
    return selected


def _historical_state(row: DiscoveryRow, cutoff: datetime) -> str:
    timestamp_fields = (
        "ingested_at",
        "activated_at",
        "superseded_at",
        "rejected_at",
        "link_failed_at",
    )
    if any(_has_invalid_datetime(row.get(field)) for field in timestamp_fields):
        return "invalid_lifecycle_timestamp"
    if _at_or_before(row.get("rejected_at"), cutoff):
        return "rejected_by_cutoff"
    if _at_or_before(row.get("link_failed_at"), cutoff):
        return "link_failed_by_cutoff"
    activated_at = _optional_datetime(row.get("activated_at"))
    ingested_at = _optional_datetime(row.get("ingested_at"))
    inferred = False
    if activated_at is None and row.get("lifecycle_status") in {"active", "superseded"}:
        activated_at = ingested_at
        inferred = activated_at is not None
    if activated_at is None or activated_at > cutoff:
        return "not_accepted_by_cutoff"
    if _at_or_before(row.get("superseded_at"), cutoff):
        return "superseded_by_cutoff"
    return "accepted_inferred_from_ingestion" if inferred else "accepted_at_cutoff"


def _count_rows(
    counts: dict[tuple[DiscoveryScalar, ...], int],
    columns: tuple[str, ...],
    count_column: str,
) -> list[DiscoveryRow]:
    output: list[DiscoveryRow] = []
    for key in sorted(counts, key=_sort_key):
        row = dict(zip(columns, key, strict=True))
        row[count_column] = counts[key]
        output.append(row)
    return output


def _sort_key(values: tuple[DiscoveryScalar, ...]) -> tuple[str, ...]:
    return tuple("" if value is None else str(value) for value in values)


def _json_object(value: DiscoveryScalar) -> dict[str, object] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _nested_object(value: dict[str, object] | None, key: str) -> dict[str, object] | None:
    nested = value.get(key) if value is not None else None
    return nested if isinstance(nested, dict) else None


def _increment(row: DiscoveryRow, key: str) -> None:
    current = row[key]
    if not isinstance(current, int) or isinstance(current, bool):
        raise ValueError(f"aggregate counter {key} is not an integer")
    row[key] = current + 1


def _required_string(row: DiscoveryRow, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"discovery row omitted required string {key}")
    return value


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _version(row: DiscoveryRow) -> int:
    version = _integer(row.get("source_record_version"))
    if version is None or version < 1:
        raise ValueError("source_record_version must be a positive integer")
    return version


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _optional_datetime(value: DiscoveryScalar) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = _datetime(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _has_invalid_datetime(value: DiscoveryScalar) -> bool:
    return isinstance(value, str) and bool(value) and _optional_datetime(value) is None


def _at_or_before(value: DiscoveryScalar, cutoff: datetime) -> bool:
    parsed = _optional_datetime(value)
    return parsed is not None and parsed <= cutoff
