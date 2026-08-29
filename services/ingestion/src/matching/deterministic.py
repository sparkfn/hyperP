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

from src.models import (
    SYSTEM_FAMILY,
    EngineType,
    MatchDecision,
    MatchResult,
    NormalizedAttribute,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
)

logger = logging.getLogger(__name__)


# Cypher snippets only used by this module — kept here so the deterministic
# layer is self-contained.
_FIND_ACTIVE_NO_MATCH_LOCKS = """
UNWIND $candidate_inputs AS candidate_input
MATCH (candidate:Person {person_id: candidate_input.person_id})
UNWIND $identifier_inputs AS identifier_input
MATCH (id:Identifier {
    identifier_type: identifier_input.identifier_type,
    normalized_value: identifier_input.normalized_value
})<-[rel:IDENTIFIED_BY]-(owner:Person {status: 'active'})
WHERE coalesce(rel.is_active, true) = true
  AND owner.person_id <> candidate.person_id
MATCH (owner)-[lock:NO_MATCH_LOCK]-(candidate)
WHERE lock.expires_at IS NULL OR lock.expires_at > datetime()
WITH candidate_input, identifier_input, owner
ORDER BY candidate_input.input_index, identifier_input.input_index, owner.person_id
RETURN candidate_input.person_id AS candidate_person_id,
       owner.person_id AS owner_person_id
"""

_PERSON_HAS_IDENTIFIER = """
MATCH (p:Person {person_id: $person_id})
      -[rel:IDENTIFIED_BY]->(id:Identifier {
          identifier_type: $identifier_type,
          normalized_value: $normalized_value
      })
WHERE coalesce(rel.is_active, true) = true
RETURN p.person_id AS person_id
LIMIT 1
"""

# Government-ID hard rules require a VALID-quality edge on the candidate side
# too — symmetric with the incoming filter (only VALID incoming NRICs reach
# these checks). A partial_parse / low-quality NRIC edge must not drive a
# confidence-1.0 auto-merge or a hard no-match block.
_PERSON_HAS_VALID_GOVT_ID = """
MATCH (p:Person {person_id: $person_id})
      -[rel:IDENTIFIED_BY]->(id:Identifier {
          identifier_type: 'nric',
          normalized_value: $normalized_value
      })
WHERE coalesce(rel.is_active, true) = true
  AND rel.quality_flag = 'valid'
RETURN p.person_id AS person_id
LIMIT 1
"""

_PERSON_HAS_CONFLICTING_GOVT_ID = """
MATCH (p:Person {person_id: $person_id})
      -[rel:IDENTIFIED_BY]->(id:Identifier {
          identifier_type: 'nric'
      })
WHERE coalesce(rel.is_active, true) = true
  AND rel.quality_flag = 'valid'
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
    attributes: list[NormalizedAttribute],
    record_type: RecordType,
    *,
    no_match_lock_owners: dict[str, str] | None = None,
) -> MatchResult | None:
    """Apply hard rules. Returns a result or ``None`` to fall through.

    TODO: also gate on candidate-side evidence type — if the candidate's
    only support for the matching identifier is a conversation source
    record, the deterministic merge should likewise be suppressed.
    """
    if no_match_lock_owners is None:
        locked = _check_no_match_lock(tx, candidate_person_id, identifiers)
    else:
        owner_person_id = no_match_lock_owners.get(candidate_person_id)
        locked = (
            _no_match_lock_result(owner_person_id, candidate_person_id)
            if owner_person_id is not None
            else None
        )
    if locked is not None:
        return locked
    if govt := _check_government_id(tx, candidate_person_id, identifiers):
        # Conflicting govt IDs (hard NO_MATCH) still apply for conversation
        # records; only the MERGE branch is suppressed below.
        if govt.decision == MatchDecision.NO_MATCH:
            return govt
        if record_type in SYSTEM_FAMILY:
            return govt
    if record_type not in SYSTEM_FAMILY:
        return None
    if trusted := _check_trusted_id(tx, candidate_person_id, identifiers):
        return trusted
    return None


def prefetch_no_match_lock_owners(
    tx: ManagedTransaction,
    candidate_person_ids: list[str],
    identifiers: list[NormalizedIdentifier],
) -> dict[str, str]:
    """Return one active lock owner per blocked candidate in a single query.

    Candidate and identifier input indexes preserve the engine's evaluation
    order. The owner ID is used only for the persisted explanation; the hard
    NO_MATCH decision remains identical when several active owners exist.
    """
    identifier_inputs = [
        {
            "input_index": input_index,
            "identifier_type": ident.identifier_type,
            "normalized_value": ident.normalized_value,
        }
        for input_index, ident in enumerate(identifiers)
        if is_usable(ident.quality_flag)
    ]
    if not candidate_person_ids or not identifier_inputs:
        return {}

    candidate_inputs = [
        {"input_index": input_index, "person_id": person_id}
        for input_index, person_id in enumerate(candidate_person_ids)
    ]
    owners: dict[str, str] = {}
    for record in tx.run(
        _FIND_ACTIVE_NO_MATCH_LOCKS,
        candidate_inputs=candidate_inputs,
        identifier_inputs=identifier_inputs,
    ):
        candidate_person_id = str(record["candidate_person_id"])
        owners.setdefault(candidate_person_id, str(record["owner_person_id"]))
    return owners


def _check_no_match_lock(
    tx: ManagedTransaction,
    candidate_person_id: str,
    identifiers: list[NormalizedIdentifier],
) -> MatchResult | None:
    """Hard NO_MATCH if an identifier owner has an active lock vs. candidate."""
    owners = prefetch_no_match_lock_owners(tx, [candidate_person_id], identifiers)
    owner_person_id = owners.get(candidate_person_id)
    if owner_person_id is None:
        return None
    return _no_match_lock_result(owner_person_id, candidate_person_id)


def _no_match_lock_result(owner_person_id: str, candidate_person_id: str) -> MatchResult:
    logger.info(
        "NO_MATCH_LOCK between %s and candidate %s - hard no-match",
        owner_person_id,
        candidate_person_id,
    )
    return MatchResult(
        decision=MatchDecision.NO_MATCH,
        confidence=1.0,
        reasons=[
            f"NO_MATCH_LOCK exists between person {owner_person_id} "
            f"and candidate {candidate_person_id}"
        ],
        engine_type=EngineType.DETERMINISTIC,
        matched_person_id=None,
    )


def _check_government_id(
    tx: ManagedTransaction,
    candidate_person_id: str,
    identifiers: list[NormalizedIdentifier],
) -> MatchResult | None:
    """Government ID hash: exact match → hard MERGE; conflict → hard NO_MATCH.

    A matching valid-quality NRIC always hard-merges — name is never consulted
    for this check, for any record type.
    """
    govt_ids = [
        i
        for i in identifiers
        if i.identifier_type == "nric" and i.quality_flag == QualityFlag.VALID
    ]
    for govt_id in govt_ids:
        if tx.run(
            _PERSON_HAS_VALID_GOVT_ID,
            person_id=candidate_person_id,
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
        i
        for i in identifiers
        if i.identifier_type in TRUSTED_ID_TYPES and i.quality_flag == QualityFlag.VALID
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
                candidate_person_id,
                tid.identifier_type,
                tid.normalized_value,
            )
            return MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                reasons=[f"Exact trusted {tid.identifier_type} match: {tid.normalized_value}"],
                engine_type=EngineType.DETERMINISTIC,
                matched_person_id=candidate_person_id,
            )
    return None
