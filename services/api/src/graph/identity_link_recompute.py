"""API-side maintenance of one Person's derived ``HAS_IDENTIFIER`` projection.

Manual merge/unmerge and review approval move the legacy ``LINKED_TO`` /
``IDENTIFIED_BY`` projections only. These helpers re-derive the C3 projection
from the same immutable ``ASSERTS`` evidence with the ingestion writer's
semantics (C4 weights, zero-diff conditional writes), so HAS-first reads stay
current without an ingest touching the Person.

This module deliberately does not import ``src`` code from the ingestion
service; the Cypher mirrors the ingestion constants and remains API-owned.
The C4 weight formula lives in :mod:`src.graph.identity_link_weights`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypedDict

from neo4j import AsyncManagedTransaction, Record
from pydantic.types import JsonValue

from src.graph.converters import (
    to_int,
    to_optional_bool,
    to_optional_float,
    to_optional_int,
    to_optional_str,
    to_str,
    to_str_dict,
)
from src.graph.golden_profile import TRUST_RANK
from src.graph.identity_link_weights import (
    CANDIDATE_LINK_STATUS,
    LINK_POLICY_VERSION,
    age_days_between,
    assertion_score,
    combine_weights,
    exclusivity_for_count,
    half_life_for,
    max_evidence_time,
    rank_score,
    round6,
)
from src.graph.queries.identity_links import (
    DELETE_PERSON_HAS_IDENTIFIER_LINKS,
    DELETE_STALE_PERSON_HAS_IDENTIFIER_LINKS,
    FETCH_PERSON_ASSERTION_EVIDENCE,
    LOCK_PERSON_FOR_LINK_RECOMPUTE,
    MERGE_PERSON_HAS_IDENTIFIER_LINKS,
    RECOMPUTE_IDENTIFIER_PARTICIPANT_COUNTS,
)

LOWEST_TRUST_TIER = "tier_4"
LINK_BASIS = "ASSERTS"
LINK_STATUS_ACTIVE = "active"

type IdentifierKeyTuple = tuple[str, str, str]


class _Evidence(TypedDict):
    """One attributed SourceRecord assertion row feeding a link computation."""

    source_record_pk: str
    identifier_id: str
    identifier_type: str
    identifier_scope: str
    normalized_value: str
    quality_flag: str | None
    is_verified: bool | None
    trust_tier: str | None
    observed_at: str | None
    extraction_confidence: float | None
    record_type: str | None
    link_status: str | None
    participant_count: int | None


@dataclass(frozen=True)
class _LinkAggregate:
    assertion_count: int
    source_count: int
    verified_count: int
    max_tier: str
    first_seen_at: str | None
    last_seen_at: str | None
    last_confirmed_at: str | None
    score: float
    exclusivity: float
    rank_score: float
    policy_version: str = LINK_POLICY_VERSION


def link_id(person_id: str, identifier_id: str) -> str:
    """Return the deterministic ``HAS_IDENTIFIER`` identity for a pair."""
    return hashlib.sha1(f"{person_id}|{identifier_id}".encode()).hexdigest()


def build_link_rows(
    person_id: str,
    evidence: Sequence[_Evidence],
) -> tuple[list[dict[str, JsonValue]], list[str]]:
    """Build one deterministic ``HAS_IDENTIFIER`` row per evidenced Identifier.

    Returns the parameter rows plus their ``link_id`` strings, so callers never
    read ids back out of the JsonValue parameter dicts.

    Decay is anchored per D2: the reference time is the max
    ``last_confirmed_at`` among this Person's links of the same identifier
    type, never wall-clock.
    """
    grouped: dict[IdentifierKeyTuple, list[_Evidence]] = {}
    for row in evidence:
        key = (row["identifier_type"], row["identifier_scope"], row["normalized_value"])
        grouped.setdefault(key, []).append(row)
    reference_by_type: dict[str, str | None] = {}
    for key in sorted(grouped):
        pair = grouped[key]
        identifier_type = pair[0]["identifier_type"]
        reference_by_type[identifier_type] = max_evidence_time(
            [reference_by_type.get(identifier_type), _group_last_confirmed(pair)]
        )
    links: list[dict[str, JsonValue]] = []
    link_ids: list[str] = []
    for key in sorted(grouped):
        pair = grouped[key]
        aggregate = _aggregate(pair, reference_at=reference_by_type[pair[0]["identifier_type"]])
        resolved_link_id = link_id(person_id, pair[0]["identifier_id"])
        link_ids.append(resolved_link_id)
        links.append(
            {
                "link_id": resolved_link_id,
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
                "basis": LINK_BASIS,
                "status": LINK_STATUS_ACTIVE,
                "policy_version": aggregate.policy_version,
            }
        )
    return links, link_ids


def _ordered_link_ids(person_id: str, evidence: Sequence[_Evidence]) -> list[str]:
    """Return deterministic link ids, one per evidenced Identifier."""
    seen: set[str] = set()
    ordered: list[str] = []
    for row in evidence:
        if row["identifier_id"] not in seen:
            seen.add(row["identifier_id"])
            ordered.append(link_id(person_id, row["identifier_id"]))
    return ordered


async def recompute_person_has_identifier_links(tx: AsyncManagedTransaction, person_id: str) -> int:
    """Re-derive one Person's ``HAS_IDENTIFIER`` links from attributed evidence.

    Stale links are deleted and identifier counters refreshed before the link
    rows are built, so the freshly recomputed ``participant_count`` feeds the
    weights in the same pass.
    """
    await tx.run(LOCK_PERSON_FOR_LINK_RECOMPUTE, person_id=person_id)
    evidence = await _fetch_evidence(tx, person_id)
    link_ids = _ordered_link_ids(person_id, evidence)
    stale_keys = await _delete_stale_links(tx, person_id, link_ids)
    await _recompute_participant_counts(tx, _evidence_keys(evidence) | stale_keys)
    # Re-read after the counter refresh: the first read predates it, and the
    # weights must observe this Person's own recount in the same pass.
    evidence = await _fetch_evidence(tx, person_id)
    links, link_ids = build_link_rows(person_id, evidence)
    link_count = 0
    if links:
        result = await tx.run(
            MERGE_PERSON_HAS_IDENTIFIER_LINKS,
            person_id=person_id,
            links=links,
        )
        row = await result.single()
        if row is not None:
            link_count = to_int(row["link_count"])
    return link_count


async def release_person_has_identifier_links(tx: AsyncManagedTransaction, person_id: str) -> int:
    """Delete an absorbed Person's ``HAS_IDENTIFIER`` edges and refresh counts.

    The merged Person's identity reads resolve through the canonical survivor,
    so its HAS projection must not keep an identifier participant alive.
    """
    await tx.run(LOCK_PERSON_FOR_LINK_RECOMPUTE, person_id=person_id)
    result = await tx.run(DELETE_PERSON_HAS_IDENTIFIER_LINKS, person_id=person_id)
    released_keys: set[IdentifierKeyTuple] = set()
    async for record in result:
        released_keys.add(_key_from_record(record))
    await _recompute_participant_counts(tx, released_keys)
    return len(released_keys)


async def _fetch_evidence(tx: AsyncManagedTransaction, person_id: str) -> list[_Evidence]:
    result = await tx.run(FETCH_PERSON_ASSERTION_EVIDENCE, person_id=person_id)
    evidence: list[_Evidence] = []
    async for record in result:
        identifier_type = to_str(record["identifier_type"])
        field_trust: dict[str, str] = to_str_dict(record["field_trust"])
        evidence.append(
            _Evidence(
                source_record_pk=to_str(record["source_record_pk"]),
                identifier_id=to_str(record["identifier_id"]),
                identifier_type=identifier_type,
                identifier_scope=to_str(record["identifier_scope"]),
                normalized_value=to_str(record["normalized_value"]),
                quality_flag=to_optional_str(record["quality_flag"]),
                is_verified=to_optional_bool(record["is_verified"]),
                trust_tier=field_trust.get(identifier_type),
                observed_at=to_optional_str(record["observed_at"]),
                extraction_confidence=to_optional_float(record["extraction_confidence"]),
                record_type=to_optional_str(record["record_type"]),
                link_status=to_optional_str(record["link_status"]),
                participant_count=to_optional_int(record["participant_count"]),
            )
        )
    return evidence


async def _delete_stale_links(
    tx: AsyncManagedTransaction,
    person_id: str,
    link_ids: Sequence[str],
) -> set[IdentifierKeyTuple]:
    result = await tx.run(
        DELETE_STALE_PERSON_HAS_IDENTIFIER_LINKS,
        person_id=person_id,
        link_ids=list(link_ids),
    )
    stale_keys: set[IdentifierKeyTuple] = set()
    async for record in result:
        stale_keys.add(_key_from_record(record))
    return stale_keys


async def _recompute_participant_counts(
    tx: AsyncManagedTransaction,
    keys: set[IdentifierKeyTuple],
) -> None:
    rows: list[dict[str, JsonValue]] = [
        {
            "identifier_type": identifier_type,
            "identifier_scope": scope,
            "normalized_value": value,
        }
        for identifier_type, scope, value in sorted(keys)
    ]
    if rows:
        await tx.run(RECOMPUTE_IDENTIFIER_PARTICIPANT_COUNTS, identifier_keys=rows)


def _key_from_record(record: Record) -> IdentifierKeyTuple:
    return (
        to_str(record["identifier_type"]),
        to_str(record["identifier_scope"]),
        to_str(record["normalized_value"]),
    )


def _aggregate(
    evidence: Sequence[_Evidence],
    *,
    participant_count: int | None = None,
    reference_at: str | None = None,
) -> _LinkAggregate:
    observations = sorted(row["observed_at"] for row in evidence if row["observed_at"])
    first_seen_at = observations[0] if observations else None
    last_seen_at = observations[-1] if observations else None
    last_confirmed_at = last_seen_at
    resolved_count = participant_count
    if resolved_count is None:
        resolved_count = _evidence_participant_count(evidence)
    confirmed, candidate = _link_scores(evidence)
    weight = round6(combine_weights(confirmed, candidate))
    exclusivity = round6(exclusivity_for_count(resolved_count))
    anchor = reference_at if reference_at is not None else last_confirmed_at
    identifier_type = evidence[0]["identifier_type"] if evidence else ""
    half_life = half_life_for(identifier_type) if evidence else None
    rank = round6(
        rank_score(
            weight=weight,
            exclusivity=exclusivity,
            age_days=age_days_between(last_confirmed_at, anchor),
            half_life_days=half_life,
        )
    )
    return _LinkAggregate(
        assertion_count=len(evidence),
        source_count=len({row["source_record_pk"] for row in evidence}),
        verified_count=sum(
            1 for row in evidence if _has_complete_metadata(row) and row["is_verified"] is True
        ),
        # API TRUST_RANK orders tier_1=1 (highest) .. tier_4=4 (lowest), so the
        # best-supported tier is the minimum, matching ingestion's ranking.
        max_tier=min(
            (_evidence_tier(row) for row in evidence),
            default=LOWEST_TRUST_TIER,
            key=TRUST_RANK.__getitem__,
        ),
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        last_confirmed_at=last_confirmed_at,
        score=weight,
        exclusivity=exclusivity,
        rank_score=rank,
    )


def _link_scores(evidence: Sequence[_Evidence]) -> tuple[list[float], list[float]]:
    """Split deduped assertion strengths into confirmed/candidate buckets.

    Rows sharing a ``source_record_pk`` collapse to their max strength; the
    bucket follows the surviving row's ``link_status``.
    """
    best: dict[str, tuple[float, bool]] = {}
    for row in evidence:
        complete = _has_complete_metadata(row)
        strength = assertion_score(
            trust_tier=row["trust_tier"],
            quality_flag=row["quality_flag"],
            is_verified=row["is_verified"],
            complete=complete,
            extraction_confidence=row.get("extraction_confidence"),
            record_type=row.get("record_type"),
        )
        candidate_only = (row.get("link_status") or "") == CANDIDATE_LINK_STATUS
        previous = best.get(row["source_record_pk"])
        if previous is None or strength > previous[0]:
            best[row["source_record_pk"]] = (strength, candidate_only)
    confirmed = [strength for strength, candidate in best.values() if not candidate]
    candidate = [strength for strength, candidate in best.values() if candidate]
    return confirmed, candidate


def _evidence_participant_count(evidence: Sequence[_Evidence]) -> int | None:
    """Return the first well-typed ``participant_count`` in the evidence."""
    for row in evidence:
        count = row.get("participant_count")
        if isinstance(count, int) and not isinstance(count, bool):
            return count
    return None


def _group_last_confirmed(rows: Sequence[_Evidence]) -> str | None:
    """Return the latest observation time of one link's evidence."""
    latest = sorted(row["observed_at"] for row in rows if row["observed_at"])
    return latest[-1] if latest else None


def _has_complete_metadata(row: _Evidence) -> bool:
    return row["quality_flag"] is not None and row["is_verified"] is not None


def _evidence_tier(row: _Evidence) -> str:
    if not _has_complete_metadata(row):
        return LOWEST_TRUST_TIER
    tier = row["trust_tier"]
    if isinstance(tier, str) and tier in TRUST_RANK:
        return tier
    return LOWEST_TRUST_TIER


def _evidence_keys(evidence: Sequence[_Evidence]) -> set[IdentifierKeyTuple]:
    return {
        (row["identifier_type"], row["identifier_scope"], row["normalized_value"])
        for row in evidence
    }
