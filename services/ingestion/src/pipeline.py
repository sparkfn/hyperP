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

from src.golden_profile import compute_golden_profile
from src.graph import queries
from src.graph.client import Neo4jClient
from src.matching.engine import MatchEngine
from src.models import (
    CandidateResult,
    IngestResult,
    MatchDecision,
    MatchResult,
    NormalizedAttribute,
    NormalizedIdentifier,
    SourceRecordEnvelope,
)
from src.models import (
    NormalizedAddress as NormalizedAddressModel,
)
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
        identifiers = normalize_envelope_identifiers(envelope)
        address = normalize_envelope_address(envelope)
        attributes = normalize_envelope_attributes(envelope)

        # Steps 3-13 run inside a single write transaction
        def _work(tx: ManagedTransaction) -> IngestResult:
            return self._execute_ingest(
                tx, envelope, identifiers, address, attributes,
                ingest_run_id=ingest_run_id,
            )

        with self._client.session() as session:
            return session.execute_write(_work)

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

    def _execute_ingest(
        self,
        tx: ManagedTransaction,
        envelope: SourceRecordEnvelope,
        identifiers: list[NormalizedIdentifier],
        address: NormalizedAddressModel | None,
        attributes: list[NormalizedAttribute],
        ingest_run_id: str | None = None,
    ) -> IngestResult:
        """Orchestrate steps 3–13 of the ingest flow inside one write tx."""
        # 3. Upsert Identifier + Address graph nodes
        upsert_nodes(tx, identifiers, address)

        # 4. Candidate generation (graph traversal with cardinality caps)
        candidates = find_candidates(tx, identifiers, address)

        # 5. Match engine chain (deterministic → heuristic → LLM-shadow)
        match_result = self._match_engine.evaluate(
            tx, candidates, identifiers, address, attributes,
            record_type=envelope.record_type,
        )

        # 6. Resolve to a Person — create new or pick from candidates
        person_id, is_new_person = self._resolve_person(
            tx, match_result, candidates,
        )

        # 7. Persist SourceRecord, MatchDecision, optional ReviewCase
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
        match_decision_id = persist_match_decision(tx, match_result, source_record_pk)
        review_case_id = create_review_case_if_needed(
            tx, match_result, match_decision_id,
        )

        # 8–11. Link the source record into the Person's subgraph
        link_record_to_graph(
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
            record_auto_merge_event(
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
