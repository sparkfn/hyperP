"""Layer 1 deterministic match rules — hard merge / hard no-match.

Hard NO_MATCH rules (locks, conflicting government IDs) always run regardless
of the incoming record's provenance — they are blockers, not merges. Hard
MERGE rules (exact government ID, trusted external ID) are suppressed when
the incoming record is a ``conversation`` extract: heuristically-extracted
identifiers are never sufficient on their own for an auto-merge.
"""

from __future__ import annotations

import logging

from neo4j import ManagedTransaction

from src.graph import queries
from src.models import (
    EngineType,
    MatchDecision,
    MatchResult,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
)

logger = logging.getLogger(__name__)


# Cypher snippets only used by this module — kept here so the deterministic
# layer is self-contained.
_FIND_OWNERS_OF_IDENTIFIER = """
MATCH (id:Identifier {
    identifier_type: $identifier_type,
    normalized_value: $normalized_value
})<-[rel:IDENTIFIED_BY]-(owner:Person {status: 'active'})
WHERE rel.is_active = true
  AND owner.person_id <> $candidate_person_id
RETURN owner.person_id AS owner_person_id
"""

_PERSON_HAS_IDENTIFIER = """
MATCH (p:Person {person_id: $person_id})
      -[rel:IDENTIFIED_BY]->(id:Identifier {
          identifier_type: $identifier_type,
          normalized_value: $normalized_value
      })
WHERE rel.is_active = true
RETURN p.person_id AS person_id
LIMIT 1
"""

_PERSON_HAS_CONFLICTING_GOVT_ID = """
MATCH (p:Person {person_id: $person_id})
      -[rel:IDENTIFIED_BY]->(id:Identifier {
          identifier_type: 'nric'
      })
WHERE rel.is_active = true
  AND id.normalized_value <> $normalized_value
RETURN id.normalized_value AS conflicting_value
LIMIT 1
"""

#: External ID types that produce a deterministic merge on exact match.
TRUSTED_ID_TYPES: tuple[str, ...] = ("external_customer_id", "membership_id")


def is_usable(flag: QualityFlag) -> bool:
    return flag in (QualityFlag.VALID, QualityFlag.PARTIAL_PARSE)


def evaluate_deterministic(
    tx: ManagedTransaction,
    candidate_person_id: str,
    identifiers: list[NormalizedIdentifier],
    record_type: RecordType,
) -> MatchResult | None:
    """Apply hard rules. Returns a result or ``None`` to fall through.

    TODO: also gate on candidate-side evidence type — if the candidate's
    only support for the matching identifier is a conversation source
    record, the deterministic merge should likewise be suppressed.
    """
    if (locked := _check_no_match_lock(tx, candidate_person_id, identifiers)):
        return locked
    if (govt := _check_government_id(tx, candidate_person_id, identifiers)):
        # Conflicting govt IDs (hard NO_MATCH) still apply for conversation
        # records; only the MERGE branch is suppressed below.
        if govt.decision == MatchDecision.NO_MATCH:
            return govt
        if record_type == RecordType.SYSTEM:
            return govt
    if record_type != RecordType.SYSTEM:
        return None
    if (trusted := _check_trusted_id(tx, candidate_person_id, identifiers)):
        return trusted
    return None


def _check_no_match_lock(
    tx: ManagedTransaction,
    candidate_person_id: str,
    identifiers: list[NormalizedIdentifier],
) -> MatchResult | None:
    """Hard NO_MATCH if any owner of these identifiers has a lock vs. candidate."""
    for ident in identifiers:
        if not is_usable(ident.quality_flag):
            continue
        owner_result = tx.run(
            _FIND_OWNERS_OF_IDENTIFIER,
            identifier_type=ident.identifier_type,
            normalized_value=ident.normalized_value,
            candidate_person_id=candidate_person_id,
        )
        for owner_rec in owner_result:
            owner_pid = owner_rec["owner_person_id"]
            lock_result = tx.run(
                queries.CHECK_NO_MATCH_LOCK,
                left_person_id=owner_pid,
                right_person_id=candidate_person_id,
            )
            lock_rec = lock_result.single()
            if lock_rec and lock_rec["is_locked"]:
                logger.info(
                    "NO_MATCH_LOCK between %s and candidate %s — hard no-match",
                    owner_pid, candidate_person_id,
                )
                return MatchResult(
                    decision=MatchDecision.NO_MATCH,
                    confidence=1.0,
                    reasons=[
                        f"NO_MATCH_LOCK exists between person {owner_pid} "
                        f"and candidate {candidate_person_id}"
                    ],
                    engine_type=EngineType.DETERMINISTIC,
                    matched_person_id=None,
                )
    return None


def _check_government_id(
    tx: ManagedTransaction,
    candidate_person_id: str,
    identifiers: list[NormalizedIdentifier],
) -> MatchResult | None:
    """Government ID hash: exact match → hard MERGE; conflict → hard NO_MATCH."""
    govt_ids = [
        i for i in identifiers
        if i.identifier_type == "nric"
        and i.quality_flag == QualityFlag.VALID
    ]
    for govt_id in govt_ids:
        if tx.run(
            _PERSON_HAS_IDENTIFIER,
            person_id=candidate_person_id,
            identifier_type="nric",
            normalized_value=govt_id.normalized_value,
        ).single():
            logger.info(
                "Deterministic hard merge: candidate %s shares govt ID hash",
                candidate_person_id,
            )
            return MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                reasons=["Exact government ID hash match"],
                engine_type=EngineType.DETERMINISTIC,
                matched_person_id=candidate_person_id,
            )

        if tx.run(
            _PERSON_HAS_CONFLICTING_GOVT_ID,
            person_id=candidate_person_id,
            normalized_value=govt_id.normalized_value,
        ).single():
            logger.info(
                "Deterministic hard no-match: candidate %s has conflicting govt ID",
                candidate_person_id,
            )
            return MatchResult(
                decision=MatchDecision.NO_MATCH,
                confidence=1.0,
                reasons=["Conflicting government ID hash — hard no-match"],
                engine_type=EngineType.DETERMINISTIC,
                matched_person_id=None,
            )
    return None


def _check_trusted_id(
    tx: ManagedTransaction,
    candidate_person_id: str,
    identifiers: list[NormalizedIdentifier],
) -> MatchResult | None:
    """Trusted migration-map IDs: exact match → namespace-scoped hard MERGE."""
    trusted_ids = [
        i for i in identifiers
        if i.identifier_type in TRUSTED_ID_TYPES
        and i.quality_flag == QualityFlag.VALID
    ]
    for tid in trusted_ids:
        if tx.run(
            _PERSON_HAS_IDENTIFIER,
            person_id=candidate_person_id,
            identifier_type=tid.identifier_type,
            normalized_value=tid.normalized_value,
        ).single():
            logger.info(
                "Deterministic hard merge: candidate %s shares trusted ID %s=%s",
                candidate_person_id, tid.identifier_type, tid.normalized_value,
            )
            return MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                reasons=[
                    f"Exact trusted {tid.identifier_type} match: "
                    f"{tid.normalized_value}"
                ],
                engine_type=EngineType.DETERMINISTIC,
                matched_person_id=candidate_person_id,
            )
    return None
