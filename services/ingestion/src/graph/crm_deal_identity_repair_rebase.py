"""Atomic, fail-closed #424 allocation-boundary rebase repository."""

from __future__ import annotations

from dataclasses import asdict
from typing import cast

from neo4j import ManagedTransaction

from src.crm_deal_identity_repair.allocation import AllocationPlan, allocation_origin_hmac
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.execution_boundary_models import RepairBoundarySnapshot
from src.crm_deal_identity_repair.execution_status_models import RepairQualificationRun
from src.crm_deal_identity_repair.rebase import (
    RepairBoundaryRebaseRequest,
    RepairBoundaryRebaseResult,
    rebase_audit_digest,
    rebase_hmac,
    rebase_receipt_digest,
    required_rebase_int,
    required_rebase_string,
    validate_rebase_hmac,
)
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_control import (
    _allocation_receipt_digest,
    _lease,
    _mapping,
    _required_int,
    _validate_allocation_receipt,
)
from src.graph.crm_deal_identity_repair_ledger_records import stored_qualification_from_record
from src.graph.crm_deal_identity_repair_status_snapshot import status_snapshot_from_transaction
from src.graph.queries.crm_deal_identity_repair_ledger import GET_REPAIR_RUN
from src.graph.queries.crm_deal_identity_repair_rebase import (
    ADVANCE_REBASE_CONTROL,
    COMMIT_REBASE_BOUNDARY,
    LOCK_REBASE_AUTHORITY,
    READ_EFFECTIVE_REBASE_BOUNDARY,
    READ_REBASE_GUARDS,
    READ_REBASE_REPLAY,
)
from src.models import JsonValue

def _snapshot_components(snapshot: RepairBoundarySnapshot) -> dict[str, JsonValue]:
    return {
        "source_records_digest": snapshot.source_records_digest,
        "source_instance_digest": snapshot.source_instance_digest,
        "stale_run_evidence_digest": snapshot.stale_run_evidence_digest,
        "control_digest": snapshot.control_digest,
        "inventory_digest": snapshot.inventory_digest,
        "inventory_row_count": snapshot.inventory_row_count,
        "eligible_unit_count": snapshot.eligible_unit_count,
        "negative_control_count": snapshot.negative_control_count,
    }


def _control_components(control: object) -> dict[str, JsonValue]:
    values = _mapping(control, "effective rebase control")
    return {
        "source_records_digest": required_rebase_string(
            values, "sealed_source_records_digest"
        ),
        "source_instance_digest": required_rebase_string(
            values, "sealed_source_instance_digest"
        ),
        "stale_run_evidence_digest": required_rebase_string(
            values, "sealed_stale_run_evidence_digest"
        ),
        "control_digest": required_rebase_string(values, "sealed_control_digest"),
        "inventory_digest": required_rebase_string(values, "sealed_inventory_digest"),
        "inventory_row_count": required_rebase_int(values, "sealed_inventory_row_count"),
        "eligible_unit_count": required_rebase_int(values, "sealed_eligible_unit_count"),
        "negative_control_count": required_rebase_int(values, "sealed_negative_control_count"),
    }


def _same_rebase_authority(
    left: RepairBoundaryRebaseResult, right: RepairBoundaryRebaseResult
) -> bool:
    return (
        left.lease == right.lease
        and left.previous_boundary_digest == right.previous_boundary_digest
        and left.replacement_boundary_digest == right.replacement_boundary_digest
        and left.receipt_digest == right.receipt_digest
        and left.audit_digest == right.audit_digest
    )


class CrmDealRepairRebaseRepository:
    """Owns only the #424 replacement-seal transaction and authenticated reader."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def rebase_boundary(
        self,
        request: RepairBoundaryRebaseRequest,
        *,
        plan: AllocationPlan,
        fresh_artifact_manifest_hmac: str,
        fresh_inventory_digest: str,
        fresh_producer_repository_sha: str,
        fresh_producer_image_digest: str,
        approval_key_id: str,
        approval_secret: bytes,
    ) -> RepairBoundaryRebaseResult:
        """Atomically rotate only an intact allocated run's effective boundary."""
        if not approval_key_id or not approval_secret:
            raise ValueError("rebase signing configuration is missing")
        units: list[JsonValue] = [cast(JsonValue, asdict(unit)) for unit in plan.units]
        unit_ids = [unit.unit_id for unit in plan.units]
        unit_set_digest = object_digest(
            b"crm-deal-identity-repair-allocation-unit-set-v1\x00", {"units": units}
        )
        if len(unit_ids) != len(set(unit_ids)):
            raise RuntimeError("rebase expected allocation has duplicate unit IDs")
        replay = self._read_rebase_replay(
            request,
            fresh_artifact_manifest_hmac=fresh_artifact_manifest_hmac,
            fresh_inventory_digest=fresh_inventory_digest,
            fresh_producer_repository_sha=fresh_producer_repository_sha,
            fresh_producer_image_digest=fresh_producer_image_digest,
            approval_key_id=approval_key_id,
            approval_secret=approval_secret,
        )
        if replay is not None:
            return replay

        def work(tx: ManagedTransaction) -> RepairBoundaryRebaseResult:
            locked = tx.run(
                LOCK_REBASE_AUTHORITY,
                repair_id=request.control.repair_id,
                run_id=request.control.run_id,
                boundary_digest=plan.completion.boundary_digest,
                rebase_request_digest=request.request_digest,
            ).single()
            if locked is None:
                raise RuntimeError("repair rebase lock authority rejected")
            qualification = tx.run(GET_REPAIR_RUN, repair_id=request.control.repair_id).single()
            if qualification is None:
                raise RuntimeError("repair rebase qualification boundary is missing")
            stored = stored_qualification_from_record(request.control.repair_id, qualification)
            if stored.run.run_id != request.control.run_id:
                raise RuntimeError("repair rebase requires the exact qualified run")
            before = status_snapshot_from_transaction(
                tx,
                stored.run.source_instance_id,
                stored.run.control_instance_id,
                stored.source_record_pks,
            )
            if before.boundary_digest != request.expected_observed_boundary_digest:
                raise RuntimeError("repair rebase observed boundary differs from the request")
            guard = tx.run(
                READ_REBASE_GUARDS,
                repair_id=request.control.repair_id,
                run_id=request.control.run_id,
                owner_id=request.control.owner_id,
                token_digest=request.control.token_digest,
                expected_revision=request.control.expected_revision,
                boundary_digest=stored.run.boundary_digest,
                completion_id=plan.completion.completion_id,
                overlay_digest=plan.completion.overlay_digest,
                allocation_digest=plan.completion.allocation_digest,
                unit_count=plan.completion.unit_count,
                unit_ids=unit_ids,
                unit_set_digest=unit_set_digest,
                units=units,
                approval_key_id=approval_key_id,
            ).single()
            if guard is None:
                raise RuntimeError("repair rebase allocation or zero-execution guard rejected")
            control = _mapping(guard["control"], "rebase control")
            completion = _mapping(guard["completion"], "rebase completion")
            _validate_allocation_receipt(
                completion,
                origin_key_id=approval_key_id,
                origin_secret=approval_secret,
            )
            if (
                before.inventory_digest,
                before.inventory_row_count,
                before.eligible_unit_count,
                before.negative_control_count,
            ) != (
                fresh_inventory_digest,
                stored.run.inventory_row_count,
                stored.run.eligible_unit_count,
                stored.run.negative_control_count,
            ):
                raise RuntimeError("repair rebase fresh inventory differs from current allocation")
            if before.negative_control_count != 6:
                raise RuntimeError("repair rebase requires exactly six negative controls")
            if before.source_instance_digest != required_rebase_string(
                control, "sealed_source_instance_digest"
            ):
                raise RuntimeError("repair rebase source-instance evidence changed")
            advanced = tx.run(
                ADVANCE_REBASE_CONTROL,
                repair_id=request.control.repair_id,
                run_id=request.control.run_id,
                owner_id=request.control.owner_id,
                token_digest=request.control.token_digest,
                expected_revision=request.control.expected_revision,
                completion_id=plan.completion.completion_id,
            ).single()
            if advanced is None:
                raise RuntimeError("repair rebase lifecycle compare-and-set was rejected")
            revision = _required_int(advanced["revision"], "rebase revision")
            after = status_snapshot_from_transaction(
                tx,
                stored.run.source_instance_id,
                stored.run.control_instance_id,
                stored.source_record_pks,
            )
            previous_boundary = required_rebase_string(control, "sealed_boundary_digest")
            previous_receipt = required_rebase_string(completion, "receipt_digest")
            previous_origin = required_rebase_string(completion, "allocation_origin_hmac")
            completion_id = required_rebase_string(completion, "completion_id")
            allocation_digest = required_rebase_string(completion, "allocation_digest")
            request_digest = required_rebase_string(completion, "request_digest")
            rebase_receipt = rebase_receipt_digest(
                request_digest=request.request_digest,
                run_id=stored.run.run_id,
                completion_id=completion_id,
                previous_boundary_digest=previous_boundary,
                replacement_boundary_digest=after.boundary_digest,
                revision=revision,
            )
            allocation_receipt = _allocation_receipt_digest(
                control_instance_id=stored.run.control_instance_id,
                run_id=stored.run.run_id,
                owner_id=request.control.owner_id,
                token_digest=request.control.token_digest,
                revision=revision,
                boundary_digest=stored.run.boundary_digest,
                sealed_boundary_digest=after.boundary_digest,
            )
            origin_hmac = allocation_origin_hmac(
                secret=approval_secret,
                key_id=approval_key_id,
                control_instance_id=stored.run.control_instance_id,
                run_id=stored.run.run_id,
                owner_id=request.control.owner_id,
                token_digest=request.control.token_digest,
                revision=revision,
                boundary_digest=stored.run.boundary_digest,
                sealed_boundary_digest=after.boundary_digest,
                completion_id=completion_id,
                overlay_digest=plan.completion.overlay_digest,
                allocation_digest=allocation_digest,
                unit_count=plan.completion.unit_count,
                unit_set_digest=unit_set_digest,
                request_digest=request_digest,
            )
            audit_digest = rebase_audit_digest(
                request=request,
                completion_id=completion_id,
                allocation_digest=allocation_digest,
                unit_set_digest=unit_set_digest,
                fresh_artifact_manifest_hmac=fresh_artifact_manifest_hmac,
                fresh_inventory_digest=fresh_inventory_digest,
                fresh_producer_repository_sha=fresh_producer_repository_sha,
                fresh_producer_image_digest=fresh_producer_image_digest,
                previous_boundary_digest=previous_boundary,
                replacement_boundary_digest=after.boundary_digest,
                replacement_components=_snapshot_components(after),
                previous_receipt_digest=previous_receipt,
                previous_origin_hmac=previous_origin,
                revision=revision,
            )
            committed = tx.run(
                COMMIT_REBASE_BOUNDARY,
                repair_id=request.control.repair_id,
                run_id=request.control.run_id,
                owner_id=request.control.owner_id,
                token_digest=request.control.token_digest,
                revision=revision,
                completion_id=completion_id,
                rebase_request_digest=request.request_digest,
                approval_id=request.approval_id,
                fresh_artifact_id=request.fresh_artifact_id,
                fresh_artifact_manifest_hmac=fresh_artifact_manifest_hmac,
                fresh_inventory_digest=fresh_inventory_digest,
                fresh_producer_repository_sha=fresh_producer_repository_sha,
                fresh_producer_image_digest=fresh_producer_image_digest,
                expected_observed_boundary_digest=request.expected_observed_boundary_digest,
                previous_boundary_digest=previous_boundary,
                replacement_boundary_digest=after.boundary_digest,
                replacement_source_records_digest=after.source_records_digest,
                replacement_source_instance_digest=after.source_instance_digest,
                replacement_stale_run_evidence_digest=after.stale_run_evidence_digest,
                replacement_control_digest=after.control_digest,
                replacement_inventory_digest=after.inventory_digest,
                replacement_inventory_row_count=after.inventory_row_count,
                replacement_eligible_unit_count=after.eligible_unit_count,
                replacement_negative_control_count=after.negative_control_count,
                replacement_receipt_digest=allocation_receipt,
                rebase_receipt_digest=rebase_receipt,
                replacement_origin_hmac=origin_hmac,
                rebase_audit_digest=audit_digest,
                approval_key_id=approval_key_id,
                rebase_hmac=rebase_hmac(
                    secret=approval_secret,
                    key_id=approval_key_id,
                    audit_digest=audit_digest,
                ),
            ).single()
            if committed is None:
                raise RuntimeError("repair rebase seal compare-and-set was rejected")
            return self._rebase_result(
                committed,
                request=request,
                approval_key_id=approval_key_id,
                approval_secret=approval_secret,
                replayed=False,
            )

        try:
            committed = self._client.execute_write(work)
        except Exception:
            recovered = self._read_rebase_replay(
                request,
                fresh_artifact_manifest_hmac=fresh_artifact_manifest_hmac,
                fresh_inventory_digest=fresh_inventory_digest,
                fresh_producer_repository_sha=fresh_producer_repository_sha,
                fresh_producer_image_digest=fresh_producer_image_digest,
                approval_key_id=approval_key_id,
                approval_secret=approval_secret,
            )
            if recovered is not None:
                return recovered
            raise
        durable = self._read_rebase_replay(
            request,
            fresh_artifact_manifest_hmac=fresh_artifact_manifest_hmac,
            fresh_inventory_digest=fresh_inventory_digest,
            fresh_producer_repository_sha=fresh_producer_repository_sha,
            fresh_producer_image_digest=fresh_producer_image_digest,
            approval_key_id=approval_key_id,
            approval_secret=approval_secret,
        )
        if durable is None or not _same_rebase_authority(committed, durable):
            raise RuntimeError("repair rebase durable readback differs")
        return committed

    def _read_rebase_replay(
        self,
        request: RepairBoundaryRebaseRequest,
        *,
        fresh_artifact_manifest_hmac: str,
        fresh_inventory_digest: str,
        fresh_producer_repository_sha: str,
        fresh_producer_image_digest: str,
        approval_key_id: str,
        approval_secret: bytes,
    ) -> RepairBoundaryRebaseResult | None:
        def work(tx: ManagedTransaction) -> RepairBoundaryRebaseResult | None:
            record = tx.run(
                READ_REBASE_REPLAY,
                repair_id=request.control.repair_id,
                run_id=request.control.run_id,
                owner_id=request.control.owner_id,
                token_digest=request.control.token_digest,
                rebase_request_digest=request.request_digest,
                approval_id=request.approval_id,
                fresh_artifact_id=request.fresh_artifact_id,
                expected_observed_boundary_digest=request.expected_observed_boundary_digest,
            ).single()
            if record is None:
                return None
            completion = _mapping(record["completion"], "rebase replay completion")
            if (
                required_rebase_string(completion, "rebase_fresh_artifact_manifest_hmac")
                != fresh_artifact_manifest_hmac
                or required_rebase_string(completion, "rebase_fresh_inventory_digest")
                != fresh_inventory_digest
                or required_rebase_string(completion, "rebase_fresh_producer_repository_sha")
                != fresh_producer_repository_sha
                or required_rebase_string(completion, "rebase_fresh_producer_image_digest")
                != fresh_producer_image_digest
            ):
                raise RuntimeError("repair rebase replay fresh artifact evidence differs")
            return self._rebase_result(
                record,
                request=request,
                approval_key_id=approval_key_id,
                approval_secret=approval_secret,
                replayed=True,
            )

        return self._client.execute_read(work)

    @staticmethod
    def _rebase_result(
        record: object,
        *,
        request: RepairBoundaryRebaseRequest,
        approval_key_id: str,
        approval_secret: bytes,
        replayed: bool,
    ) -> RepairBoundaryRebaseResult:
        value = _mapping(record, "rebase result")
        control = _mapping(value.get("control"), "rebase control")
        completion = _mapping(value.get("completion"), "rebase completion")
        audit = required_rebase_string(completion, "rebase_audit_digest")
        if required_rebase_string(completion, "rebase_key_id") != approval_key_id:
            raise RuntimeError("repair rebase key identity is invalid")
        validate_rebase_hmac(
            secret=approval_secret,
            key_id=approval_key_id,
            audit_digest=audit,
            supplied_hmac=required_rebase_string(completion, "rebase_hmac"),
        )
        revision = required_rebase_int(completion, "rebase_revision")
        expected_audit = rebase_audit_digest(
            request=request,
            completion_id=required_rebase_string(completion, "completion_id"),
            allocation_digest=required_rebase_string(completion, "allocation_digest"),
            unit_set_digest=required_rebase_string(completion, "unit_set_digest"),
            fresh_artifact_manifest_hmac=required_rebase_string(
                completion, "rebase_fresh_artifact_manifest_hmac"
            ),
            fresh_inventory_digest=required_rebase_string(
                completion, "rebase_fresh_inventory_digest"
            ),
            fresh_producer_repository_sha=required_rebase_string(
                completion, "rebase_fresh_producer_repository_sha"
            ),
            fresh_producer_image_digest=required_rebase_string(
                completion, "rebase_fresh_producer_image_digest"
            ),
            previous_boundary_digest=required_rebase_string(
                completion, "rebase_previous_boundary_digest"
            ),
            replacement_boundary_digest=required_rebase_string(
                completion, "rebase_replacement_boundary_digest"
            ),
            replacement_components=_control_components(control),
            previous_receipt_digest=required_rebase_string(
                completion, "rebase_previous_receipt_digest"
            ),
            previous_origin_hmac=required_rebase_string(
                completion, "rebase_previous_origin_hmac"
            ),
            revision=revision,
        )
        if audit != expected_audit:
            raise RuntimeError("repair rebase audit digest is invalid")
        receipt = rebase_receipt_digest(
            request_digest=request.request_digest,
            run_id=request.control.run_id,
            completion_id=required_rebase_string(completion, "completion_id"),
            previous_boundary_digest=required_rebase_string(
                completion, "rebase_previous_boundary_digest"
            ),
            replacement_boundary_digest=required_rebase_string(
                completion, "rebase_replacement_boundary_digest"
            ),
            revision=revision,
        )
        if receipt != required_rebase_string(completion, "rebase_receipt_digest"):
            raise RuntimeError("repair rebase receipt digest is invalid")
        _validate_allocation_receipt(
            completion,
            origin_key_id=approval_key_id,
            origin_secret=approval_secret,
        )
        if (
            required_rebase_string(control, "sealed_boundary_digest")
            != required_rebase_string(completion, "rebase_replacement_boundary_digest")
            or required_rebase_int(control, "sealed_revision") != revision
        ):
            raise RuntimeError("repair rebase effective seal is inconsistent")
        return RepairBoundaryRebaseResult(
            _lease(control),
            required_rebase_string(completion, "rebase_previous_boundary_digest"),
            required_rebase_string(completion, "rebase_replacement_boundary_digest"),
            receipt,
            audit,
            replayed,
        )

    def effective_boundary_digest(
        self,
        run: RepairQualificationRun,
        *,
        approval_key_id: str | None,
        approval_secret: bytes | None,
    ) -> str | None:
        """Read the effective seal in its own transaction for status callers."""
        return self._client.execute_read(
            lambda tx: self.effective_boundary_digest_from_transaction(
                tx,
                run,
                approval_key_id=approval_key_id,
                approval_secret=approval_secret,
            )
        )

    def effective_boundary_digest_from_transaction(
        self,
        tx: ManagedTransaction,
        run: RepairQualificationRun,
        *,
        approval_key_id: str | None,
        approval_secret: bytes | None,
    ) -> str | None:
        """Authenticate a replacement seal without opening a nested transaction."""
        repair_id = run.repair_id
        run_id = run.run_id
        record = tx.run(READ_EFFECTIVE_REBASE_BOUNDARY, run_id=run_id).single()
        if record is None:
            return None
        control = _mapping(record["control"], "effective rebase control")
        _mapping(record["dispatch"], "effective rebase dispatch")
        raw_completions = record["completions"]
        if not isinstance(raw_completions, list):
            raise RuntimeError("repair effective rebase completion is malformed")
        completions = [_mapping(value, "effective rebase completion") for value in raw_completions]
        if len(completions) != 1:
            raise RuntimeError("repair effective rebase completion is ambiguous")
        completion = completions[0]
        marker = completion.get("rebase_request_digest")
        rebase_keys = tuple(key for key in completion if str(key).startswith("rebase_"))
        if marker is None:
            if rebase_keys:
                raise RuntimeError("repair effective rebase marker is malformed")
            return None
        if not isinstance(marker, str) or not marker:
            raise RuntimeError("repair effective rebase marker is malformed")
        if not approval_key_id or not approval_secret:
            raise RuntimeError("repair rebase effective seal cannot be authenticated")
        from src.crm_deal_identity_repair.control_models import _trusted_request_from_durable_digest

        request = RepairBoundaryRebaseRequest(
            _trusted_request_from_durable_digest(
                repair_id,
                run_id,
                required_rebase_string(control, "owner_id"),
                required_rebase_string(control, "token_digest"),
                required_rebase_int(completion, "rebase_previous_revision"),
            ),
            required_rebase_string(completion, "rebase_approval_id"),
            required_rebase_string(completion, "rebase_fresh_artifact_id"),
            required_rebase_string(completion, "rebase_expected_observed_boundary_digest"),
        )
        if request.request_digest != marker:
            raise RuntimeError("repair effective rebase request differs")
        result = self._rebase_result(
            {"control": control, "completion": completion},
            request=request,
            approval_key_id=approval_key_id,
            approval_secret=approval_secret,
            replayed=True,
        )
        return result.replacement_boundary_digest
