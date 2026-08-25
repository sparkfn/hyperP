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
            "execution_ready": False,
            "observation_fenced": False,
            "source_hydration": "required_before_execution",
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
        snapshots = tuple(_source_snapshot(item.to_dict()) for item in inventory.items)
        proposed_versions = tuple(_unhydrated_proposal(item.to_dict()) for item in inventory.items)
        rollback_template = tuple(_rollback_template(item.to_dict()) for item in inventory.items)
        population_counts = inventory.population_counts.to_dict()
        manifest = seal_inventory_artifact(
            store,
            context=context,
            items=inventory.items,
            source_snapshots=snapshots,
            proposed_versions=proposed_versions,
            rollback_template=rollback_template,
            population_counts=population_counts,
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
        "execution_ready": False,
        "execution_blocker": "Bitrix source hydration and separate #255 approval are required",
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


def _source_snapshot(item: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "source_system": item["source_system"],
        "source_record_id": item["source_record_id"],
        "source_record_pk": item["source_record_pk"],
        "status": "requires_live_bitrix_hydration",
        "live_source_fingerprint": None,
        "stored_payload_fingerprint": item["stored_payload_fingerprint"],
        "execution_allowed": False,
    }


def _unhydrated_proposal(item: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "source_system": item["source_system"],
        "source_record_id": item["source_record_id"],
        "source_record_pk": item["source_record_pk"],
        "status": "requires_bitrix_source_hydration",
        "execution_allowed": False,
    }


def _rollback_template(item: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "source_system": item["source_system"],
        "source_record_id": item["source_record_id"],
        "source_record_pk": item["source_record_pk"],
        "graph_fingerprint": item["graph_fingerprint"],
        "captured_relationships": item["payload"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
