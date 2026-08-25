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

import json
import logging

from neo4j import ManagedTransaction

from src.bitrix_backfill_runtime import record_terminal_unit, source_lineage_text
from src.bitrix_ingestion_models import ExecutionContext, FenceContext
from src.exclusions import ExclusionContext, is_excluded_vehicle_observation
from src.golden_profile import compute_golden_profile
from src.graph import queries
from src.graph.bitrix_deal_scope import DealScopeObservation, record_scope_batch_in_transaction
from src.graph.bootstrap import MATCH_ONLY_SOURCE_KEYS
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import assert_active_bitrix_fence
from src.matching.deterministic import prefetch_no_match_lock_owners
from src.matching.engine import MatchEngine, ambiguous_prior_owners_result
from src.models import (
    MATCH_ONLY_RECORD_TYPES,
    PERSON_CREATING_RECORD_TYPES,
    CandidateResult,
    EngineType,
    IngestResult,
    JsonValue,
    MatchDecision,
    MatchResult,
    NormalizedAttribute,
    NormalizedIdentifier,
    RawIdentifier,
    RecordType,
    SourceRecordEnvelope,
    SourceRecordLifecycleStatus,
)
from src.models import (
    NormalizedAddress as NormalizedAddressModel,
)
from src.pipeline_bankruptcy import bankruptcy_case_blueprint, materialize_bankruptcy_case
from src.pipeline_crm_identity import (
    apply_crm_deal_match_policy,
    blocked_crm_owner_result,
    crm_deal_requires_quarantine,
    deterministic_crm_owner_result,
    projected_identifiers,
    resolve_canonical_crm_contact,
)
from src.pipeline_knows import activate_knows_projection, knows_projection_blueprints
from src.pipeline_normalization import (
    normalize_envelope_addresses,
    normalize_envelope_attributes,
    normalize_envelope_identifiers,
)
from src.pipeline_person_pairs import audit_person_pairs
from src.pipeline_writes import (
    build_normalized_source_payload,
    create_person,
    create_review_case_if_needed,
    find_candidates,
    link_record_to_graph,
    persist_match_decision,
    persist_source_record,
    record_auto_merge_event,
    retire_identity_projections,
    upsert_nodes,
)
from src.profile_analysis_dirty import mark_profile_analysis_dirty
from src.record_lifecycle import (
    DuplicateVersion,
    PlannedVersion,
    activate_staged_version,
    load_locked_source_state,
    plan_incoming_version,
    reject_replaced_pending,
)
from src.vehicle_extraction import observations_from_chat_inquiries
from src.vehicles import (
    normalize_lta_tag,
    normalize_serial_number,
)


def _is_match_only_source(source_key: str) -> bool:
    """Compatibility helper for source-wide match-only policy checks."""
    return source_key in MATCH_ONLY_SOURCE_KEYS


def _is_match_only_record(source_key: str, record_type: RecordType) -> bool:
    """Whether this source record may only attach to an existing Person."""
    return (
        _is_match_only_source(source_key)
        or record_type in MATCH_ONLY_RECORD_TYPES
        or record_type not in PERSON_CREATING_RECORD_TYPES
    )


logger = logging.getLogger(__name__)

_CRM_DEAL_CONTINUITY_RAW_KEYS = (
    "primary_contact_id",
    "primary_contact_kind",
    "contact_count",
    "crm_contact_groups",
    "crm_contact_raw_groups",
    "crm_contact_ids",
    "crm_contact_resolution_required",
    "crm_deal_identity_policy_version",
    "crm_contact_identity_metadata",
)


class IngestPipeline:
    """Processes a single source record through the full ingestion flow.

    All graph mutations for one record run inside a single explicit
    ``session.execute_write`` transaction.
    """

    def __init__(
        self,
        client: Neo4jClient,
        *,
        fence_context: FenceContext | None = None,
        execution_context: ExecutionContext | None = None,
    ) -> None:
        if execution_context is not None and fence_context is not None:
            raise ValueError("supply execution_context or fence_context, not both")
        self._client = client
        self._match_engine = MatchEngine()
        self._execution_context = execution_context
        self._fence_context = (
            execution_context.fence_context if execution_context is not None else fence_context
        )

    def ingest(
        self,
        envelope: SourceRecordEnvelope,
        ingest_run_id: str | None = None,
        exclusion_context: ExclusionContext | None = None,
    ) -> IngestResult:
        """Ingest a single source record.  Returns an ``IngestResult``."""

        # Lock, idempotency classification, version assignment, and all writes
        # share one transaction so concurrent updates cannot allocate one version.
        active_exclusion_context = (
            exclusion_context if exclusion_context is not None else ExclusionContext()
        )

        # Steps 3-13 run inside a single write transaction
        def _work(tx: ManagedTransaction) -> IngestResult:
            if self._fence_context is not None:
                assert_active_bitrix_fence(tx, self._fence_context)
            state = load_locked_source_state(tx, envelope.source_system, envelope.source_record_id)
            plan = plan_incoming_version(state, envelope.record_hash)
            if isinstance(plan, DuplicateVersion):
                result = IngestResult(
                    source_record_id=envelope.source_record_id,
                    source_record_pk=plan.source_record_pk,
                    skipped_duplicate=True,
                    ingest_run_id=ingest_run_id,
                )
                self._finalize_bitrix_unit(tx, envelope, result)
                return result
            envelope.source_record_version = str(plan.version)
            result = self._execute_ingest(
                tx,
                envelope,
                normalize_envelope_identifiers(envelope),
                normalize_envelope_addresses(envelope),
                normalize_envelope_attributes(envelope),
                ingest_run_id=ingest_run_id,
                lifecycle_plan=plan,
                exclusion_context=active_exclusion_context,
            )
            self._finalize_bitrix_unit(tx, envelope, result)
            return result

        with self._client.session() as session:
            return session.execute_write(_work)

    def _finalize_bitrix_unit(
        self,
        tx: ManagedTransaction,
        envelope: SourceRecordEnvelope,
        result: IngestResult,
    ) -> None:
        context = self._execution_context
        if context is None:
            return
        scope_state: str | None = None
        if envelope.record_type == RecordType.CRM_DEAL:
            deal_id = envelope.source_record_id.rsplit("-", maxsplit=1)[-1]
            category_id = source_lineage_text(
                envelope.raw_payload,
                "category_id",
                "CATEGORY_ID",
            )
            if category_id is None:
                raise ValueError("Bitrix CRM deal requires category lineage")
            if envelope.entity_key is None:
                raise ValueError("Bitrix CRM deal requires entity ownership")
            record_scope_batch_in_transaction(
                tx,
                [
                    DealScopeObservation(
                        deal_id=deal_id,
                        scope_state="in_scope",
                        category_id=category_id,
                        entity_key=envelope.entity_key,
                        source_record_pk=result.source_record_pk,
                    )
                ],
                fence_context=context.fence_context,
            )
            scope_state = "in_scope"
        if envelope.record_type == RecordType.CONVERSATION and result.source_record_pk is not None:
            activity_ids = envelope.raw_payload.get("crm_activity_ids")
            if isinstance(activity_ids, list):
                tx.run(
                    queries.LINK_CONVERSATION_TO_CRM_HISTORY,
                    conversation_source_record_pk=result.source_record_pk,
                    source_system=envelope.source_system,
                    crm_activity_ids=[
                        value for value in activity_ids if isinstance(value, str) and value
                    ],
                ).consume()
        record_terminal_unit(
            tx,
            context=context,
            envelope=envelope,
            result=result,
            scope_state=scope_state,
        )

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
        addresses: list[NormalizedAddressModel],
        attributes: list[NormalizedAttribute],
        ingest_run_id: str | None = None,
        lifecycle_plan: PlannedVersion | None = None,
        exclusion_context: ExclusionContext | None = None,
    ) -> IngestResult:
        """Orchestrate steps 3–13 of the ingest flow inside one write tx."""
        active_exclusion_context = (
            exclusion_context if exclusion_context is not None else ExclusionContext()
        )
        if lifecycle_plan is None:
            lifecycle_plan = PlannedVersion(
                version=int(envelope.source_record_version or "1"),
                active_source_record_pk=None,
                prior_person_ids=(),
                pending_to_reject=None,
            )
        if lifecycle_plan.pending_to_reject is not None:
            reject_replaced_pending(tx, lifecycle_plan.pending_to_reject)
        activation_blueprint = self._activation_blueprint(
            envelope,
            active_exclusion_context,
        )
        person_identifiers = projected_identifiers(envelope, identifiers)
        continuity_fast_path = self._has_unchanged_crm_identity(
            envelope=envelope,
            identifiers=person_identifiers,
            addresses=addresses,
            attributes=attributes,
            activation_blueprint=activation_blueprint,
            lifecycle_plan=lifecycle_plan,
        )
        if continuity_fast_path:
            candidates: list[CandidateResult] = []
            match_result = MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                reasons=["unchanged_crm_identity_continuity"],
                engine_type=EngineType.DETERMINISTIC,
                matched_person_id=lifecycle_plan.prior_person_ids[0],
            )
        else:
            candidates = find_candidates(tx, identifiers, addresses)
            continuity_person_id = (
                lifecycle_plan.prior_person_ids[0]
                if len(lifecycle_plan.prior_person_ids) == 1
                else None
            )
            multi_contact_result = self._resolve_ambiguous_crm_deal_contacts(
                tx,
                envelope,
                identifiers,
                continuity_person_id=continuity_person_id,
            )
            canonical_contact_result = resolve_canonical_crm_contact(
                tx,
                envelope,
                identifiers,
                continuity_person_id=continuity_person_id,
            )
            if (
                self._requires_ambiguous_crm_contact_resolution(envelope)
                and multi_contact_result is None
                and canonical_contact_result is None
            ):
                logger.info(
                    "Dropping CRM deal %s: contacts do not resolve to one existing person",
                    envelope.source_record_id,
                )
                return IngestResult(
                    source_record_id=envelope.source_record_id,
                    ingest_run_id=ingest_run_id,
                    dropped=True,
                )
            if len(lifecycle_plan.prior_person_ids) > 1:
                match_result = ambiguous_prior_owners_result(lifecycle_plan.prior_person_ids)
            elif multi_contact_result is not None:
                match_result = multi_contact_result
            elif canonical_contact_result is not None:
                match_result = canonical_contact_result
            else:
                match_result = self._match_engine.evaluate(
                    tx,
                    candidates,
                    identifiers,
                    addresses[0] if addresses else None,
                    attributes,
                    record_type=envelope.record_type,
                    continuity_person_id=(
                        lifecycle_plan.prior_person_ids[0]
                        if lifecycle_plan.prior_person_ids
                        else None
                    ),
                )
            match_result = apply_crm_deal_match_policy(
                envelope,
                match_result,
                continuity_person_id=continuity_person_id,
            )
        durable_quarantine = crm_deal_requires_quarantine(match_result)
        if (
            _is_match_only_record(envelope.source_system, envelope.record_type)
            and not durable_quarantine
            and not self._has_usable_match(match_result, candidates)
        ):
            logger.info(
                "Dropping unmatched match-only record %s (source=%s, decision=%s)",
                envelope.source_record_id,
                envelope.source_system,
                match_result.decision.value,
            )
            return IngestResult(
                source_record_id=envelope.source_record_id,
                ingest_run_id=ingest_run_id,
                dropped=True,
            )
        if durable_quarantine:
            logger.info(
                "Persisting quarantined CRM deal %s for review: %s",
                envelope.source_record_id,
                match_result.reasons,
            )
            return self._persist_unlinked_review(
                tx,
                envelope=envelope,
                identifiers=person_identifiers,
                addresses=addresses,
                attributes=attributes,
                match_result=match_result,
                ingest_run_id=ingest_run_id,
                lifecycle_plan=lifecycle_plan,
                activation_blueprint=activation_blueprint,
            )
        if not continuity_fast_path:
            upsert_nodes(tx, person_identifiers, addresses)
        person_id, is_new_person = self._resolve_person(tx, match_result, candidates)
        source_record_pk = persist_source_record(
            tx,
            envelope=envelope,
            identifiers=person_identifiers,
            addresses=addresses,
            attributes=attributes,
            match_result=match_result,
            is_new_person=is_new_person,
            ingest_run_id=ingest_run_id,
            lifecycle_status=SourceRecordLifecycleStatus.PENDING_REVIEW,
            expected_active_source_record_pk=lifecycle_plan.active_source_record_pk,
            activation_blueprint=activation_blueprint,
        )
        match_decision_id = persist_match_decision(tx, match_result, source_record_pk)
        review_case_id = create_review_case_if_needed(tx, match_result, match_decision_id)
        # A REVIEW-band match against an *existing* candidate is provisional: the
        # record is linked so the reviewer can compare it, but its identifiers /
        # addresses / facts must NOT be wired onto the candidate person — nor the
        # golden profile recomputed — until a human approves the merge. Otherwise
        # an unconfirmed record silently commingles into the candidate and cannot
        # be cleanly split off on reject. (Reviewer-workflow Side-Effect Matrix:
        # a record only becomes "linked" on a merge action.)
        provisional_review = match_result.decision == MatchDecision.REVIEW
        link_record_to_graph(
            tx,
            envelope=envelope,
            identifiers=person_identifiers,
            addresses=addresses,
            attributes=attributes,
            person_id=person_id,
            source_record_pk=source_record_pk,
            attach_evidence=not provisional_review,
        )
        accepted = match_result.decision is not MatchDecision.REVIEW
        affected_person_ids = set(lifecycle_plan.prior_person_ids)
        if accepted:
            activate_knows_projection(tx, envelope, person_id, source_record_pk)
            if lifecycle_plan.active_source_record_pk is not None:
                tx.run(
                    queries.RETIRE_KNOWS_PROJECTION,
                    source_record_pk=lifecycle_plan.active_source_record_pk,
                )
                if envelope.record_type == RecordType.CONVERSATION:
                    tx.run(
                        queries.RETIRE_CONVERSATION_VEHICLE_MENTIONS,
                        source_record_pk=lifecycle_plan.active_source_record_pk,
                    )
            self._write_chat_vehicle_observations(
                tx,
                envelope=envelope,
                source_record_pk=source_record_pk,
                exclusion_context=active_exclusion_context,
            )
            if lifecycle_plan.active_source_record_pk is not None:
                if continuity_fast_path:
                    retired_person_ids = retire_identity_projections(
                        tx,
                        lifecycle_plan.active_source_record_pk,
                        person_ids=lifecycle_plan.prior_person_ids,
                    )
                else:
                    retired_person_ids = retire_identity_projections(
                        tx, lifecycle_plan.active_source_record_pk
                    )
                affected_person_ids.update(retired_person_ids)
            materialize_bankruptcy_case(
                tx,
                envelope=envelope,
                person_id=person_id,
                source_record_pk=source_record_pk,
                replaced_source_record_pk=lifecycle_plan.active_source_record_pk,
            )
            affected_person_ids.add(person_id)
        if (
            envelope.record_type is RecordType.CRM_DEAL
            and match_result.additional_linked_person_ids
        ):
            raise AssertionError("CRM deals must not link to additional merge candidates")
        # Multi-match: the record reached the merge band against more than one
        # distinct person. Link the record + its extracted evidence to every
        # other matched person too — WITHOUT merging the persons, which may
        # legitimately share an identifier — and recompute each golden profile.
        for other_person_id in match_result.additional_linked_person_ids if accepted else []:
            if other_person_id == person_id:
                continue
            link_record_to_graph(
                tx,
                envelope=envelope,
                identifiers=person_identifiers,
                addresses=addresses,
                attributes=attributes,
                person_id=other_person_id,
                source_record_pk=source_record_pk,
                attach_evidence=True,
            )
            affected_person_ids.add(other_person_id)
        # Person↔person audit: any usable identifier this record carries that now
        # links 2+ active persons opens a pairwise review case (deduped, fanout-
        # capped). Audit-only — never merges or links persons.
        if accepted:
            activate_staged_version(
                tx,
                source_system=envelope.source_system,
                source_record_id=envelope.source_record_id,
                old_source_record_pk=lifecycle_plan.active_source_record_pk,
                new_source_record_pk=source_record_pk,
            )
            if envelope.record_type == RecordType.CRM_DEAL:
                tx.run(
                    queries.ACTIVATE_PENDING_CALLS_FOR_DEAL,
                    deal_source_record_pk=source_record_pk,
                )
            if not continuity_fast_path:
                for affected_person_id in sorted(affected_person_ids):
                    compute_golden_profile(tx, affected_person_id)
                audit_person_pairs(tx, person_identifiers, envelope.record_type)
            mark_profile_analysis_dirty(
                tx,
                source_record_pks=(
                    ()
                    if continuity_fast_path
                    else (
                        source_record_pk,
                        lifecycle_plan.active_source_record_pk or "",
                    )
                ),
                person_ids=affected_person_ids,
            )
        if (
            match_result.decision == MatchDecision.MERGE
            and not is_new_person
            and not continuity_fast_path
        ):
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

    def _write_chat_vehicle_observations(
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
            if is_excluded_vehicle_observation(observation, exclusion_context):
                continue
            row = tx.run(
                queries.RESOLVE_EXISTING_VEHICLE_FOR_CHAT,
                normalized_lta_tag=normalize_lta_tag(observation.lta_tag),
                normalized_serial_number=normalize_serial_number(observation.serial_number),
                product=observation.product,
            ).single()
            if row is None:
                continue
            vehicle_ids = [str(item) for item in row["vehicle_ids"]]
            if len(vehicle_ids) != 1:
                continue
            vehicle_id = vehicle_ids[0]
            tx.run(
                queries.LINK_CHAT_SOURCE_RECORD_MENTIONS_VEHICLE,
                source_record_pk=source_record_pk,
                source_system_key=observation.source_system_key,
                source_record_id=observation.source_record_id,
                vehicle_id=vehicle_id,
                raw_context=observation.raw_context,
                observed_at=observation.observed_at,
                confidence=observation.confidence,
                quality_flag=observation.quality_flag.value,
            )

    @staticmethod
    def _activation_blueprint(
        envelope: SourceRecordEnvelope, exclusion_context: ExclusionContext
    ) -> dict[str, JsonValue]:
        blueprint: dict[str, JsonValue] = {}
        blueprint["knows_relationships"] = knows_projection_blueprints(envelope)
        bankruptcy = bankruptcy_case_blueprint(envelope)
        if bankruptcy is not None:
            blueprint["bankruptcy_case"] = bankruptcy
        if envelope.record_type != RecordType.CONVERSATION:
            return blueprint
        inquiries = envelope.raw_payload.get("inquiries")
        if not isinstance(inquiries, list):
            return blueprint
        mentions: list[JsonValue] = []
        for observation in observations_from_chat_inquiries(
            source_system_key=envelope.source_system,
            source_record_id=envelope.source_record_id,
            observed_at=envelope.observed_at,
            inquiries=inquiries,
        ):
            if is_excluded_vehicle_observation(observation, exclusion_context):
                continue
            mentions.append(
                {
                    "normalized_lta_tag": normalize_lta_tag(observation.lta_tag),
                    "normalized_serial_number": normalize_serial_number(observation.serial_number),
                    "product": observation.product,
                    "raw_context": observation.raw_context,
                    "observed_at": observation.observed_at,
                    "confidence": observation.confidence,
                    "quality_flag": observation.quality_flag.value,
                    "source_system_key": observation.source_system_key,
                    "source_record_id": observation.source_record_id,
                }
            )
        blueprint["vehicle_mentions"] = mentions
        return blueprint

    @staticmethod
    def _has_unchanged_crm_identity(
        *,
        envelope: SourceRecordEnvelope,
        identifiers: list[NormalizedIdentifier],
        addresses: list[NormalizedAddressModel],
        attributes: list[NormalizedAttribute],
        activation_blueprint: dict[str, JsonValue],
        lifecycle_plan: PlannedVersion,
    ) -> bool:
        """Reuse one prior owner only when every identity-bearing value is unchanged."""
        if (
            envelope.record_type is not RecordType.CRM_DEAL
            or lifecycle_plan.active_source_record_pk is None
            or len(lifecycle_plan.prior_person_ids) != 1
            or lifecycle_plan.active_normalized_payload is None
            or lifecycle_plan.active_raw_payload is None
        ):
            return False
        try:
            prior_normalized = json.loads(lifecycle_plan.active_normalized_payload)
            prior_raw = json.loads(lifecycle_plan.active_raw_payload)
        except (TypeError, ValueError):
            return False
        if not isinstance(prior_normalized, dict) or not isinstance(prior_raw, dict):
            return False
        incoming_normalized = build_normalized_source_payload(
            envelope=envelope,
            identifiers=identifiers,
            addresses=addresses,
            attributes=attributes,
            activation_blueprint=activation_blueprint,
        )
        if prior_normalized != incoming_normalized:
            return False
        return all(
            prior_raw.get(key) == envelope.raw_payload.get(key)
            for key in _CRM_DEAL_CONTINUITY_RAW_KEYS
        )

    @staticmethod
    def _has_usable_match(
        match_result: MatchResult,
        candidates: list[CandidateResult],
    ) -> bool:
        """True when the match result resolves to an existing person.

        MERGE resolves to an existing person only when the engine emits a
        ``matched_person_id`` — a MERGE with no matched person would otherwise
        fall through to ``_resolve_person``'s create-person fallback, violating
        the match-only-sources-never-create-persons invariant. REVIEW resolves
        to an existing person only when the engine or the top candidate provides
        one — a REVIEW with no ``matched_person_id`` and no candidates has
        nothing to attach to.
        """
        if match_result.decision == MatchDecision.MERGE:
            return match_result.matched_person_id is not None
        if match_result.decision == MatchDecision.REVIEW:
            return match_result.matched_person_id is not None or bool(candidates)
        return False

    @staticmethod
    def _requires_ambiguous_crm_contact_resolution(envelope: SourceRecordEnvelope) -> bool:
        return (
            envelope.record_type == RecordType.CRM_DEAL
            and envelope.raw_payload.get("crm_contact_resolution_required") is True
        )

    @staticmethod
    def _resolve_ambiguous_crm_deal_contacts(
        tx: ManagedTransaction,
        envelope: SourceRecordEnvelope,
        identifiers: list[NormalizedIdentifier],
        *,
        continuity_person_id: str | None,
    ) -> MatchResult | None:
        """Resolve only owners shared by every contact and enforce hard blockers."""
        if not IngestPipeline._requires_ambiguous_crm_contact_resolution(envelope):
            return None
        raw_groups = envelope.raw_payload.get("crm_contact_groups")
        if not isinstance(raw_groups, list) or len(raw_groups) < 2:
            return None
        candidate_groups: list[set[str]] = []
        for raw_group in raw_groups:
            person_ids = IngestPipeline._crm_contact_group_person_ids(tx, envelope, raw_group)
            if not person_ids:
                return None
            candidate_groups.append(person_ids)
        shared_person_ids = sorted(set.intersection(*candidate_groups))
        if not shared_person_ids:
            group_values: list[JsonValue] = []
            for group in candidate_groups:
                person_values: list[JsonValue] = []
                person_values.extend(sorted(group))
                group_values.append(person_values)
            return MatchResult(
                decision=MatchDecision.REVIEW,
                confidence=1.0,
                reasons=["disjoint_multi_contact_crm_owners_require_primary"],
                engine_type=EngineType.DETERMINISTIC,
                feature_snapshot={
                    "crm_deal_quarantine": True,
                    "multi_contact_crm_candidate_groups": group_values,
                    "continuity_person_id": continuity_person_id,
                },
            )
        blocked_owners = prefetch_no_match_lock_owners(tx, shared_person_ids, identifiers)
        eligible_person_ids = [
            person_id for person_id in shared_person_ids if person_id not in blocked_owners
        ]
        if not eligible_person_ids:
            return blocked_crm_owner_result(
                shared_person_ids,
                reason="multi_contact_crm_owner_blocked_by_no_match_lock",
                snapshot_key="blocked_multi_contact_crm_candidate_ids",
            )
        if len(shared_person_ids) == 1:
            return deterministic_crm_owner_result(
                eligible_person_ids[0],
                continuity_person_id=continuity_person_id,
                merge_reason="All non-primary CRM contacts resolve to the same existing person",
                continuity_review_reason="changed_multi_contact_crm_owner_requires_review",
            )
        review_person_id = (
            continuity_person_id
            if continuity_person_id in eligible_person_ids
            else eligible_person_ids[0]
        )
        candidate_values: list[JsonValue] = list(eligible_person_ids)
        blocked_values: list[JsonValue] = []
        blocked_values.extend(sorted(blocked_owners))
        return MatchResult(
            decision=MatchDecision.REVIEW,
            confidence=1.0,
            reasons=["ambiguous_multi_contact_crm_owners"],
            engine_type=EngineType.DETERMINISTIC,
            matched_person_id=review_person_id,
            proposed_person_id=eligible_person_ids[0],
            review_candidate_person_ids=eligible_person_ids,
            feature_snapshot={
                "multi_contact_crm_candidate_ids": candidate_values,
                "blocked_multi_contact_crm_candidate_ids": blocked_values,
                "continuity_person_id": continuity_person_id,
            },
        )

    @staticmethod
    def _crm_contact_group_person_ids(
        tx: ManagedTransaction,
        envelope: SourceRecordEnvelope,
        raw_group: object,
    ) -> set[str]:
        if not isinstance(raw_group, list):
            return set()
        try:
            raw_identifiers = [RawIdentifier.model_validate(item) for item in raw_group]
        except ValueError:
            return set()
        group_envelope = envelope.model_copy(
            update={"identifiers": raw_identifiers, "addresses": [], "attributes": {}}
        )
        canonical_identifiers = [
            identifier
            for identifier in normalize_envelope_identifiers(group_envelope)
            if identifier.identifier_type == "crm_contact_id"
        ]
        if not canonical_identifiers:
            return set()
        return {candidate.person_id for candidate in find_candidates(tx, canonical_identifiers, [])}

    @staticmethod
    def _persist_unlinked_review(
        tx: ManagedTransaction,
        *,
        envelope: SourceRecordEnvelope,
        identifiers: list[NormalizedIdentifier],
        addresses: list[NormalizedAddressModel],
        attributes: list[NormalizedAttribute],
        match_result: MatchResult,
        ingest_run_id: str | None,
        lifecycle_plan: PlannedVersion,
        activation_blueprint: dict[str, JsonValue],
    ) -> IngestResult:
        source_record_pk = persist_source_record(
            tx,
            envelope=envelope,
            identifiers=identifiers,
            addresses=addresses,
            attributes=attributes,
            match_result=match_result,
            is_new_person=False,
            ingest_run_id=ingest_run_id,
            lifecycle_status=SourceRecordLifecycleStatus.PENDING_REVIEW,
            expected_active_source_record_pk=lifecycle_plan.active_source_record_pk,
            activation_blueprint=activation_blueprint,
        )
        match_decision_id = persist_match_decision(tx, match_result, source_record_pk)
        review_case_id = create_review_case_if_needed(tx, match_result, match_decision_id)
        return IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=source_record_pk,
            candidate_count=len(match_result.review_candidate_person_ids),
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
