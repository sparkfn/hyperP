"""Read-only operator command for CRM-deal identity repair inventory (#254)."""

from __future__ import annotations

import json
import logging
import os
from argparse import Namespace
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from itertools import zip_longest
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from src.crm_deal_identity_repair.allocation import AllocationPlan
from src.crm_deal_identity_repair.cli import parse_arguments
from src.crm_deal_identity_repair.control_models import RepairControlRequest
from src.crm_deal_identity_repair.execution_models import (
    RepairBoundaryDriftReason,
    RepairBoundarySnapshot,
    RepairExecutionBoundaryManifest,
    RepairQualificationRun,
)
from src.crm_deal_identity_repair.execution_protocols import (
    RepairBoundaryReader,
    RepairQualificationRepository,
)
from src.crm_deal_identity_repair.models import RepairInventoryItem, inventory_item_from_json
from src.crm_deal_identity_repair.rebase import (
    RepairBoundaryRebaseRequest,
    RepairBoundaryRebaseResult,
)
from src.models import JsonValue

if TYPE_CHECKING:
    from src.config import Settings
    from src.crm_deal_identity_repair.artifacts import RepairArtifactContext
    from src.crm_deal_identity_repair.qualification import VerifiedRepairArtifact


class _RepairStatusRepository(RepairQualificationRepository, RepairBoundaryReader, Protocol):
    def status_snapshot(
        self,
        *,
        source_instance_id: str,
        control_instance_id: str,
        source_record_pks: tuple[str, ...],
    ) -> RepairBoundarySnapshot: ...


class _RebaseRepository(Protocol):
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
    ) -> RepairBoundaryRebaseResult: ...


class _RepairRuntimeSettings(Protocol):
    @property
    def deployment_environment(self) -> str: ...

    @property
    def crm_deal_identity_repair_enabled(self) -> bool: ...

    @property
    def crm_deal_identity_repair_repository_sha(self) -> str: ...

    @property
    def crm_deal_identity_repair_image_digest(self) -> str: ...


def main(argv: Sequence[str] | None = None) -> int:
    """Run one staging-only, read-only graph inventory and seal its evidence."""
    arguments = parse_arguments(argv)

    if arguments.command == "inventory":
        return _inventory(arguments)
    if arguments.command == "qualify":
        return _qualify(arguments)
    if arguments.command == "status":
        return _status(arguments)
    if arguments.command in {
        "apply",
        "verify",
        "rollback-status",
        "rollback",
        "accept",
        "release-dispatch",
    }:
        _integration(arguments)
        return 0
    return _control(arguments)


def _control(arguments: Namespace) -> int:
    """Run a default-off #310 metadata command; no command dispatches work."""
    from src.config import get_settings
    from src.graph.client import Neo4jClient
    from src.graph.crm_deal_identity_repair_control import CrmDealRepairControlRepository
    from src.graph.crm_deal_identity_repair_ledger import CrmDealRepairLedgerRepository
    from src.graph.crm_deal_identity_repair_ledger_migration import (
        assert_crm_deal_repair_ledger_ready,
    )

    settings = get_settings()
    _validate_runtime_gate(settings, require_enabled=True)
    control_token = os.environ.get("CRM_DEAL_IDENTITY_REPAIR_CONTROL_TOKEN")
    if not control_token:
        raise RuntimeError(
            "repair control token must be supplied through its secret environment channel"
        )
    request = RepairControlRequest(
        arguments.repair_id,
        arguments.run_id,
        arguments.owner_id,
        control_token,
        arguments.expected_revision,
    )
    client = Neo4jClient(settings)
    rebase_result: RepairBoundaryRebaseResult | None = None
    try:
        assert_crm_deal_repair_ledger_ready(client)
        ledger = CrmDealRepairLedgerRepository(client)
        run = ledger.get_qualification(arguments.repair_id)
        if run is None or run.run_id != request.run_id:
            raise RuntimeError("repair control requires the exact qualified run")
        repository = CrmDealRepairControlRepository(client)
        if arguments.command == "pause":
            lease = repository.pause(request)
        elif arguments.command == "resume":
            lease = repository.resume(request)
        elif arguments.command == "rebase-boundary":
            from src.graph.crm_deal_identity_repair_rebase import CrmDealRepairRebaseRepository

            rebase_result = _rebase_boundary(
                arguments, settings, CrmDealRepairRebaseRepository(client), run, request
            )
            lease = rebase_result.lease
        elif arguments.command == "quiesce":
            # The CLI intentionally invokes its own bounded inspectors rather
            # than accepting caller-provided observations as authorization.
            from src.crm_deal_identity_repair.artifacts import (
                repair_artifact_store_from_settings,
            )
            from src.crm_deal_identity_repair.qualification import (
                read_qualified_stale_run_id,
                verify_qualified_repair_artifact,
            )
            from src.crm_deal_identity_repair.quiescence import RepairQuiescenceService
            from src.crm_deal_identity_repair.task_inspection import (
                CeleryWorkerInspector,
                RedisCeleryBrokerInspector,
            )

            proof_secret_text = (
                settings.crm_deal_identity_repair_absence_proof_key_secret.get_secret_value()
            )
            secret = proof_secret_text.encode()
            if not secret or not settings.crm_deal_identity_repair_absence_proof_key_id:
                raise RuntimeError("repair task-absence proof signing configuration is missing")

            with repair_artifact_store_from_settings(settings) as store:
                verified = verify_qualified_repair_artifact(store, run=run)
            stale_run_id = read_qualified_stale_run_id(verified)
            result = RepairQuiescenceService(
                repository,
                CeleryWorkerInspector(),
                RedisCeleryBrokerInspector(settings.celery_broker_url),
            ).quiesce(
                request=request,
                boundary_digest=run.boundary_digest,
                control_instance_id=run.control_instance_id,
                expected_workers=tuple(
                    sorted(settings.crm_deal_identity_repair_expected_worker_ids)
                ),
                timeout_seconds=settings.crm_deal_identity_repair_worker_timeout_seconds,
                max_age_seconds=settings.crm_deal_identity_repair_absence_max_age_seconds,
                proof_key_id=settings.crm_deal_identity_repair_absence_proof_key_id,
                proof_secret=secret,
                stale_run_id=stale_run_id,
            )
            lease = result.lease
        else:
            # Allocation is metadata-only. Authenticate the already-qualified
            # artifact from the stored manifest rather than rebuilding #300
            # inputs from allocate CLI arguments.
            from src.crm_deal_identity_repair.allocation import plan_allocation
            from src.crm_deal_identity_repair.approval_overlay import (
                assert_overlay_binds_qualification,
                verify_approval_overlay,
            )
            from src.crm_deal_identity_repair.artifacts import repair_artifact_store_from_settings
            from src.crm_deal_identity_repair.qualification import (
                iter_verified_inventory_lines,
                verify_qualified_repair_artifact,
            )

            secret = (
                settings.crm_deal_identity_repair_approval_key_secret.get_secret_value().encode()
            )
            key_id = settings.crm_deal_identity_repair_approval_key_id
            if not secret or not key_id:
                raise RuntimeError("repair approval overlay signing configuration is missing")
            with repair_artifact_store_from_settings(settings) as store:
                verified = verify_qualified_repair_artifact(store, run=run)
            overlay = verify_approval_overlay(
                Path(settings.crm_deal_identity_repair_approval_root)
                / f"{arguments.approval_id}.json",
                secret=secret,
            )
            assert_overlay_binds_qualification(overlay, run=run, expected_key_id=key_id)
            inventory = tuple(
                _inventory_item(json.loads(line))
                for line in iter_verified_inventory_lines(verified)
            )
            plan = plan_allocation(
                run_id=run.run_id,
                boundary_digest=run.boundary_digest,
                inventory=inventory,
                overlay=overlay,
            )
            lease = repository.allocate(
                request,
                boundary_digest=run.boundary_digest,
                proof_digest=repository.proof_digest(request),
                plan=plan,
                allocation_origin_key_id=key_id,
                allocation_origin_secret=secret,
            )
    finally:
        client.close()
    print(
        json.dumps(
            {
                "repair_id": arguments.repair_id,
                "run_id": lease.run_id,
                "state": lease.state,
                "revision": lease.revision,
                "execution_allowed": False,
                **(
                    {
                        "rebase_replayed": rebase_result.replayed,
                        "replacement_boundary_digest": rebase_result.replacement_boundary_digest,
                        "rebase_receipt_digest": rebase_result.receipt_digest,
                        "rebase_audit_digest": rebase_result.audit_digest,
                    }
                    if rebase_result is not None
                    else {}
                ),
            },
            sort_keys=True,
        )
    )
    return 0


def _rebase_boundary(
    arguments: Namespace,
    settings: Settings,
    repository: _RebaseRepository,
    run: RepairQualificationRun,
    control: RepairControlRequest,
) -> RepairBoundaryRebaseResult:
    """Authenticate original/fresh artifacts before the one repository CAS."""
    from src.crm_deal_identity_repair.allocation import plan_allocation
    from src.crm_deal_identity_repair.approval_overlay import (
        assert_overlay_binds_qualification,
        verify_approval_overlay,
    )
    from src.crm_deal_identity_repair.artifacts import (
        repair_artifact_store_from_settings,
        repair_inventory_configuration_digest,
    )
    from src.crm_deal_identity_repair.qualification import (
        iter_verified_inventory_lines,
        verify_qualified_repair_artifact,
        verify_rebase_artifact,
    )

    secret = settings.crm_deal_identity_repair_approval_key_secret.get_secret_value().encode()
    key_id = settings.crm_deal_identity_repair_approval_key_id
    if not secret or not key_id:
        raise RuntimeError("repair approval overlay signing configuration is missing")
    configuration_digest = repair_inventory_configuration_digest(settings)
    with repair_artifact_store_from_settings(settings) as store:
        original = verify_qualified_repair_artifact(store, run=run)
        fresh = verify_rebase_artifact(
            store,
            artifact_id=arguments.fresh_artifact_id,
            repair_id=run.repair_id,
            source_contract_uuid=run.manifest.source_contract_uuid,
            configuration_digest=configuration_digest,
        )
    _assert_fresh_rebase_inventory(original, fresh, run)
    overlay = verify_approval_overlay(
        Path(settings.crm_deal_identity_repair_approval_root) / f"{arguments.approval_id}.json",
        secret=secret,
    )
    assert_overlay_binds_qualification(overlay, run=run, expected_key_id=key_id)
    inventory = tuple(
        _inventory_item(json.loads(line)) for line in iter_verified_inventory_lines(original)
    )
    plan = plan_allocation(
        run_id=run.run_id,
        boundary_digest=run.boundary_digest,
        inventory=inventory,
        overlay=overlay,
    )
    request = RepairBoundaryRebaseRequest(
        control,
        arguments.approval_id,
        arguments.fresh_artifact_id,
        arguments.expected_observed_boundary_digest,
    )
    result = repository.rebase_boundary(
        request,
        plan=plan,
        fresh_artifact_manifest_hmac=fresh.manifest.manifest_hmac,
        fresh_inventory_digest=fresh.inventory_digest,
        fresh_producer_repository_sha=fresh.manifest.provenance.repository_sha,
        fresh_producer_image_digest=fresh.manifest.provenance.image_digest,
        approval_key_id=key_id,
        approval_secret=secret,
    )
    return result


def _assert_fresh_rebase_inventory(
    original: VerifiedRepairArtifact,
    fresh: VerifiedRepairArtifact,
    run: RepairQualificationRun,
) -> None:
    """Require a separate freshly sealed artifact with exact immutable inventory bytes."""
    if fresh.manifest.artifact_id == original.manifest.artifact_id:
        raise RuntimeError("repair rebase requires a separate fresh inventory artifact")
    if (
        fresh.inventory_digest,
        fresh.inventory_row_count,
        fresh.eligible_unit_count,
        fresh.negative_control_count,
        fresh.inventory_source_record_pks,
    ) != (
        run.inventory_digest,
        run.inventory_row_count,
        run.eligible_unit_count,
        run.negative_control_count,
        original.inventory_source_record_pks,
    ):
        raise RuntimeError("repair rebase fresh artifact does not preserve allocated inventory")
    if fresh.negative_control_count != 6:
        raise RuntimeError("repair rebase requires exactly six negative controls")
    from src.crm_deal_identity_repair.qualification import iter_verified_inventory_lines

    for original_line, fresh_line in zip_longest(
        iter_verified_inventory_lines(original), iter_verified_inventory_lines(fresh)
    ):
        if original_line != fresh_line:
            raise RuntimeError("repair rebase fresh artifact inventory bytes differ")


def _inventory_item(raw: object) -> RepairInventoryItem:
    """Backward-compatible local wrapper for the shared inventory decoder."""
    return inventory_item_from_json(cast(JsonValue, raw))


def _inventory(arguments: Namespace) -> int:
    from src.config import get_settings
    from src.crm_deal_identity_repair.artifacts import (
        repair_artifact_store_from_settings,
        repair_inventory_configuration_digest,
        seal_inventory_artifact,
    )
    from src.crm_deal_identity_repair.digests import inventory_digest
    from src.crm_deal_identity_repair.inventory import collect_repair_inventory
    from src.graph.client import Neo4jClient

    settings = get_settings()
    _validate_runtime_gate(settings, require_enabled=True)
    context = _inventory_context(
        arguments,
        settings,
        repair_inventory_configuration_digest(settings),
    )
    with repair_artifact_store_from_settings(settings) as store:
        client = Neo4jClient(settings)
        try:
            inventory = collect_repair_inventory(
                client,
                source_system=arguments.source_system,
            )
        finally:
            client.close()
        population_counts = inventory.population_counts.to_dict()
        manifest = seal_inventory_artifact(
            store,
            context=context,
            items=inventory.items,
            population_counts=population_counts,
            stale_run_evidence=inventory.stale_run_evidence,
            representative_replay_limit=arguments.representative_replay_limit,
        )
    summary = _inventory_summary(
        artifact_id=manifest.artifact_id,
        inventory_digest_value=inventory_digest(inventory.items),
        ownership_repair_count=len(inventory.ownership_repairs),
        projection_cleanup_count=len(inventory.projection_cleanups),
        negative_control_count=len(inventory.negative_controls),
        population_counts=population_counts,
        stale_run_state=inventory.stale_run_evidence["state"],
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


def _inventory_context(
    arguments: Namespace,
    settings: Settings,
    configuration_digest: str,
) -> RepairArtifactContext:
    from src.crm_deal_identity_repair.artifacts import RepairArtifactContext

    return RepairArtifactContext(
        repair_id=arguments.repair_id,
        environment="staging",
        source_contract_uuid=arguments.source_contract_uuid,
        repository_sha=settings.crm_deal_identity_repair_repository_sha,
        image_digest=settings.crm_deal_identity_repair_image_digest,
        configuration_digest=configuration_digest,
        boundary={
            "source_system": arguments.source_system,
            "inventory_mode": "graph_only_read_only",
            "artifact_scope": "graph_discovery_only",
            "execution_allowed": False,
        },
        retention_expires_at=datetime.now(UTC) + timedelta(days=arguments.retention_days),
    )


def _inventory_summary(
    *,
    artifact_id: str,
    inventory_digest_value: str,
    ownership_repair_count: int,
    projection_cleanup_count: int,
    negative_control_count: int,
    population_counts: Mapping[str, JsonValue],
    stale_run_state: JsonValue,
) -> dict[str, JsonValue]:
    return {
        "artifact_id": artifact_id,
        "inventory_digest": inventory_digest_value,
        "ownership_repair_count": ownership_repair_count,
        "projection_cleanup_count": projection_cleanup_count,
        "negative_control_count": negative_control_count,
        "population_counts": dict(population_counts),
        "artifact_scope": "graph_discovery_only",
        "execution_allowed": False,
        "execution_blocker": "separate #255 execution scope is required",
        "stale_run_state": stale_run_state,
    }


def _qualify(arguments: Namespace) -> int:
    from src.config import get_settings
    from src.crm_deal_identity_repair.artifacts import repair_inventory_configuration_digest
    from src.graph.client import Neo4jClient
    from src.graph.crm_deal_identity_repair_ledger import CrmDealRepairLedgerRepository
    from src.graph.crm_deal_identity_repair_ledger_migration import (
        assert_crm_deal_repair_ledger_ready,
    )

    settings = get_settings()
    _validate_runtime_gate(settings, require_enabled=True)
    configuration_digest = repair_inventory_configuration_digest(settings)
    artifact = _qualification_artifact(arguments, settings, configuration_digest)
    client = Neo4jClient(settings)
    try:
        assert_crm_deal_repair_ledger_ready(client)
        repository = CrmDealRepairLedgerRepository(client)
        snapshot = repository.snapshot(
            source_instance_id=arguments.source_instance_id,
            control_instance_id=arguments.control_instance_id,
            source_record_pks=artifact.inventory_source_record_pks,
        )
        manifest = _qualification_manifest(
            arguments, settings, artifact, configuration_digest, snapshot
        )
        run = repository.qualify(manifest, snapshot)
    finally:
        client.close()
    print(
        json.dumps(
            {
                "repair_id": run.repair_id,
                "run_id": run.run_id,
                "status": run.status,
                "manifest_digest": run.manifest_digest,
                "boundary_digest": run.boundary_digest,
                "execution_allowed": False,
            },
            sort_keys=True,
        )
    )
    return 0


def _qualification_artifact(
    arguments: Namespace,
    settings: Settings,
    configuration_digest: str,
) -> VerifiedRepairArtifact:
    from src.crm_deal_identity_repair.artifacts import repair_artifact_store_from_settings
    from src.crm_deal_identity_repair.qualification import verify_repair_artifact

    with repair_artifact_store_from_settings(settings) as store:
        return verify_repair_artifact(
            store,
            artifact_id=arguments.artifact_id,
            repair_id=arguments.repair_id,
            source_contract_uuid=arguments.source_contract_uuid,
            repository_sha=settings.crm_deal_identity_repair_repository_sha,
            image_digest=settings.crm_deal_identity_repair_image_digest,
            configuration_digest=configuration_digest,
        )


def _qualification_manifest(
    arguments: Namespace,
    settings: Settings,
    artifact: VerifiedRepairArtifact,
    configuration_digest: str,
    snapshot: RepairBoundarySnapshot,
) -> RepairExecutionBoundaryManifest:
    from src.crm_deal_identity_repair.qualification import build_execution_manifest

    return build_execution_manifest(
        artifact,
        repair_id=arguments.repair_id,
        source_contract_uuid=arguments.source_contract_uuid,
        repository_sha=settings.crm_deal_identity_repair_repository_sha,
        image_digest=settings.crm_deal_identity_repair_image_digest,
        configuration_digest=configuration_digest,
        approval_reference=arguments.approval_reference,
        unit_ceiling=arguments.unit_ceiling,
        stop_conditions=tuple(arguments.stop_condition),
        source_instance_id=arguments.source_instance_id,
        control_instance_id=arguments.control_instance_id,
        rollback_authority_reference=arguments.rollback_authority_reference,
        rollback_authority_policy=arguments.rollback_authority_policy,
        graph_boundary_digest=snapshot.boundary_digest,
    )


def _status(arguments: Namespace) -> int:
    from src.config import get_settings
    from src.graph.client import Neo4jClient
    from src.graph.crm_deal_identity_repair_control import CrmDealRepairControlRepository
    from src.graph.crm_deal_identity_repair_ledger import CrmDealRepairLedgerRepository
    from src.graph.crm_deal_identity_repair_ledger_migration import (
        assert_crm_deal_repair_ledger_ready,
    )

    settings = get_settings()
    _validate_runtime_gate(settings, require_enabled=False)
    notifications_logger = logging.getLogger("neo4j.notifications")
    previous_notification_level = notifications_logger.level
    notifications_logger.setLevel(logging.ERROR)
    client: Neo4jClient | None = None
    try:
        client = Neo4jClient(settings)
        assert_crm_deal_repair_ledger_ready(client)
        repository = CrmDealRepairLedgerRepository(client)
        run = repository.get_qualification(arguments.repair_id)
        snapshot, drift_reason = _status_snapshot(repository, run)
        control_repository = CrmDealRepairControlRepository(client)
        approval_secret_value = getattr(
            settings, "crm_deal_identity_repair_approval_key_secret", None
        )
        approval_secret = (
            approval_secret_value.get_secret_value().encode()
            if approval_secret_value is not None
            else None
        )
        approval_key_id = getattr(settings, "crm_deal_identity_repair_approval_key_id", None)
        if run is None:
            effective_boundary = None
        else:
            from src.graph.crm_deal_identity_repair_rebase import CrmDealRepairRebaseRepository

            effective_boundary = CrmDealRepairRebaseRepository(client).effective_boundary_digest(
                run, approval_key_id=approval_key_id, approval_secret=approval_secret
            )
        status = repository.get_status(
            arguments.repair_id, snapshot, drift_reason, effective_boundary
        )
        control_status = control_repository.status(arguments.repair_id)
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            notifications_logger.setLevel(previous_notification_level)
    print(
        json.dumps(
            {
                "repair_id": status.repair_id,
                "admissibility": status.admissibility,
                "reason_code": status.reason_code,
                "manifest_digest": status.manifest_digest,
                "qualification_identity": status.qualification_identity,
                "expected_boundary_digest": status.expected_boundary_digest,
                "observed_boundary_digest": status.observed_boundary_digest,
                "effective_boundary_digest": status.effective_boundary_digest,
                "source_instance_id": status.source_instance_id,
                "control_instance_id": status.control_instance_id,
                "inventory_row_count": status.inventory_row_count,
                "eligible_unit_count": status.eligible_unit_count,
                "negative_control_count": status.negative_control_count,
                "control_state": control_status.control_state,
                "dispatch_blocked": control_status.dispatch_blocked,
                "dispatch_revision": control_status.dispatch_revision,
                "quiescence_state": control_status.quiescence_state,
                "allocation_state": control_status.allocation_state,
                "paused_from_state": control_status.paused_from_state,
                "allocated_unit_count": control_status.allocated_unit_count,
                "execution_allowed": False,
            },
            sort_keys=True,
        )
    )
    return 0


def _status_snapshot(
    repository: _RepairStatusRepository,
    run: RepairQualificationRun | None,
) -> tuple[RepairBoundarySnapshot | None, RepairBoundaryDriftReason | None]:
    """Read persisted evidence only; expected drift never masks malformed ledger state."""
    if run is None:
        return None, None
    from src.graph.crm_deal_identity_repair_ledger import ExpectedRepairBoundaryDriftError

    try:
        snapshot = repository.status_snapshot(
            source_instance_id=run.source_instance_id,
            control_instance_id=run.control_instance_id,
            source_record_pks=repository.source_record_pks(run.repair_id),
        )
    except ExpectedRepairBoundaryDriftError as exc:
        return None, exc.reason
    return snapshot, None


def _validate_runtime_gate(
    settings: _RepairRuntimeSettings, *, require_enabled: bool = True
) -> None:
    if settings.deployment_environment != "staging":
        raise RuntimeError("CRM-deal repair inventory requires DEPLOYMENT_ENVIRONMENT=staging")
    if require_enabled and not settings.crm_deal_identity_repair_enabled:
        raise RuntimeError(
            "CRM-deal repair inventory requires CRM_DEAL_IDENTITY_REPAIR_ENABLED=true"
        )


def _integration(arguments: Namespace) -> dict[str, str]:
    """Delegate guarded execution to the typed #313 runtime factory."""
    from src.crm_deal_identity_repair.integration_runtime import execute_integration

    report = execute_integration(arguments)
    print(json.dumps(report, sort_keys=True))
    return report


if __name__ == "__main__":
    raise SystemExit(main())
