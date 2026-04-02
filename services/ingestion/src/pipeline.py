"""Ingestion pipeline — full ingest flow in a single explicit Neo4j transaction."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from neo4j import ManagedTransaction

from src.graph import queries
from src.graph.client import Neo4jClient
from src.matching.engine import MatchEngine
from src.models import (
    CandidateResult,
    IngestResult,
    MatchDecision,
    NormalizedAddress as NormalizedAddressModel,
    NormalizedAttribute,
    NormalizedIdentifier,
    QualityFlag,
    SourceRecordEnvelope,
)
from src.golden_profile import compute_golden_profile
from src.normalizers.address import normalize_address
from src.normalizers.email import normalize_email
from src.normalizers.name import normalize_name
from src.normalizers.phone import normalize_phone

logger = logging.getLogger(__name__)

# Registry: identifier_type -> normalizer function returning (value|None, QualityFlag)
_IDENTIFIER_NORMALIZERS: dict[str, Any] = {
    "phone": normalize_phone,
    "email": normalize_email,
}

# Registry: attribute_name -> normalizer function returning (value|None, QualityFlag)
_ATTRIBUTE_NORMALIZERS: dict[str, Any] = {
    "full_name": normalize_name,
    "preferred_name": normalize_name,
    "legal_name": normalize_name,
}

# Attributes handled outside the normalizer registry
_SKIP_ATTRIBUTES = frozenset({"address"})


def _passthrough_normalize(raw: str) -> tuple[str | None, QualityFlag]:
    """Fallback normalizer: strip whitespace, return valid if non-empty."""
    value = raw.strip()
    return (value, QualityFlag.VALID) if value else (None, QualityFlag.INVALID_FORMAT)


class IngestPipeline:
    """Processes a single source record through the full ingestion flow.

    All graph mutations for one record run inside a single explicit
    ``session.execute_write`` transaction.
    """

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client
        self._match_engine = MatchEngine()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def ingest(
        self,
        envelope: SourceRecordEnvelope,
        ingest_run_id: str | None = None,
    ) -> IngestResult:
        """Ingest a single source record.  Returns an ``IngestResult``."""

        # Step 1: Idempotency check (read-only, outside write tx)
        existing_pk = self._check_idempotency(envelope)
        if existing_pk is not None:
            logger.info(
                "Duplicate source record %s (hash=%s) — skipping",
                envelope.source_record_id,
                envelope.record_hash,
            )
            return IngestResult(
                source_record_id=envelope.source_record_id,
                source_record_pk=existing_pk,
                skipped_duplicate=True,
            )

        # Step 2: Normalize identifiers, address, attributes
        identifiers = self._normalize_identifiers(envelope)
        address = self._normalize_address(envelope)
        attributes = self._normalize_attributes(envelope)

        # Steps 3-11 run inside a single write transaction
        def _work(tx: ManagedTransaction) -> IngestResult:
            return self._execute_ingest(
                tx, envelope, identifiers, address, attributes,
                ingest_run_id=ingest_run_id,
            )

        with self._client.session() as session:
            return session.execute_write(_work)

    # ------------------------------------------------------------------
    # Idempotency check (read)
    # ------------------------------------------------------------------

    def _check_idempotency(self, envelope: SourceRecordEnvelope) -> str | None:
        """Return existing ``source_record_pk`` if a duplicate exists, else None."""

        def _read(tx: ManagedTransaction) -> str | None:
            result = tx.run(
                queries.CHECK_SOURCE_RECORD_EXISTS,
                source_system=envelope.source_system,
                source_record_id=envelope.source_record_id,
                record_hash=envelope.record_hash,
            )
            record = result.single()
            return record["source_record_pk"] if record else None

        return self._client.execute_read(_read)

    # ------------------------------------------------------------------
    # Normalization helpers
    # ------------------------------------------------------------------

    def _normalize_identifiers(
        self, envelope: SourceRecordEnvelope,
    ) -> list[NormalizedIdentifier]:
        results: list[NormalizedIdentifier] = []
        for raw_id in envelope.identifiers:
            id_type = raw_id.type.lower().strip()
            normalizer = _IDENTIFIER_NORMALIZERS.get(id_type, _passthrough_normalize)
            normalized, flag = normalizer(raw_id.value)
            if normalized:
                results.append(NormalizedIdentifier(
                    identifier_type=id_type,
                    normalized_value=normalized,
                    is_verified=raw_id.is_verified,
                    quality_flag=flag,
                ))
            else:
                logger.warning(
                    "%s normalization failed for %s: %s",
                    id_type, raw_id.value, flag,
                )
        return results

    def _normalize_address(
        self, envelope: SourceRecordEnvelope,
    ) -> NormalizedAddressModel | None:
        raw_address = envelope.attributes.get("address")
        if not raw_address or not isinstance(raw_address, str):
            return None

        parsed, flag = normalize_address(raw_address)
        if parsed is None:
            logger.warning("Address normalization failed for record %s: %s",
                           envelope.source_record_id, flag)
            return None

        return NormalizedAddressModel(
            unit_number=parsed.unit_number,
            street_number=parsed.street_number,
            street_name=parsed.street_name,
            building_name=parsed.building_name,
            city=parsed.city,
            state_province=parsed.state_province,
            postal_code=parsed.postal_code,
            country_code=parsed.country_code,
            normalized_full=parsed.normalized_full,
            quality_flag=flag,
        )

    def _normalize_attributes(
        self, envelope: SourceRecordEnvelope,
    ) -> list[NormalizedAttribute]:
        results: list[NormalizedAttribute] = []
        for attr_name, raw_value in envelope.attributes.items():
            if attr_name in _SKIP_ATTRIBUTES:
                continue
            if not isinstance(raw_value, str):
                raw_value = str(raw_value)
            normalizer = _ATTRIBUTE_NORMALIZERS.get(attr_name, _passthrough_normalize)
            normalized, flag = normalizer(raw_value)
            if normalized:
                results.append(NormalizedAttribute(
                    attribute_name=attr_name,
                    attribute_value=normalized,
                    quality_flag=flag,
                ))
        return results

    # ------------------------------------------------------------------
    # Main write transaction
    # ------------------------------------------------------------------

    def _execute_ingest(
        self,
        tx: ManagedTransaction,
        envelope: SourceRecordEnvelope,
        identifiers: list[NormalizedIdentifier],
        address: NormalizedAddressModel | None,
        attributes: list[NormalizedAttribute],
        ingest_run_id: str | None = None,
    ) -> IngestResult:
        # Step 3: Upsert Identifier and Address nodes
        for ident in identifiers:
            tx.run(
                queries.UPSERT_IDENTIFIER,
                identifier_type=ident.identifier_type,
                normalized_value=ident.normalized_value,
            )

        if address and address.quality_flag in (QualityFlag.VALID, QualityFlag.PARTIAL_PARSE):
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

        # Step 4: Find candidates via graph traversal (with cardinality caps)
        # Default fanout caps per identifier type (from policy doc)
        fanout_caps: dict[str, int] = {
            "phone": 50,
            "email": 100,
            "government_id_hash": 5,
        }

        candidates: list[CandidateResult] = []
        for ident in identifiers:
            if ident.quality_flag not in (QualityFlag.VALID, QualityFlag.PARTIAL_PARSE):
                continue

            # Check cardinality cap — skip high-fanout identifiers
            cap = fanout_caps.get(ident.identifier_type)
            if cap is not None:
                fanout_result = tx.run(
                    queries.CHECK_IDENTIFIER_FANOUT,
                    identifier_type=ident.identifier_type,
                    normalized_value=ident.normalized_value,
                )
                fanout_rec = fanout_result.single()
                if fanout_rec and fanout_rec["fanout"] > cap:
                    logger.warning(
                        "Skipping high-fanout identifier %s=%s (fanout=%d, cap=%d)",
                        ident.identifier_type, ident.normalized_value,
                        fanout_rec["fanout"], cap,
                    )
                    continue

            result = tx.run(
                queries.FIND_CANDIDATES_BY_IDENTIFIER,
                identifier_type=ident.identifier_type,
                normalized_value=ident.normalized_value,
            )
            for record in result:
                candidates.append(CandidateResult(
                    person_id=record["person_id"],
                    source="identifier",
                ))

        if address and address.quality_flag in (QualityFlag.VALID, QualityFlag.PARTIAL_PARSE):
            result = tx.run(
                queries.FIND_CANDIDATES_BY_ADDRESS,
                country_code=address.country_code,
                postal_code=address.postal_code,
                street_name=address.street_name,
                street_number=address.street_number,
                unit_number=address.unit_number or "",
            )
            for record in result:
                candidates.append(CandidateResult(
                    person_id=record["person_id"],
                    source="address",
                ))

        # Step 5: Evaluate match engine chain
        match_result = self._match_engine.evaluate(
            tx, candidates, identifiers, address, attributes,
        )

        is_new_person = match_result.is_new_person
        person_id: str | None = match_result.matched_person_id

        # Step 6: Create Person if new (+ MergeEvent person_created)
        if is_new_person:
            create_result = tx.run(queries.CREATE_PERSON)
            person_record = create_result.single()
            person_id = person_record["person_id"]

            tx.run(
                queries.CREATE_MERGE_EVENT_PERSON_CREATED,
                person_id=person_id,
            )
            logger.info("Created new person %s", person_id)

        # For review decisions, pick the best candidate if we have one
        if match_result.decision == MatchDecision.REVIEW and person_id is None:
            if candidates:
                person_id = candidates[0].person_id
            else:
                # Fallback: create new person for review case
                create_result = tx.run(queries.CREATE_PERSON)
                person_record = create_result.single()
                person_id = person_record["person_id"]
                is_new_person = True
                tx.run(
                    queries.CREATE_MERGE_EVENT_PERSON_CREATED,
                    person_id=person_id,
                )

        # For merge decisions, person_id is already set by the match engine
        # For no_match with is_new_person=False, this is a hard no-match
        # against a candidate — we still need a person for the source record
        if person_id is None and not is_new_person:
            create_result = tx.run(queries.CREATE_PERSON)
            person_record = create_result.single()
            person_id = person_record["person_id"]
            is_new_person = True
            tx.run(
                queries.CREATE_MERGE_EVENT_PERSON_CREATED,
                person_id=person_id,
            )

        assert person_id is not None  # invariant: we always have a person_id here

        # Build normalized_payload for storage
        normalized_payload = {
            "identifiers": [i.model_dump() for i in identifiers],
            "address": address.model_dump() if address else None,
            "attributes": [a.model_dump() for a in attributes],
        }

        # Step 7: Create SourceRecord + FROM_SOURCE
        sr_result = tx.run(
            queries.CREATE_SOURCE_RECORD,
            source_system=envelope.source_system,
            source_record_id=envelope.source_record_id,
            source_record_version=envelope.source_record_version,
            link_status="linked" if match_result.decision == MatchDecision.MERGE or is_new_person
                        else "pending_review",
            observed_at=envelope.observed_at,
            record_hash=envelope.record_hash,
            raw_payload=json.dumps(envelope.raw_payload, default=str),
            normalized_payload=json.dumps(normalized_payload, default=str),
        )
        sr_record = sr_result.single()
        source_record_pk = sr_record["source_record_pk"]

        # Step 7b: Link SourceRecord to IngestRun
        if ingest_run_id is not None:
            tx.run(
                queries.LINK_SOURCE_RECORD_TO_RUN,
                source_record_pk=source_record_pk,
                ingest_run_id=ingest_run_id,
            )

        # Step 7c: Persist MatchDecision node
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
        match_decision_id = md_record["match_decision_id"]

        # Link MatchDecision to the SourceRecord (left side)
        tx.run(
            queries.LINK_MATCH_DECISION_LEFT_SOURCE_RECORD,
            match_decision_id=match_decision_id,
            source_record_pk=source_record_pk,
        )

        # Link MatchDecision to the candidate Person (right side), if one exists
        if match_result.matched_person_id is not None:
            tx.run(
                queries.LINK_MATCH_DECISION_RIGHT_PERSON,
                match_decision_id=match_decision_id,
                person_id=match_result.matched_person_id,
            )

        # Step 7d: Create ReviewCase if decision is REVIEW
        review_case_id: str | None = None
        if match_result.decision == MatchDecision.REVIEW:
            sla_due_at = (
                datetime.now(timezone.utc) + timedelta(days=7)
            ).isoformat()
            rc_result = tx.run(
                queries.CREATE_REVIEW_CASE,
                match_decision_id=match_decision_id,
                priority=100,
                sla_due_at=sla_due_at,
            )
            rc_record = rc_result.single()
            review_case_id = rc_record["review_case_id"]
            logger.info("Created ReviewCase %s for MatchDecision %s",
                        review_case_id, match_decision_id)

        # Step 8: LINKED_TO (SourceRecord -> Person)
        tx.run(
            queries.LINK_SOURCE_RECORD_TO_PERSON,
            source_record_pk=source_record_pk,
            person_id=person_id,
        )

        # Step 9: IDENTIFIED_BY relationships (Person -> Identifier)
        for ident in identifiers:
            if ident.quality_flag not in (QualityFlag.VALID, QualityFlag.PARTIAL_PARSE):
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

        # Step 10: LIVES_AT relationship (Person -> Address)
        if address and address.quality_flag in (QualityFlag.VALID, QualityFlag.PARTIAL_PARSE):
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

        # Step 11: HAS_FACT relationships (Person -> SourceRecord)
        for attr in attributes:
            tx.run(
                queries.CREATE_ATTRIBUTE_FACT,
                person_id=person_id,
                source_record_pk=source_record_pk,
                attribute_name=attr.attribute_name,
                attribute_value=attr.attribute_value,
                source_trust_tier="tier_3",  # default; looked up from SourceSystem in production
                confidence=1.0,
                quality_flag=attr.quality_flag.value,
                observed_at=envelope.observed_at,
            )

        # Step 12: Compute golden profile (synchronous within transaction)
        compute_golden_profile(tx, person_id)

        # Step 13: Full merge logic for auto-merge decisions
        if match_result.decision == MatchDecision.MERGE and not is_new_person:
            survivor_id = match_result.matched_person_id
            # person_id is the survivor here (record was linked to it above)
            # But actually: if the engine said "merge into matched_person_id",
            # and we set person_id = matched_person_id earlier, there's no
            # absorbed person yet — the incoming record just gets linked to
            # the existing person. A real person-to-person merge only happens
            # if there was a PRIOR person that needs to be absorbed.
            #
            # For now, we just record the merge event + TRIGGERED_BY.
            # Full person-to-person merge (rewire LINKED_TO, IDENTIFIED_BY,
            # LIVES_AT, HAS_FACT, MERGED_INTO, path compression) is handled
            # by the API service's manual-merge and review-merge flows, since
            # during ingestion we're linking a NEW source record to an existing
            # person — not merging two existing persons.
            me_result = tx.run(
                queries.CREATE_MERGE_EVENT_AUTO_MERGE,
                from_person_id=person_id,
                to_person_id=survivor_id,
                reason="; ".join(match_result.reasons),
            )
            me_record = me_result.single()
            merge_event_id = me_record["merge_event_id"]

            # TRIGGERED_BY: link MergeEvent to the MatchDecision
            tx.run(
                queries.LINK_MERGE_EVENT_TRIGGERED_BY,
                merge_event_id=merge_event_id,
                match_decision_id=match_decision_id,
            )

            # AFFECTED_RECORD: link MergeEvent to the source record
            tx.run(
                queries.LINK_MERGE_EVENT_AFFECTED_RECORD,
                merge_event_id=merge_event_id,
                source_record_pk=source_record_pk,
            )

            logger.info(
                "Merge event %s: TRIGGERED_BY %s, AFFECTED_RECORD %s",
                merge_event_id, match_decision_id, source_record_pk,
            )

        logger.info(
            "Ingested record %s -> person %s (new=%s, decision=%s, candidates=%d)",
            envelope.source_record_id,
            person_id,
            is_new_person,
            match_result.decision.value,
            len(candidates),
        )

        return IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=source_record_pk,
            person_id=person_id,
            is_new_person=is_new_person,
            candidate_count=len(candidates),
            match_decision=match_result.decision,
            ingest_run_id=ingest_run_id,
            match_decision_id=match_decision_id,
            review_case_id=review_case_id,
        )
