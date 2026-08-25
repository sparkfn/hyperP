"""Person↔person review-case detection.

After a record is linked, any usable identifier it carries may connect two or
more *active* persons. This module applies thresholds selected from the
triggering record type: relationship records use 0.20/0.10 merge/review bands,
while other records retain 0.40/0.20.

Detection reuses the existing fanout cap (high-fanout identifiers are
non-discriminating) and the canonical pair ordering (left.person_id < right).
Auto-merges preserve a MatchDecision, MergeEvent, and absorbed-person lineage.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from neo4j import ManagedTransaction

from src.graph import queries
from src.identifier_scopes import identifier_scope
from src.matching.pair_score import score_person_pair
from src.matching.thresholds import classify_confidence, has_hard_conflict, thresholds_for
from src.models import JsonValue, MatchDecision, NormalizedIdentifier, RecordType
from src.pipeline_normalization import fanout_cap_for, is_usable
from src.pipeline_person_merge import (
    PairPersonAttrs,
    fetch_pair_attrs,
    merge_person_pair,
    select_survivor,
)

logger = logging.getLogger(__name__)

_ENGINE_VERSION = "v0.1.0"
_POLICY_VERSION = "v0.1.0"
_PRIORITY = 100
_SLA_DAYS = 7


@dataclass(frozen=True)
class _PairOutcome:
    kind: Literal["review_case", "merged"]
    id: str


def audit_person_pairs(
    tx: ManagedTransaction,
    identifiers: list[NormalizedIdentifier],
    record_type: RecordType = RecordType.IDENTITY,
) -> list[str]:
    """Open person↔person review cases for shared-identifier bridges.

    Returns the list of created ``review_case_id``s (may be empty).
    """
    created: list[str] = []
    merged: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()

    usable_identifiers = [ident for ident in identifiers if is_usable(ident.quality_flag)]
    rows = [
        {
            "input_index": index,
            "identifier_type": ident.identifier_type,
            "identifier_scope": identifier_scope(ident.identifier_type, ident.source_instance_id),
            "normalized_value": ident.normalized_value,
        }
        for index, ident in enumerate(usable_identifiers)
    ]
    if not rows:
        return []

    for record in tx.run(queries.FIND_PERSONS_SHARING_IDENTIFIERS_BATCH, identifiers=rows):
        ident = usable_identifiers[int(record["input_index"])]
        fanout = int(record["fanout"])
        cap = fanout_cap_for(ident.identifier_type)
        if cap is not None and fanout > cap:
            logger.warning(
                "Skipping high-fanout identifier %s=%s (fanout=%d, cap=%d)",
                ident.identifier_type,
                ident.normalized_value,
                fanout,
                cap,
            )
            continue
        person_ids = sorted({str(pid) for pid in record["person_ids"]})
        for i in range(len(person_ids)):
            for j in range(i + 1, len(person_ids)):
                pair = (person_ids[i], person_ids[j])  # left < right by sort
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                outcome = _process_pair(tx, pair[0], pair[1], ident, record_type)
                if outcome is None:
                    continue
                if outcome.kind == "review_case":
                    created.append(outcome.id)
                else:
                    merged.append(outcome.id)

    if created:
        logger.info("Opened %d person-pair review case(s): %s", len(created), created)
    if merged:
        logger.info("Auto-merged %d person-pair(s): %s", len(merged), merged)
    return created


def _process_pair(
    tx: ManagedTransaction,
    left_person_id: str,
    right_person_id: str,
    ident: NormalizedIdentifier,
    record_type: RecordType,
) -> _PairOutcome | None:
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

    attrs = fetch_pair_attrs(tx, left_person_id, right_person_id)
    if attrs is None:
        return None
    left_attrs, right_attrs = attrs
    if left_attrs.status != "active" or right_attrs.status != "active":
        return None

    score = score_person_pair(tx, left_person_id, right_person_id)
    auto_merge, review = thresholds_for(record_type)
    snapshot: dict[str, JsonValue] = {
        "bridging_identifier_type": ident.identifier_type,
        "bridging_identifier_value": ident.normalized_value,
        # Band the heuristic score *would* fall in, surfaced for triage only.
        "heuristic_band": score.decision.value,
        **score.feature_snapshot,
        "threshold_policy": (
            "relationship" if record_type == RecordType.RELATIONSHIP else "default"
        ),
        "auto_merge_threshold": auto_merge,
        "review_threshold": review,
    }
    feature_snapshot = json.dumps(snapshot)
    reasons = [
        f"Shared {ident.identifier_type} links 2 active persons "
        f"({left_person_id}, {right_person_id})",
        *score.reasons,
    ]
    decision = classify_confidence(
        score.confidence,
        record_type,
        has_hard_conflict=has_hard_conflict(score.feature_snapshot),
    )
    if decision == MatchDecision.MERGE:
        return _auto_merge_pair(
            tx,
            left_person_id=left_person_id,
            right_person_id=right_person_id,
            left_attrs=left_attrs,
            right_attrs=right_attrs,
            confidence=score.confidence,
            reasons=reasons,
            feature_snapshot=feature_snapshot,
        )
    if decision == MatchDecision.NO_MATCH:
        return None
    return _open_review_case(
        tx,
        left_person_id=left_person_id,
        right_person_id=right_person_id,
        confidence=score.confidence,
        reasons=reasons,
        feature_snapshot=feature_snapshot,
    )


def _auto_merge_pair(
    tx: ManagedTransaction,
    *,
    left_person_id: str,
    right_person_id: str,
    left_attrs: PairPersonAttrs,
    right_attrs: PairPersonAttrs,
    confidence: float,
    reasons: list[str],
    feature_snapshot: str,
) -> _PairOutcome:
    record = tx.run(
        queries.CREATE_MATCH_DECISION,
        engine_type="pair_audit",
        engine_version=_ENGINE_VERSION,
        decision="merge",
        confidence=confidence,
        reasons=reasons,
        blocking_conflicts=[],
        feature_snapshot=feature_snapshot,
        policy_version=_POLICY_VERSION,
    ).single()
    assert record is not None, "CREATE_MATCH_DECISION must return a row"
    match_decision_id = record["match_decision_id"]
    assert isinstance(match_decision_id, str)
    tx.run(
        queries.LINK_MATCH_DECISION_LEFT_PERSON,
        match_decision_id=match_decision_id,
        person_id=left_person_id,
    )
    tx.run(
        queries.LINK_MATCH_DECISION_RIGHT_PERSON,
        match_decision_id=match_decision_id,
        person_id=right_person_id,
    )
    survivor_id, absorbed_id = select_survivor(left_attrs, right_attrs)
    merge_event_id = merge_person_pair(
        tx,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        match_decision_id=match_decision_id,
        reason="; ".join(reasons),
    )
    return _PairOutcome(kind="merged", id=merge_event_id)


def _open_review_case(
    tx: ManagedTransaction,
    *,
    left_person_id: str,
    right_person_id: str,
    confidence: float,
    reasons: list[str],
    feature_snapshot: str,
) -> _PairOutcome | None:
    sla_due_at = (datetime.now(UTC) + timedelta(days=_SLA_DAYS)).isoformat()
    record = tx.run(
        queries.CREATE_PERSON_PAIR_REVIEW_CASE,
        left_person_id=left_person_id,
        right_person_id=right_person_id,
        priority=_PRIORITY,
        sla_due_at=sla_due_at,
        engine_version=_ENGINE_VERSION,
        policy_version=_POLICY_VERSION,
        confidence=confidence,
        reasons=reasons,
        feature_snapshot=feature_snapshot,
    ).single()
    if record is None:
        return None
    return _PairOutcome(kind="review_case", id=str(record["review_case_id"]))
