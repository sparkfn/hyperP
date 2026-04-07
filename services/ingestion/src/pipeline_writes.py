"""Graph-write helpers used by the ingest pipeline.

These are free functions (not methods) so they can be unit-tested against a
fake transaction without instantiating the full :class:`IngestPipeline`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from neo4j import ManagedTransaction

from src.graph import queries
from src.models import (
    CandidateResult,
    MatchDecision,
    MatchResult,
    NormalizedAttribute,
    NormalizedIdentifier,
    SourceRecordEnvelope,
)
from src.models import (
    NormalizedAddress as NormalizedAddressModel,
)
from src.pipeline_normalization import fanout_cap_for, is_usable

logger = logging.getLogger(__name__)


# --- Step 3: ensure shared nodes exist ------------------------------------


def upsert_nodes(
    tx: ManagedTransaction,
    identifiers: list[NormalizedIdentifier],
    address: NormalizedAddressModel | None,
) -> None:
    """Step 3: ensure Identifier and Address nodes exist."""
    for ident in identifiers:
        tx.run(
            queries.UPSERT_IDENTIFIER,
            identifier_type=ident.identifier_type,
            normalized_value=ident.normalized_value,
        )
    if address and is_usable(address.quality_flag):
        tx.run(
            queries.UPSERT_ADDRESS,
            country_code=address.country_code,
            postal_code=address.postal_code,
            street_name=address.street_name,
            street_number=address.street_number,
            unit_number=address.unit_number or "",
            building_name=address.building_name,
            city=address.city,
            state_province=address.state_province,
            normalized_full=address.normalized_full,
        )


# --- Step 4: candidate generation -----------------------------------------


def find_candidates(
    tx: ManagedTransaction,
    identifiers: list[NormalizedIdentifier],
    address: NormalizedAddressModel | None,
) -> list[CandidateResult]:
    """Step 4: graph traversal candidate generation with fanout caps."""
    candidates: list[CandidateResult] = []

    for ident in identifiers:
        if not is_usable(ident.quality_flag):
            continue
        if exceeds_fanout_cap(tx, ident):
            continue
        result = tx.run(
            queries.FIND_CANDIDATES_BY_IDENTIFIER,
            identifier_type=ident.identifier_type,
            normalized_value=ident.normalized_value,
        )
        for record in result:
            candidates.append(
                CandidateResult(person_id=record["person_id"], source="identifier")
            )

    if address and is_usable(address.quality_flag):
        result = tx.run(
            queries.FIND_CANDIDATES_BY_ADDRESS,
            country_code=address.country_code,
            postal_code=address.postal_code,
            street_name=address.street_name,
            street_number=address.street_number,
            unit_number=address.unit_number or "",
        )
        for record in result:
            candidates.append(
                CandidateResult(person_id=record["person_id"], source="address")
            )

    return candidates


def exceeds_fanout_cap(
    tx: ManagedTransaction,
    ident: NormalizedIdentifier,
) -> bool:
    """Return True if this identifier hits more persons than its cap allows."""
    cap = fanout_cap_for(ident.identifier_type)
    if cap is None:
        return False
    fanout_result = tx.run(
        queries.CHECK_IDENTIFIER_FANOUT,
        identifier_type=ident.identifier_type,
        normalized_value=ident.normalized_value,
    )
    fanout_rec = fanout_result.single()
    if fanout_rec is None or fanout_rec["fanout"] <= cap:
        return False
    logger.warning(
        "Skipping high-fanout identifier %s=%s (fanout=%d, cap=%d)",
        ident.identifier_type,
        ident.normalized_value,
        fanout_rec["fanout"],
        cap,
    )
    return True


# --- Step 6: person creation ----------------------------------------------


def create_person(tx: ManagedTransaction) -> str:
    """Create a Person + person_created MergeEvent. Returns the new ``person_id``."""
    create_result = tx.run(queries.CREATE_PERSON)
    record = create_result.single()
    assert record is not None, "CREATE_PERSON must return a row"
    person_id: str = record["person_id"]
    tx.run(queries.CREATE_MERGE_EVENT_PERSON_CREATED, person_id=person_id)
    logger.info("Created new person %s", person_id)
    return person_id


# --- Step 7: persist source record + match decision + review case ---------


def persist_source_record(
    tx: ManagedTransaction,
    *,
    envelope: SourceRecordEnvelope,
    identifiers: list[NormalizedIdentifier],
    address: NormalizedAddressModel | None,
    attributes: list[NormalizedAttribute],
    match_result: MatchResult,
    is_new_person: bool,
    ingest_run_id: str | None,
) -> str:
    """Step 7 + 7b: persist SourceRecord and link to IngestRun."""
    normalized_payload = {
        "identifiers": [i.model_dump() for i in identifiers],
        "address": address.model_dump() if address else None,
        "attributes": [a.model_dump() for a in attributes],
    }
    link_status = (
        "linked"
        if match_result.decision == MatchDecision.MERGE or is_new_person
        else "pending_review"
    )
    sr_result = tx.run(
        queries.CREATE_SOURCE_RECORD,
        source_system=envelope.source_system,
        source_record_id=envelope.source_record_id,
        source_record_version=envelope.source_record_version,
        record_type=envelope.record_type.value,
        extraction_confidence=envelope.extraction_confidence,
        extraction_method=envelope.extraction_method,
        conversation_ref=(
            json.dumps(envelope.conversation_ref, default=str)
            if envelope.conversation_ref is not None
            else None
        ),
        link_status=link_status,
        observed_at=envelope.observed_at,
        record_hash=envelope.record_hash,
        raw_payload=json.dumps(envelope.raw_payload, default=str),
        normalized_payload=json.dumps(normalized_payload, default=str),
    )
    sr_record = sr_result.single()
    assert sr_record is not None, "CREATE_SOURCE_RECORD must return a row"
    source_record_pk: str = sr_record["source_record_pk"]

    if ingest_run_id is not None:
        tx.run(
            queries.LINK_SOURCE_RECORD_TO_RUN,
            source_record_pk=source_record_pk,
            ingest_run_id=ingest_run_id,
        )
    return source_record_pk


def persist_match_decision(
    tx: ManagedTransaction,
    match_result: MatchResult,
    source_record_pk: str,
) -> str:
    """Step 7c: persist MatchDecision and wire it to the source record + person."""
    md_result = tx.run(
        queries.CREATE_MATCH_DECISION,
        engine_type=match_result.engine_type.value,
        engine_version=match_result.engine_version,
        decision=match_result.decision.value,
        confidence=match_result.confidence,
        reasons=list(match_result.reasons),
        blocking_conflicts=[],
        feature_snapshot=json.dumps(match_result.feature_snapshot, default=str),
        policy_version="v0.1.0",
    )
    md_record = md_result.single()
    assert md_record is not None, "CREATE_MATCH_DECISION must return a row"
    match_decision_id: str = md_record["match_decision_id"]

    tx.run(
        queries.LINK_MATCH_DECISION_LEFT_SOURCE_RECORD,
        match_decision_id=match_decision_id,
        source_record_pk=source_record_pk,
    )
    if match_result.matched_person_id is not None:
        tx.run(
            queries.LINK_MATCH_DECISION_RIGHT_PERSON,
            match_decision_id=match_decision_id,
            person_id=match_result.matched_person_id,
        )
    return match_decision_id


def create_review_case_if_needed(
    tx: ManagedTransaction,
    match_result: MatchResult,
    match_decision_id: str,
) -> str | None:
    """Step 7d: create a ReviewCase when the engine returns REVIEW."""
    if match_result.decision != MatchDecision.REVIEW:
        return None
    sla_due_at = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    rc_result = tx.run(
        queries.CREATE_REVIEW_CASE,
        match_decision_id=match_decision_id,
        priority=100,
        sla_due_at=sla_due_at,
    )
    rc_record = rc_result.single()
    assert rc_record is not None, "CREATE_REVIEW_CASE must return a row"
    review_case_id: str = rc_record["review_case_id"]
    logger.info(
        "Created ReviewCase %s for MatchDecision %s",
        review_case_id, match_decision_id,
    )
    return review_case_id


# --- Steps 8–11: wire the source record into the Person subgraph ----------


def link_record_to_graph(
    tx: ManagedTransaction,
    *,
    envelope: SourceRecordEnvelope,
    identifiers: list[NormalizedIdentifier],
    address: NormalizedAddressModel | None,
    attributes: list[NormalizedAttribute],
    person_id: str,
    source_record_pk: str,
) -> None:
    """Steps 8–11: wire the source record into the Person subgraph."""
    # 8. SourceRecord -> Person
    tx.run(
        queries.LINK_SOURCE_RECORD_TO_PERSON,
        source_record_pk=source_record_pk,
        person_id=person_id,
    )

    # 9. Person -> Identifier (IDENTIFIED_BY)
    for ident in identifiers:
        if not is_usable(ident.quality_flag):
            continue
        tx.run(
            queries.LINK_PERSON_TO_IDENTIFIER,
            person_id=person_id,
            identifier_type=ident.identifier_type,
            normalized_value=ident.normalized_value,
            is_verified=ident.is_verified,
            verification_method=None,
            quality_flag=ident.quality_flag.value,
            source_system_key=envelope.source_system,
            source_record_pk=source_record_pk,
        )

    # 10. Person -> Address (LIVES_AT)
    if address and is_usable(address.quality_flag):
        tx.run(
            queries.LINK_PERSON_TO_ADDRESS,
            person_id=person_id,
            country_code=address.country_code,
            postal_code=address.postal_code,
            street_name=address.street_name,
            street_number=address.street_number,
            unit_number=address.unit_number or "",
            is_verified=False,
            quality_flag=address.quality_flag.value,
            source_system_key=envelope.source_system,
            source_record_pk=source_record_pk,
        )

    # 11. Person -> SourceRecord HAS_FACT (one per attribute)
    for attr in attributes:
        tx.run(
            queries.CREATE_ATTRIBUTE_FACT,
            person_id=person_id,
            source_record_pk=source_record_pk,
            attribute_name=attr.attribute_name,
            attribute_value=attr.attribute_value,
            # default tier; production looks this up from SourceSystem.
            source_trust_tier="tier_3",
            confidence=1.0,
            quality_flag=attr.quality_flag.value,
            observed_at=envelope.observed_at,
        )


# --- Step 13: auto-merge bookkeeping --------------------------------------


def record_auto_merge_event(
    tx: ManagedTransaction,
    *,
    match_result: MatchResult,
    match_decision_id: str,
    person_id: str,
    source_record_pk: str,
) -> None:
    """Step 13: bookkeeping for an engine-driven MERGE decision.

    Note: this only writes the MergeEvent + TRIGGERED_BY + AFFECTED_RECORD.
    Full person-to-person rewiring (LINKED_TO, IDENTIFIED_BY, LIVES_AT,
    HAS_FACT, MERGED_INTO, path compression) is handled by the API service's
    manual-merge / review-merge flows. During ingestion we are attaching a
    *new* source record to an existing person — there is no prior person to
    absorb.
    """
    survivor_id = match_result.matched_person_id
    me_result = tx.run(
        queries.CREATE_MERGE_EVENT_AUTO_MERGE,
        from_person_id=person_id,
        to_person_id=survivor_id,
        reason="; ".join(match_result.reasons),
    )
    me_record = me_result.single()
    assert me_record is not None, "CREATE_MERGE_EVENT_AUTO_MERGE must return a row"
    merge_event_id: str = me_record["merge_event_id"]

    tx.run(
        queries.LINK_MERGE_EVENT_TRIGGERED_BY,
        merge_event_id=merge_event_id,
        match_decision_id=match_decision_id,
    )
    tx.run(
        queries.LINK_MERGE_EVENT_AFFECTED_RECORD,
        merge_event_id=merge_event_id,
        source_record_pk=source_record_pk,
    )
    logger.info(
        "Merge event %s: TRIGGERED_BY %s, AFFECTED_RECORD %s",
        merge_event_id, match_decision_id, source_record_pk,
    )
