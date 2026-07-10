"""Score an existing person↔person pair by reusing the Layer-2 heuristic engine.

The shared-identifier pair auditor (:mod:`src.pipeline_person_pairs`) opens a
review case whenever one identifier bridges two active persons. On its own that
case carried no confidence, so reviewers could not tell a same-name/same-phone
duplicate apart from two distinct people who merely share a phone.

This adapter reuses the *exact* scoring criteria of the record match engine
(:func:`src.matching.heuristic.evaluate_heuristic`). One person is represented
as the "incoming record" — its golden identifiers, facts, and address — and
scored against the other person's candidate snapshot. The resulting confidence
is advisory triage signal only: the auditor still routes every bridge to human
review (precision over recall — a shared identifier never auto-merges persons).
"""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.matching.heuristic import evaluate_heuristic
from src.matching.snapshot import CandidateSnapshot, fetch_candidate_snapshot
from src.models import (
    MatchResult,
    NormalizedAddress,
    NormalizedAttribute,
    NormalizedIdentifier,
)

#: A person-pair bridge scoring at or above this confidence auto-merges the
#: two persons instead of opening a human review case. Distinct from
#: ``matching.heuristic.CONFIDENCE_AUTO_MERGE`` (0.90), which gates
#: source-record-to-person matching — person-pair merges use a higher bar
#: since no human confirms the merge.
PERSON_PAIR_AUTO_MERGE: float = 0.60


def score_person_pair(
    tx: ManagedTransaction,
    left_person_id: str,
    right_person_id: str,
) -> MatchResult:
    """Heuristic confidence for an existing person pair.

    ``left`` is represented as the incoming record and scored against ``right``'s
    candidate snapshot, reusing the record match engine's scoring criteria. The
    caller keeps the review decision regardless of the returned band — the score
    is advisory, never an auto-merge trigger. ``record_type`` is left at its
    default so conversation-promotion logic does not apply to pair audits.
    """
    left = fetch_candidate_snapshot(tx, left_person_id)
    return evaluate_heuristic(
        tx,
        right_person_id,
        _snapshot_identifiers(left),
        _snapshot_address(left),
        _snapshot_attributes(left),
    )


def _snapshot_identifiers(snapshot: CandidateSnapshot) -> list[NormalizedIdentifier]:
    identifiers: list[NormalizedIdentifier] = []
    for rec in snapshot.idents:
        identifier_type = rec.get("identifier_type")
        normalized_value = rec.get("normalized_value")
        if not isinstance(identifier_type, str) or not isinstance(normalized_value, str):
            continue
        identifiers.append(
            NormalizedIdentifier(
                identifier_type=identifier_type,
                normalized_value=normalized_value,
                is_verified=bool(rec.get("is_verified")),
            )
        )
    return identifiers


def _snapshot_attributes(snapshot: CandidateSnapshot) -> list[NormalizedAttribute]:
    attributes: list[NormalizedAttribute] = []
    for rec in snapshot.facts:
        attribute_name = rec.get("attribute_name")
        attribute_value = rec.get("attribute_value")
        if not isinstance(attribute_name, str) or not isinstance(attribute_value, str):
            continue
        attributes.append(
            NormalizedAttribute(attribute_name=attribute_name, attribute_value=attribute_value)
        )
    return attributes


def _snapshot_address(snapshot: CandidateSnapshot) -> NormalizedAddress | None:
    for rec in snapshot.addrs:
        normalized_full = rec.get("normalized_full")
        if isinstance(normalized_full, str) and normalized_full.strip():
            return NormalizedAddress(normalized_full=normalized_full)
    return None
