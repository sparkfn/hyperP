"""Non-Person source-reference ingestion for CRM company records."""

from __future__ import annotations

import logging

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient
from src.identity_link_revisions import IdentityLinkDesiredRevision, append_identity_link_revisions
from src.models import (
    IngestResult,
    MatchDecision,
    MatchResult,
    RecordType,
    SourceRecordEnvelope,
    SourceRecordLifecycleStatus,
)
from src.pipeline_normalization import normalize_envelope_attributes
from src.pipeline_writes import persist_source_record
from src.record_lifecycle import (
    DuplicateVersion,
    activate_staged_version,
    load_locked_source_state,
    plan_incoming_version,
    reject_replaced_pending,
)
from src.source_instances import effective_source_instance_id

logger = logging.getLogger(__name__)


def ingest_reference_record(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    ingest_run_id: str | None = None,
) -> IngestResult:
    """Persist a non-Person source reference without calling the match engine."""
    if envelope.record_type is not RecordType.CRM_COMPANY:
        raise ValueError("reference ingestion only accepts crm_company records")

    def _tx(tx: ManagedTransaction) -> IngestResult:
        state = load_locked_source_state(
            tx, envelope.source_system, envelope.source_record_id, envelope.source_instance_id
        )
        plan = plan_incoming_version(state, envelope.record_hash)
        if isinstance(plan, DuplicateVersion):
            return IngestResult(
                source_record_id=envelope.source_record_id,
                source_record_pk=plan.source_record_pk,
                skipped_duplicate=True,
                ingest_run_id=ingest_run_id,
            )
        if plan.pending_to_reject is not None:
            reject_replaced_pending(tx, plan.pending_to_reject)
        envelope.source_record_version = str(plan.version)
        source_record_pk = persist_source_record(
            tx,
            envelope=envelope,
            identifiers=[],
            addresses=[],
            attributes=normalize_envelope_attributes(envelope),
            match_result=MatchResult(
                decision=MatchDecision.NO_MATCH,
                confidence=1.0,
                reasons=["non_person_source_reference"],
            ),
            is_new_person=False,
            ingest_run_id=ingest_run_id,
            lifecycle_status=SourceRecordLifecycleStatus.PENDING_REVIEW,
            expected_active_source_record_pk=plan.active_source_record_pk,
            link_status="not_applicable",
        )
        activate_staged_version(
            tx,
            source_system=envelope.source_system,
            source_record_id=envelope.source_record_id,
            source_instance_id=envelope.source_instance_id,
            old_source_record_pk=plan.active_source_record_pk,
            new_source_record_pk=source_record_pk,
        )
        if envelope.source_entity_id is not None and envelope.identity_policy_version is not None:
            append_identity_link_revisions(
                tx,
                [
                    IdentityLinkDesiredRevision(
                        source_system=envelope.source_system,
                        source_instance_id=effective_source_instance_id(
                            envelope.source_instance_id
                        ),
                        source_entity_type="company",
                        source_entity_id=envelope.source_entity_id,
                        identity_policy_version=envelope.identity_policy_version,
                        link_status="unresolved",
                        hyperp_person_id=None,
                        resolution_kind=(
                            "source_supersession"
                            if plan.active_source_record_pk
                            else "automatic_activation"
                        ),
                        effective_at=(envelope.observed_at or "1970-01-01T00:00:00+00:00"),
                        cause_key=f"activation:{source_record_pk}",
                    )
                ],
            )
        logger.info("Ingested non-Person reference %s", envelope.source_record_id)
        return IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=source_record_pk,
            ingest_run_id=ingest_run_id,
        )

    with client.session() as session:
        return session.execute_write(_tx)
