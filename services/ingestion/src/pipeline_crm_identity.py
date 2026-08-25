"""CRM-deal-specific identity resolution and projection safeguards."""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.matching.deterministic import prefetch_no_match_lock_owners
from src.models import (
    EngineType,
    JsonValue,
    MatchDecision,
    MatchResult,
    NormalizedIdentifier,
    RecordType,
    SourceRecordEnvelope,
)
from src.pipeline_writes import find_candidates

_CRM_QUARANTINE_KEY = "crm_deal_quarantine"
_MERGE_CANDIDATE_KEY = "merge_candidate_person_ids"


def resolve_canonical_crm_contact(
    tx: ManagedTransaction,
    envelope: SourceRecordEnvelope,
    identifiers: list[NormalizedIdentifier],
    *,
    continuity_person_id: str | None = None,
) -> MatchResult | None:
    """Resolve a primary Bitrix CRM contact without bypassing hard blockers."""
    if envelope.record_type is not RecordType.CRM_DEAL:
        return None
    contact_id = envelope.raw_payload.get("primary_contact_id")
    if not isinstance(contact_id, str) or not contact_id:
        return None
    canonical = [
        item
        for item in identifiers
        if item.identifier_type == "crm_contact_id" and item.normalized_value == contact_id
    ]
    if not canonical:
        return None
    canonical_candidates = find_candidates(tx, canonical, [])
    person_ids = sorted({candidate.person_id for candidate in canonical_candidates})
    if not person_ids:
        return None
    blocked_owners = prefetch_no_match_lock_owners(tx, person_ids, identifiers)
    eligible_person_ids = [person_id for person_id in person_ids if person_id not in blocked_owners]
    if not eligible_person_ids:
        blocked_values: list[JsonValue] = list(person_ids)
        return MatchResult(
            decision=MatchDecision.REVIEW,
            confidence=1.0,
            reasons=["canonical_crm_contact_owner_blocked_by_no_match_lock"],
            engine_type=EngineType.DETERMINISTIC,
            feature_snapshot={
                _CRM_QUARANTINE_KEY: True,
                "blocked_canonical_crm_contact_candidate_ids": blocked_values,
            },
        )
    if len(person_ids) == 1:
        return deterministic_crm_owner_result(
            eligible_person_ids[0],
            continuity_person_id=continuity_person_id,
            merge_reason="canonical_crm_contact_id",
            continuity_review_reason="changed_canonical_crm_contact_requires_review",
        )
    review_person_id = (
        continuity_person_id
        if continuity_person_id in eligible_person_ids
        else eligible_person_ids[0]
    )
    candidate_values: list[JsonValue] = list(eligible_person_ids)
    blocked_candidate_values: list[JsonValue] = list(blocked_owners)
    return MatchResult(
        decision=MatchDecision.REVIEW,
        confidence=1.0,
        reasons=["ambiguous_canonical_crm_contact_id"],
        engine_type=EngineType.DETERMINISTIC,
        matched_person_id=review_person_id,
        proposed_person_id=eligible_person_ids[0],
        review_candidate_person_ids=eligible_person_ids,
        feature_snapshot={
            "canonical_crm_contact_candidate_ids": candidate_values,
            "blocked_canonical_crm_contact_candidate_ids": blocked_candidate_values,
            "continuity_person_id": continuity_person_id,
        },
    )


def deterministic_crm_owner_result(
    person_id: str,
    *,
    continuity_person_id: str | None,
    merge_reason: str,
    continuity_review_reason: str,
) -> MatchResult:
    """Preserve source continuity when deterministic CRM ownership changes."""
    if continuity_person_id is not None and person_id != continuity_person_id:
        return MatchResult(
            decision=MatchDecision.REVIEW,
            confidence=1.0,
            reasons=[continuity_review_reason],
            engine_type=EngineType.DETERMINISTIC,
            matched_person_id=continuity_person_id,
            proposed_person_id=person_id,
            feature_snapshot={
                "continuity_person_id": continuity_person_id,
                "proposed_crm_owner_person_id": person_id,
            },
        )
    return MatchResult(
        decision=MatchDecision.MERGE,
        confidence=1.0,
        reasons=[merge_reason],
        engine_type=EngineType.DETERMINISTIC,
        matched_person_id=person_id,
    )


def apply_crm_deal_match_policy(
    envelope: SourceRecordEnvelope,
    match_result: MatchResult,
    *,
    continuity_person_id: str | None = None,
) -> MatchResult:
    """Apply CRM ambiguity and reassignment safeguards to a generic result."""
    if envelope.record_type is not RecordType.CRM_DEAL:
        return match_result
    candidates = _generic_merge_candidates(match_result)
    if len(candidates) > 1:
        candidate_values: list[JsonValue] = list(candidates)
        review_person_id = (
            continuity_person_id if continuity_person_id in candidates else candidates[0]
        )
        return match_result.model_copy(
            update={
                "decision": MatchDecision.REVIEW,
                "matched_person_id": review_person_id,
                "proposed_person_id": candidates[0],
                "additional_linked_person_ids": [],
                "review_candidate_person_ids": candidates,
                "reasons": [*match_result.reasons, "ambiguous_crm_deal_merge_candidates"],
                "feature_snapshot": {
                    **match_result.feature_snapshot,
                    "crm_deal_merge_candidate_ids": candidate_values,
                },
            }
        )
    destination_person_id = match_result.proposed_person_id or match_result.matched_person_id
    if (
        continuity_person_id is not None
        and destination_person_id is not None
        and destination_person_id != continuity_person_id
    ):
        return match_result.model_copy(
            update={
                "decision": MatchDecision.REVIEW,
                "matched_person_id": continuity_person_id,
                "proposed_person_id": destination_person_id,
                "additional_linked_person_ids": [],
                "reasons": [
                    *match_result.reasons,
                    "generic_crm_owner_change_requires_review",
                ],
                "feature_snapshot": {
                    **match_result.feature_snapshot,
                    "continuity_person_id": continuity_person_id,
                    "proposed_crm_owner_person_id": destination_person_id,
                },
            }
        )
    return match_result


def crm_deal_requires_quarantine(match_result: MatchResult) -> bool:
    """Return whether a hard blocker makes this CRM deal unsafe to persist."""
    return match_result.feature_snapshot.get(_CRM_QUARANTINE_KEY) is True


def projected_identifiers(
    envelope: SourceRecordEnvelope,
    identifiers: list[NormalizedIdentifier],
) -> list[NormalizedIdentifier]:
    """Keep CRM channel values match-only; never project them onto a Person."""
    if envelope.record_type is not RecordType.CRM_DEAL:
        return identifiers
    return [
        identifier
        for identifier in identifiers
        if identifier.identifier_type not in {"phone", "email"}
    ]


def _generic_merge_candidates(match_result: MatchResult) -> list[str]:
    candidates = {
        person_id
        for person_id in [
            match_result.matched_person_id,
            *match_result.additional_linked_person_ids,
        ]
        if person_id is not None
    }
    snapshot_candidates = match_result.feature_snapshot.get(_MERGE_CANDIDATE_KEY)
    if isinstance(snapshot_candidates, list):
        candidates.update(item for item in snapshot_candidates if isinstance(item, str))
    return sorted(candidates)
