"""One managed Neo4j transaction for a qualified CRM-deal repair unit."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from neo4j import ManagedTransaction, Record

from src.crm_deal_identity_repair.digests import (
    mutation_request_digest,
    mutation_result_digest,
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
    external_authority_evidence_digest,
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
from src.graph.crm_deal_identity_repair_mutation_records import (
    atomic_result_from_record,
    canonical_payload,
)
from src.graph.queries.crm_deal_identity_repair_mutation import (
    ACTIVATE_REPAIRED_SOURCE_RECORD,
    CREATE_REPAIR_DECISION,
    CREATE_REPAIRED_SOURCE_RECORD,
    CREATE_UNRECONSTRUCTABLE_REVIEW_SOURCE_RECORD,
    FIND_COMMITTED_REPAIR_MUTATION,
    LOCK_AND_ASSERT_REPAIR_MUTATION_FINAL_GUARD,
    LOCK_AND_ASSERT_REPAIR_MUTATION_GUARD,
    LOCK_REPAIR_MUTATION_UNIT,
    PERSIST_REPAIR_MUTATION_LEDGER,
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
        rollback = _rollback_payload(request, plan, snapshot, expected_state, parsed.envelope)
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
        self._final_guard(tx, request, plan)
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
        except (RuntimeError, ValueError) as exc:
            raise RepairMutationDriftError("repair committed bundle payload differs") from exc
        assert replay.mutation is not None
        assert replay.rollback_image is not None
        assert replay.checkpoint is not None
        assert replay.outbox_event is not None
        image_values = _record_object(row, "image")
        payload_json = image_values.get("payload_json")
        if not isinstance(payload_json, str) or not payload_json:
            raise RepairMutationDriftError("repair committed rollback image is malformed")
        payload = canonical_payload(payload_json)
        payload_body = payload.get("payload")
        if not isinstance(payload_body, dict):
            raise RepairMutationDriftError("repair committed rollback payload is malformed")
        request_body = payload_body.get("request")
        authority_context = payload_body.get("authority_context")
        desired_state = payload_body.get("desired_state")
        if not isinstance(request_body, dict) or not isinstance(authority_context, dict):
            raise RepairMutationDriftError("repair committed replay context is malformed")
        if not isinstance(desired_state, dict):
            raise RepairMutationDriftError("repair desired-state context is malformed")
        committed_request_digest = result.get("request_digest")
        if not isinstance(committed_request_digest, str):
            raise RepairMutationDriftError("repair request digest is malformed")
        if mutation_request_digest(request_body) != committed_request_digest:
            raise RepairMutationDriftError("repair request digest differs")
        owner_values = authority_context.get("current_owner_ids")
        if not isinstance(owner_values, list) or not all(
            isinstance(value, str) for value in owner_values
        ):
            raise RepairMutationDriftError("repair authority owner context is malformed")
        owner_ids = tuple(sorted(value for value in owner_values if isinstance(value, str)))
        retired_values = desired_state.get("retired_source_record_pks")
        if not isinstance(retired_values, list) or not all(
            isinstance(value, str) and value for value in retired_values
        ):
            raise RepairMutationDriftError("repair retired-source context is malformed")
        retired_source_record_pks = tuple(
            sorted({value for value in retired_values if isinstance(value, str)})
        )
        # Read external authority against the committed replacement, not the
        # retired source.  The mutation intentionally changes the retired
        # source's lifecycle, links, and descendant contamination; those
        # mutation-owned changes must not turn an otherwise exact retry into
        # authority drift.  The replacement retains the source/control scope
        # that the authority query locks and verifies.
        current_evidence = _authority_evidence(
            tx,
            request,
            owner_ids,
            source_record_pk=committed_source_record_pk,
        )
        external_evidence_digest = external_authority_evidence_digest(
            owner_ids,
            current_evidence,
            mutation_id=request.mutation_id,
            excluded_source_record_pks=(
                *retired_source_record_pks,
                committed_source_record_pk,
            ),
        )
        committed_external_digest = authority_context.get("external_authority_digest")
        if not isinstance(committed_external_digest, str):
            raise RepairMutationDriftError("repair external authority digest is malformed")
        if external_evidence_digest != committed_external_digest:
            raise RepairMutationDriftError("repair authority changed after commit")
        recomputed_result_digest = mutation_result_digest(
            {
                "request_digest": committed_request_digest,
                "authority_digest": replay.mutation.evidence_digest,
                "rollback_image_digest": replay.rollback_image.image_digest,
                "expected_repaired_digest": replay.rollback_image.expected_repaired_digest,
                "desired_state": desired_state,
            }
        )
        if recomputed_result_digest != replay.mutation.result_digest:
            raise RepairMutationDriftError("repair result digest differs")
        if (
            replay.rollback_image.rollback_image_id != request.rollback_image_id
            or replay.checkpoint.checkpoint_id != request.checkpoint_id
            or replay.outbox_event.event_id != request.outbox_event_id
        ):
            raise RepairMutationDriftError("repair child ledger identity differs")
        for artifact in (
            replay.mutation,
            replay.rollback_image,
            replay.checkpoint,
            replay.outbox_event,
        ):
            if (
                artifact.generation != request.unit.generation
                or artifact.sequence != request.unit.sequence
                or artifact.attempt != request.unit.attempt
                or artifact.owner_id != request.fence.owner_id
                or artifact.boundary_digest != request.unit.boundary_digest
            ):
                raise RepairMutationDriftError("repair committed ledger scope differs")
        if (
            replay.mutation.fence_token != request.fence.token
            or replay.rollback_image.fence_token != request.fence.token
            or replay.checkpoint.fence_token != request.fence.token
            or replay.outbox_event.delivery_token != request.fence.token
            or replay.mutation.unit_fingerprint != request.unit.inventory_fingerprint
        ):
            raise RepairMutationDriftError("repair committed ledger fence differs")
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

    def _final_guard(
        self,
        tx: ManagedTransaction,
        request: RepairMutationCommand,
        plan: RepairMutationPlan,
    ) -> None:
        params = _guard_parameters(request)
        params["new_source_record_pk"] = plan.source_record_pk
        params["new_lifecycle_status"] = (
            "active" if plan.disposition == "applied" else "pending_review"
        )
        row = tx.run(LOCK_AND_ASSERT_REPAIR_MUTATION_FINAL_GUARD, **params).single()
        if row is None:
            raise RepairMutationAuthorityError("repair final fence/control/source guard rejected")

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
