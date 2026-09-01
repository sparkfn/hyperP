"""Transactional orchestration for #311 CRM repair verification."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from neo4j import ManagedTransaction, Record

from src.crm_deal_identity_repair.digests import repaired_state_digest
from src.crm_deal_identity_repair.execution_models import RepairOutboxEvent
from src.crm_deal_identity_repair.verification_models import (
    RepairAtomicVerificationResult,
    RepairRunEquationCommand,
    RepairRunEquationResult,
    RepairSecondaryDispositionDetail,
    RepairVerificationCommand,
)
from src.golden_profile import recompute_golden_profile_from_active_authority
from src.graph.client import Neo4jClient
from src.graph.crm_deal_count import recompute_person_crm_deal_counts
from src.graph.crm_deal_identity_repair_mutation_payloads import _postcondition_state
from src.graph.crm_deal_identity_repair_mutation_records import outbox_event_from_properties
from src.graph.crm_deal_identity_repair_verification_derived import (
    PersonDerivedState,
    affected_person_ids,
    assert_no_existing_dispositions,
    build_context_details,
    build_invalidation_details,
    build_person_details,
    canonical_details,
    derive_state_digest,
    read_person_states,
    reconcile_identity_link_revision,
)
from src.graph.crm_deal_identity_repair_verification_errors import RepairVerificationDriftError
from src.graph.crm_deal_identity_repair_verification_pair import (
    read_pair_snapshot,
    reconcile_pair_cases,
)
from src.graph.crm_deal_identity_repair_verification_records import (
    VerificationBundle,
    VerificationBundleError,
    decode_verification_bundle,
)
from src.graph.crm_deal_identity_repair_verification_replay import replay_acknowledged_verification
from src.graph.crm_deal_identity_repair_verification_run import read_run_equation
from src.graph.crm_deal_identity_repair_verification_secondary import (
    SecondarySubjectError,
    assert_exact_disposition_subjects,
)
from src.graph.crm_deal_identity_repair_verification_support import (
    build_unit_equation,
    build_verification_record,
    bundle_parameters,
    json_mapping,
    list_mappings,
    mapping,
    outbox_parameters,
    persist_parameters,
    postcondition_closure_source_record_pks,
    primary_matches,
    required_record_int,
    required_str,
    retired_source_record_pks,
    retirement_requirements,
    strings,
)
from src.graph.queries import crm_deal_identity_repair_verification as queries

VerificationFailureStage = Literal[
    "after_bundle",
    "after_claim",
    "after_counts",
    "after_profiles",
    "after_pairs",
    "after_revisions",
    "after_invalidation",
    "after_persistence",
]


class CrmDealIdentityRepairVerificationRepository:
    """Owns one write transaction and a separate read-only run-equation path."""

    def __init__(
        self,
        client: Neo4jClient,
        *,
        failpoint: Callable[[VerificationFailureStage], None] | None = None,
    ) -> None:
        self._client = client
        self._failpoint = failpoint

    def verify_and_reconcile_unit(
        self, command: RepairVerificationCommand
    ) -> RepairAtomicVerificationResult:
        return self._client.execute_write(lambda tx: self._verify(tx, command))

    def read_run_equation(self, command: RepairRunEquationCommand) -> RepairRunEquationResult:
        return self._client.execute_read(lambda tx: read_run_equation(tx, command))

    def _verify(
        self, tx: ManagedTransaction, command: RepairVerificationCommand
    ) -> RepairAtomicVerificationResult:
        bundle = self._decode_bundle(command, self._bundle(tx, command))
        self._verify_bundle(tx, bundle)
        outbox = bundle.outbox
        if outbox.state == "acknowledged":
            return replay_acknowledged_verification(tx, command, bundle)
        if outbox.state != "pending":
            raise RepairVerificationDriftError("verification outbox is not pending")
        self._fail("after_bundle")
        claimed = tx.run(
            queries.CLAIM_VERIFICATION_OUTBOX, **outbox_parameters(command, outbox)
        ).single()
        if claimed is None:
            state = tx.run(
                queries.READ_EXACT_OUTBOX_STATE, **outbox_parameters(command, outbox)
            ).single()
            if state is not None and state["state"] == "acknowledged":
                return replay_acknowledged_verification(tx, command, bundle)
            raise RepairVerificationDriftError("verification outbox CAS rejected")
        self._fail("after_claim")
        primary = self._read_primary(tx, command, bundle)
        person_ids = affected_person_ids(
            tx, (*retired_source_record_pks(command), bundle.replacement_pk)
        )
        details, states, changed_person_ids, before_revisions = self._rebuild_derived_state(
            tx, command, person_ids
        )
        self._fail("after_profiles")
        details.extend(reconcile_pair_cases(tx, command, person_ids))
        self._fail("after_pairs")
        details.extend(build_context_details(tx, command))
        details.append(
            reconcile_identity_link_revision(
                tx, command, bundle.replacement_pk, bundle.result.outcome
            )
        )
        self._fail("after_revisions")
        details.extend(self._invalidate(tx, command, changed_person_ids, before_revisions))
        self._fail("after_invalidation")
        return self._persist(
            tx, command, outbox, primary, person_ids, details, bundle.result.outcome
        )

    def _rebuild_derived_state(
        self,
        tx: ManagedTransaction,
        command: RepairVerificationCommand,
        person_ids: tuple[str, ...],
    ) -> tuple[
        list[RepairSecondaryDispositionDetail],
        tuple[PersonDerivedState, ...],
        tuple[str, ...],
        dict[str, int],
    ]:
        before_states = read_person_states(tx, person_ids)
        recompute_person_crm_deal_counts(tx, person_ids)
        self._fail("after_counts")
        conflicts: dict[str, tuple[str, ...]] = {}
        for person_id in person_ids:
            profile = recompute_golden_profile_from_active_authority(
                tx, person_id, invalidate_analysis=False
            )
            if profile is not None:
                conflicts[person_id] = profile.conflict_fields
        states = read_person_states(tx, person_ids)
        before_by_id = {state.person_id: state for state in before_states}
        changed_person_ids = tuple(
            state.person_id for state in states if state.person_id in before_by_id
        )
        before_revisions = {
            state.person_id: state.analysis_revision
            for state in before_states
            if state.person_id in changed_person_ids
        }
        return (
            build_person_details(command, states, conflicts),
            states,
            changed_person_ids,
            before_revisions,
        )

    def _invalidate(
        self,
        tx: ManagedTransaction,
        command: RepairVerificationCommand,
        person_ids: tuple[str, ...],
        before_revisions: dict[str, int],
    ) -> list[RepairSecondaryDispositionDetail]:
        from src.profile_analysis_dirty import mark_profile_analysis_dirty

        dirtied = mark_profile_analysis_dirty(tx, person_ids=person_ids)
        states = read_person_states(tx, dirtied)
        return build_invalidation_details(command, before_revisions, states)

    def _persist(
        self,
        tx: ManagedTransaction,
        command: RepairVerificationCommand,
        outbox: RepairOutboxEvent,
        primary: Record,
        person_ids: tuple[str, ...],
        details: list[RepairSecondaryDispositionDetail],
        outcome: str,
    ) -> RepairAtomicVerificationResult:
        details = canonical_details(details)
        records = tuple(detail.record(command) for detail in details)
        assert_no_existing_dispositions(tx, command)
        state = derive_state_digest(
            primary, records, read_person_states(tx, person_ids), read_pair_snapshot(tx, command)
        )
        verification = build_verification_record(command, outbox.evidence_digest, state)
        row = tx.run(
            queries.PERSIST_VERIFICATION,
            **persist_parameters(command, verification, details, records, outbox),
        ).single()
        if row is None:
            raise RepairVerificationDriftError("verification persistence postcondition rejected")
        self._assert_persisted_subjects(details, row)
        self._fail("after_persistence")
        equation = build_unit_equation(
            outcome,
            required_record_int(primary, "active_links"),
            required_record_int(primary, "provisional_links"),
            required_record_int(primary, "forbidden_projection_count"),
            records,
        )
        if not equation.balanced:
            raise RepairVerificationDriftError("verification equation is unbalanced")
        return RepairAtomicVerificationResult(
            "committed",
            verification,
            records,
            outbox_event_from_properties(mapping(row, "outbox")),
            equation,
            state,
        )

    def _read_primary(
        self,
        tx: ManagedTransaction,
        command: RepairVerificationCommand,
        bundle: VerificationBundle,
    ) -> Record:
        row = tx.run(
            queries.READ_PRIMARY_POSTCONDITIONS,
            new_source_record_pk=bundle.replacement_pk,
            mutation_id=command.mutation_id,
            retired_source_record_pks=list(retired_source_record_pks(command)),
            retirement_requirements=list(retirement_requirements(command)),
            closure_source_record_pks=list(
                postcondition_closure_source_record_pks(command, bundle.replacement_pk)
            ),
        ).single()
        if row is None or not primary_matches(row, bundle.result.outcome):
            raise RepairVerificationDriftError("verification primary invariant differs")
        return row

    def _bundle(self, tx: ManagedTransaction, command: RepairVerificationCommand) -> Record:
        row = tx.run(
            queries.LOCK_AND_READ_VERIFICATION_BUNDLE, **bundle_parameters(command)
        ).single()
        if row is None:
            raise RepairVerificationDriftError(
                "verification immutable bundle is incomplete or changed"
            )
        return row

    def _decode_bundle(self, command: RepairVerificationCommand, row: Record) -> VerificationBundle:
        try:
            return decode_verification_bundle(
                command.mutation_command,
                json_mapping(row, "result"),
                json_mapping(row, "image"),
                json_mapping(row, "checkpoint"),
                json_mapping(row, "outbox"),
                strings(row, "new_source_pks"),
                required_record_int(row, "new_source_count"),
                required_record_int(row, "blocked_dispatch_count"),
            )
        except VerificationBundleError as exc:
            raise RepairVerificationDriftError("verification immutable bundle differs") from exc

    def _verify_bundle(self, tx: ManagedTransaction, bundle: VerificationBundle) -> None:
        observed = repaired_state_digest(_postcondition_state(tx, bundle.replacement_pk))
        if observed != bundle.image.expected_repaired_digest:
            raise RepairVerificationDriftError("verification repaired-state digest differs")

    def _assert_persisted_subjects(
        self, details: list[RepairSecondaryDispositionDetail], row: Record
    ) -> None:
        try:
            assert_exact_disposition_subjects(
                details,
                (
                    required_str(value, "subject_fingerprint")
                    for value in list_mappings(row, "dispositions")
                ),
            )
        except SecondarySubjectError as exc:
            raise RepairVerificationDriftError("persisted secondary subject set differs") from exc

    def _fail(self, stage: VerificationFailureStage) -> None:
        if self._failpoint is not None:
            self._failpoint(stage)
