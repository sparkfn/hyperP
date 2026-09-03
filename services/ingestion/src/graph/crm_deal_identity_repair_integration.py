"""Neo4j CAS adapter for the #313 repair orchestration lifecycle."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

from neo4j import ManagedTransaction

from src.crm_deal_identity_repair.allocation import allocation_origin_hmac
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.execution_records import RepairFence, RepairUnit
from src.crm_deal_identity_repair.execution_status_models import RepairQualificationRun
from src.crm_deal_identity_repair.integration_models import (
    RepairIntegrationRequest,
    rollback_status_receipt_digest,
)
from src.crm_deal_identity_repair.integration_service import (
    RepairIntegrationAuthority,
    RepairIntegrationContext,
)
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.crm_deal_identity_repair.rollback_models import RepairRollbackAuthorization
from src.crm_deal_identity_repair.verification_equations import (
    RepairRunEquationCommand,
    RepairRunEquationResult,
)
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_ledger_records import canonical_json_text
from src.graph.crm_deal_identity_repair_rollback_records import (
    fence_from_properties,
    image_from_properties,
    mutation_from_properties,
    unit_from_properties,
)
from src.graph.crm_deal_identity_repair_verification_run import read_run_equation
from src.graph.queries.crm_deal_identity_repair_integration import (
    ACCEPT_AND_RELEASE,
    CLAIM_ADMITTED_FENCE,
    CREATE_AND_READ_ROLLBACK_AUTHORIZATION,
    READ_ACCEPTANCE,
    READ_ANY_EXECUTION_EVIDENCE,
    READ_AUTHORITY,
    READ_FENCE,
    READ_RELEASE_AUTHORITY,
    READ_RUN_RECEIPTS,
    READ_RUN_SETS,
    READ_TERMINAL_ROLLBACK_REPLAY,
    READ_UNIT_EXECUTION_EVIDENCE,
    READ_UNIT_FOR_ADMISSION,
    RELEASE_DISPATCH,
    RELEASE_TERMINAL_FENCE,
    STORE_ROLLBACK_RECEIPT,
)
from src.models import JsonValue


class CrmDealRepairIntegrationRepository:
    """Owns only integration CAS records; component services retain domain behavior."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def load_authority(
        self,
        request: RepairIntegrationRequest,
        run: RepairQualificationRun,
        overlay_digest: str,
        origin_key_id: str,
        origin_secret: bytes,
    ) -> RepairIntegrationAuthority:
        params: dict[str, JsonValue] = {
            "repair_id": request.control.repair_id,
            "run_id": request.control.run_id,
            "owner_id": request.control.owner_id,
            "token_digest": request.control.token_digest,
            "revision": request.control.expected_revision,
            "boundary_digest": run.boundary_digest,
            "source_instance_id": run.source_instance_id,
            "control_instance_id": run.control_instance_id,
            "qualification_identity": run.qualification_identity,
            "manifest_digest": run.manifest_digest,
            "artifact_id": run.artifact_id,
            "artifact_manifest_hmac": run.artifact_manifest_hmac,
            "manifest_json": canonical_json_text(run.manifest.to_dict(), "manifest"),
            "inventory_digest": run.inventory_digest,
            "inventory_row_count": run.inventory_row_count,
            "eligible_unit_count": run.eligible_unit_count,
            "negative_control_count": run.negative_control_count,
            "request_digest": request.request_digest,
        }

        def work(tx: ManagedTransaction) -> RepairIntegrationAuthority:
            query = (
                READ_RELEASE_AUTHORITY
                if request.operation in {"accept", "release-dispatch"}
                else READ_AUTHORITY
            )
            record = tx.run(query, **params).single()  # type: ignore[arg-type]
            if record is None:
                raise RuntimeError("repair current allocation/control/dispatch authority rejected")
            completion = _mapping(record["completion"], "allocation completion")
            authority = _authority_from_completion(completion, record["sealed_boundary_digest"])
            if (
                authority.overlay_digest != overlay_digest
                or authority.allocation_origin_key_id != origin_key_id
            ):
                raise RuntimeError("repair allocation overlay or origin key differs")
            expected = allocation_origin_hmac(
                secret=origin_secret,
                key_id=origin_key_id,
                control_instance_id=run.control_instance_id,
                run_id=run.run_id,
                owner_id=request.control.owner_id,
                token_digest=request.control.token_digest,
                revision=authority.allocation_revision,
                boundary_digest=run.boundary_digest,
                sealed_boundary_digest=authority.sealed_boundary_digest,
                completion_id=authority.completion_id,
                overlay_digest=authority.overlay_digest,
                allocation_digest=authority.allocation_digest,
                unit_count=_integer(completion.get("unit_count"), "allocation unit count"),
                unit_set_digest=authority.allocation_unit_set_digest,
                request_digest=authority.allocation_request_digest,
            )
            if not hmac.compare_digest(authority.allocation_origin_hmac, expected):
                raise RuntimeError("repair allocation origin HMAC is invalid")
            return authority

        return self._client.execute_read(work)

    def has_execution_evidence(
        self, request: RepairIntegrationRequest, run: RepairQualificationRun
    ) -> bool:
        """Return true only for the requested unit's existing #309 evidence."""
        if request.unit_id is None:
            return False
        return self._read_execution_evidence(
            READ_UNIT_EXECUTION_EVIDENCE, {"run_id": run.run_id, "unit_id": request.unit_id}
        )

    def has_any_execution_evidence(self, run: RepairQualificationRun) -> bool:
        """Tell initial-unit admission from a run that already has another authorized unit."""
        return self._read_execution_evidence(READ_ANY_EXECUTION_EVIDENCE, {"run_id": run.run_id})

    def assert_next_unit_boundary(
        self,
        request: RepairIntegrationRequest,
        run: RepairQualificationRun,
        inventory: tuple[RepairInventoryItem, ...],
    ) -> None:
        """Use #311 accounting to admit a new unit after earlier authorized mutations."""
        command = RepairRunEquationCommand(
            run.repair_id,
            run.run_id,
            run.boundary_digest,
            inventory,
            run.inventory_digest,
            run.source_instance_id,
            run.control_instance_id,
            request.request_digest,
        )

        def work(tx: ManagedTransaction) -> None:
            equation = read_run_equation(tx, command)
            if (
                equation.drifted_units
                or equation.failed_units
                or equation.drifted_negative_controls
                or equation.missing_negative_controls
                or equation.stamped_negative_controls
                or equation.unsupported_multi_links
                or equation.failed_secondaries
                or equation.pending_secondaries
                or equation.unexplained_secondary_remainder
            ):
                raise RuntimeError("repair integration next-unit boundary drift detected")

        self._client.execute_read(work)

    def _read_execution_evidence(self, query: str, params: dict[str, JsonValue]) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            record = tx.run(query, **params).single()  # type: ignore[arg-type]
            return record is not None and record["exists"] is True

        return self._client.execute_read(work)

    def admit_and_claim_fence(
        self, request: RepairIntegrationRequest, context: RepairIntegrationContext
    ) -> tuple[RepairUnit, RepairFence]:
        """Atomically serialize exact-next-unit admission and the fence claim."""
        if request.unit_id is None:
            raise RuntimeError("repair apply requires unit scope")

        def work(tx: ManagedTransaction) -> tuple[RepairUnit, RepairFence]:
            base = self._authority_params(request, context) | {"unit_id": request.unit_id}
            row = tx.run(READ_UNIT_FOR_ADMISSION, **base).single()  # type: ignore[arg-type]
            if row is None:
                raise RuntimeError("repair allocated unit/current control authority rejected")
            unit = unit_from_properties(_mapping(row["unit"], "unit"))
            fence_id, fingerprint = self._fence_identity(request, context, unit)
            params = self._unit_params(request, context, unit) | {
                "fence_id": fence_id,
                "fence_fingerprint": fingerprint,
            }
            record = tx.run(CLAIM_ADMITTED_FENCE, **params).single()  # type: ignore[arg-type]
            if record is None:
                raise RuntimeError("repair unit admission or fence claim rejected")
            returned_unit = unit_from_properties(_mapping(record["unit"], "unit"))
            fence = fence_from_properties(_mapping(record["fence"], "fence"))
            return returned_unit, fence

        return self._client.execute_write(work)

    def read_fence(
        self, request: RepairIntegrationRequest, context: RepairIntegrationContext
    ) -> tuple[RepairUnit, RepairFence]:
        if request.unit_id is None:
            raise RuntimeError("repair unit fence read requires unit scope")
        params = self._authority_params(request, context) | {"unit_id": request.unit_id}

        def work(tx: ManagedTransaction) -> tuple[RepairUnit, RepairFence]:
            record = tx.run(READ_FENCE, **params).single()  # type: ignore[arg-type]
            if record is None:
                raise RuntimeError("repair fence is absent, foreign, released, or stale")
            unit = unit_from_properties(_mapping(record["unit"], "unit"))
            fence = fence_from_properties(_mapping(record["fence"], "fence"))
            expected_id, expected_fingerprint = self._fence_identity(request, context, unit)
            if (fence.fence_id, fence.fence_fingerprint) != (expected_id, expected_fingerprint):
                raise RuntimeError("repair fence identity is stale or foreign")
            return unit, fence

        return self._client.execute_read(work)

    def read_terminal_rollback_replay(
        self,
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
        authorization_token_digest: str,
        policy: str,
    ) -> RepairRollbackAuthorization | None:
        """Reconstruct only an exact #312 terminal replay after this fence was released."""
        if request.unit_id is None:
            raise RuntimeError("repair rollback replay requires unit scope")
        params = self._authority_params(request, context) | {"unit_id": request.unit_id}

        def work(tx: ManagedTransaction) -> RepairRollbackAuthorization | None:
            unit_record = tx.run(READ_UNIT_FOR_ADMISSION, **params).single()  # type: ignore[arg-type]
            if unit_record is None:
                return None
            candidate_unit = unit_from_properties(_mapping(unit_record["unit"], "unit"))
            fence_id, fence_fingerprint = self._fence_identity(request, context, candidate_unit)
            candidate_fence = RepairFence(
                candidate_unit.run_id,
                candidate_unit.unit_id,
                fence_id,
                candidate_unit.generation,
                candidate_unit.sequence,
                candidate_unit.attempt,
                request.control.owner_id,
                request.control.token_digest,
                candidate_unit.boundary_digest,
                fence_fingerprint,
                "released",
            )
            transition_id = self._authorization_transition_id(
                candidate_unit, candidate_fence, authorization_token_digest, request, policy
            )
            terminal_params = params | {
                "fence_id": fence_id,
                "fence_fingerprint": fence_fingerprint,
                "authorization_reference": request.authorization_reference,
                "authorization_token_digest": authorization_token_digest,
                "predecessor_transition_id": request.predecessor_transition_id,
                "authorization_policy": policy,
                "authorization_transition_id": transition_id,
            }
            record = tx.run(READ_TERMINAL_ROLLBACK_REPLAY, **terminal_params).single()  # type: ignore[arg-type]
            if record is None:
                return None
            stored_unit = unit_from_properties(_mapping(record["unit"], "unit"))
            stored_fence = fence_from_properties(_mapping(record["fence"], "fence"))
            mutation = mutation_from_properties(_mapping(record["result"], "mutation result"))
            image = image_from_properties(_mapping(record["image"], "rollback image"))
            if mutation.outcome == "applied":
                unit = replace(stored_unit, state="applied")
            elif mutation.outcome == "review_required":
                unit = replace(stored_unit, state="review_required")
            else:
                raise RuntimeError("terminal rollback mutation outcome is not replayable")
            expected_fence_id, expected_fingerprint = self._fence_identity(request, context, unit)
            if (
                stored_fence.state != "released"
                or stored_fence.fence_id != expected_fence_id
                or stored_fence.fence_fingerprint != expected_fingerprint
                or (stored_fence.generation, stored_fence.sequence, stored_fence.attempt)
                != (unit.generation, unit.sequence, unit.attempt)
            ):
                raise RuntimeError("terminal rollback released fence identity differs")
            fence = replace(stored_fence, state="claimed")
            transition_id = self._authorization_transition_id(
                unit, fence, authorization_token_digest, request, policy
            )
            values = _mapping(record["authorization"], "rollback authorization")
            if _returned_authorization_transition_id(values, transition_id) != transition_id:
                raise RuntimeError("terminal rollback authorization identity differs")
            return RepairRollbackAuthorization(
                unit,
                fence,
                mutation,
                image,
                _required(request.authorization_reference, "rollback authorization reference"),
                authorization_token_digest,
                _required(request.predecessor_transition_id, "rollback predecessor transition"),
                policy,
                transition_id,
            )

        return self._client.execute_read(work)

    def create_rollback_authorization(
        self,
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
        unit: RepairUnit,
        fence: RepairFence,
        authorization_token_digest: str,
        policy: str,
    ) -> RepairRollbackAuthorization:
        transition_id = self._authorization_transition_id(
            unit, fence, authorization_token_digest, request, policy
        )
        params = self._unit_params(request, context, unit) | {
            "fence_id": fence.fence_id,
            "fence_token": fence.token,
            "fence_fingerprint": fence.fence_fingerprint,
            "authorization_reference": request.authorization_reference,
            "authorization_token_digest": authorization_token_digest,
            "predecessor_transition_id": request.predecessor_transition_id,
            "authorization_policy": policy,
            "authorization_transition_id": transition_id,
        }

        def work(tx: ManagedTransaction) -> RepairRollbackAuthorization:
            record = tx.run(CREATE_AND_READ_ROLLBACK_AUTHORIZATION, **params).single()  # type: ignore[arg-type]
            if record is None:
                raise RuntimeError("rollback authorization CAS rejected")
            return RepairRollbackAuthorization(
                unit_from_properties(_mapping(record["unit"], "unit")),
                fence_from_properties(_mapping(record["fence"], "fence")),
                mutation_from_properties(_mapping(record["result"], "mutation result")),
                image_from_properties(_mapping(record["image"], "rollback image")),
                _required(request.authorization_reference, "rollback authorization reference"),
                authorization_token_digest,
                _required(request.predecessor_transition_id, "rollback predecessor transition"),
                policy,
                _returned_authorization_transition_id(
                    _mapping(record["authorization"], "authorization"), transition_id
                ),
            )

        return self._client.execute_write(work)

    def store_rollback_receipt(
        self,
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
        authorization: RepairRollbackAuthorization,
        status_digest: str,
    ) -> None:
        receipt_id = self._receipt_id(request, authorization.image.image_digest)
        receipt_digest = rollback_status_receipt_digest(
            run_id=authorization.unit.run_id,
            unit_id=authorization.unit.unit_id,
            receipt_id=receipt_id,
            fence_id=authorization.fence.fence_id,
            mutation_id=authorization.mutation.mutation_id,
            image_digest=authorization.image.image_digest,
            authorization_transition_id=authorization.authorization_transition_id,
            authorization_digest=authorization.digest,
            status_digest=status_digest,
            control_revision=request.control.expected_revision,
            allocation_revision=context.authority.allocation_revision,
            completion_id=context.authority.completion_id,
            generation=authorization.unit.generation,
            sequence=authorization.unit.sequence,
            attempt=authorization.unit.attempt,
        )
        params: dict[str, JsonValue] = self._unit_params(request, context, authorization.unit) | {
            "fence_id": authorization.fence.fence_id,
            "receipt_id": receipt_id,
            "receipt_digest": receipt_digest,
            "request_digest": request.request_digest,
            "status_digest": status_digest,
            "image_digest": authorization.image.image_digest,
            "authorization_transition_id": authorization.authorization_transition_id,
            "authorization_digest": authorization.digest,
            "mutation_id": authorization.mutation.mutation_id,
            "generation": authorization.unit.generation,
            "sequence": authorization.unit.sequence,
            "attempt": authorization.unit.attempt,
        }

        def work(tx: ManagedTransaction) -> None:
            record = tx.run(STORE_ROLLBACK_RECEIPT, **params).single()  # type: ignore[arg-type]
            if record is None or record["receipt_digest"] != receipt_digest:
                raise RuntimeError("rollback receipt CAS rejected")

        self._client.execute_write(work)

    def release_terminal_fence(
        self,
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
        authorization: RepairRollbackAuthorization,
        result_digest: str,
    ) -> None:
        params = self._unit_params(request, context, authorization.unit) | {
            "fence_id": authorization.fence.fence_id,
            "result_digest": result_digest,
            "image_digest": authorization.image.image_digest,
            "authorization_transition_id": authorization.authorization_transition_id,
        }
        self._write_single(
            RELEASE_TERMINAL_FENCE, params, "terminal rollback fence release rejected"
        )

    def accept(
        self,
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
        equation_command: RepairRunEquationCommand,
    ) -> None:
        def work(tx: ManagedTransaction) -> None:
            equation = read_run_equation(tx, equation_command)
            _assert_acceptance_equation(equation)
            unit_digest, fence_digest = self._set_digests(tx, context)
            receipt_bindings = self._receipt_bindings(tx, request, context)
            receipt_digest = self._acceptance_receipt_digest(
                request, context, equation.digest, unit_digest, fence_digest
            )
            params = self._authority_params(request, context) | {
                "request_digest": request.request_digest,
                "unit_set_digest": unit_digest,
                "fence_set_digest": fence_digest,
                "equation_digest": equation.digest,
                "acceptance_receipt_digest": receipt_digest,
                "computed_allocation_unit_set_digest": unit_digest,
                "receipt_bindings": receipt_bindings,
            }
            record = tx.run(ACCEPT_AND_RELEASE, **params).single()  # type: ignore[arg-type]
            if record is None or record["receipt_digest"] != receipt_digest:
                raise RuntimeError("repair acceptance eligibility or exact fence CAS rejected")

        self._client.execute_write(work)

    def _receipt_bindings(
        self,
        tx: ManagedTransaction,
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
    ) -> list[JsonValue]:
        params = self._authority_params(request, context)
        record = tx.run(READ_RUN_RECEIPTS, **params).single()  # type: ignore[arg-type]
        if record is None or not isinstance(record["receipts"], list):
            raise RuntimeError("repair rollback receipt set is missing")
        expected_count = _integer(record["unit_count"], "rollback receipt unit count")
        receipts = [_mapping(value, "rollback receipt") for value in record["receipts"]]
        if len(receipts) != expected_count:
            raise RuntimeError("repair rollback receipt set is incomplete")
        bindings: list[JsonValue] = []
        seen_units: set[str] = set()
        seen_receipts: set[str] = set()
        for receipt in receipts:
            binding = _validated_receipt_binding(receipt, request, context)
            unit_id = _required_value(receipt.get("unit_id"), "rollback receipt unit ID")
            receipt_id = _required_value(receipt.get("receipt_id"), "rollback receipt ID")
            if unit_id in seen_units or receipt_id in seen_receipts:
                raise RuntimeError("repair rollback receipt cardinality is ambiguous")
            seen_units.add(unit_id)
            seen_receipts.add(receipt_id)
            bindings.append(binding)
        return sorted(bindings, key=_receipt_binding_sort_key)

    def _set_digests(
        self, tx: ManagedTransaction, context: RepairIntegrationContext
    ) -> tuple[str, str]:
        authority = context.authority
        record = tx.run(
            READ_RUN_SETS,
            run_id=context.run.run_id,
            boundary_digest=context.run.boundary_digest,
            allocation_unit_set_digest=authority.allocation_unit_set_digest,
        ).single()
        if (
            record is None
            or not isinstance(record["units"], list)
            or not isinstance(record["fences"], list)
        ):
            raise RuntimeError("repair immutable unit/fence set is missing")
        units = tuple(unit_from_properties(_mapping(value, "unit")) for value in record["units"])
        fences = tuple(
            fence_from_properties(_mapping(value, "fence")) for value in record["fences"]
        )
        if _integer(record["unit_count"], "allocated unit count") != len(units):
            raise RuntimeError("repair allocated unit set is incomplete")
        canonical_units: list[JsonValue] = [
            _canonical_unit(unit) for unit in sorted(units, key=_unit_sort_key)
        ]
        allocation_digest = object_digest(
            b"crm-deal-identity-repair-allocation-unit-set-v1\x00", {"units": canonical_units}
        )
        if allocation_digest != authority.allocation_unit_set_digest:
            raise RuntimeError("repair allocated unit-set evidence differs")
        canonical_fences: list[JsonValue] = [
            _canonical_fence(fence) for fence in sorted(fences, key=_fence_sort_key)
        ]
        return allocation_digest, object_digest(
            b"crm-deal-identity-repair-fence-set-v2\x00", {"fences": canonical_fences}
        )

    @staticmethod
    def _acceptance_receipt_digest(
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
        equation_digest: str,
        unit_set_digest: str,
        fence_set_digest: str,
    ) -> str:
        return object_digest(
            b"crm-deal-identity-repair-acceptance-receipt-v1\x00",
            {
                "run_id": request.control.run_id,
                "request_digest": request.request_digest,
                "current_revision": request.control.expected_revision,
                "allocation_revision": context.authority.allocation_revision,
                "boundary_digest": context.run.boundary_digest,
                "equation_digest": equation_digest,
                "unit_set_digest": unit_set_digest,
                "fence_set_digest": fence_set_digest,
            },
        )

    def acceptance(self, context: RepairIntegrationContext) -> tuple[str, str]:
        def work(tx: ManagedTransaction) -> tuple[str, str]:
            record = tx.run(READ_ACCEPTANCE, run_id=context.run.run_id).single()
            if (
                record is None
                or not isinstance(record["receipt_digest"], str)
                or not isinstance(record["fence_set_digest"], str)
            ):
                raise RuntimeError("repair acceptance is absent or malformed")
            return record["receipt_digest"], record["fence_set_digest"]

        return self._client.execute_read(work)

    def release_dispatch(
        self,
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
        fence_set_digest: str,
        acceptance_digest: str,
    ) -> None:
        params = self._authority_params(request, context) | {
            "fence_set_digest": fence_set_digest,
            "acceptance_receipt_digest": acceptance_digest,
            "request_digest": request.request_digest,
        }
        self._write_single(RELEASE_DISPATCH, params, "repair dispatch release exact CAS rejected")

    def _read_unit(self, query: str, params: dict[str, JsonValue]) -> RepairUnit:
        def work(tx: ManagedTransaction) -> RepairUnit:
            record = tx.run(query, **params).single()  # type: ignore[arg-type]
            if record is None:
                raise RuntimeError("repair allocated unit/current control authority rejected")
            return unit_from_properties(_mapping(record["unit"], "unit"))

        return self._client.execute_read(work)

    def _write_single(self, query: str, params: dict[str, JsonValue], message: str) -> None:
        def work(tx: ManagedTransaction) -> None:
            record = tx.run(query, **params).single()  # type: ignore[arg-type]
            if record is None:
                raise RuntimeError(message)

        self._client.execute_write(work)

    @staticmethod
    def _authority_params(
        request: RepairIntegrationRequest, context: RepairIntegrationContext
    ) -> dict[str, JsonValue]:
        authority = context.authority
        run = context.run
        return {
            "repair_id": request.control.repair_id,
            "run_id": request.control.run_id,
            "owner_id": request.control.owner_id,
            "token_digest": request.control.token_digest,
            "revision": request.control.expected_revision,
            "boundary_digest": run.boundary_digest,
            "qualification_identity": run.qualification_identity,
            "manifest_digest": run.manifest_digest,
            "artifact_id": run.artifact_id,
            "artifact_manifest_hmac": run.artifact_manifest_hmac,
            "inventory_digest": run.inventory_digest,
            "manifest_json": canonical_json_text(run.manifest.to_dict(), "manifest"),
            "inventory_row_count": run.inventory_row_count,
            "eligible_unit_count": run.eligible_unit_count,
            "negative_control_count": run.negative_control_count,
            "source_instance_id": run.source_instance_id,
            "control_instance_id": run.control_instance_id,
            "completion_id": authority.completion_id,
            "overlay_digest": authority.overlay_digest,
            "allocation_digest": authority.allocation_digest,
            "allocation_unit_set_digest": authority.allocation_unit_set_digest,
            "allocation_request_digest": authority.allocation_request_digest,
            "allocation_origin_key_id": authority.allocation_origin_key_id,
            "allocation_origin_hmac": authority.allocation_origin_hmac,
            "allocation_receipt_digest": authority.allocation_receipt_digest,
            "allocation_revision": authority.allocation_revision,
            "sealed_boundary_digest": authority.sealed_boundary_digest,
        }

    def _unit_params(
        self, request: RepairIntegrationRequest, context: RepairIntegrationContext, unit: RepairUnit
    ) -> dict[str, JsonValue]:
        return self._authority_params(request, context) | {
            "unit_id": unit.unit_id,
            "generation": unit.generation,
            "sequence": unit.sequence,
            "attempt": unit.attempt,
            "inventory_fingerprint": unit.inventory_fingerprint,
            "inventory_binding_digest": unit.inventory_binding_digest,
        }

    @staticmethod
    def _fence_identity(
        request: RepairIntegrationRequest, context: RepairIntegrationContext, unit: RepairUnit
    ) -> tuple[str, str]:
        authority = context.authority
        identity: dict[str, JsonValue] = {
            "run_id": unit.run_id,
            "unit_id": unit.unit_id,
            "generation": unit.generation,
            "sequence": unit.sequence,
            "attempt": unit.attempt,
            "owner_id": request.control.owner_id,
            "token_digest": request.control.token_digest,
            "boundary_digest": unit.boundary_digest,
            "inventory_binding_digest": _required(
                unit.inventory_binding_digest, "unit inventory binding"
            ),
            "completion_id": authority.completion_id,
            "allocation_digest": authority.allocation_digest,
            "overlay_digest": authority.overlay_digest,
        }
        fingerprint = object_digest(b"crm-deal-identity-repair-fence-fingerprint-v2\x00", identity)
        fence_id = object_digest(b"crm-deal-identity-repair-fence-id-v2\x00", identity)[-36:]
        return fence_id, fingerprint

    @staticmethod
    def _authorization_transition_id(
        unit: RepairUnit,
        fence: RepairFence,
        authorization_token_digest: str,
        request: RepairIntegrationRequest,
        policy: str,
    ) -> str:
        return object_digest(
            b"crm-deal-identity-repair-rollback-authorization-v1\x00",
            {
                "run_id": unit.run_id,
                "unit_id": unit.unit_id,
                "generation": unit.generation,
                "sequence": unit.sequence,
                "attempt": unit.attempt,
                "fence_id": fence.fence_id,
                "fence_token": fence.token,
                "image_authority": request.authorization_reference,
                "token_digest": authorization_token_digest,
                "predecessor": request.predecessor_transition_id,
                "policy": policy,
            },
        )

    @staticmethod
    def _receipt_id(request: RepairIntegrationRequest, image_digest: str) -> str:
        return object_digest(
            b"crm-deal-identity-repair-rollback-receipt-v2\x00",
            {
                "run_id": request.control.run_id,
                "unit_id": request.unit_id,
                "request_digest": request.request_digest,
                "image_digest": image_digest,
            },
        )[-36:]


def _assert_acceptance_equation(equation: RepairRunEquationResult) -> None:
    """Keep #311 negative/drift accounting while #313 binds its allocated subset in CAS."""
    if (
        equation.drifted_units
        or equation.failed_units
        or equation.drifted_negative_controls
        or equation.missing_negative_controls
        or equation.stamped_negative_controls
        or equation.unsupported_multi_links
        or equation.active_deal_origin_phone_projections
        or equation.active_deal_origin_email_projections
        or equation.active_deal_origin_g_us_projections
        or equation.failed_secondaries
        or equation.pending_secondaries
        or equation.unexplained_secondary_remainder
    ):
        raise RuntimeError("repair acceptance requires an exact balanced run equation")


def _canonical_unit(unit: RepairUnit) -> dict[str, JsonValue]:
    """Render the #310 unit-set payload without leaking dataclasses.asdict Any."""
    return {
        "run_id": unit.run_id,
        "unit_id": unit.unit_id,
        "generation": unit.generation,
        "sequence": unit.sequence,
        "attempt": unit.attempt,
        "boundary_digest": unit.boundary_digest,
        "inventory_fingerprint": unit.inventory_fingerprint,
        "state": "allocated",
        "inventory_key": unit.inventory_key,
        "source_record_pk": unit.source_record_pk,
        "inventory_graph_fingerprint": unit.inventory_graph_fingerprint,
        "inventory_stored_payload_fingerprint": unit.inventory_stored_payload_fingerprint,
        "inventory_binding_digest": unit.inventory_binding_digest,
    }


def _canonical_fence(fence: RepairFence) -> dict[str, JsonValue]:
    """Render the integration-owned claimed fence-set payload canonically."""
    return {
        "run_id": fence.run_id,
        "unit_id": fence.unit_id,
        "fence_id": fence.fence_id,
        "generation": fence.generation,
        "sequence": fence.sequence,
        "attempt": fence.attempt,
        "owner_id": fence.owner_id,
        "token": fence.token,
        "boundary_digest": fence.boundary_digest,
        "fence_fingerprint": fence.fence_fingerprint,
        "state": "claimed",
    }


def _unit_sort_key(unit: RepairUnit) -> tuple[int, str, int, int]:
    """Mirror #310's allocation sequence before digesting a graph set."""
    return (unit.sequence, unit.unit_id, unit.generation, unit.attempt)


def _fence_sort_key(fence: RepairFence) -> tuple[int, str, str, int, int]:
    """Give the integration-owned fence set a stable independent graph ordering."""
    return (fence.sequence, fence.unit_id, fence.fence_id, fence.generation, fence.attempt)


def _returned_authorization_transition_id(
    authorization: Mapping[str, JsonValue], expected: str
) -> str:
    transition_id = _required_value(
        authorization.get("authorization_transition_id"), "authorization transition ID"
    )
    if transition_id != expected:
        raise RuntimeError("rollback authorization transition identity differs")
    return transition_id


def _validated_receipt_binding(
    receipt: Mapping[str, JsonValue],
    request: RepairIntegrationRequest,
    context: RepairIntegrationContext,
) -> dict[str, JsonValue]:
    values = {
        key: _required_value(receipt.get(key), "rollback receipt " + key)
        for key in (
            "run_id",
            "unit_id",
            "receipt_id",
            "fence_id",
            "mutation_id",
            "image_digest",
            "authorization_transition_id",
            "authorization_digest",
            "status_digest",
            "completion_id",
            "receipt_digest",
        )
    }
    if (
        values["run_id"] != context.run.run_id
        or values["completion_id"] != context.authority.completion_id
    ):
        raise RuntimeError("repair rollback receipt authority differs")
    generation = _integer(receipt.get("generation"), "rollback receipt generation")
    sequence = _integer(receipt.get("sequence"), "rollback receipt sequence")
    attempt = _integer(receipt.get("attempt"), "rollback receipt attempt")
    control_revision = _integer(
        receipt.get("control_revision"), "rollback receipt control revision"
    )
    allocation_revision = _integer(
        receipt.get("allocation_revision"), "rollback receipt allocation revision"
    )
    if (
        control_revision != request.control.expected_revision
        or allocation_revision != context.authority.allocation_revision
    ):
        raise RuntimeError("repair rollback receipt revision differs")
    expected = rollback_status_receipt_digest(
        run_id=values["run_id"],
        unit_id=values["unit_id"],
        receipt_id=values["receipt_id"],
        fence_id=values["fence_id"],
        mutation_id=values["mutation_id"],
        image_digest=values["image_digest"],
        authorization_transition_id=values["authorization_transition_id"],
        authorization_digest=values["authorization_digest"],
        status_digest=values["status_digest"],
        control_revision=control_revision,
        allocation_revision=allocation_revision,
        completion_id=values["completion_id"],
        generation=generation,
        sequence=sequence,
        attempt=attempt,
    )
    if values["receipt_digest"] != expected:
        raise RuntimeError("repair rollback receipt digest differs")
    return {"receipt_id": values["receipt_id"], "receipt_digest": values["receipt_digest"]}


def _receipt_binding_sort_key(value: JsonValue) -> str:
    if not isinstance(value, dict):
        raise RuntimeError("repair rollback receipt binding is malformed")
    return _required_value(value.get("receipt_id"), "rollback receipt binding ID")


def _authority_from_completion(
    value: Mapping[str, JsonValue], sealed: object
) -> RepairIntegrationAuthority:
    sealed_boundary = _required_value(sealed, "sealed boundary")
    return RepairIntegrationAuthority(
        _required_value(value.get("completion_id"), "allocation completion"),
        _required_value(value.get("overlay_digest"), "allocation overlay"),
        _required_value(value.get("allocation_digest"), "allocation digest"),
        _required_value(value.get("unit_set_digest"), "allocation unit set"),
        _required_value(value.get("request_digest"), "allocation request"),
        _required_value(value.get("allocation_origin_key_id"), "allocation origin key"),
        _required_value(value.get("allocation_origin_hmac"), "allocation origin HMAC"),
        _required_value(value.get("receipt_digest"), "allocation receipt"),
        sealed_boundary,
        _integer(value.get("allocation_revision"), "allocation revision"),
    )


def _mapping(value: object, label: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise RuntimeError("repair integration " + label + " record is malformed")
    return cast(Mapping[str, JsonValue], value)


def _required(value: str | None, label: str) -> str:
    if not value:
        raise RuntimeError(label + " is missing")
    return value


def _required_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("repair integration " + label + " is malformed")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError("repair integration " + label + " is malformed")
    return value
