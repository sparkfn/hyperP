"""Graph-write helpers used by the ingest pipeline.

These are free functions (not methods) so they can be unit-tested against a
fake transaction without instantiating the full :class:`IngestPipeline`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from neo4j import ManagedTransaction

from src.graph import queries
from src.graph.crm_deal_count import recompute_person_crm_deal_counts
from src.identifier_scopes import (
    identifier_scope,
    source_instance_for_identifier,
)
from src.identity_link_revisions import identity_link_key
from src.models import (
    CandidateResult,
    ChatDifficulty,
    ChatOutcome,
    ChatPurpose,
    ChatTone,
    JsonValue,
    MatchDecision,
    MatchResult,
    NormalizedAttribute,
    NormalizedIdentifier,
    RecordType,
    SourceRecordEnvelope,
    SourceRecordLifecycleStatus,
)
from src.models import (
    NormalizedAddress as NormalizedAddressModel,
)
from src.pipeline_normalization import fanout_cap_for, is_usable
from src.source_instances import effective_source_instance_id
from src.source_version_keys import encode_source_version_key

logger = logging.getLogger(__name__)

_CHAT_CLASSIFICATION_TYPES: dict[str, type[StrEnum]] = {
    "tone": ChatTone,
    "purpose": ChatPurpose,
    "outcome": ChatOutcome,
    "difficulty": ChatDifficulty,
}


# --- Step 3: ensure shared nodes exist ------------------------------------


def upsert_nodes(
    tx: ManagedTransaction,
    identifiers: list[NormalizedIdentifier],
    addresses: list[NormalizedAddressModel],
) -> None:
    """Step 3: ensure Identifier and Address nodes exist."""
    identifier_rows = [
        {
            "identifier_type": ident.identifier_type,
            "identifier_scope": identifier_scope(ident.identifier_type, ident.source_instance_id),
            "source_instance_id": source_instance_for_identifier(
                ident.identifier_type, ident.source_instance_id
            ),
            "normalized_value": ident.normalized_value,
        }
        for ident in identifiers
    ]
    if identifier_rows:
        tx.run(
            queries.UPSERT_IDENTIFIERS_BATCH,
            identifiers=identifier_rows,
        )
    address_rows = [
        {
            "country_code": address.country_code,
            "postal_code": address.postal_code,
            "street_name": address.street_name,
            "street_number": address.street_number,
            "unit_number": address.unit_number or "",
            "building_name": address.building_name,
            "city": address.city,
            "state_province": address.state_province,
            "normalized_full": address.normalized_full,
        }
        for address in addresses
        if is_usable(address.quality_flag)
    ]
    if address_rows:
        tx.run(
            queries.UPSERT_ADDRESSES_BATCH,
            addresses=address_rows,
        )


# --- Step 4: candidate generation -----------------------------------------


def find_candidates(
    tx: ManagedTransaction,
    identifiers: list[NormalizedIdentifier],
    addresses: list[NormalizedAddressModel],
) -> list[CandidateResult]:
    """Step 4: graph traversal candidate generation with fanout caps."""
    candidates: list[CandidateResult] = []
    seen: set[tuple[str, str]] = set()

    def append_candidate(person_id: str, source: str) -> None:
        key = (person_id, source)
        if key in seen:
            return
        seen.add(key)
        candidates.append(CandidateResult(person_id=person_id, source=source))

    usable_identifiers = [ident for ident in identifiers if is_usable(ident.quality_flag)]
    identifier_rows = [
        {
            "input_index": index,
            "identifier_type": ident.identifier_type,
            "identifier_scope": identifier_scope(ident.identifier_type, ident.source_instance_id),
            "normalized_value": ident.normalized_value,
        }
        for index, ident in enumerate(usable_identifiers)
    ]
    if identifier_rows:
        rows = tx.run(
            queries.FIND_CANDIDATES_BY_IDENTIFIERS_BATCH,
            identifiers=identifier_rows,
        )
        for record in rows:
            index = int(record["input_index"])
            ident = usable_identifiers[index]
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
            for person_id in record["person_ids"]:
                append_candidate(str(person_id), "identifier")

    usable_addresses = [address for address in addresses if is_usable(address.quality_flag)]
    address_rows = [
        {
            "input_index": index,
            "country_code": address.country_code,
            "postal_code": address.postal_code,
            "street_name": address.street_name,
            "street_number": address.street_number,
            "unit_number": address.unit_number or "",
        }
        for index, address in enumerate(usable_addresses)
    ]
    if address_rows:
        for record in tx.run(queries.FIND_CANDIDATES_BY_ADDRESSES_BATCH, addresses=address_rows):
            for person_id in record["person_ids"]:
                append_candidate(str(person_id), "address")

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
        identifier_scope=identifier_scope(ident.identifier_type, ident.source_instance_id),
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


def retire_identity_projections(
    tx: ManagedTransaction,
    source_record_pk: str,
    *,
    person_ids: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Deactivate identity evidence belonging strictly to one source version.

    When the lifecycle fence already proved the complete prior owner set, scope
    projection retirement through those indexed Person nodes. This preserves
    the same source-version semantics without scanning every relationship of
    each projection type in the graph.
    """
    if person_ids:
        rows = tx.run(
            queries.RETIRE_IDENTITY_PROJECTIONS_FOR_PERSONS,
            source_record_pk=source_record_pk,
            person_ids=sorted(set(person_ids)),
        )
    else:
        rows = tx.run(
            queries.RETIRE_IDENTITY_PROJECTIONS,
            source_record_pk=source_record_pk,
        )
    return tuple(sorted({str(row["person_id"]) for row in rows}))


def retire_address_projection(tx: ManagedTransaction, source_record_pk: str) -> None:
    """Deactivate one source version's address assertion while preserving history."""
    tx.run(queries.RETIRE_ADDRESS_PROJECTION, source_record_pk=source_record_pk)


# --- Step 7: persist source record + match decision + review case ---------


def persist_source_record(
    tx: ManagedTransaction,
    *,
    envelope: SourceRecordEnvelope,
    identifiers: list[NormalizedIdentifier],
    addresses: list[NormalizedAddressModel],
    attributes: list[NormalizedAttribute],
    match_result: MatchResult,
    is_new_person: bool,
    ingest_run_id: str | None,
    lifecycle_status: SourceRecordLifecycleStatus,
    expected_active_source_record_pk: str | None,
    activation_blueprint: dict[str, JsonValue] | None = None,
    link_status: str | None = None,
) -> str:
    """Step 7 + 7b: persist SourceRecord and link to IngestRun."""
    normalized = build_normalized_source_payload(
        envelope=envelope,
        identifiers=identifiers,
        addresses=addresses,
        attributes=attributes,
        activation_blueprint=activation_blueprint,
    )
    resolved_link_status = (
        link_status
        if link_status is not None
        else "linked"
        if match_result.decision == MatchDecision.MERGE or is_new_person
        else "pending_review"
    )
    conv_ref = (
        json.dumps(envelope.conversation_ref, default=str)
        if envelope.conversation_ref is not None
        else None
    )
    source_record_version = envelope.source_record_version
    assert source_record_version is not None, "lifecycle planning must allocate a version"
    sr_result = tx.run(
        queries.CREATE_SOURCE_RECORD,
        source_system=envelope.source_system,
        source_instance_id=effective_source_instance_id(envelope.source_instance_id),
        source_record_id=envelope.source_record_id,
        entity_key=envelope.entity_key,
        source_record_version=source_record_version,
        source_version_key=encode_source_version_key(
            envelope.source_system,
            envelope.source_record_id,
            source_record_version,
            source_instance_id=effective_source_instance_id(envelope.source_instance_id),
        ),
        expected_active_source_record_pk=expected_active_source_record_pk,
        lifecycle_status=lifecycle_status.value,
        is_latest=lifecycle_status is SourceRecordLifecycleStatus.ACTIVE,
        record_type=envelope.record_type.value,
        extraction_confidence=envelope.extraction_confidence,
        extraction_method=envelope.extraction_method,
        conversation_ref=conv_ref,
        parent_source_system=(
            envelope.parent_ref.parent_source_system if envelope.parent_ref is not None else None
        ),
        parent_source_instance_id=(
            effective_source_instance_id(envelope.parent_ref.parent_source_instance_id)
            if envelope.parent_ref is not None
            else None
        ),
        parent_source_record_id=(
            envelope.parent_ref.parent_source_record_id if envelope.parent_ref is not None else None
        ),
        parent_record_type=(
            envelope.parent_ref.parent_record_type.value
            if envelope.parent_ref is not None
            else None
        ),
        link_status=resolved_link_status,
        observed_at=envelope.observed_at,
        record_hash=envelope.record_hash,
        raw_payload=json.dumps(envelope.raw_payload, default=str),
        normalized_payload=json.dumps(normalized, default=str),
        crm_deal_stage_id=_crm_deal_stage_id(envelope),
        source_entity_type=envelope.source_entity_type,
        source_entity_id=envelope.source_entity_id,
        identity_policy_version=envelope.identity_policy_version,
        identity_link_key=(
            identity_link_key(
                envelope.source_system,
                effective_source_instance_id(envelope.source_instance_id),
                envelope.source_entity_type,
                envelope.source_entity_id,
                envelope.identity_policy_version,
            )
            if envelope.source_entity_type is not None
            and envelope.source_entity_id is not None
            and envelope.identity_policy_version is not None
            else None
        ),
    )
    sr_record = sr_result.single()
    assert sr_record is not None, "CREATE_SOURCE_RECORD must return a row"
    pk: str = sr_record["source_record_pk"]
    if ingest_run_id is not None:
        tx.run(queries.LINK_SOURCE_RECORD_TO_RUN, source_record_pk=pk, ingest_run_id=ingest_run_id)
    return pk


def _crm_deal_stage_id(envelope: SourceRecordEnvelope) -> str | None:
    """Return the canonical stage ID projected from a CRM-deal payload.

    ``raw_payload`` is intentionally JSON-serialized before it reaches Neo4j,
    so readers cannot safely dereference it as a Cypher map. Keep this narrow,
    immutable projection alongside the persisted payload for CRM metrics.
    """
    if envelope.record_type is not RecordType.CRM_DEAL:
        return None
    stage_id = envelope.raw_payload.get("stage_id")
    if not isinstance(stage_id, str):
        stage_id = envelope.raw_payload.get("STAGE_ID")
    return stage_id if isinstance(stage_id, str) and stage_id else None


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
        review_candidate_person_ids=match_result.review_candidate_person_ids,
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
    right_person_id = match_result.proposed_person_id or match_result.matched_person_id
    if right_person_id is not None:
        tx.run(
            queries.LINK_MATCH_DECISION_RIGHT_PERSON,
            match_decision_id=match_decision_id,
            person_id=right_person_id,
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
        review_case_id,
        match_decision_id,
    )
    return review_case_id


def build_normalized_source_payload(
    *,
    envelope: SourceRecordEnvelope,
    identifiers: list[NormalizedIdentifier],
    addresses: list[NormalizedAddressModel],
    attributes: list[NormalizedAttribute],
    activation_blueprint: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """Build the canonical normalized payload stored on a SourceRecord."""
    normalized: dict[str, JsonValue] = {
        "identifiers": [i.model_dump() for i in identifiers],
        "address": addresses[0].model_dump() if addresses else None,
        "addresses": [address.model_dump() for address in addresses],
        "attributes": [a.model_dump() for a in attributes],
    }
    if activation_blueprint is not None:
        normalized.update(activation_blueprint)
    summary = envelope.raw_payload.get("summary")
    if (
        envelope.record_type.value == "conversation"
        and isinstance(summary, str)
        and summary.strip()
    ):
        normalized["summary"] = summary.strip()
    if envelope.record_type.value == "conversation":
        for key, enum_type in _CHAT_CLASSIFICATION_TYPES.items():
            value = envelope.raw_payload.get(key)
            if not isinstance(value, str):
                continue
            try:
                normalized[key] = enum_type(value).value
            except ValueError:
                continue
        for key in ("customer_sentiment", "chat_members", "inquiries"):
            value = envelope.raw_payload.get(key)
            if value is not None:
                normalized[key] = value
    return normalized


def _attribute_str(envelope: SourceRecordEnvelope, key: str) -> str:
    value = envelope.attributes.get(key)
    return value if isinstance(value, str) else ""


def _attribute_bool(envelope: SourceRecordEnvelope, key: str) -> bool:
    value = envelope.attributes.get(key)
    return value if isinstance(value, bool) else False


def link_source_record_to_address(
    tx: ManagedTransaction,
    *,
    envelope: SourceRecordEnvelope,
    source_record_pk: str,
) -> None:
    """Persist an address-only source record onto the shared Address graph."""
    postal_code = _attribute_str(envelope, "postal_code")
    country_code = _attribute_str(envelope, "country_code") or "SG"
    block_no = _attribute_str(envelope, "block_no")
    street_name = _attribute_str(envelope, "street_name")
    town_name = _attribute_str(envelope, "town_name")
    normalized_full = " ".join(
        part for part in [block_no, street_name, postal_code, country_code] if part
    )

    tx.run(
        queries.UPSERT_ADDRESS,
        country_code=country_code,
        postal_code=postal_code,
        street_name=street_name,
        street_number=block_no,
        unit_number="",
        building_name=None,
        city=town_name,
        state_province=None,
        normalized_full=normalized_full,
    )
    tx.run(
        queries.LINK_SOURCE_RECORD_TO_ADDRESS,
        source_record_pk=source_record_pk,
        country_code=country_code,
        postal_code=postal_code,
        street_name=street_name,
        street_number=block_no,
        unit_number="",
        source_system_key=envelope.source_system,
        flat_type=_attribute_str(envelope, "flat_type"),
        is_active=_attribute_bool(envelope, "is_active"),
    )


# --- Steps 8–11: wire the source record into the Person subgraph ----------


def _link_identifiers(
    tx: ManagedTransaction,
    identifiers: list[NormalizedIdentifier],
    person_id: str,
    source_system_key: str,
    source_record_pk: str,
) -> None:
    """Step 9: create IDENTIFIED_BY edges for usable identifiers."""
    for ident in identifiers:
        if not is_usable(ident.quality_flag):
            continue
        tx.run(
            queries.LINK_PERSON_TO_IDENTIFIER,
            person_id=person_id,
            identifier_type=ident.identifier_type,
            identifier_scope=identifier_scope(ident.identifier_type, ident.source_instance_id),
            normalized_value=ident.normalized_value,
            is_verified=ident.is_verified,
            verification_method=None,
            quality_flag=ident.quality_flag.value,
            source_system_key=source_system_key,
            source_record_pk=source_record_pk,
        )


def _link_address(
    tx: ManagedTransaction,
    address: NormalizedAddressModel,
    person_id: str,
    source_system_key: str,
    source_record_pk: str,
) -> None:
    """Step 10: create LIVES_AT edge for a usable address."""
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
        source_system_key=source_system_key,
        source_record_pk=source_record_pk,
    )
    tx.run(
        queries.LINK_SOURCE_RECORD_TO_ADDRESS,
        source_record_pk=source_record_pk,
        country_code=address.country_code,
        postal_code=address.postal_code,
        street_name=address.street_name,
        street_number=address.street_number,
        unit_number=address.unit_number or "",
        source_system_key=source_system_key,
        flat_type=None,
        is_active=True,
    )


def link_record_to_graph(
    tx: ManagedTransaction,
    *,
    envelope: SourceRecordEnvelope,
    identifiers: list[NormalizedIdentifier],
    addresses: list[NormalizedAddressModel],
    attributes: list[NormalizedAttribute],
    person_id: str,
    source_record_pk: str,
    attach_evidence: bool = True,
) -> None:
    """Steps 8–11: wire the source record into the Person subgraph.

    The SourceRecord → Person provenance edge (``LINKED_TO``) is always created.
    When ``attach_evidence`` is ``False`` the identifier / address / fact edges
    are *not* written onto ``person_id`` — used for a provisional REVIEW-band
    match so an unconfirmed record does not commingle into an existing
    candidate's subgraph (and golden profile) before a human approves the merge.
    """
    tx.run(
        queries.LINK_SOURCE_RECORD_TO_PERSON,
        source_record_pk=source_record_pk,
        person_id=person_id,
    )
    if envelope.record_type is RecordType.CRM_DEAL:
        recompute_person_crm_deal_counts(tx, [person_id])
    if not attach_evidence:
        return
    _link_identifiers(tx, identifiers, person_id, envelope.source_system, source_record_pk)
    for address in addresses:
        if is_usable(address.quality_flag):
            _link_address(tx, address, person_id, envelope.source_system, source_record_pk)

    for attr in attributes:
        tx.run(
            queries.CREATE_ATTRIBUTE_FACT,
            person_id=person_id,
            source_record_pk=source_record_pk,
            attribute_name=attr.attribute_name,
            attribute_value=attr.attribute_value,
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
        merge_event_id,
        match_decision_id,
        source_record_pk,
    )
