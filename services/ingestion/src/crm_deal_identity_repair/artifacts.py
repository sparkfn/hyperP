"""Sealed restricted artifacts for read-only CRM-deal repair inventory."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from src.connectors.bitrix_stage_history.artifact_manifest import (
    ArtifactManifest,
    canonical_json_bytes,
)
from src.connectors.bitrix_stage_history.artifact_provenance import ArtifactProvenanceInput
from src.connectors.bitrix_stage_history.artifact_runtime import (
    ArtifactStoreConfiguration,
    decode_signing_secret,
    sha256_digest,
)
from src.connectors.bitrix_stage_history.artifact_store import LocalRestrictedArtifactStore
from src.crm_deal_identity_repair.digests import inventory_digest, inventory_jsonl
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.models import JsonValue

if TYPE_CHECKING:
    from src.config import Settings

CRM_DEAL_IDENTITY_REPAIR_MANIFEST_HMAC_DOMAIN = b"crm-deal-identity-repair-manifest-v1\x00"
_ARTIFACT_KIND = "crm-deal-identity-repair-graph-discovery"
_REQUIRED_POPULATION_COUNTS = {
    "active_deal_count",
    "authoritative_version_count",
    "active_link_count",
    "active_distinct_owner_count",
    "multi_linked_deal_count",
    "maximum_links_per_deal",
    "maximum_distinct_owners_per_deal",
    "projection_cleanup_deal_count",
    "clean_deal_count",
}


def repair_inventory_configuration_digest(settings: Settings) -> str:
    """Digest the non-secret runtime settings that shape read-only inventory."""
    payload: dict[str, JsonValue] = {
        "deployment_environment": settings.deployment_environment,
        "artifact_primary_root": settings.crm_deal_identity_repair_artifact_primary_root,
        "artifact_backup_root": settings.crm_deal_identity_repair_artifact_backup_root,
        "artifact_signing_key_id": settings.crm_deal_identity_repair_artifact_signing_key_id,
        "repository_sha": settings.crm_deal_identity_repair_repository_sha,
        "image_digest": settings.crm_deal_identity_repair_image_digest,
        "identity_policy_version": "crm_deal_identity_v2",
    }
    return sha256_digest(canonical_json_bytes(payload))


def repair_artifact_store_from_settings(settings: Settings) -> LocalRestrictedArtifactStore:
    """Open the repair store without exposing its signing secret to callers.

    The object boundary avoids importing ``Settings`` at runtime and keeps this
    module usable in narrowly configured operator tests.
    """
    secret = decode_signing_secret(
        settings.crm_deal_identity_repair_artifact_signing_key_secret.get_secret_value()
    )
    return ArtifactStoreConfiguration(
        primary_root=Path(settings.crm_deal_identity_repair_artifact_primary_root),
        backup_root=Path(settings.crm_deal_identity_repair_artifact_backup_root),
        signing_key_id=settings.crm_deal_identity_repair_artifact_signing_key_id,
        signing_key_secret=secret,
        hmac_domain=CRM_DEAL_IDENTITY_REPAIR_MANIFEST_HMAC_DOMAIN,
    ).open()


@dataclass(frozen=True)
class RepairArtifactContext:
    """Immutable operator/build boundary authenticated with repair inventory."""

    repair_id: str
    environment: str
    source_contract_uuid: str
    repository_sha: str
    image_digest: str
    configuration_digest: str
    boundary: Mapping[str, JsonValue]
    retention_expires_at: datetime

    def __post_init__(self) -> None:
        if not self.repair_id:
            raise ValueError("repair artifact ID must be non-empty")
        if self.environment != "staging":
            raise ValueError("CRM-deal repair inventory is staging-only")
        if not self.boundary:
            raise ValueError("repair artifact boundary must be non-empty")
        ArtifactProvenanceInput.create(
            source_contract_uuid=self.source_contract_uuid,
            repository_sha=self.repository_sha,
            image_digest=self.image_digest,
            configuration_digest=self.configuration_digest,
            restricted_boundaries=self.boundary,
            counts={"preflight": 1},
        )


def seal_inventory_artifact(
    store: LocalRestrictedArtifactStore,
    *,
    context: RepairArtifactContext,
    items: tuple[RepairInventoryItem, ...],
    source_snapshots: tuple[dict[str, JsonValue], ...],
    proposed_versions: tuple[dict[str, JsonValue], ...],
    rollback_template: tuple[dict[str, JsonValue], ...],
    population_counts: Mapping[str, int],
) -> ArtifactManifest:
    """Seal non-executable graph discovery without creating any graph state."""
    if not items:
        raise ValueError("repair inventory cannot be empty")
    _validate_population_counts(population_counts)
    digest = inventory_digest(items)
    expected_items = {item.inventory_key: item for item in items}
    ordered_snapshots = _validated_auxiliary_rows(
        "source snapshots", source_snapshots, expected_items
    )
    ordered_proposals = _validated_auxiliary_rows(
        "proposed version placeholders", proposed_versions, expected_items
    )
    ordered_rollback = _validated_auxiliary_rows(
        "rollback discovery rows", rollback_template, expected_items
    )
    counts = _partition_counts(items)
    _validate_condition_counts(counts, population_counts)
    condition_counts: dict[str, JsonValue] = dict(counts)
    verification_plan: dict[str, JsonValue] = {
        "inventory_digest": digest,
        "repair_condition_counts": condition_counts,
        "artifact_scope": "graph_discovery_only",
        "execution_ready": False,
        "required_invariants": [
            "inventory_digest_reproduces",
            "read_only_inventory_writes_zero_neo4j_records",
            "negative_controls_remain_unchanged",
            "bitrix_hydration_required_before_execution_artifact",
        ],
    }
    with store.begin(artifact_kind=_ARTIFACT_KIND) as artifact:
        artifact.write_bytes("inventory.jsonl", inventory_jsonl(items))
        artifact.write_bytes("graph-source-snapshots.jsonl", _jsonl(ordered_snapshots))
        artifact.write_bytes("unhydrated-v2-placeholders.jsonl", _jsonl(ordered_proposals))
        artifact.write_bytes("graph-rollback-discovery.jsonl", _jsonl(ordered_rollback))
        artifact.write_json("verification-plan.json", verification_plan)
        return artifact.seal(
            metadata={
                "repair_id": context.repair_id,
                "environment": context.environment,
                "artifact_scope": "graph_discovery_only",
                "execution_ready": False,
                "execution_blocker": "Bitrix hydration and executable rollback are absent",
                "inventory_digest": digest,
                "repair_condition_counts": condition_counts,
                "population_counts": dict(population_counts),
                "graph_snapshot_count": len(ordered_snapshots),
                "unhydrated_placeholder_count": len(ordered_proposals),
                "graph_rollback_discovery_count": len(ordered_rollback),
            },
            provenance=ArtifactProvenanceInput.create(
                source_contract_uuid=context.source_contract_uuid,
                repository_sha=context.repository_sha,
                image_digest=context.image_digest,
                configuration_digest=context.configuration_digest,
                restricted_boundaries=dict(context.boundary),
                counts={
                    "inventory_rows": len(items),
                    **counts,
                    **population_counts,
                },
            ),
            retention_expires_at=context.retention_expires_at,
        )


def _partition_counts(items: tuple[RepairInventoryItem, ...]) -> dict[str, int]:
    identities: dict[str, set[str]] = {
        "ownership_repair": set(),
        "projection_cleanup": set(),
        "negative_control": set(),
    }
    for item in items:
        for condition in item.repair_conditions:
            identities[condition].add(item.source_record_id)
    return {condition: len(source_ids) for condition, source_ids in identities.items()}


def _validated_auxiliary_rows(
    label: str,
    rows: tuple[dict[str, JsonValue], ...],
    expected_items: Mapping[str, RepairInventoryItem],
) -> tuple[dict[str, JsonValue], ...]:
    by_identity: dict[str, dict[str, JsonValue]] = {}
    for row in rows:
        identity = _auxiliary_identity(row, label)
        expected_item = expected_items.get(identity)
        if expected_item is None:
            raise ValueError(f"repair {label} contain an extra identity: {identity}")
        _validate_auxiliary_schema(label, row, expected_item)
        if identity in by_identity:
            raise ValueError(f"repair {label} contain duplicate identity: {identity}")
        by_identity[identity] = row
    if set(by_identity) != set(expected_items):
        raise ValueError(f"repair {label} identities must exactly match repair inventory")
    return tuple(by_identity[identity] for identity in sorted(expected_items))


def _auxiliary_identity(row: dict[str, JsonValue], label: str) -> str:
    values: list[str] = []
    for key in ("source_system", "source_record_id", "source_record_pk"):
        value = row.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"repair {label} {key} must be a non-empty string")
        values.append(value)
    return "|".join(values)


def _validate_auxiliary_schema(
    label: str,
    row: dict[str, JsonValue],
    expected_item: RepairInventoryItem,
) -> None:
    identity_fields = {"source_system", "source_record_id", "source_record_pk"}
    if label == "source snapshots":
        expected_fields = identity_fields | {
            "status",
            "live_source_fingerprint",
            "stored_payload_fingerprint",
            "execution_allowed",
        }
        if set(row) != expected_fields:
            raise ValueError("repair source snapshot fields are invalid")
        if (
            row.get("status") != "requires_live_bitrix_hydration"
            or row.get("live_source_fingerprint") is not None
            or row.get("execution_allowed") is not False
        ):
            raise ValueError("repair source snapshots must be non-executable hydration plans")
        _validate_digest(row.get("stored_payload_fingerprint"), "stored payload")
        if row.get("stored_payload_fingerprint") != expected_item.stored_payload_fingerprint:
            raise ValueError("repair source snapshot fingerprint does not match inventory")
        return
    if label == "proposed version placeholders":
        expected_fields = identity_fields | {"status", "execution_allowed"}
        if set(row) != expected_fields:
            raise ValueError("repair proposed version placeholder fields are invalid")
        if (
            row.get("status") != "requires_bitrix_source_hydration"
            or row.get("execution_allowed") is not False
        ):
            raise ValueError("repair proposed versions must be unhydrated placeholders")
        return
    if label == "rollback discovery rows":
        expected_fields = identity_fields | {
            "graph_fingerprint",
            "captured_relationships",
        }
        if set(row) != expected_fields:
            raise ValueError("repair rollback discovery fields are invalid")
        _validate_digest(row.get("graph_fingerprint"), "graph")
        if row.get("graph_fingerprint") != expected_item.graph_fingerprint:
            raise ValueError("repair rollback fingerprint does not match inventory")
        if not isinstance(row.get("captured_relationships"), dict):
            raise ValueError("repair rollback discovery must contain captured relationships")
        if row.get("captured_relationships") != expected_item.payload:
            raise ValueError("repair rollback discovery payload does not match inventory")
        return
    raise ValueError(f"unsupported repair auxiliary row label: {label}")


def _validate_population_counts(population_counts: Mapping[str, int]) -> None:
    if set(population_counts) != _REQUIRED_POPULATION_COUNTS:
        raise ValueError("repair inventory population count fields are invalid")
    for key, value in population_counts.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"repair inventory population count is invalid: {key}")
    if population_counts["authoritative_version_count"] < population_counts["active_deal_count"]:
        raise ValueError("repair authoritative version count cannot be below logical deal count")
    active_deals = population_counts["active_deal_count"]
    multi_linked = population_counts["multi_linked_deal_count"]
    cleanup = population_counts["projection_cleanup_deal_count"]
    clean = population_counts["clean_deal_count"]
    if max(multi_linked, cleanup) + clean > active_deals:
        raise ValueError("repair population classifications exceed logical deal count")
    if population_counts["active_distinct_owner_count"] > population_counts[
        "active_link_count"
    ]:
        raise ValueError("repair distinct owner count cannot exceed active link count")
    if population_counts["maximum_distinct_owners_per_deal"] > population_counts[
        "maximum_links_per_deal"
    ]:
        raise ValueError("repair maximum distinct owners cannot exceed maximum links")


def _validate_condition_counts(
    condition_counts: Mapping[str, int],
    population_counts: Mapping[str, int],
) -> None:
    if condition_counts["ownership_repair"] != population_counts["multi_linked_deal_count"]:
        raise ValueError("repair ownership rows do not match population counts")
    if condition_counts["projection_cleanup"] != population_counts[
        "projection_cleanup_deal_count"
    ]:
        raise ValueError("repair cleanup rows do not match population counts")
    if condition_counts["negative_control"] > population_counts["clean_deal_count"]:
        raise ValueError("repair negative-control sample exceeds clean population")


def _validate_digest(value: JsonValue | None, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"repair {label} fingerprint must be a sha256 digest")
    prefix, separator, hexadecimal = value.partition(":")
    if (
        prefix != "sha256"
        or separator != ":"
        or len(hexadecimal) != 64
        or any(character not in "0123456789abcdef" for character in hexadecimal)
    ):
        raise ValueError(f"repair {label} fingerprint must be a sha256 digest")


def _jsonl(rows: tuple[dict[str, JsonValue], ...]) -> bytes:
    return b"".join(canonical_json_bytes(row) for row in rows)
