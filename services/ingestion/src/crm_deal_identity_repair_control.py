"""Read-only operator command for CRM-deal identity repair inventory (#254)."""

from __future__ import annotations

import json
from argparse import Namespace
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from src.crm_deal_identity_repair.cli import parse_arguments
from src.crm_deal_identity_repair.execution_models import (
    RepairBoundaryDriftReason,
    RepairBoundarySnapshot,
    RepairExecutionBoundaryManifest,
    RepairQualificationRun,
)
from src.crm_deal_identity_repair.task_inspection import (
    RepairBrokerInspector,
    RepairTaskIdentity,
    RepairTaskInspection,
    RepairTaskInspector,
)
from src.crm_deal_identity_repair.execution_protocols import (
    RepairBoundaryReader,
    RepairQualificationRepository,
)
from src.models import JsonValue

if TYPE_CHECKING:
    from src.config import Settings
    from src.crm_deal_identity_repair.artifacts import RepairArtifactContext
    from src.crm_deal_identity_repair.qualification import VerifiedRepairArtifact


class _RepairStatusRepository(RepairQualificationRepository, RepairBoundaryReader, Protocol):
    pass


class _RepairSigningSecret(Protocol):
    """Restricted secret value needed only for detached overlay HMAC verification."""

    def get_secret_value(self) -> str: ...


class _RepairRuntimeSettings(Protocol):
    @property
    def deployment_environment(self) -> str: ...

    @property
    def crm_deal_identity_repair_enabled(self) -> bool: ...

    @property
    def crm_deal_identity_repair_expected_workers(self) -> tuple[str, ...]: ...

    @property
    def crm_deal_identity_repair_worker_timeout_seconds(self) -> int: ...

    @property
    def crm_deal_identity_repair_repository_sha(self) -> str: ...

    @property
    def crm_deal_identity_repair_image_digest(self) -> str: ...

    @property
    def crm_deal_identity_repair_artifact_signing_key_secret(self) -> _RepairSigningSecret: ...

    @property
    def crm_deal_identity_repair_approval_overlay_verification_secret(self) -> _RepairSigningSecret: ...


def main(argv: Sequence[str] | None = None) -> int:
    """Run one staging-only, read-only graph inventory and seal its evidence."""
    arguments = parse_arguments(argv)

    if arguments.command == "inventory":
        return _inventory(arguments)
    if arguments.command == "qualify":
        return _qualify(arguments)
    if arguments.command == "status":
        return _status(arguments)
    return _control_command(arguments)


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
    """Read qualification/control evidence; this command stays default-off and read-only."""
    from src.config import get_settings
    from src.graph.client import Neo4jClient
    from src.graph.crm_deal_identity_repair_control import CrmDealRepairControlRepository
    from src.crm_deal_identity_repair.control_models import RepairBoundaryComponentProof
    from src.graph.crm_deal_identity_repair_ledger import CrmDealRepairLedgerRepository
    from src.graph.crm_deal_identity_repair_ledger_migration import (
        assert_crm_deal_repair_ledger_ready,
    )

    settings = get_settings()
    _validate_runtime_gate(settings, require_enabled=False)
    client = Neo4jClient(settings)
    try:
        assert_crm_deal_repair_ledger_ready(client)
        repository = CrmDealRepairLedgerRepository(client)
        run = repository.get_qualification(arguments.repair_id)
        snapshot, drift_reason = _status_snapshot(repository, run)
        status = repository.get_status(arguments.repair_id, snapshot, drift_reason)
        control_repository = (
            None if run is None else CrmDealRepairControlRepository(client, run.control_instance_id)
        )
        control = None if control_repository is None else control_repository.read_status(run.run_id)
        boundary_proof_admissible = (
            False
            if control_repository is None or snapshot is None
            else _control_boundary_proof_admissible(
                control_repository.read_boundary_component_proof(run.run_id),
                RepairBoundaryComponentProof.from_snapshot(snapshot),
            )
        )
    finally:
        client.close()
    payload: dict[str, JsonValue] = {
        "repair_id": status.repair_id,
        "qualification": {
            "admissibility": status.admissibility,
            "reason_code": status.reason_code,
            "manifest_digest": status.manifest_digest,
            "qualification_identity": status.qualification_identity,
            "expected_boundary_digest": status.expected_boundary_digest,
            "observed_boundary_digest": status.observed_boundary_digest,
            "source_instance_id": status.source_instance_id,
            "control_instance_id": status.control_instance_id,
            "inventory_row_count": status.inventory_row_count,
            "eligible_unit_count": status.eligible_unit_count,
            "negative_control_count": status.negative_control_count,
        },
        "execution_allowed": False,
    }
    if control is not None:
        payload["control"] = {
            "state": control.state, "owner_id": control.owner_id, "revision": control.revision,
            "boundary_digest": control.boundary_digest, "prior_state": control.prior_state,
            "dispatch_blocked": control.dispatch_blocked,
            "dispatch_owner_id": control.dispatch_owner_id,
            "task_proof_state": control.task_proof_state,
            "stale_run_proof_count": control.stale_run_proof_count,
            "topology_active_count": control.topology_active_count,
            "topology_superseded_count": control.topology_superseded_count,
            "allocation_digest": control.allocation_digest,
            "allocation_unit_count": control.allocation_unit_count,
            "completion_unit_count": control.completion_unit_count,
            "pause_or_stop_reason": control.stop_reason,
            "boundary_proof_admissible": boundary_proof_admissible,
        }
    print(json.dumps(payload, sort_keys=True))
    return 0

def _control_boundary_proof_admissible(
    stored: tuple[RepairBoundaryComponentProof, str, str] | None,
    current: RepairBoundaryComponentProof,
) -> bool:
    """Status-only check of the #310 derived proof; it never masks #300 qualification drift."""
    if stored is None:
        return False
    baseline, authorized_control_digest, authorized_stale_digest = stored
    return (
        baseline.immutable_matches(current)
        and current.control_digest == authorized_control_digest
        and current.stale_run_evidence_digest == authorized_stale_digest
    )


def _status_snapshot(
    repository: _RepairStatusRepository,
    run: RepairQualificationRun | None,
) -> tuple[RepairBoundarySnapshot | None, RepairBoundaryDriftReason | None]:
    """Read persisted evidence only; expected drift never masks malformed ledger state."""
    if run is None:
        return None, None
    from src.graph.crm_deal_identity_repair_ledger import ExpectedRepairBoundaryDriftError

    try:
        snapshot = repository.snapshot(
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


class _UnavailableTaskInspector:
    """Defensive pause-only placeholder: it cannot inspect runtime workers."""

    def inspect(
        self,
        _expected_workers: tuple[str, ...],
        _tasks: tuple[RepairTaskIdentity, ...],
        _timeout_seconds: float,
    ) -> RepairTaskInspection:
        raise RuntimeError("live task inspection is disabled; provide an offline proof for resume")


class _UnavailableBrokerInspector:
    """Defensive pause-only placeholder: it cannot inspect a broker."""

    def has_queued_delivery(
        self,
        _tasks: tuple[RepairTaskIdentity, ...],
        _timeout_seconds: float,
    ) -> bool | None:
        return None


class _RecordedTaskInspector:
    """Offline evidence adapter: no Celery control command is constructed here."""

    def __init__(self, inspection: RepairTaskInspection) -> None:
        self._inspection = inspection

    def inspect(
        self,
        _expected_workers: tuple[str, ...],
        _tasks: tuple[RepairTaskIdentity, ...],
        _timeout_seconds: float,
    ) -> RepairTaskInspection:
        return self._inspection


class _RecordedBrokerInspector:
    """Offline broker evidence adapter; its single result is deliberately fail-closed."""

    def __init__(self, queued: bool | None) -> None:
        self._queued = queued

    def has_queued_delivery(
        self,
        _tasks: tuple[RepairTaskIdentity, ...],
        _timeout_seconds: float,
    ) -> bool | None:
        return self._queued


def _recorded_task_proof(
    path: Path,
) -> tuple[
    tuple[str, ...],
    tuple[RepairTaskIdentity, ...],
    RepairTaskInspector,
    RepairBrokerInspector,
]:
    """Parse a bounded operator-supplied proof fixture; it never talks to Celery or Redis."""
    from src.crm_deal_identity_repair.task_inspection import RepairObservedTask

    decoded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise RuntimeError("task proof must be a JSON object")
    payload: dict[str, object] = {key: value for key, value in decoded.items()}
    allowed_fields = {
        "expected_workers", "responders", "tasks", "active", "reserved", "scheduled",
        "queued", "unknown_task_ids", "inspection_failed", "timed_out", "reply_errors",
        "broker_queued",
    }
    if set(payload) - allowed_fields:
        raise RuntimeError("task proof contains unsupported fields")

    def text_tuple(name: str) -> tuple[str, ...]:
        value = payload.get(name)
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise RuntimeError(f"task proof {name} must be non-empty strings")
        result = tuple(value)
        if len(set(result)) != len(result):
            raise RuntimeError(f"task proof {name} must not contain duplicates")
        return result

    def observations(name: str) -> tuple[RepairObservedTask, ...]:
        value = payload.get(name, [])
        if not isinstance(value, list):
            raise RuntimeError(f"task proof {name} must be a list")
        parsed: list[RepairObservedTask] = []
        for item in value:
            if not isinstance(item, dict):
                raise RuntimeError(f"task proof {name} contains an invalid observation")
            task_id = _optional_task_text(item.get("task_id"), name)
            task_name = _optional_task_text(item.get("task_name"), name)
            queue = _optional_task_text(item.get("queue"), name)
            kwargs_digest = _optional_task_text(item.get("kwargs_digest"), name)
            parsed.append(RepairObservedTask(task_id, task_name, queue, kwargs_digest))
        return tuple(parsed)

    task_values = payload.get("tasks")
    if not isinstance(task_values, list):
        raise RuntimeError("task proof tasks must be a list")
    tasks: list[RepairTaskIdentity] = []
    for item in task_values:
        if not isinstance(item, dict):
            raise RuntimeError("task proof contains an invalid expected task")
        task_id = _required_task_text(item.get("task_id"), "task_id")
        task_name = _required_task_text(item.get("task_name"), "task_name")
        queue = _required_task_text(item.get("queue"), "queue")
        kwargs_digest = _required_task_text(item.get("kwargs_digest"), "kwargs_digest")
        tasks.append(RepairTaskIdentity(task_id, task_name, queue, kwargs_digest))
    if len({task.task_id for task in tasks}) != len(tasks):
        raise RuntimeError("task proof expected tasks must not contain duplicates")
    inspection_failed = payload.get("inspection_failed", False)
    timed_out = payload.get("timed_out", False)
    if not isinstance(inspection_failed, bool) or not isinstance(timed_out, bool):
        raise RuntimeError("task proof inspection flags must be booleans")
    inspection = RepairTaskInspection(
        responders=text_tuple("responders"),
        active=observations("active"),
        reserved=observations("reserved"),
        scheduled=observations("scheduled"),
        queued=observations("queued"),
        unknown_task_ids=text_tuple("unknown_task_ids") if "unknown_task_ids" in payload else (),
        inspection_failed=inspection_failed,
        timed_out=timed_out,
        reply_errors=text_tuple("reply_errors") if "reply_errors" in payload else (),
    )
    queued_value = payload.get("broker_queued")
    if queued_value is not None and not isinstance(queued_value, bool):
        raise RuntimeError("task proof broker_queued must be boolean or null")
    return (
        text_tuple("expected_workers"),
        tuple(tasks),
        _RecordedTaskInspector(inspection),
        _RecordedBrokerInspector(queued_value),
    )


def _optional_task_text(value: object, section: str) -> str | None:
    """Validate optional recorded task identity fields without coercion."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"task proof {section} contains invalid task text")
    return value


def _required_task_text(value: object, field: str) -> str:
    """Validate one non-empty expected task identity field."""
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"task proof expected {field} must be a non-empty string")
    return value


def _repair_overlay_signing_secret(settings: _RepairRuntimeSettings) -> bytes:
    """Read the restricted overlay HMAC key without logging or serializing it."""
    value = settings.crm_deal_identity_repair_approval_overlay_verification_secret.get_secret_value()
    if not value:
        raise RuntimeError("repair allocation requires a configured overlay verification secret")
    return value.encode("utf-8")


def _control_command(arguments: Namespace) -> int:
    """Run gated control-plane proof transitions only; it never dispatches or mutates CRM data."""
    from src.config import get_settings
    from src.graph.client import Neo4jClient
    from src.graph.crm_deal_identity_repair_control import CrmDealRepairControlRepository
    from src.graph.crm_deal_identity_repair_ledger import CrmDealRepairLedgerRepository
    from src.graph.crm_deal_identity_repair_ledger_migration import (
        assert_crm_deal_repair_ledger_ready,
    )
    from src.crm_deal_identity_repair.approval_overlay import (
        allocation_digest,
        allocate_units,
        verify_sealed_approval_overlay,
    )
    from src.crm_deal_identity_repair.control_models import (
        RepairAllocationCompletion,
        RepairControlLease,
    )
    from src.crm_deal_identity_repair.quiescence import (
        RepairQuiescenceRequest,
        RepairQuiescenceService,
    )

    settings = get_settings()
    _validate_runtime_gate(settings, require_enabled=True)
    client = Neo4jClient(settings)
    try:
        assert_crm_deal_repair_ledger_ready(client)
        ledger = CrmDealRepairLedgerRepository(client)
        run = ledger.get_qualification(arguments.repair_id)
        if run is None:
            raise RuntimeError("repair is not qualified")
        repository = CrmDealRepairControlRepository(client, run.control_instance_id)
        existing = repository.read(run.run_id)
        if arguments.command in {"quiesce", "resume"}:
            expected_workers, tasks, inspector, broker = _recorded_task_proof(
                Path(arguments.task_proof_file)
            )
            configured_workers = settings.crm_deal_identity_repair_expected_workers
            if not configured_workers or expected_workers != configured_workers:
                raise RuntimeError(
                    "task proof workers must exactly match configured repair workers"
                )
            timeout_seconds = min(
                arguments.task_timeout_seconds,
                float(settings.crm_deal_identity_repair_worker_timeout_seconds),
            )
            service = RepairQuiescenceService(repository, ledger, inspector, broker)
        if arguments.command == "quiesce":
            if existing is None:
                lease = RepairControlLease(
                    run.run_id, arguments.owner_id, arguments.control_token, 1, "quiescing",
                    run.boundary_digest,
                )
                expected_revision = 0
            else:
                lease = RepairControlLease(
                    run.run_id, arguments.owner_id, arguments.control_token,
                    arguments.expected_revision + 1, "quiescing", run.boundary_digest,
                )
                expected_revision = arguments.expected_revision
            result = service.quiesce(
                RepairQuiescenceRequest(
                    repair_id=arguments.repair_id, lease=lease, expected_revision=expected_revision,
                    expected_workers=expected_workers, tasks=tasks,
                    timeout_seconds=timeout_seconds, stale_run_id=arguments.stale_run_id,
                )
            )
        elif existing is None:
            raise RuntimeError("repair control has not been claimed")
        elif existing.owner_id != arguments.owner_id or existing.token != arguments.control_token:
            raise RuntimeError("repair control ownership was lost")
        elif arguments.command == "pause":
            service = RepairQuiescenceService(
                repository,
                ledger,
                _UnavailableTaskInspector(),
                _UnavailableBrokerInspector(),
            )
            result = service.pause(arguments.repair_id, existing, arguments.expected_revision)
        elif arguments.command == "resume":
            result = service.resume(
                arguments.repair_id, existing, arguments.expected_revision, expected_workers, tasks,
                timeout_seconds,
            )
        elif arguments.command == "allocate":
            if existing.owner_id != arguments.owner_id or existing.token != arguments.control_token:
                raise RuntimeError("repair allocation control ownership was lost")
            manifest = ledger.get_execution_manifest(arguments.repair_id)
            if manifest is None:
                raise RuntimeError("repair allocation manifest is missing")
            signing_secret = _repair_overlay_signing_secret(settings)
            overlay = verify_sealed_approval_overlay(
                Path(arguments.approval_overlay).read_bytes(),
                manifest=manifest,
                signing_secret=signing_secret,
            )
            units = allocate_units(overlay, run_id=run.run_id, manifest=manifest, generation=1)
            completion = RepairAllocationCompletion(
                run_id=run.run_id, allocation_digest=allocation_digest(overlay, units),
                executable_count=len(units), unit_count=len(units),
            )
            repository.allocate(
                existing,
                arguments.expected_revision,
                units,
                completion,
                overlay,
                manifest,
                ledger.source_record_pks(arguments.repair_id),
            )
            refreshed = repository.read(run.run_id)
            if refreshed is None:
                raise RuntimeError("repair allocation control readback is missing")
            result = refreshed
        else:
            raise RuntimeError("unsupported repair control command")
    finally:
        client.close()
    print(json.dumps({
        "repair_id": arguments.repair_id, "state": result.state, "revision": result.revision,
        "execution_allowed": False,
    }, sort_keys=True))
    return 0
