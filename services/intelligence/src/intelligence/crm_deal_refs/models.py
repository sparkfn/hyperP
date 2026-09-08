"""Strict privacy-minimal schema for versioned immutable CRM deal-reference snapshots."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal

from intelligence.artifacts import canonical_json

IdentityStatus = Literal[
    "resolved", "unresolved", "pending_review", "blocked", "rejected", "retired"
]
Availability = Literal["known_by_cutoff", "unknown"]
PageKind = Literal["deal-references", "identity-revisions"]

SCHEMA_VERSION = 2
QUERY_VERSION = "crm-deal-refs-v2"
IDENTITY_POLICY_VERSION = "crm_deal_identity_v2"
SOURCE_SYSTEM = "bitrix_chat"
MAX_PAGE_SIZE = 1_000
MAX_RECORDS = 10_000
MAX_PAYLOAD_BYTES = 1_000_000
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")


@dataclass(frozen=True, order=True)
class DealKey:
    source_record_id: str
    source_record_version: int
    source_record_pk: str


@dataclass(frozen=True)
class DealReference:
    """One immutable source version plus explicitly capture-time mutable observations."""

    source_system: str
    source_instance_id: str
    identity_policy_version: str
    key: DealKey
    record_hash: str
    source_entity_type: str
    source_entity_id: str
    entity_key: str | None
    category_id: str | None
    stage_id: str | None
    stage_semantic_id: str | None
    source_outcome_ref: str | None
    observed_at: str | None
    available_at: str | None
    first_known_at: str | None
    source_event_at: str | None
    source_effective_at: str | None
    source_close_date: str | None
    availability: Availability
    point_in_time_eligible: bool
    lifecycle_status_observed: str | None
    link_status_observed: str | None
    observation_captured_at: str


@dataclass(frozen=True)
class IdentityRevision:
    """Relevant immutable identity history; Person status is capture-time observation only."""

    event_id: str
    global_revision: int
    source_system: str
    source_instance_id: str
    identity_policy_version: str
    source_entity_id: str
    link_status: IdentityStatus
    hyperp_person_id: str | None
    person_status_observed: str | None
    person_observation_captured_at: str | None
    resolution_kind: str
    resolution_revision: int
    effective_at: str | None
    available_at: str | None
    first_known_at: str | None
    availability: Availability
    person_reference_eligible: bool


@dataclass(frozen=True)
class Boundary:
    schema_version: int
    query_version: str
    source_system: str
    source_instance_id: str
    identity_policy_version: str
    as_of: str
    captured_at: str
    page_size: int
    max_records: int
    identity_revision_ceiling: int
    source_membership_count: int
    source_terminal_key: DealKey | None
    source_membership_sha256: str
    immutable_facts_sha256: str
    identity_facts_sha256: str
    mutable_observations_sha256: str


@dataclass(frozen=True)
class PageManifest:
    sequence: int
    kind: PageKind
    path: str
    source_system: str
    source_instance_id: str
    identity_policy_version: str
    as_of: str
    boundary_sha256: str
    current_cursor: str | None
    next_cursor: str | None
    first_key: str | None
    last_key: str | None
    count: int
    byte_count: int
    sha256: str


@dataclass(frozen=True)
class Checkpoint:
    schema_version: int
    boundary_sha256: str
    deal_pages: int
    identity_pages: int
    deal_records: int
    identity_records: int
    deal_next_cursor: str | None
    identity_next_cursor: str | None
    completed: bool


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_cutoff(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("as-of must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("as-of must include a timezone")
    normalized = parsed.astimezone(UTC)
    if normalized > datetime.now(UTC):
        raise ValueError("as-of must not be in the future")
    return normalized.isoformat().replace("+00:00", "Z")


def safe_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def canonical_digest(value: object) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def json_value(value: object) -> object:
    if isinstance(
        value, (DealKey, DealReference, IdentityRevision, Boundary, PageManifest, Checkpoint)
    ):
        return asdict(value)
    raise TypeError("unsupported CRM deal-reference evidence")
