"""Ingestion pipeline — full ingest flow in a single explicit Neo4j transaction.

The pipeline orchestrates the per-record steps from the architecture doc.
Heavy lifting lives in two sibling modules:

- :mod:`src.pipeline_normalization` — identifier / address / attribute
  normalization, registries, and fanout caps.
- :mod:`src.pipeline_writes` — Cypher writes (upserts, candidate generation,
  source-record persistence, match-decision persistence, review-case
  creation, person-subgraph linking, auto-merge bookkeeping).

This module keeps just the orchestrator class and the read-side idempotency
check, so the per-record flow is readable end-to-end in one screen.
"""

from __future__ import annotations

import logging

from neo4j import ManagedTransaction

from src.exclusions import ExclusionContext, is_excluded_machine_unit_observation
from src.golden_profile import compute_golden_profile
from src.graph import queries
from src.graph.client import Neo4jClient
from src.machine_unit_extraction import observations_from_chat_inquiries
from src.machine_units import (
    normalize_lta_tag,
    normalize_machine_product,
    normalize_serial_number,
)
from src.matching.engine import MatchEngine
from src.models import (
    CandidateResult,
    IngestResult,
    MatchDecision,
    MatchResult,
    NormalizedAttribute,
    NormalizedIdentifier,
    RecordType,
    SourceRecordEnvelope,
)
from src.models import (
    NormalizedAddress as NormalizedAddressModel,
)
from src.pipeline_bankruptcy import materialize_bankruptcy_case
from src.pipeline_normalization import (
    normalize_envelope_address,
    normalize_envelope_attributes,
    normalize_envelope_identifiers,
)
from src.pipeline_writes import (
    create_person,
    create_review_case_if_needed,
    find_candidates,
    link_record_to_graph,
    persist_match_decision,
    persist_source_record,
    record_auto_merge_event,
    upsert_nodes,
)

logger = logging.getLogger(__name__)


class IngestPipeline:
    """Processes a single source record through the full ingestion flow.

    All graph mutations for one record run inside a single explicit
    ``session.execute_write`` transaction.
    """

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client
        self._match_engine = MatchEngine()

    def ingest(
        self,
        envelope: SourceRecordEnvelope,
        ingest_run_id: str | None = None,
        exclusion_context: ExclusionContext | None = None,
    ) -> IngestResult:
        """Ingest a single source record.  Returns an ``IngestResult``."""

        # Step 1: Idempotency check (read-only, outside write tx)
        previous_pk, latest_hash, next_version = self._latest_source_record(envelope)
        if previous_pk is not None and latest_hash == envelope.record_hash:
            logger.info(
                "Duplicate source record %s (hash=%s) — skipping",
                envelope.source_record_id,
                envelope.record_hash,
            )
            return IngestResult(
                source_record_id=envelope.source_record_id,
                source_record_pk=previous_pk,
                skipped_duplicate=True,
            )
        envelope.source_record_version = str(next_version)

        # Step 2: Normalize identifiers, address, attributes
        identifiers = normalize_envelope_identifiers(envelope)
        address = normalize_envelope_address(envelope)
        attributes = normalize_envelope_attributes(envelope)
        active_exclusion_context = (
            exclusion_context if exclusion_context is not None else ExclusionContext()
        )

        # Steps 3-13 run inside a single write transaction
        def _work(tx: ManagedTransaction) -> IngestResult:
            return self._execute_ingest(
                tx,
                envelope,
                identifiers,
                address,
                attributes,
                ingest_run_id=ingest_run_id,
                previous_source_record_pk=previous_pk,
                exclusion_context=active_exclusion_context,
            )

        with self._client.session() as session:
            return session.execute_write(_work)

    def _latest_source_record(
        self,
        envelope: SourceRecordEnvelope,
    ) -> tuple[str | None, str | None, int]:
        """Return latest source record pk, hash, and next version for this source ID."""

        def _read(tx: ManagedTransaction) -> tuple[str | None, str | None, int]:
            result = tx.run(
                queries.GET_LATEST_SOURCE_RECORD,
                source_system=envelope.source_system,
                source_record_id=envelope.source_record_id,
            )
            record = result.single()
            if record is None:
                return None, None, 1
            version = int(record.get("source_record_version", 1))
            return str(record["source_record_pk"]), str(record.get("record_hash", "")), version + 1

        return self._client.execute_read(_read)

    def _check_idempotency(self, envelope: SourceRecordEnvelope) -> str | None:
        """Return latest source_record_pk if this exact latest version already exists."""
        latest_pk, latest_hash, _next_version = self._latest_source_record(envelope)
        if latest_pk is not None and latest_hash == envelope.record_hash:
            return latest_pk
        return None

    def _execute_ingest(
        self,
        tx: ManagedTransaction,
        envelope: SourceRecordEnvelope,
        identifiers: list[NormalizedIdentifier],
        address: NormalizedAddressModel | None,
        attributes: list[NormalizedAttribute],
        ingest_run_id: str | None = None,
        previous_source_record_pk: str | None = None,
        exclusion_context: ExclusionContext | None = None,
    ) -> IngestResult:
        """Orchestrate steps 3–13 of the ingest flow inside one write tx."""
        active_exclusion_context = (
            exclusion_context if exclusion_context is not None else ExclusionContext()
        )
        upsert_nodes(tx, identifiers, address)
        candidates = find_candidates(tx, identifiers, address)
        match_result = self._match_engine.evaluate(
            tx,
            candidates,
            identifiers,
            address,
            attributes,
            record_type=envelope.record_type,
        )
        person_id, is_new_person = self._resolve_person(tx, match_result, candidates)
        source_record_pk = persist_source_record(
            tx,
            envelope=envelope,
            identifiers=identifiers,
            address=address,
            attributes=attributes,
            match_result=match_result,
            is_new_person=is_new_person,
            ingest_run_id=ingest_run_id,
        )
        if previous_source_record_pk is not None:
            tx.run(
                queries.SUPERSEDE_SOURCE_RECORD,
                old_source_record_pk=previous_source_record_pk,
                new_source_record_pk=source_record_pk,
            )
        self._write_chat_machine_unit_observations(
            tx,
            envelope=envelope,
            source_record_pk=source_record_pk,
            exclusion_context=active_exclusion_context,
        )
        match_decision_id = persist_match_decision(tx, match_result, source_record_pk)
        review_case_id = create_review_case_if_needed(tx, match_result, match_decision_id)
        link_record_to_graph(
            tx,
            envelope=envelope,
            identifiers=identifiers,
            address=address,
            attributes=attributes,
            person_id=person_id,
            source_record_pk=source_record_pk,
        )
        materialize_bankruptcy_case(
            tx,
            envelope=envelope,
            person_id=person_id,
            source_record_pk=source_record_pk,
        )
        compute_golden_profile(tx, person_id)
        if match_result.decision == MatchDecision.MERGE and not is_new_person:
            record_auto_merge_event(
                tx,
                match_result=match_result,
                match_decision_id=match_decision_id,
                person_id=person_id,
                source_record_pk=source_record_pk,
            )
        logger.info(
            "Ingested %s -> person %s (new=%s, decision=%s, candidates=%d)",
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

    def _write_chat_machine_unit_observations(
        self,
        tx: ManagedTransaction,
        *,
        envelope: SourceRecordEnvelope,
        source_record_pk: str,
        exclusion_context: ExclusionContext,
    ) -> None:
        if envelope.record_type != RecordType.CONVERSATION:
            return
        inquiries_raw = envelope.raw_payload.get("inquiries")
        if not isinstance(inquiries_raw, list):
            return
        observations = observations_from_chat_inquiries(
            source_system_key=envelope.source_system,
            source_record_id=envelope.source_record_id,
            observed_at=envelope.observed_at,
            inquiries=inquiries_raw,
        )
        for observation in observations:
            if is_excluded_machine_unit_observation(observation, exclusion_context):
                continue
            row = tx.run(
                queries.RESOLVE_EXISTING_MACHINE_UNIT_FOR_CHAT,
                normalized_machine_product=normalize_machine_product(observation.machine_product),
                normalized_lta_tag=normalize_lta_tag(observation.lta_tag),
                normalized_serial_number=normalize_serial_number(observation.serial_number),
            ).single()
            if row is None:
                continue
            machine_unit_ids = [str(item) for item in row["machine_unit_ids"]]
            if len(machine_unit_ids) != 1:
                continue
            machine_unit_id = machine_unit_ids[0]
            tx.run(
                queries.LINK_CHAT_SOURCE_RECORD_MENTIONS_EXISTING_UNIT,
                source_record_pk=source_record_pk,
                source_system_key=observation.source_system_key,
                source_record_id=observation.source_record_id,
                machine_unit_id=machine_unit_id,
                raw_context=observation.raw_context,
                observed_at=observation.observed_at,
                confidence=observation.confidence,
                quality_flag=observation.quality_flag.value,
            )

    @staticmethod
    def _resolve_person(
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
            person_id = create_person(tx)

        # REVIEW with no engine-picked person: reuse top candidate or create new.
        if match_result.decision == MatchDecision.REVIEW and person_id is None:
            if candidates:
                person_id = candidates[0].person_id
            else:
                person_id = create_person(tx)
                is_new_person = True

        # Hard NO_MATCH against a candidate still needs its own Person.
        if person_id is None and not is_new_person:
            person_id = create_person(tx)
            is_new_person = True

        assert person_id is not None, "invariant: every record resolves to a person"
        return person_id, is_new_person
