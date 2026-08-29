"""One managed Neo4j transaction for a qualified CRM-deal repair unit."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from neo4j import ManagedTransaction, Record

from src.crm_deal_identity_repair.digests import (
    authority_evidence_digest,
    object_digest,
    repaired_state_digest,
)
from src.crm_deal_identity_repair.mutation_classifier import (
    build_repair_plan,
    parse_repair_inventory,
)
from src.crm_deal_identity_repair.mutation_models import (
    MutationFailureStage,
    RepairAtomicMutationResult,
    RepairMutationCommand,
    RepairMutationPlan,
    build_outbox_digest,
    build_result_digest,
)
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_mutation_authority import (
    _assert_frozen_inventory,
    _authority_evidence,
    _current_inventory_item,
    _lock_support_records,
    _match_result,
    _stage_active,
    _stage_review,
)
from src.graph.crm_deal_identity_repair_mutation_errors import (
    RepairMutationAuthorityError,
    RepairMutationDriftError,
)
from src.graph.crm_deal_identity_repair_mutation_payloads import (
    _canonical_json,
    _expected_state,
    _guard_parameters,
    _ledger_parameters,
    _postcondition_state,
    _record_int,
    _record_object,
    _required_record_string,
    _rollback_payload,
    _snapshot,
    _source_values,
)
from src.graph.crm_deal_identity_repair_mutation_records import atomic_result_from_record
from src.graph.queries.crm_deal_identity_repair_mutation import (
    ACTIVATE_REPAIRED_SOURCE_RECORD,
    CREATE_REPAIR_DECISION,
    CREATE_REPAIRED_SOURCE_RECORD,
    CREATE_UNRECONSTRUCTABLE_REVIEW_SOURCE_RECORD,
    FIND_COMMITTED_REPAIR_MUTATION,
    LOCK_AND_ASSERT_REPAIR_MUTATION_GUARD,
    LOCK_REPAIR_MUTATION_UNIT,
    PERSIST_REPAIR_MUTATION_LEDGER,
    READ_REPAIRED_OWNER_IDS,
    RETIRE_EXACT_CONTAMINATION,
    STAGE_REVIEW_SOURCE_RECORD,
)
from src.models import SourceRecordEnvelope
from src.source_version_keys import encode_source_version_key

T = TypeVar("T")


class CrmDealIdentityRepairMutationRepository:
    """Commit one deal mutation and every ledger effect in one Neo4j transaction."""

    def __init__(
        self,
        client: Neo4jClient,
        *,
        failpoint: Callable[[MutationFailureStage], None] | None = None,
    ) -> None:
        self._client = client
        self._failpoint = failpoint

    def commit_atomic_mutation(self, request: RepairMutationCommand) -> RepairAtomicMutationResult:
        """Commit or exactly replay one pre-authorized unit."""
        return self._client.execute_write(lambda tx: self._commit(tx, request))

    def _commit(
        self,
        tx: ManagedTransaction,
        request: RepairMutationCommand,
    ) -> RepairAtomicMutationResult:
        self._lock_unit(tx, request)
        replay = self._replay(tx, request)
        if replay is not None:
            return replay
        guard = self._guard(tx, request)
        self._fail("after_guard")
        current_item = _current_inventory_item(tx, request)
        _assert_frozen_inventory(request.inventory, current_item)
        self._fail("after_source_lock")
        entity_key = _required_record_string(guard, "entity_key")
        parsed = parse_repair_inventory(current_item, request.source_instance_id, entity_key)
        evidence = _authority_evidence(tx, request, parsed.current_owner_ids)
        _lock_support_records(tx, request, evidence)
        locked_evidence = _authority_evidence(tx, request, parsed.current_owner_ids)
        if locked_evidence != evidence:
            raise RepairMutationDriftError(
                "current authority changed while acquiring serialization"
            )
        evidence = locked_evidence
        plan = build_repair_plan(parsed, _match_result(parsed.current_owner_ids), evidence)
        self._fail("after_classification")
        expected_state = _expected_state(parsed.envelope, plan)
        snapshot = _snapshot(tx, request, plan.retired_source_record_pks, parsed.envelope)
        rollback = _rollback_payload(request, plan, snapshot, expected_state)
        self._fail("after_rollback_image")
        self._create_source(tx, request, plan)
        self._fail("after_source_record")
        tx.run(
            RETIRE_EXACT_CONTAMINATION,
            retired_source_record_pks=list(plan.retired_source_record_pks),
            mutation_id=request.mutation_id,
        ).consume()
        self._fail("after_retirement")
        decision_id = request.mutation_id + ":decision"
        self._create_decision(tx, request, plan, decision_id)
        self._fail("after_decision")
        self._stage_outcome(tx, request, parsed.envelope, plan, decision_id)
        self._fail("after_staging")
        observed_state = _postcondition_state(tx, plan.source_record_pk)
        if observed_state != expected_state:
            raise RuntimeError("repair transaction-local postcondition digest differs")
        self._guard(tx, request)
        result_digest = build_result_digest(request, plan, rollback)
        outbox_digest = build_outbox_digest(request, result_digest)
        persisted = tx.run(
            PERSIST_REPAIR_MUTATION_LEDGER,
            **_ledger_parameters(request, plan, rollback, result_digest, outbox_digest),
        ).single()
        if persisted is None:
            raise RepairMutationAuthorityError("repair ledger CAS rejected the mutation")
        self._fail("after_ledger")
        self._fail("after_checkpoint")
        self._fail("after_outbox")
        self._fail("after_postcondition")
        bundle = tx.run(
            FIND_COMMITTED_REPAIR_MUTATION,
            run_id=request.unit.run_id,
            unit_id=request.unit.unit_id,
        ).single()
        if bundle is None:
            raise RuntimeError("committed repair bundle readback is missing")
        return atomic_result_from_record(bundle, replayed=False)

    def _lock_unit(self, tx: ManagedTransaction, request: RepairMutationCommand) -> None:
        row = tx.run(
            LOCK_REPAIR_MUTATION_UNIT,
            run_id=request.unit.run_id,
            unit_id=request.unit.unit_id,
            mutation_id=request.mutation_id,
        ).single()
        if row is None:
            replay = self._replay(tx, request)
            if replay is not None:
                return
            raise RepairMutationDriftError("repair unit is bound to another mutation request")

    def _replay(
        self,
        tx: ManagedTransaction,
        request: RepairMutationCommand,
    ) -> RepairAtomicMutationResult | None:
        row = tx.run(
            FIND_COMMITTED_REPAIR_MUTATION,
            run_id=request.unit.run_id,
            unit_id=request.unit.unit_id,
        ).single()
        if row is None:
            return None
        result = _record_object(row, "result")
        if any(
            _record_int(row, key) != 1
            for key in ("image_count", "checkpoint_count", "outbox_count", "source_count")
        ):
            raise RepairMutationDriftError("repair committed bundle cardinality differs")
        if (
            result.get("mutation_id") != request.mutation_id
            or result.get("request_digest") != request.request_digest
        ):
            raise RepairMutationDriftError("repair unit was committed with changed bound evidence")
        committed_source_record_pk = _required_record_string(row, "committed_source_record_pk")
        if result.get("new_source_record_pk") != committed_source_record_pk:
            raise RepairMutationDriftError("repair committed source identity differs")
        expected_ids = {
            "rollback_image_id": request.rollback_image_id,
            "checkpoint_id": request.checkpoint_id,
            "outbox_event_id": request.outbox_event_id,
        }
        if any(result.get(key) != value for key, value in expected_ids.items()):
            raise RepairMutationDriftError("repair committed bundle identity differs")
        try:
            replay = atomic_result_from_record(row, replayed=True)
        except RuntimeError as exc:
            raise RepairMutationDriftError("repair committed bundle payload differs") from exc
        assert replay.mutation is not None
        if replay.mutation.outcome == "applied":
            owner_row = tx.run(
                READ_REPAIRED_OWNER_IDS,
                source_record_pk=committed_source_record_pk,
            ).single()
            if owner_row is None or not isinstance(owner_row["owner_ids"], list):
                raise RepairMutationDriftError("repair authority readback is malformed")
            owner_ids = tuple(
                sorted(value for value in owner_row["owner_ids"] if isinstance(value, str))
            )
            current_evidence = _authority_evidence(
                tx,
                request,
                owner_ids,
                source_record_pk=committed_source_record_pk,
            )
            current_evidence_digest = authority_evidence_digest(
                {
                    "current_owner_ids": list(owner_ids),
                    "evidence": [item.to_dict() for item in current_evidence],
                }
            )
            if current_evidence_digest != replay.mutation.evidence_digest:
                raise RepairMutationDriftError("repair authority changed after commit")
        assert replay.checkpoint is not None
        assert replay.outbox_event is not None
        checkpoint_digest = object_digest(
            b"crm-deal-identity-repair-checkpoint-v1\x00",
            {"result_digest": replay.mutation.result_digest},
        )
        if replay.checkpoint.checkpoint_digest != checkpoint_digest:
            raise RepairMutationDriftError("repair checkpoint digest differs")
        if replay.outbox_event.payload_digest != build_outbox_digest(
            request, replay.mutation.result_digest
        ):
            raise RepairMutationDriftError("repair outbox digest differs")
        observed_state = _postcondition_state(tx, committed_source_record_pk)
        if repaired_state_digest(observed_state) != replay.repaired_state_digest:
            raise RepairMutationDriftError("repair desired state changed after commit")
        return replay

    def _guard(self, tx: ManagedTransaction, request: RepairMutationCommand) -> Record:
        row = tx.run(
            LOCK_AND_ASSERT_REPAIR_MUTATION_GUARD,
            **_guard_parameters(request),
        ).single()
        if row is None:
            raise RepairMutationAuthorityError(
                "repair run/unit/fence/control/source guard rejected"
            )
        return row

    def _create_source(
        self,
        tx: ManagedTransaction,
        request: RepairMutationCommand,
        plan: RepairMutationPlan,
    ) -> None:
        if plan.source_record_payload is None:
            row = tx.run(
                CREATE_UNRECONSTRUCTABLE_REVIEW_SOURCE_RECORD,
                old_source_record_pk=request.inventory.source_record_pk,
                new_source_record_pk=plan.source_record_pk,
                source_record_version=str(plan.source_record_version),
                source_version_key=encode_source_version_key(
                    "bitrix_chat",
                    request.inventory.source_record_id,
                    str(plan.source_record_version),
                    source_instance_id=request.source_instance_id,
                ),
                mutation_id=request.mutation_id,
            ).single()
        else:
            row = tx.run(CREATE_REPAIRED_SOURCE_RECORD, **_source_values(request, plan)).single()
        if row is None or row["source_record_pk"] != plan.source_record_pk:
            raise RepairMutationDriftError("repair source-version CAS rejected")

    def _create_decision(
        self,
        tx: ManagedTransaction,
        request: RepairMutationCommand,
        plan: RepairMutationPlan,
        decision_id: str,
    ) -> None:
        row = tx.run(
            CREATE_REPAIR_DECISION,
            new_source_record_pk=plan.source_record_pk,
            match_decision_id=decision_id,
            mutation_id=request.mutation_id,
            decision="merge" if plan.disposition == "applied" else "review",
            reason_codes=list(plan.reason_codes),
            review_candidate_person_ids=list(plan.current_owner_ids),
            feature_snapshot=_canonical_json(
                {
                    "authority_digest": plan.authority_digest,
                    "repair_mutation_id": request.mutation_id,
                }
            ),
        ).single()
        if row is None:
            raise RuntimeError("repair decision creation failed")

    def _stage_outcome(
        self,
        tx: ManagedTransaction,
        request: RepairMutationCommand,
        envelope: SourceRecordEnvelope | None,
        plan: RepairMutationPlan,
        decision_id: str,
    ) -> None:
        if plan.disposition == "applied":
            assert plan.selected_person_id is not None
            assert envelope is not None
            _stage_active(tx, request, envelope, plan, decision_id)
            lifecycle_query = ACTIVATE_REPAIRED_SOURCE_RECORD
        else:
            _stage_review(tx, request, envelope, plan, decision_id)
            lifecycle_query = STAGE_REVIEW_SOURCE_RECORD
        row = tx.run(
            lifecycle_query,
            old_source_record_pk=request.inventory.source_record_pk,
            new_source_record_pk=plan.source_record_pk,
        ).single()
        if row is None:
            raise RepairMutationDriftError("repair source lifecycle CAS rejected")

    def _fail(self, stage: MutationFailureStage) -> None:
        if self._failpoint is not None:
            self._failpoint(stage)
