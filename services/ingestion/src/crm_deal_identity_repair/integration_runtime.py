"""Typed disabled-by-default runtime factory for the six #313 commands."""

from __future__ import annotations

import json
import os
import re
from argparse import Namespace
from collections.abc import Callable
from pathlib import Path

from src.config import Settings, get_settings
from src.crm_deal_identity_repair.approval_overlay import (
    assert_overlay_binds_qualification,
    verify_approval_overlay,
)
from src.crm_deal_identity_repair.artifacts import repair_artifact_store_from_settings
from src.crm_deal_identity_repair.control_models import RepairControlRequest
from src.crm_deal_identity_repair.integration_models import RepairIntegrationRequest
from src.crm_deal_identity_repair.integration_service import (
    CrmDealIdentityRepairIntegrationService,
    RepairIntegrationContext,
)
from src.crm_deal_identity_repair.models import RepairInventoryItem, inventory_item_from_json
from src.crm_deal_identity_repair.mutation_service import CrmDealIdentityRepairMutationService
from src.crm_deal_identity_repair.qualification import (
    VerifiedRepairArtifact,
    iter_verified_inventory_lines,
    verify_qualified_repair_artifact,
)
from src.crm_deal_identity_repair.rollback_service import CrmDealIdentityRepairRollbackService
from src.crm_deal_identity_repair.verification_service import RepairVerificationService
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_integration import CrmDealRepairIntegrationRepository
from src.graph.crm_deal_identity_repair_ledger import CrmDealRepairLedgerRepository
from src.graph.crm_deal_identity_repair_ledger_migration import assert_crm_deal_repair_ledger_ready
from src.graph.crm_deal_identity_repair_mutation import CrmDealIdentityRepairMutationRepository
from src.graph.crm_deal_identity_repair_rollback import CrmDealIdentityRepairRollbackRepository
from src.graph.crm_deal_identity_repair_verification import (
    CrmDealIdentityRepairVerificationRepository,
)


def execute_integration(arguments: Namespace) -> dict[str, str]:
    """Execute exactly one operator-selected transition and return redacted evidence."""
    settings = get_settings()
    _validate_gate(settings.deployment_environment, settings.crm_deal_identity_repair_enabled)
    control_secret = os.environ.get("CRM_DEAL_IDENTITY_REPAIR_CONTROL_TOKEN")
    if not control_secret:
        raise RuntimeError(
            "repair control token must be supplied through its secret environment channel"
        )
    request = RepairIntegrationRequest(
        operation=arguments.command,
        control=RepairControlRequest(
            arguments.repair_id,
            arguments.run_id,
            arguments.owner_id,
            control_secret,
            arguments.expected_revision,
        ),
        approval_id=arguments.approval_id,
        unit_id=arguments.unit_id,
        authorization_reference=arguments.authorization_reference,
        predecessor_transition_id=arguments.predecessor_transition_id,
    )
    approval_secret = (
        settings.crm_deal_identity_repair_approval_key_secret.get_secret_value().encode()
    )
    approval_key_id = settings.crm_deal_identity_repair_approval_key_id
    if not approval_secret or not approval_key_id:
        raise RuntimeError("repair approval overlay signing configuration is missing")
    client = Neo4jClient(settings)
    try:
        assert_crm_deal_repair_ledger_ready(client)
        ledger = CrmDealRepairLedgerRepository(client)
        integration = CrmDealRepairIntegrationRepository(client)
        service = CrmDealIdentityRepairIntegrationService(
            integration,
            _context_loader(
                request, ledger, integration, settings, approval_secret, approval_key_id
            ),
            CrmDealIdentityRepairMutationService(CrmDealIdentityRepairMutationRepository(client)),
            RepairVerificationService(CrmDealIdentityRepairVerificationRepository(client)),
            CrmDealIdentityRepairRollbackService(CrmDealIdentityRepairRollbackRepository(client)),
            _rollback_authorization_token_digest
            if request.operation in {"rollback-status", "rollback"}
            else None,
        )
        receipt = service.execute(request)
    finally:
        client.close()
    return {
        "operation": receipt.operation,
        "state": receipt.state,
        "request_digest": receipt.request_digest,
        "receipt_digest": receipt.receipt_digest,
    }


def _context_loader(
    request: RepairIntegrationRequest,
    ledger: CrmDealRepairLedgerRepository,
    integration: CrmDealRepairIntegrationRepository,
    settings: Settings,
    approval_secret: bytes,
    approval_key_id: str,
) -> Callable[[RepairIntegrationRequest], RepairIntegrationContext]:
    """Build an artifact-authenticated context before each CAS operation."""

    def load(_: RepairIntegrationRequest) -> RepairIntegrationContext:
        run = ledger.get_qualification(request.control.repair_id)
        if run is None or run.run_id != request.control.run_id:
            raise RuntimeError("repair integration requires exact qualified run")
        overlay_path = _approval_overlay_path(
            settings.crm_deal_identity_repair_approval_root, request.approval_id
        )
        overlay = verify_approval_overlay(overlay_path, secret=approval_secret)
        if overlay.approval_id != request.approval_id:
            raise RuntimeError("repair approval overlay ID does not match the request")
        assert_overlay_binds_qualification(overlay, run=run, expected_key_id=approval_key_id)
        with repair_artifact_store_from_settings(settings) as store:
            verified = verify_qualified_repair_artifact(store, run=run)
        inventory = _read_inventory(verified)
        authority = integration.load_authority(
            request, run, overlay.overlay_digest, approval_key_id, approval_secret
        )
        if request.operation == "apply" and not integration.has_execution_evidence(request, run):
            if integration.has_any_execution_evidence(run):
                integration.assert_next_unit_boundary(request, run, inventory)
            else:
                snapshot = ledger.snapshot(
                    source_instance_id=run.source_instance_id,
                    control_instance_id=run.control_instance_id,
                    source_record_pks=ledger.source_record_pks(run.repair_id),
                )
                if snapshot.boundary_digest != run.boundary_digest:
                    raise RuntimeError("repair integration boundary drift detected")
        return RepairIntegrationContext(run, inventory, authority)

    return load


def _approval_overlay_path(root_value: str, approval_id: str) -> Path:
    """Resolve one approved overlay without allowing a request to escape its authority root."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", approval_id):
        raise RuntimeError("repair approval ID is not a file identity")
    if approval_id.endswith((".", " ")):
        raise RuntimeError("repair approval ID is not a file identity")
    basename = approval_id.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    if basename in reserved:
        raise RuntimeError("repair approval ID is not a file identity")
    root = Path(root_value).resolve(strict=True)
    candidate = (root / f"{approval_id}.json").resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("repair approval overlay escapes its configured root") from exc
    return candidate


def _read_inventory(artifact: VerifiedRepairArtifact) -> tuple[RepairInventoryItem, ...]:
    """Strictly decode the qualified immutable inventory artifact."""
    rows: list[RepairInventoryItem] = []
    for line in iter_verified_inventory_lines(artifact):
        rows.append(inventory_item_from_json(json.loads(line)))
    inventory = tuple(rows)
    if not inventory:
        raise RuntimeError("repair qualified inventory is empty")
    return inventory


def _validate_gate(environment: str, enabled: bool) -> None:
    if environment != "staging":
        raise RuntimeError("CRM-deal repair requires DEPLOYMENT_ENVIRONMENT=staging")
    if not enabled:
        raise RuntimeError("CRM-deal repair requires CRM_DEAL_IDENTITY_REPAIR_ENABLED=true")


def _rollback_authorization_token_digest() -> str:
    """Read the #312 digest only from the approved non-payload environment channel."""
    from src.crm_deal_identity_repair.execution_records import _digest

    value = os.environ.get("CRM_DEAL_IDENTITY_REPAIR_ROLLBACK_AUTHORIZATION_TOKEN_DIGEST")
    if not value:
        raise RuntimeError("repair rollback authorization digest secret is missing")
    _digest(value, "rollback authorization token digest")
    return value
