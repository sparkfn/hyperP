"""Validation boundary for accepted CRM stage evidence rows (issue #125).

Parsing rules are ported from the issue #149 Gate 1 contract
(``services/api/src/sales_prediction_gate_labels.py``) so the dataset is built
from exactly the evidence shapes the accepted gate decision used: an invalid
row censors its whole parent timeline, naive timestamps are rejected, and
amount/currency states use the same state machines. Payload parsing
additionally extracts the v1 feature facts (amount value, currency, assignment
indicator, contact count) directly from the version's raw payload, following
the same top-level/nested-``deal`` lookup as the gate's amount extraction.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from src.sales_prediction.models import DealVersion, MappedState, PayloadFacts, StageEvent

_SUPPORTED_CURRENCIES = frozenset({"SGD", "USD", "MYR"})
_TIMESTAMP_FIELDS = (
    "observed_at",
    "ingested_at",
    "activated_at",
    "superseded_at",
    "rejected_at",
    "link_failed_at",
    "latest_linked_at",
)


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive values are rejected."""
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
    """Parse stage-event rows; any invalid row censors its whole parent."""
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
                category_id=_optional_text(row.get("category_id")),
                stage_id=_optional_text(row.get("stage_id")),
                source_semantic=_optional_text(row.get("source_semantic")),
            )
        )
    censored = [event for event in events if event.parent_key not in invalid]
    return censored, frozenset(invalid)


def parse_deal_rows(rows: Sequence[Mapping[str, object]]) -> list[DealVersion]:
    """Parse deal-version rows into typed versions with payload facts."""
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
        facts = parse_payload_facts(row.get("raw_payload"))
        timestamps_valid = all(
            row.get(field) in {None, ""} or parse_timestamp(row.get(field)) is not None
            for field in _TIMESTAMP_FIELDS
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
                amount_state=facts.amount_state,
                currency_status=facts.currency_status,
                lifecycle_status=(
                    lifecycle if isinstance(lifecycle, str) and lifecycle else "unknown"
                ),
                amount_value=facts.amount_value,
                currency=facts.currency,
                assigned_known=facts.assigned_known,
                contact_count=facts.contact_count,
                linked_person_ids=_text_tuple(row.get("linked_person_ids")),
                active_person_ids=_text_tuple(row.get("active_person_ids")),
            )
        )
    return versions


def parse_payload_facts(raw: object) -> PayloadFacts:
    """Extract v1 feature facts from a raw deal payload, gate-consistently."""
    if not isinstance(raw, str):
        return PayloadFacts(amount_state="missing", currency_status="missing")
    try:
        payload: object = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return PayloadFacts(amount_state="invalid", currency_status="invalid")
    if not isinstance(payload, dict):
        return PayloadFacts(amount_state="invalid", currency_status="invalid")
    nested = payload.get("deal")
    deal = nested if isinstance(nested, dict) else {}
    amount = _first(payload, deal, "amount", "opportunity", "OPPORTUNITY")
    currency = _first(payload, deal, "currency", "currency_id", "CURRENCY_ID")
    assigned = _first(payload, deal, "ASSIGNED_BY_ID", "assigned_by_id")
    contacts = _first(payload, deal, "CONTACT_IDS", "CONTACT_ID")
    amount_state, amount_value = _amount_state(amount)
    currency_status, currency_code = _currency_status(currency)
    return PayloadFacts(
        amount_state=amount_state,
        currency_status=currency_status,
        amount_value=amount_value,
        currency=currency_code,
        assigned_known=_assigned_known(assigned),
        contact_count=_contact_count(contacts),
    )


def _amount_state(value: object) -> tuple[str, float | None]:
    if value is None or value == "":
        return "missing", None
    if isinstance(value, bool):
        return "invalid", None
    try:
        number = float(value) if isinstance(value, str | int | float) else math.nan
    except ValueError:
        return "invalid", None
    if not math.isfinite(number) or number < 0:
        return "invalid", None
    return ("zero" if number == 0 else "known", number)


def _currency_status(value: object) -> tuple[str, str | None]:
    if value is None or value == "":
        return "missing", None
    if not isinstance(value, str) or len(value.strip()) != 3 or not value.strip().isalpha():
        return "invalid", None
    normalized = value.strip().upper()
    return ("supported" if normalized in _SUPPORTED_CURRENCIES else "unsupported", normalized)


def _assigned_known(value: object) -> bool:
    if value is None or value == "" or value == 0 or value == "0":
        return False
    return isinstance(value, str | int)


def _contact_count(value: object) -> int:
    if isinstance(value, list):
        return sum(1 for item in value if isinstance(item, str | int) and str(item).strip())
    if isinstance(value, str | int) and str(value).strip() and str(value) != "0":
        return 1
    return 0


def _first(first: dict[str, object], second: dict[str, object], *keys: str) -> object:
    for container in (first, second):
        for key in keys:
            if key in container:
                return container[key]
    return None


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
