"""Person↔person review-case detection.

After a record is linked, any usable identifier it carries may now connect two
or more *active* persons. That shared-identifier bridge is the signal that the
persons might be duplicates — but the match engine deliberately does not merge
persons that merely share an identifier. This module opens a pairwise
person↔person ReviewCase for each newly-bridged pair so a human can adjudicate.

Detection reuses the existing fanout cap (high-fanout identifiers are
non-discriminating) and the canonical pair ordering (left.person_id < right).
It only creates audit cases; it never merges or links persons.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from neo4j import ManagedTransaction

from src.graph import queries
from src.models import NormalizedIdentifier
from src.pipeline_normalization import is_usable
from src.pipeline_writes import exceeds_fanout_cap

logger = logging.getLogger(__name__)

_ENGINE_VERSION = "v0.1.0"
_POLICY_VERSION = "v0.1.0"
_PRIORITY = 100
_SLA_DAYS = 7


def audit_person_pairs(
    tx: ManagedTransaction,
    identifiers: list[NormalizedIdentifier],
) -> list[str]:
    """Open person↔person review cases for shared-identifier bridges.

    Returns the list of created ``review_case_id``s (may be empty).
    """
    created: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()

    for ident in identifiers:
        if not is_usable(ident.quality_flag):
            continue
        if exceeds_fanout_cap(tx, ident):
            continue
        record = tx.run(
            queries.FIND_PERSONS_SHARING_IDENTIFIER,
            identifier_type=ident.identifier_type,
            normalized_value=ident.normalized_value,
        ).single()
        if record is None:
            continue
        person_ids = sorted({str(pid) for pid in record["person_ids"]})
        for i in range(len(person_ids)):
            for j in range(i + 1, len(person_ids)):
                pair = (person_ids[i], person_ids[j])  # left < right by sort
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                case_id = _create_pair_case_if_needed(tx, pair[0], pair[1], ident)
                if case_id is not None:
                    created.append(case_id)

    if created:
        logger.info("Opened %d person-pair review case(s): %s", len(created), created)
    return created


def _create_pair_case_if_needed(
    tx: ManagedTransaction,
    left_person_id: str,
    right_person_id: str,
    ident: NormalizedIdentifier,
) -> str | None:
    lock = tx.run(
        queries.CHECK_NO_MATCH_LOCK,
        left_person_id=left_person_id,
        right_person_id=right_person_id,
    ).single()
    if lock is not None and bool(lock["is_locked"]):
        return None

    existing = tx.run(
        queries.CHECK_OPEN_PERSON_PAIR_CASE,
        left_person_id=left_person_id,
        right_person_id=right_person_id,
    ).single()
    if existing is not None and existing.get("review_case_id") is not None:
        return None

    sla_due_at = (datetime.now(UTC) + timedelta(days=_SLA_DAYS)).isoformat()
    feature_snapshot = json.dumps(
        {
            "bridging_identifier_type": ident.identifier_type,
            "bridging_identifier_value": ident.normalized_value,
        }
    )
    reasons = [
        f"Shared {ident.identifier_type} links 2 active persons "
        f"({left_person_id}, {right_person_id})"
    ]
    record = tx.run(
        queries.CREATE_PERSON_PAIR_REVIEW_CASE,
        left_person_id=left_person_id,
        right_person_id=right_person_id,
        priority=_PRIORITY,
        sla_due_at=sla_due_at,
        engine_version=_ENGINE_VERSION,
        policy_version=_POLICY_VERSION,
        reasons=reasons,
        feature_snapshot=feature_snapshot,
    ).single()
    if record is None:
        return None
    return str(record["review_case_id"])
