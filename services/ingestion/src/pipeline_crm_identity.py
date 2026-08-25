"""CRM-deal-specific identity resolution and projection safeguards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
RepairClassification = Literal[
    "not_crm_deal",
    "same_owner_clean_reversion",
    "changed_owner_requires_review",
    "historical_owner_requires_review",
    "ambiguous_owner_requires_review",
    "blocked_owner_requires_review",
    "unlinked_requires_review",
]
OwnerProvenanceClass = Literal[
    "independent_trusted",
    "reviewed_v2",
    "historical_deal_only",
    "self_supporting",
    "blocked_or_conflicting",
]


@dataclass(frozen=True)
class CrmOwnerProvenance:
    """Typed evidence boundary for repair-time automatic ownership."""

    person_id: str
    provenance_class: OwnerProvenanceClass
    supporting_source_record_pks: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.person_id:
            raise ValueError("CRM owner provenance person_id must be non-empty")
        if self.provenance_class not in {
            "independent_trusted",
            "reviewed_v2",
            "historical_deal_only",
            "self_supporting",
            "blocked_or_conflicting",
        }:
            raise ValueError("CRM owner provenance class is invalid")
        if not self.supporting_source_record_pks or any(
            not source_record_pk for source_record_pk in self.supporting_source_record_pks
        ):
            raise ValueError("CRM owner provenance requires supporting source identities")


@dataclass(frozen=True)
class CrmDealIdentityPlan:
    """Policy result reusable by normal ingestion and historical repair.

    ``owner_provenance`` is intentionally optional. Normal ingestion passes
    nothing and preserves its established behavior. Repair passes typed
    provenance, preventing contaminated historical links from authorizing a
    repair by convention alone.
    """

    match_result: MatchResult
    projected_identifiers: tuple[NormalizedIdentifier, ...]
    classification: RepairClassification
    selected_person_id: str | None
    provisional_person_id: str | None
    eligible_person_ids: tuple[str, ...]
    blocked_person_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


def plan_crm_deal_identity(
    envelope: SourceRecordEnvelope,
    identifiers: list[NormalizedIdentifier],
    match_result: MatchResult,
    *,
    current_owner_ids: tuple[str, ...] = (),
    owner_provenance: tuple[CrmOwnerProvenance, ...] | None = None,
    repair_source_record_pk: str | None = None,
    continuity_person_id: str | None = None,
) -> CrmDealIdentityPlan:
    """Apply v2 policy and classify the result for ingestion or repair.

    Repair callers must supply typed owner provenance. Only independently
    trusted or reviewed-v2 evidence can authorize automatic retention.
    Omitting provenance retains normal ingestion behavior and is therefore not
    an authority shortcut for the repair subsystem.
    """
    policy_result = apply_crm_deal_match_policy(
        envelope,
        match_result,
        continuity_person_id=continuity_person_id,
    )
    projected = tuple(projected_identifiers(envelope, identifiers))
    if envelope.record_type is not RecordType.CRM_DEAL:
        return CrmDealIdentityPlan(
            match_result=policy_result,
            projected_identifiers=projected,
            classification="not_crm_deal",
            selected_person_id=None,
            provisional_person_id=None,
            eligible_person_ids=(),
            blocked_person_ids=(),
            reason_codes=tuple(policy_result.reasons),
        )

    candidates = _candidate_person_ids(policy_result)
    blocked = _blocked_person_ids(policy_result)
    eligible = tuple(person_id for person_id in candidates if person_id not in blocked)
    destination = policy_result.proposed_person_id or policy_result.matched_person_id
    if crm_deal_requires_quarantine(policy_result):
        return CrmDealIdentityPlan(
            match_result=policy_result,
            projected_identifiers=projected,
            classification="blocked_owner_requires_review",
            selected_person_id=None,
            provisional_person_id=None,
            eligible_person_ids=eligible,
            blocked_person_ids=blocked,
            reason_codes=tuple(policy_result.reasons),
        )
    if policy_result.decision is MatchDecision.REVIEW:
        provisional = destination if len(eligible) == 1 and destination in eligible else None
        return CrmDealIdentityPlan(
            match_result=policy_result,
            projected_identifiers=projected,
            classification=(
                "ambiguous_owner_requires_review" if eligible else "unlinked_requires_review"
            ),
            selected_person_id=None,
            provisional_person_id=provisional,
            eligible_person_ids=eligible,
            blocked_person_ids=blocked,
            reason_codes=tuple(policy_result.reasons),
        )
    if destination is None:
        return CrmDealIdentityPlan(
            match_result=policy_result,
            projected_identifiers=projected,
            classification="unlinked_requires_review",
            selected_person_id=None,
            provisional_person_id=None,
            eligible_person_ids=eligible,
            blocked_person_ids=blocked,
            reason_codes=tuple(policy_result.reasons),
        )
    if owner_provenance is None:
        return CrmDealIdentityPlan(
            match_result=policy_result,
            projected_identifiers=projected,
            classification="same_owner_clean_reversion",
            selected_person_id=destination,
            provisional_person_id=None,
            eligible_person_ids=eligible or (destination,),
            blocked_person_ids=blocked,
            reason_codes=tuple(policy_result.reasons),
        )
    if not repair_source_record_pk:
        raise ValueError("repair planning requires the contaminated source-record identity")
    provenance_by_person: dict[str, list[CrmOwnerProvenance]] = {}
    for evidence in owner_provenance:
        provenance_by_person.setdefault(evidence.person_id, []).append(evidence)
    trusted: set[str] = set()
    disqualifying_classes = {
        "historical_deal_only",
        "self_supporting",
        "blocked_or_conflicting",
    }
    for person_id, evidence_rows in provenance_by_person.items():
        classes = {evidence.provenance_class for evidence in evidence_rows}
        has_independent_support = any(
            evidence.provenance_class in {"independent_trusted", "reviewed_v2"}
            and any(
                source_record_pk != repair_source_record_pk
                for source_record_pk in evidence.supporting_source_record_pks
            )
            for evidence in evidence_rows
        )
        if has_independent_support and classes.isdisjoint(disqualifying_classes):
            trusted.add(person_id)
    if destination not in current_owner_ids:
        classification: RepairClassification = "changed_owner_requires_review"
    elif destination not in trusted:
        classification = "historical_owner_requires_review"
    else:
        classification = "same_owner_clean_reversion"
    return CrmDealIdentityPlan(
        match_result=policy_result,
        projected_identifiers=projected,
        classification=classification,
        selected_person_id=(
            destination if classification == "same_owner_clean_reversion" else None
        ),
        provisional_person_id=(
            None if classification == "same_owner_clean_reversion" else destination
        ),
        eligible_person_ids=eligible or (destination,),
        blocked_person_ids=blocked,
        reason_codes=tuple(policy_result.reasons),
    )


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
        return blocked_crm_owner_result(
            person_ids,
            reason="canonical_crm_contact_owner_blocked_by_no_match_lock",
            snapshot_key="blocked_canonical_crm_contact_candidate_ids",
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


def blocked_crm_owner_result(
    person_ids: list[str],
    *,
    reason: str,
    snapshot_key: str,
) -> MatchResult:
    """Build a durable review for CRM owners excluded by hard blockers."""
    candidates = sorted(set(person_ids))
    candidate_values: list[JsonValue] = list(candidates)
    return MatchResult(
        decision=MatchDecision.REVIEW,
        confidence=1.0,
        reasons=[reason],
        engine_type=EngineType.DETERMINISTIC,
        feature_snapshot={
            _CRM_QUARANTINE_KEY: True,
            snapshot_key: candidate_values,
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


def _candidate_person_ids(match_result: MatchResult) -> tuple[str, ...]:
    candidates = set(match_result.review_candidate_person_ids)
    for person_id in (
        match_result.matched_person_id,
        match_result.proposed_person_id,
        *match_result.additional_linked_person_ids,
    ):
        if person_id is not None:
            candidates.add(person_id)
    for snapshot_key in (
        "canonical_crm_contact_candidate_ids",
        "multi_contact_crm_candidate_ids",
        "crm_deal_merge_candidate_ids",
        _MERGE_CANDIDATE_KEY,
    ):
        candidates.update(_snapshot_person_ids(match_result, snapshot_key))
    return tuple(sorted(candidates))


def _blocked_person_ids(match_result: MatchResult) -> tuple[str, ...]:
    blocked: set[str] = set()
    for snapshot_key in (
        "blocked_canonical_crm_contact_candidate_ids",
        "blocked_multi_contact_crm_candidate_ids",
    ):
        blocked.update(_snapshot_person_ids(match_result, snapshot_key))
    return tuple(sorted(blocked))


def _snapshot_person_ids(match_result: MatchResult, key: str) -> set[str]:
    value = match_result.feature_snapshot.get(key)
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


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
