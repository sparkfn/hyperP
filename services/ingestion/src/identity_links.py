"""Derived C3 identity-link helpers over immutable C1 ``ASSERTS`` evidence.

Pure helpers: deterministic link IDs, assertion-evidence aggregation, the
Profile materialization threshold, canonical Identifier lock ordering, and the
behaviour-free writer contract types. There is no scoring formula and no Cypher
here; the transaction writer lives in :mod:`src.identity_link_writes`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict

from src.golden_profile_active_authority import _TRUST_TIER_RANK
from src.identity_registry import identifier_policy
from src.models import JsonValue, QualityFlag
from src.pipeline_normalization import is_usable

if TYPE_CHECKING:
    from src.identity_link_member_evidence import MemberEvidence

#: Provisional C3 values. Placeholders until C4 computes weights; they must
#: never feed matching or golden-profile selection.
PROVISIONAL_SCORE = 0.0
PROVISIONAL_EXCLUSIVITY = 0.0
PROVISIONAL_RANK_SCORE = 0.0
PROVISIONAL_POLICY_VERSION = "c3-provisional"
LINK_BASIS = "ASSERTS"
LINK_STATUS_ACTIVE = "active"
LOWEST_TRUST_TIER = "tier_4"
MATERIALIZATION_MIN_SHARED_RECORDS = 2
MATERIALIZATION_MIN_INDEPENDENT_KEYS = 2
#: Cluster-expansion bound for identifier types without a registry fanout cap.
MAX_RECORD_EXPANSION = 200


class IdentifierKey(TypedDict):
    """One Identifier node identity, used for locking and link targeting."""

    identifier_type: str
    identifier_scope: str
    normalized_value: str


class AssertionEvidence(TypedDict):
    """One SourceRecord ``ASSERTS`` row feeding a link computation.

    ``quality_flag`` and ``is_verified`` are ``None`` when an older shadow
    assertion predates that metadata; :func:`aggregate_link` and
    :func:`materializes` apply the documented fallback rules for those rows.
    ``observed_at`` is the evidenced SourceRecord's ISO-8601 observation time.
    """

    source_record_pk: str
    identifier_type: str
    identifier_scope: str
    normalized_value: str
    quality_flag: str | None
    is_verified: bool | None
    trust_tier: str | None
    observed_at: str | None


class ParticipantEvidence(AssertionEvidence):
    """One attributed SourceRecord assertion row including its Identifier ID."""

    identifier_id: str


ParticipantKind = Literal["person", "profile"]

MaterializationStatus = Literal["no_keys", "parked", "created", "joined"]

PromotionStatus = Literal["promoted", "already_promoted", "rejected"]


@dataclass(frozen=True)
class LinkRecomputeOutcome:
    """Counts from one participant's ``HAS_IDENTIFIER`` recompute."""

    participant_kind: ParticipantKind
    participant_id: str
    link_count: int
    stale_link_count: int


@dataclass(frozen=True)
class MaterializationOutcome:
    """Result of materializing the cluster containing one SourceRecord."""

    status: MaterializationStatus
    profile_id: str | None
    absorbed_profile_ids: tuple[str, ...] = ()
    member_count: int = 0


@dataclass(frozen=True)
class PromotionOutcome:
    """Result of a gate-approved NRIC promotion of an open Profile."""

    status: PromotionStatus
    profile_id: str
    person_id: str | None = None
    person_created: bool = False
    reason: str | None = None
    promoted_members: tuple[MemberEvidence, ...] = ()


class IdentityLinkConflictError(RuntimeError):
    """A CAS-guarded Profile or link write lost to concurrent modification."""


@dataclass(frozen=True)
class LinkAggregate:
    """Derived ``HAS_IDENTIFIER`` values for one participant/Identifier pair."""

    assertion_count: int
    source_count: int
    verified_count: int
    max_tier: str
    first_seen_at: str | None
    last_seen_at: str | None
    last_confirmed_at: str | None
    score: float = PROVISIONAL_SCORE
    exclusivity: float = PROVISIONAL_EXCLUSIVITY
    rank_score: float = PROVISIONAL_RANK_SCORE
    golden_rank: int | None = None
    basis: str = LINK_BASIS
    status: str = LINK_STATUS_ACTIVE
    policy_version: str = PROVISIONAL_POLICY_VERSION


def link_id(participant_id: str, identifier_id: str) -> str:
    """Return the deterministic ``HAS_IDENTIFIER`` identity for a pair."""
    identity = f"{participant_id}|{identifier_id}"
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()


def aggregate_link(evidence: Sequence[AssertionEvidence]) -> LinkAggregate:
    """Aggregate the assertion evidence of one participant/Identifier pair.

    Observation boundaries are taken from the evidenced SourceRecords. An
    assertion missing ``quality_flag`` or ``is_verified`` contributes nothing to
    ``verified_count`` and only the lowest trust tier; ``last_confirmed_at`` is
    the most recent observation that still evidences the link.
    """
    observations = sorted(row["observed_at"] for row in evidence if row["observed_at"])
    first_seen_at = observations[0] if observations else None
    last_seen_at = observations[-1] if observations else None
    return LinkAggregate(
        assertion_count=len(evidence),
        source_count=len({row["source_record_pk"] for row in evidence}),
        verified_count=sum(
            1 for row in evidence if _has_complete_metadata(row) and row["is_verified"] is True
        ),
        max_tier=max(
            (_evidence_tier(row) for row in evidence),
            default=LOWEST_TRUST_TIER,
            key=_tier_rank,
        ),
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        last_confirmed_at=last_seen_at,
    )


def build_link_rows(
    participant_id: str,
    evidence: Sequence[ParticipantEvidence],
) -> list[dict[str, JsonValue]]:
    """Build one deterministic ``HAS_IDENTIFIER`` row per evidenced Identifier."""
    grouped: dict[tuple[str, str, str], list[ParticipantEvidence]] = {}
    for row in evidence:
        key = (row["identifier_type"], row["identifier_scope"], row["normalized_value"])
        grouped.setdefault(key, []).append(row)
    links: list[dict[str, JsonValue]] = []
    for key in sorted(grouped):
        pair = grouped[key]
        aggregate = aggregate_link(pair)
        link: dict[str, JsonValue] = {
            "link_id": link_id(participant_id, pair[0]["identifier_id"]),
            "identifier_id": pair[0]["identifier_id"],
            "assertion_count": aggregate.assertion_count,
            "source_count": aggregate.source_count,
            "verified_count": aggregate.verified_count,
            "max_tier": aggregate.max_tier,
            "first_seen_at": aggregate.first_seen_at,
            "last_seen_at": aggregate.last_seen_at,
            "last_confirmed_at": aggregate.last_confirmed_at,
            "score": aggregate.score,
            "exclusivity": aggregate.exclusivity,
            "rank_score": aggregate.rank_score,
            "golden_rank": aggregate.golden_rank,
            "basis": aggregate.basis,
            "status": aggregate.status,
            "policy_version": aggregate.policy_version,
        }
        links.append(link)
    return links


def materializes(evidence: Sequence[AssertionEvidence]) -> bool:
    """Return whether the evidence crosses the C3 Profile materialization threshold.

    A cluster materializes when two attributed records share an eligible key, or
    when one record asserts two independent eligible high-entropy keys. Eligible
    keys are usable, ``candidate_eligible`` Identifier nodes with complete
    assertion metadata; an older assertion missing ``quality_flag`` or
    ``is_verified`` never counts as one.
    """
    records_by_key: dict[tuple[str, str, str], set[str]] = {}
    keys_by_record: dict[str, set[tuple[str, str, str]]] = {}
    for row in evidence:
        if not _is_independent_key(row):
            continue
        key = (row["identifier_type"], row["identifier_scope"], row["normalized_value"])
        records_by_key.setdefault(key, set()).add(row["source_record_pk"])
        keys_by_record.setdefault(row["source_record_pk"], set()).add(key)
    if any(
        len(records) >= MATERIALIZATION_MIN_SHARED_RECORDS for records in records_by_key.values()
    ):
        return True
    return any(
        len(keys) >= MATERIALIZATION_MIN_INDEPENDENT_KEYS for keys in keys_by_record.values()
    )


def sorted_lock_rows(keys: Iterable[IdentifierKey]) -> list[IdentifierKey]:
    """Return Identifier keys in the canonical lock acquisition order."""
    return sorted(
        keys,
        key=lambda key: (
            key["identifier_type"],
            key["identifier_scope"],
            key["normalized_value"],
        ),
    )


def identifier_key_tuple(key: IdentifierKey) -> tuple[str, str, str]:
    """Return an Identifier key's canonical tuple form."""
    return (key["identifier_type"], key["identifier_scope"], key["normalized_value"])


def identifier_key_row(parts: tuple[str, str, str]) -> IdentifierKey:
    """Return an Identifier key TypedDict from its canonical tuple form."""
    return IdentifierKey(
        identifier_type=parts[0],
        identifier_scope=parts[1],
        normalized_value=parts[2],
    )


def parse_field_trust(value: object) -> dict[str, str] | None:
    """Parse the bootstrap-stored JSON field-trust map; None when malformed."""
    parsed: object = value
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    result: dict[str, str] = {}
    for key, item in parsed.items():
        if isinstance(key, str) and isinstance(item, str):
            result[key] = item
    return result


def _has_complete_metadata(row: AssertionEvidence) -> bool:
    return row["quality_flag"] is not None and row["is_verified"] is not None


def _quality(row: AssertionEvidence) -> QualityFlag | None:
    value = row["quality_flag"]
    if value is None:
        return None
    try:
        return QualityFlag(value)
    except ValueError:
        return None


def _tier_rank(tier: str) -> int:
    return _TRUST_TIER_RANK.get(tier, 0)


def _evidence_tier(row: AssertionEvidence) -> str:
    if not _has_complete_metadata(row):
        return LOWEST_TRUST_TIER
    tier = row["trust_tier"]
    if isinstance(tier, str) and tier in _TRUST_TIER_RANK:
        return tier
    return LOWEST_TRUST_TIER


def _is_independent_key(row: AssertionEvidence) -> bool:
    quality = _quality(row)
    return (
        quality is not None
        and is_usable(quality)
        and _has_complete_metadata(row)
        and identifier_policy(row["identifier_type"]).candidate_eligible
    )
