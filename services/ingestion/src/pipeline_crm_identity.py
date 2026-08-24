"""CRM-deal-specific identity resolution and projection safeguards."""

from __future__ import annotations

from neo4j import ManagedTransaction

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


def resolve_canonical_crm_contact(
    tx: ManagedTransaction,
    envelope: SourceRecordEnvelope,
    identifiers: list[NormalizedIdentifier],
    *,
    continuity_person_id: str | None = None,
) -> MatchResult | None:
    """Resolve a primary Bitrix CRM contact before generic channel matching."""
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
    if len(person_ids) == 1:
        return deterministic_crm_owner_result(
            person_ids[0],
            continuity_person_id=continuity_person_id,
            merge_reason="canonical_crm_contact_id",
            continuity_review_reason="changed_canonical_crm_contact_requires_review",
        )
    if len(person_ids) > 1:
        review_person_id = continuity_person_id or person_ids[0]
        candidate_values: list[JsonValue] = list(person_ids)
        return MatchResult(
            decision=MatchDecision.REVIEW,
            confidence=1.0,
            reasons=["ambiguous_canonical_crm_contact_id"],
            engine_type=EngineType.DETERMINISTIC,
            matched_person_id=review_person_id,
            feature_snapshot={
                "canonical_crm_contact_candidate_ids": candidate_values,
                "continuity_person_id": continuity_person_id,
            },
        )
    return None


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
) -> MatchResult:
    """CRM deals review ambiguity instead of using generic link-to-all semantics."""
    if (
        envelope.record_type is not RecordType.CRM_DEAL
        or not match_result.additional_linked_person_ids
    ):
        return match_result
    candidates = sorted(
        {
            person_id
            for person_id in [
                match_result.matched_person_id,
                *match_result.additional_linked_person_ids,
            ]
            if person_id is not None
        }
    )
    candidate_values: list[JsonValue] = list(candidates)
    return match_result.model_copy(
        update={
            "decision": MatchDecision.REVIEW,
            "matched_person_id": candidates[0],
            "additional_linked_person_ids": [],
            "reasons": [*match_result.reasons, "ambiguous_crm_deal_merge_candidates"],
            "feature_snapshot": {
                **match_result.feature_snapshot,
                "crm_deal_merge_candidate_ids": candidate_values,
            },
        }
    )


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
