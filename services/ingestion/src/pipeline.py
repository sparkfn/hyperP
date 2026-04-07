"""Ingestion pipeline — full ingest flow in a single explicit Neo4j transaction."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from neo4j import ManagedTransaction

from src.graph import queries
from src.graph.client import Neo4jClient
from src.matching.engine import MatchEngine
from src.models import (
    CandidateResult,
    IngestResult,
    MatchDecision,
    MatchResult,
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

#: Normalizer signature shared by phone/email/name/etc.
NormalizerFn = Callable[[str], tuple[str | None, QualityFlag]]

# Registry: identifier_type -> normalizer.
_IDENTIFIER_NORMALIZERS: dict[str, NormalizerFn] = {
    "phone": normalize_phone,
    "email": normalize_email,
}

# Registry: attribute_name -> normalizer.
_ATTRIBUTE_NORMALIZERS: dict[str, NormalizerFn] = {
    "full_name": normalize_name,
    "preferred_name": normalize_name,
    "legal_name": normalize_name,
}

#: Default cardinality cap for any ``social:*`` identifier type without an
#: explicit entry in :data:`_FANOUT_CAPS`.
_DEFAULT_SOCIAL_FANOUT_CAP = 25

#: Per-identifier-type cardinality caps used during candidate generation.
#: Identifiers whose fanout exceeds the cap are skipped (see CLAUDE.md policy).
_FANOUT_CAPS: dict[str, int] = {
    "phone": 50,
    "email": 100,
    "government_id_hash": 5,
    "device_id": 25,
    "social:facebook": _DEFAULT_SOCIAL_FANOUT_CAP,
    "social:google": _DEFAULT_SOCIAL_FANOUT_CAP,
    "social:apple": _DEFAULT_SOCIAL_FANOUT_CAP,
}


def _fanout_cap_for(identifier_type: str) -> int | None:
    cap = _FANOUT_CAPS.get(identifier_type)
    if cap is None and identifier_type.startswith("social:"):
        return _DEFAULT_SOCIAL_FANOUT_CAP
    return cap

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
        """Orchestrate steps 3–13 of the ingest flow inside one write tx.

        The actual work lives in private helpers below — this method only
        sequences them and threads results through. Each helper corresponds
        to one phase from the architecture doc.
        """
        # 3. Upsert Identifier + Address graph nodes
        self._upsert_nodes(tx, identifiers, address)

        # 4. Candidate generation (graph traversal with cardinality caps)
        candidates = self._find_candidates(tx, identifiers, address)

        # 5. Match engine chain (deterministic → heuristic → LLM-shadow)
        match_result = self._match_engine.evaluate(
            tx, candidates, identifiers, address, attributes,
        )

        # 6. Resolve to a Person — create new or pick from candidates
        person_id, is_new_person = self._resolve_person(
            tx, match_result, candidates,
        )

        # 7. Persist SourceRecord, MatchDecision, optional ReviewCase
        source_record_pk = self._persist_source_record(
            tx,
            envelope=envelope,
            identifiers=identifiers,
            address=address,
            attributes=attributes,
            match_result=match_result,
            is_new_person=is_new_person,
            ingest_run_id=ingest_run_id,
        )
        match_decision_id = self._persist_match_decision(
            tx, match_result, source_record_pk,
        )
        review_case_id = self._create_review_case_if_needed(
            tx, match_result, match_decision_id,
        )

        # 8–11. Link the source record into the Person's subgraph
        self._link_record_to_graph(
            tx,
            envelope=envelope,
            identifiers=identifiers,
            address=address,
            attributes=attributes,
            person_id=person_id,
            source_record_pk=source_record_pk,
        )

        # 12. Recompute golden profile for the touched Person
        compute_golden_profile(tx, person_id)

        # 13. Auto-merge bookkeeping if the engine returned MERGE
        if match_result.decision == MatchDecision.MERGE and not is_new_person:
            self._record_auto_merge_event(
                tx,
                match_result=match_result,
                match_decision_id=match_decision_id,
                person_id=person_id,
                source_record_pk=source_record_pk,
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

    # ------------------------------------------------------------------
    # Phase helpers (steps 3–13)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_usable(quality_flag: QualityFlag) -> bool:
        return quality_flag in (QualityFlag.VALID, QualityFlag.PARTIAL_PARSE)

    def _upsert_nodes(
        self,
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
        if address and self._is_usable(address.quality_flag):
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

    def _find_candidates(
        self,
        tx: ManagedTransaction,
        identifiers: list[NormalizedIdentifier],
        address: NormalizedAddressModel | None,
    ) -> list[CandidateResult]:
        """Step 4: graph traversal candidate generation with fanout caps."""
        candidates: list[CandidateResult] = []

        for ident in identifiers:
            if not self._is_usable(ident.quality_flag):
                continue
            if self._exceeds_fanout_cap(tx, ident):
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

        if address and self._is_usable(address.quality_flag):
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

    @staticmethod
    def _exceeds_fanout_cap(
        tx: ManagedTransaction,
        ident: NormalizedIdentifier,
    ) -> bool:
        """Return True if this identifier hits more persons than its cap allows."""
        cap = _fanout_cap_for(ident.identifier_type)
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

    def _resolve_person(
        self,
        tx: ManagedTransaction,
        match_result: MatchResult,
        candidates: list[CandidateResult],
    ) -> tuple[str, bool]:
        """Step 6: pick or create the Person this record will attach to.

        Returns ``(person_id, is_new_person)``. ``person_id`` is guaranteed
        to be non-None on return.
        """
        is_new_person = match_result.is_new_person
        person_id: str | None = match_result.matched_person_id

        if is_new_person:
            person_id = self._create_person(tx)

        # REVIEW with no engine-picked person: reuse top candidate or create new.
        if match_result.decision == MatchDecision.REVIEW and person_id is None:
            if candidates:
                person_id = candidates[0].person_id
            else:
                person_id = self._create_person(tx)
                is_new_person = True

        # Hard NO_MATCH against a candidate still needs its own Person.
        if person_id is None and not is_new_person:
            person_id = self._create_person(tx)
            is_new_person = True

        assert person_id is not None, "invariant: every record resolves to a person"
        return person_id, is_new_person

    @staticmethod
    def _create_person(tx: ManagedTransaction) -> str:
        """Create a Person + person_created MergeEvent. Returns the new ``person_id``."""
        create_result = tx.run(queries.CREATE_PERSON)
        record = create_result.single()
        assert record is not None, "CREATE_PERSON must return a row"
        person_id: str = record["person_id"]
        tx.run(queries.CREATE_MERGE_EVENT_PERSON_CREATED, person_id=person_id)
        logger.info("Created new person %s", person_id)
        return person_id

    def _persist_source_record(
        self,
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

    @staticmethod
    def _persist_match_decision(
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

    @staticmethod
    def _create_review_case_if_needed(
        tx: ManagedTransaction,
        match_result: MatchResult,
        match_decision_id: str,
    ) -> str | None:
        """Step 7d: create a ReviewCase when the engine returns REVIEW."""
        if match_result.decision != MatchDecision.REVIEW:
            return None
        sla_due_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
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

    def _link_record_to_graph(
        self,
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
            if not self._is_usable(ident.quality_flag):
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
        if address and self._is_usable(address.quality_flag):
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

    @staticmethod
    def _record_auto_merge_event(
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
        HAS_FACT, MERGED_INTO, path compression) is handled by the API
        service's manual-merge / review-merge flows. During ingestion we are
        attaching a *new* source record to an existing person — there is no
        prior person to absorb.
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
