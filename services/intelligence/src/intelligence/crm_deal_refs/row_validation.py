"""Strict typed row validation and frozen-boundary fingerprint binding."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from intelligence.crm_deal_refs.models import (
    IDENTITY_POLICY_VERSION,
    SOURCE_SYSTEM,
    Boundary,
    DealKey,
    DealReference,
    IdentityRevision,
    canonical_digest,
)

_RESOLUTION_KINDS = frozenset(
    {
        "baseline",
        "automatic_activation",
        "reviewed_activation",
        "review_rejection",
        "manual_no_match",
        "source_supersession",
        "person_merge",
        "person_unmerge",
        "person_retirement",
        "source_retirement",
    }
)


def validate_row(row: dict[str, object], kind: str, boundary: Boundary) -> None:
    if kind == "deal-references":
        _validate_deal(row, boundary)
        return
    if kind == "identity-revisions":
        _validate_identity(row, boundary)
        return
    raise ValueError("row kind is invalid")


def bind_full(
    boundary: Boundary, deals: list[dict[str, object]], identities: list[dict[str, object]]
) -> None:
    if boundary.source_membership_count != len(deals):
        raise ValueError("boundary membership count is invalid")
    terminal = None if not deals else deal_key(deals[-1]["key"])
    if terminal != boundary.source_terminal_key:
        raise ValueError("boundary terminal key is invalid")
    deal_cursors = [deal_key(row["key"]) for row in deals]
    if deal_cursors != sorted(deal_cursors) or len(deal_cursors) != len(set(deal_cursors)):
        raise ValueError("deal rows are unordered or duplicate")
    revisions = [positive(row.get("global_revision"), "revision") for row in identities]
    if revisions != sorted(revisions) or len(revisions) != len(set(revisions)):
        raise ValueError("identity rows are unordered or duplicate")
    immutable_deals = [_immutable_deal(row) for row in deals]
    immutable_identities = [_immutable_identity(row) for row in identities]
    observations = [_deal_observation(row) for row in deals]
    observations.extend(_identity_observation(row) for row in identities)
    actual = (
        canonical_digest([row["key"] for row in deals]),
        canonical_digest(immutable_deals),
        canonical_digest(immutable_identities),
        canonical_digest(observations),
    )
    expected = (
        boundary.source_membership_sha256,
        boundary.immutable_facts_sha256,
        boundary.identity_facts_sha256,
        boundary.mutable_observations_sha256,
    )
    if actual != expected:
        raise ValueError("snapshot fingerprints do not match boundary")


def deal_key(value: object) -> DealKey:
    if not isinstance(value, dict) or set(value) != set(DealKey.__dataclass_fields__):
        raise ValueError("deal key schema is invalid")
    return DealKey(
        bounded_text(value.get("source_record_id"), "record id"),
        positive(value.get("source_record_version"), "version"),
        bounded_text(value.get("source_record_pk"), "pk"),
    )


def _validate_deal(row: dict[str, object], boundary: Boundary) -> None:
    if set(row) != set(DealReference.__dataclass_fields__):
        raise ValueError("deal row schema is invalid")
    if not _same_boundary(row, boundary):
        raise ValueError("deal row boundary is invalid")
    deal_key(row.get("key"))
    record_hash(row.get("record_hash"))
    if row.get("source_entity_type") != "deal":
        raise ValueError("deal entity type is invalid")
    if not isinstance(row.get("point_in_time_eligible"), bool):
        raise ValueError("deal eligibility is invalid")
    if row.get("availability") not in {"known_by_cutoff", "unknown"}:
        raise ValueError("deal availability is invalid")
    for field in (
        "source_entity_id",
        "entity_key",
        "category_id",
        "stage_id",
        "stage_semantic_id",
        "source_outcome_ref",
        "lifecycle_status_observed",
        "link_status_observed",
    ):
        optional_bounded_text(row.get(field), field)
    available = optional_timestamp(row.get("available_at"), "available_at")
    first_known = optional_timestamp(row.get("first_known_at"), "first_known_at")
    if first_known != available:
        raise ValueError("deal first-known availability is invalid")
    temporal = tuple(
        optional_timestamp(row.get(field), field)
        for field in (
            "observed_at",
            "source_event_at",
            "source_effective_at",
            "source_close_date",
        )
    )
    known = available is not None and _at_or_before(available, boundary.as_of)
    if row.get("availability") != ("known_by_cutoff" if known else "unknown"):
        raise ValueError("deal availability classification is invalid")
    category = row.get("category_id")
    stage = row.get("stage_id")
    semantic = row.get("stage_semantic_id")
    eligible = (
        known
        and temporal[0] is not None
        and _at_or_before(temporal[0], boundary.as_of)
        and temporal[1] is not None
        and _at_or_before(temporal[1], boundary.as_of)
        and temporal[2] is not None
        and _at_or_before(temporal[2], boundary.as_of)
        and (temporal[3] is None or _at_or_before(temporal[3], boundary.as_of))
        and isinstance(category, str)
        and (isinstance(stage, str) or isinstance(semantic, str))
    )
    if row.get("point_in_time_eligible") is not eligible:
        raise ValueError("deal point-in-time eligibility is invalid")
    if timestamp(row.get("observation_captured_at"), "capture") != boundary.captured_at:
        raise ValueError("deal capture is invalid")


def _validate_identity(row: dict[str, object], boundary: Boundary) -> None:
    if set(row) != set(IdentityRevision.__dataclass_fields__):
        raise ValueError("identity row schema is invalid")
    if not _same_boundary(row, boundary):
        raise ValueError("identity row boundary is invalid")
    revision = positive(row.get("global_revision"), "revision")
    statuses = {"resolved", "unresolved", "pending_review", "blocked", "rejected", "retired"}
    if revision > boundary.identity_revision_ceiling or row.get("link_status") not in statuses:
        raise ValueError("identity row values are invalid")
    bounded_text(row.get("event_id"), "event id")
    bounded_text(row.get("source_entity_id"), "source entity id")
    resolution_kind = bounded_text(row.get("resolution_kind"), "resolution kind")
    if resolution_kind not in _RESOLUTION_KINDS:
        raise ValueError("identity resolution kind is invalid")
    positive(row.get("resolution_revision"), "resolution revision")
    available = optional_timestamp(row.get("available_at"), "available_at")
    first_known = optional_timestamp(row.get("first_known_at"), "first_known_at")
    effective = optional_timestamp(row.get("effective_at"), "effective_at")
    if first_known != available:
        raise ValueError("identity first-known availability is invalid")
    known = available is not None and _at_or_before(available, boundary.as_of)
    if row.get("availability") != ("known_by_cutoff" if known else "unknown"):
        raise ValueError("identity availability classification is invalid")
    eligible = (
        row.get("link_status") == "resolved"
        and known
        and effective is not None
        and _at_or_before(effective, boundary.as_of)
    )
    if row.get("person_reference_eligible") is not eligible:
        raise ValueError("identity Person eligibility is invalid")
    _validate_person_visibility(row, boundary, eligible)


def _validate_person_visibility(row: dict[str, object], boundary: Boundary, eligible: bool) -> None:
    person = row.get("hyperp_person_id")
    status = row.get("person_status_observed")
    capture = row.get("person_observation_captured_at")
    if not eligible:
        if person is not None or status is not None or capture is not None:
            raise ValueError("ineligible identity exposes Person data")
        return
    if not isinstance(person, str) or not isinstance(status, str):
        raise ValueError("resolved identity observation is invalid")
    if timestamp(capture, "person capture") != boundary.captured_at:
        raise ValueError("resolved identity capture is invalid")
    try:
        parsed = uuid.UUID(person)
    except ValueError as error:
        raise ValueError("resolved UUID is invalid") from error
    if str(parsed) != person:
        raise ValueError("resolved UUID is not canonical")
    bounded_text(status, "person status")


def _same_boundary(row: dict[str, object], boundary: Boundary) -> bool:
    return (
        row.get("source_system") == SOURCE_SYSTEM
        and row.get("source_instance_id") == boundary.source_instance_id
        and row.get("identity_policy_version") == IDENTITY_POLICY_VERSION
    )


def _immutable_deal(row: dict[str, object]) -> dict[str, object]:
    excluded = {"lifecycle_status_observed", "link_status_observed", "observation_captured_at"}
    return {key: value for key, value in row.items() if key not in excluded}


def _immutable_identity(row: dict[str, object]) -> dict[str, object]:
    excluded = {"person_status_observed", "person_observation_captured_at"}
    return {key: value for key, value in row.items() if key not in excluded}


def _deal_observation(row: dict[str, object]) -> dict[str, object]:
    return {
        "key": row["key"],
        "lifecycle_status_observed": row["lifecycle_status_observed"],
        "link_status_observed": row["link_status_observed"],
    }


def _identity_observation(row: dict[str, object]) -> dict[str, object]:
    return {
        "global_revision": row["global_revision"],
        "person_status_observed": row["person_status_observed"],
    }


def timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or canonical_timestamp(value) != value:
        raise ValueError(f"{field} is invalid")
    return value


def optional_timestamp(value: object, field: str) -> str | None:
    return None if value is None else timestamp(value, field)


def canonical_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("timestamp timezone is missing")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _at_or_before(value: str, cutoff: str) -> bool:
    return _timestamp_instant(value) <= _timestamp_instant(cutoff)


def _timestamp_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp timezone is missing")
    return parsed.astimezone(UTC)


def text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is invalid")
    return value


def bounded_text(value: object, field: str) -> str:
    result = text(value, field)
    if len(result) > 500 or any(ord(character) < 32 for character in result):
        raise ValueError(f"{field} is invalid")
    return result


def optional_bounded_text(value: object, field: str) -> str | None:
    return None if value is None else bounded_text(value, field)


def positive(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field} is invalid")
    return value


def digest(value: object) -> str:
    result = text(value, "digest")
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ValueError("digest is invalid")
    return result


def record_hash(value: object) -> str:
    result = text(value, "record hash")
    raw = result.removeprefix("sha256:")
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
        raise ValueError("record hash is invalid")
    return result
