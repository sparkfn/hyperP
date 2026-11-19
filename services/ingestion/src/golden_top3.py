"""Top-3 golden identifier ranking over derived ``HAS_IDENTIFIER`` links.

One module owns ordering, pins, conflicts and JSON for the golden stage:
candidate-only links are never rankable, display types (phone/email/nric)
take ranks 1..3, and every write is conditional so reruns are zero-diff.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import TypedDict

from neo4j import ManagedTransaction

from src.golden_profile_active_authority import parse_survivorship_overrides
from src.golden_profile_authority_models import OverrideEntry
from src.graph import queries
from src.identity_link_writes import lock_identifiers
from src.identity_links import IdentifierKey, identifier_key_row, identifier_key_tuple
from src.identity_registry import identifier_policy
from src.models import JsonValue, QualityFlag
from src.normalizers.email import normalize_email
from src.normalizers.nric import is_nric_digest, masked_for, normalize_nric
from src.normalizers.phone import normalize_phone
from src.nric_anchor import LinkDecision

#: Display types covered by top-3 ranking (D4).
DISPLAY_IDENTIFIER_TYPES: tuple[str, str, str] = ("phone", "email", "nric")

#: Ranks assigned per display type.
TOP_N_PER_TYPE = 3

#: Override fields that pin an identifier link, mapped to identifier type.
_PIN_FIELDS: dict[str, str] = {
    "preferred_phone": "phone",
    "preferred_email": "email",
    "preferred_nric": "nric",
}


@dataclass(frozen=True)
class TopLink:
    """One rankable ``HAS_IDENTIFIER`` link with its evidence liveness."""

    link_id: str
    identifier_type: str
    identifier_scope: str
    normalized_value: str
    score: float
    exclusivity: float
    rank_score: float
    verified: bool
    last_confirmed_at: str | None
    source_count: int
    candidate_only: bool
    nric_masked: str | None
    evidence_pks: tuple[str, ...]


@dataclass(frozen=True)
class RankedLink:
    """One golden-stage link with its assigned rank (``None`` when unranked)."""

    link: TopLink
    rank: int | None


@dataclass(frozen=True)
class PinResolution:
    """Pin derivation outcome: pinned links and unresolvable override fields."""

    pinned: dict[str, LinkDecision]
    unresolved: tuple[str, ...]


@dataclass(frozen=True)
class GoldenTop3Outcome:
    """Golden-stage result: rank-1 picks (nric masked), JSON and conflicts."""

    person_id: str
    picks: dict[str, str | None]
    golden_json: str | None
    golden_conflicts: str


class ValueConflict(TypedDict):
    """A single-valued type with more than one distinct rankable value."""

    type: str
    reason: str
    values: list[str | None]


ConflictEntry = str | ValueConflict


def materialize_golden_top3(
    tx: ManagedTransaction,
    person_id: str,
    *,
    locked_keys: frozenset[tuple[str, str, str]] | None = None,
) -> GoldenTop3Outcome:
    """Rank one Person's links, write ranks and the golden document.

    Takes a value-preserving Person write lock, then one sorted Identifier
    lock batch for keys outside ``locked_keys``, and only then reads links
    and overrides, ranks, and writes. A Person with no rankable display
    links and no stored golden state writes nothing.
    """
    tx.run(queries.LOCK_PERSON_FOR_GOLDEN, person_id=person_id)
    key_rows = [
        _parse_link_key(row) for row in tx.run(queries.FETCH_PERSON_LINK_KEYS, person_id=person_id)
    ]
    known = locked_keys if locked_keys is not None else frozenset()
    fresh = sorted({identifier_key_tuple(key) for key in key_rows} - known)
    if fresh:
        lock_identifiers(tx, [identifier_key_row(key) for key in fresh])
    raw_rows = list(tx.run(queries.FETCH_PERSON_TOP_LINKS, person_id=person_id))
    links = [link for row in raw_rows if (link := _parse_top_link(row)) is not None]
    resolution = resolve_pins(
        links,
        parse_survivorship_overrides(_person_optional(raw_rows, "survivorship_overrides")),
        person_id,
    )
    ranked = rank_links(links, set(resolution.pinned))
    payload = build_golden_payload(ranked)
    conflicts = build_conflicts(ranked, resolution.unresolved)
    link_rows: list[dict[str, JsonValue]] = [
        {"link_id": item.link.link_id, "golden_rank": item.rank} for item in ranked
    ]
    if link_rows:
        tx.run(queries.SET_LINK_GOLDEN_RANKS, person_id=person_id, rows=link_rows)
    stored_json = _person_optional(raw_rows, "stored_golden_json")
    stored_conflicts = _person_optional(raw_rows, "stored_golden_conflicts")
    golden_json = json.dumps(payload, sort_keys=True) if payload else None
    conflicts_json = json.dumps(conflicts)
    if not payload and stored_json is None and stored_conflicts in (None, "[]"):
        return GoldenTop3Outcome(person_id, _empty_picks(), None, conflicts_json)
    tx.run(
        queries.UPDATE_GOLDEN_TOP3,
        person_id=person_id,
        golden_json=golden_json,
        golden_conflicts=conflicts_json,
    )
    return GoldenTop3Outcome(person_id, _picks_from_payload(payload), golden_json, conflicts_json)


def rank_links(links: Sequence[TopLink], pinned_ids: AbstractSet[str]) -> list[RankedLink]:
    """Attach golden ranks, preserving input order.

    Display links rank 1..3 within their type (pinned first, then
    ``rank_score`` DESC, verified DESC, ``last_confirmed_at`` DESC,
    ``normalized_value`` ASC); everything else keeps ``None``.
    """
    groups: dict[str, list[TopLink]] = {}
    for link in links:
        groups.setdefault(link.identifier_type, []).append(link)
    ranks: dict[str, int | None] = {}
    for identifier_type, peers in groups.items():
        if identifier_type not in DISPLAY_IDENTIFIER_TYPES:
            for peer in peers:
                ranks[peer.link_id] = None
            continue
        for position, peer in enumerate(_order_group(peers, pinned_ids)):
            ranks[peer.link_id] = position + 1 if position < TOP_N_PER_TYPE else None
        for peer in peers:
            ranks.setdefault(peer.link_id, None)
    return [RankedLink(link=link, rank=ranks[link.link_id]) for link in links]


def resolve_pins(
    links: Sequence[TopLink],
    overrides: Sequence[OverrideEntry],
    person_id: str,
) -> PinResolution:
    """Derive pin decisions from survivorship overrides without new state."""
    pinned: dict[str, LinkDecision] = {}
    unresolved: list[str] = []
    for override in overrides:
        identifier_type = _PIN_FIELDS.get(override.field)
        if identifier_type is None or override.malformed:
            continue
        target = _resolve_pin_target(links, identifier_type, override)
        if target is None:
            if override.field not in unresolved:
                unresolved.append(override.field)
            continue
        if target.link_id not in pinned:
            pinned[target.link_id] = LinkDecision(
                kind="pin",
                person_id=person_id,
                nric_keys=(),
                reason=f"survivorship override {override.field}",
            )
    return PinResolution(pinned=pinned, unresolved=tuple(sorted(unresolved)))


def build_golden_payload(
    ranked: Sequence[RankedLink],
) -> dict[str, list[dict[str, object]]]:
    """Build the per-type top-3 golden payload in rank order (nric masked)."""
    by_rank: dict[str, dict[int, TopLink]] = {}
    for item in ranked:
        if item.rank is None:
            continue
        by_rank.setdefault(item.link.identifier_type, {})[item.rank] = item.link
    payload: dict[str, list[dict[str, object]]] = {}
    for identifier_type in sorted(by_rank):
        entries: list[dict[str, object]] = []
        for rank in sorted(by_rank[identifier_type]):
            link = by_rank[identifier_type][rank]
            entries.append(
                {
                    "value": _display_value(link),
                    "rank": rank,
                    "score": link.score,
                    "verified": link.verified,
                    "last_confirmed_at": link.last_confirmed_at,
                    "source_count": link.source_count,
                }
            )
        payload[identifier_type] = entries
    return payload


def build_conflicts(ranked: Sequence[RankedLink], unresolved: Sequence[str]) -> list[ConflictEntry]:
    """Build the golden-conflicts array: value conflicts then unresolved pins."""
    values_by_type: dict[str, set[str | None]] = {}
    for item in ranked:
        link = item.link
        if link.candidate_only or link.identifier_type not in DISPLAY_IDENTIFIER_TYPES:
            continue
        values_by_type.setdefault(link.identifier_type, set()).add(_display_value(link))
    entries: list[ConflictEntry] = []
    for identifier_type in sorted(values_by_type):
        if not identifier_policy(identifier_type).single_valued:
            continue
        values = sorted(values_by_type[identifier_type], key=lambda v: (v is None, v or ""))
        if len(values) > 1:
            entries.append(
                ValueConflict(
                    type=identifier_type,
                    reason="multiple_valid_values",
                    values=values,
                )
            )
    for field in sorted(set(unresolved)):
        entries.append(f"pin_unresolved:{field}")
    return entries


def _order_group(peers: Sequence[TopLink], pinned_ids: AbstractSet[str]) -> list[TopLink]:
    """Order one type's rankable links: pinned, score, verified, time, value."""
    rankable = [peer for peer in peers if not peer.candidate_only]
    ordered = sorted(rankable, key=lambda peer: peer.normalized_value)
    ordered = sorted(
        ordered,
        key=lambda peer: (peer.last_confirmed_at is not None, peer.last_confirmed_at or ""),
        reverse=True,
    )
    ordered = sorted(ordered, key=lambda peer: peer.verified, reverse=True)
    ordered = sorted(ordered, key=lambda peer: peer.rank_score, reverse=True)
    return sorted(ordered, key=lambda peer: peer.link_id not in pinned_ids)


def _resolve_pin_target(
    links: Sequence[TopLink], identifier_type: str, override: OverrideEntry
) -> TopLink | None:
    """Resolve one pin override against active links of its type."""
    peers = [
        link
        for link in links
        if link.identifier_type == identifier_type and not link.candidate_only
    ]
    if override.source_record_pk is not None:
        for peer in peers:
            if override.source_record_pk in peer.evidence_pks:
                return peer
    if override.custom_value is not None:
        wanted = _normalized_custom_value(identifier_type, override.custom_value)
        if wanted is not None:
            for peer in peers:
                if peer.normalized_value == wanted:
                    return peer
    return None


def _normalized_custom_value(identifier_type: str, custom_value: str) -> str | None:
    """Normalize an override custom value for exact link matching."""
    if identifier_type == "phone":
        normalized, quality = normalize_phone(custom_value)
        return normalized if quality == QualityFlag.VALID else None
    if identifier_type == "email":
        normalized, quality = normalize_email(custom_value)
        return normalized if quality == QualityFlag.VALID else None
    if identifier_type == "nric":
        # Digest-shaped values are already normalized. Raw NRICs canonicalize
        # here, but they still cannot match HMAC-digest links — those pins
        # resolve by source_record_pk instead.
        if is_nric_digest(custom_value):
            return custom_value
        compact = "".join(custom_value.split())
        normalized, quality = normalize_nric(compact)
        return normalized if quality == QualityFlag.VALID else None
    return custom_value


def _display_value(link: TopLink) -> str | None:
    """Return the display value for a link (masked for nric)."""
    if link.identifier_type == "nric":
        return masked_for(link.normalized_value, link.nric_masked)
    return link.normalized_value


def _empty_picks() -> dict[str, str | None]:
    """Return rank-1 picks with every display type unresolved."""
    return {identifier_type: None for identifier_type in DISPLAY_IDENTIFIER_TYPES}


def _picks_from_payload(
    payload: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, str | None]:
    """Return rank-1 picks from a golden payload (nric already masked)."""
    picks = _empty_picks()
    for identifier_type in DISPLAY_IDENTIFIER_TYPES:
        entries = payload.get(identifier_type)
        if not entries:
            continue
        value = entries[0].get("value")
        picks[identifier_type] = value if value is None or isinstance(value, str) else None
    return picks


def _parse_link_key(row: Mapping[str, object]) -> IdentifierKey:
    """Parse one key-only link Identifier row."""
    return IdentifierKey(
        identifier_type=_row_str(row, "identifier_type"),
        identifier_scope=_row_str(row, "identifier_scope"),
        normalized_value=_row_str(row, "normalized_value"),
    )


def _parse_top_link(row: Mapping[str, object]) -> TopLink | None:
    """Parse one golden-stage link row; ``None`` for the link-less row."""
    if row.get("link_id") is None:
        return None
    return TopLink(
        link_id=_row_str(row, "link_id"),
        identifier_type=_row_str(row, "identifier_type"),
        identifier_scope=_row_str(row, "identifier_scope"),
        normalized_value=_row_str(row, "normalized_value"),
        score=_row_float(row, "score"),
        exclusivity=_row_float(row, "exclusivity"),
        rank_score=_row_float(row, "rank_score"),
        verified=_row_int(row, "verified_count") > 0,
        last_confirmed_at=_row_optional_str(row, "last_confirmed_at"),
        source_count=_row_int(row, "source_count"),
        candidate_only=_row_int(row, "active_evidence_count") == 0,
        nric_masked=_row_optional_str(row, "nric_masked"),
        evidence_pks=_row_str_list(row, "evidence_pks"),
    )


def _person_optional(rows: Sequence[Mapping[str, object]], key: str) -> str | None:
    """Read a Person-level value from the first link row, if any."""
    if not rows:
        return None
    return _row_optional_str(rows[0], key)


def _row_str(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise ValueError(f"golden top link {key} is malformed")
    return value


def _row_optional_str(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"golden top link {key} is malformed")


def _row_float(row: Mapping[str, object], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool):
        raise ValueError(f"golden top link {key} is malformed")
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"golden top link {key} is malformed")


def _row_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool):
        raise ValueError(f"golden top link {key} is malformed")
    if isinstance(value, int):
        return value
    raise ValueError(f"golden top link {key} is malformed")


def _row_str_list(row: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = row.get(key)
    if value is None:
        return ()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(item for item in value if isinstance(item, str))
    raise ValueError(f"golden top link {key} is malformed")
