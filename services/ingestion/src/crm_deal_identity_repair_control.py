"""Read-only operator command for CRM-deal identity repair inventory (#254)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

from src.crm_deal_identity_repair.cli import parse_arguments
from src.models import JsonValue


class _RepairRuntimeSettings(Protocol):
    @property
    def deployment_environment(self) -> str: ...

    @property
    def crm_deal_identity_repair_enabled(self) -> bool: ...


def main(argv: Sequence[str] | None = None) -> int:
    """Run one staging-only, read-only graph inventory and seal its evidence."""
    arguments = parse_arguments(argv)

    from src.config import get_settings
    from src.crm_deal_identity_repair.artifacts import (
        RepairArtifactContext,
        repair_artifact_store_from_settings,
        repair_inventory_configuration_digest,
        seal_inventory_artifact,
    )
    from src.crm_deal_identity_repair.digests import inventory_digest
    from src.crm_deal_identity_repair.inventory import collect_repair_inventory
    from src.graph.client import Neo4jClient

    settings = get_settings()
    _validate_runtime_gate(settings)
    context = RepairArtifactContext(
        repair_id=arguments.repair_id,
        environment="staging",
        source_contract_uuid=arguments.source_contract_uuid,
        repository_sha=settings.crm_deal_identity_repair_repository_sha,
        image_digest=settings.crm_deal_identity_repair_image_digest,
        configuration_digest=repair_inventory_configuration_digest(settings),
        boundary={
            "source_system": arguments.source_system,
            "inventory_mode": "graph_only_read_only",
            "artifact_scope": "graph_discovery_only",
            "execution_allowed": False,
        },
        retention_expires_at=datetime.now(UTC) + timedelta(days=arguments.retention_days),
    )
    with repair_artifact_store_from_settings(settings) as store:
        client = Neo4jClient(settings)
        try:
            inventory = collect_repair_inventory(
                client,
                source_system=arguments.source_system,
                negative_control_limit=arguments.negative_control_limit,
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
        )
    summary_population_counts: dict[str, JsonValue] = dict(population_counts)
    summary: dict[str, JsonValue] = {
        "artifact_id": manifest.artifact_id,
        "inventory_digest": inventory_digest(inventory.items),
        "ownership_repair_count": len(inventory.ownership_repairs),
        "projection_cleanup_count": len(inventory.projection_cleanups),
        "negative_control_count": len(inventory.negative_controls),
        "population_counts": summary_population_counts,
        "artifact_scope": "graph_discovery_only",
        "execution_allowed": False,
        "execution_blocker": "separate #255 execution scope is required",
        "stale_run_state": inventory.stale_run_evidence["state"],
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


def _validate_runtime_gate(settings: _RepairRuntimeSettings) -> None:
    if settings.deployment_environment != "staging":
        raise RuntimeError("CRM-deal repair inventory requires DEPLOYMENT_ENVIRONMENT=staging")
    if not settings.crm_deal_identity_repair_enabled:
        raise RuntimeError(
            "CRM-deal repair inventory requires CRM_DEAL_IDENTITY_REPAIR_ENABLED=true"
        )
