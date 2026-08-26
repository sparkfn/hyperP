"""Sealed, read-only graph-discovery artifacts for CRM-deal repair inventory."""

from __future__ import annotations

import hashlib
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
_MAX_REPLAY_IDS = 100
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
_PRIOR_246_BASELINE: dict[str, int] = {
    "active_deal_count": 133_146,
    "active_link_count": 134_975,
    "multi_linked_deal_count": 1_255,
    "maximum_distinct_owners_per_deal": 5,
}


def repair_inventory_configuration_digest(settings: Settings) -> str:
    """Digest the non-secret settings that shape graph-only inventory."""
    payload: dict[str, JsonValue] = {
        "deployment_environment": settings.deployment_environment,
        "artifact_primary_root": settings.crm_deal_identity_repair_artifact_primary_root,
        "artifact_backup_root": settings.crm_deal_identity_repair_artifact_backup_root,
        "artifact_signing_key_id": settings.crm_deal_identity_repair_artifact_signing_key_id,
        "repository_sha": settings.crm_deal_identity_repair_repository_sha,
        "image_digest": settings.crm_deal_identity_repair_image_digest,
        "inventory_contract": "crm-deal-graph-discovery-v2",
    }
    return sha256_digest(canonical_json_bytes(payload))


def repair_artifact_store_from_settings(settings: Settings) -> LocalRestrictedArtifactStore:
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
    population_counts: Mapping[str, int],
    stale_run_evidence: Mapping[str, JsonValue],
    representative_replay_limit: int = _MAX_REPLAY_IDS,
) -> ArtifactManifest:
    """Seal graph evidence and descriptive #255 handoff guidance only."""
    if not items:
        raise ValueError("repair inventory cannot be empty")
    _validate_population_counts(population_counts)
    if representative_replay_limit < 1:
        raise ValueError("repair representative replay limit must be positive")
    digest = inventory_digest(items)
    impact = _impact_summary(items, population_counts)
    replay = _representative_replay(items, limit=representative_replay_limit)
    compensation = _compensation_guidance(items)
    clean_boundary = _clean_boundary(impact, replay, stale_run_evidence)
    with store.begin(artifact_kind=_ARTIFACT_KIND) as artifact:
        artifact.write_bytes("inventory.jsonl", inventory_jsonl(items))
        artifact.write_json("impact-summary.json", impact)
        artifact.write_json("representative-replay-plan.json", replay)
        artifact.write_json("compensation-guidance.json", compensation)
        artifact.write_json("stale-run-evidence.json", dict(stale_run_evidence))
        artifact.write_json("clean-boundary-plan.json", clean_boundary)
        return artifact.seal(
            metadata={
                "repair_id": context.repair_id,
                "environment": context.environment,
                "artifact_scope": "graph_discovery_only",
                "execution_allowed": False,
                "inventory_digest": digest,
                "population_counts": dict(population_counts),
                "stale_run_state": stale_run_evidence.get("state"),
            },
            provenance=ArtifactProvenanceInput.create(
                source_contract_uuid=context.source_contract_uuid,
                repository_sha=context.repository_sha,
                image_digest=context.image_digest,
                configuration_digest=context.configuration_digest,
                restricted_boundaries=dict(context.boundary),
                counts={"inventory_rows": len(items), **dict(population_counts)},
            ),
            retention_expires_at=context.retention_expires_at,
        )


def _impact_summary(
    items: tuple[RepairInventoryItem, ...],
    population_counts: Mapping[str, int],
) -> dict[str, JsonValue]:
    equations: dict[str, dict[str, int]] = {}
    lifecycle: dict[str, int] = {}
    impact_counts = {"descendants": 0, "reviews_decisions": 0, "owner_impacts": 0}
    for item in items:
        for condition in item.repair_conditions:
            equation = equations.setdefault(
                condition,
                {
                    "total": 0,
                    "ownership_repair": 0,
                    "projection_cleanup": 0,
                    "negative_control": 0,
                },
            )
            equation["total"] += 1
            equation[condition] += 1
        payload = item.payload
        policy = payload.get("lifecycle_policy_evidence")
        if isinstance(policy, dict):
            classification = policy.get("classification")
            if isinstance(classification, str):
                lifecycle[classification] = lifecycle.get(classification, 0) + 1
        for key, output in (
            ("descendants", "descendants"),
            ("decisions_and_reviews", "reviews_decisions"),
            ("owner_impacts", "owner_impacts"),
        ):
            value = payload.get(key)
            if not isinstance(value, list):
                raise ValueError("repair inventory closure evidence is invalid")
            impact_counts[output] += len(value)
    condition_equations: dict[str, JsonValue] = {}
    for condition_name, equation in equations.items():
        equation_json: dict[str, JsonValue] = {}
        for key, value in equation.items():
            equation_json[key] = value
        condition_equations[condition_name] = equation_json
    lifecycle_counts: dict[str, JsonValue] = {}
    for classification, count in lifecycle.items():
        lifecycle_counts[classification] = count
    closure_counts: dict[str, JsonValue] = {}
    for key, value in impact_counts.items():
        closure_counts[key] = value
    prior_counts: dict[str, JsonValue] = {}
    current_baseline_counts: dict[str, JsonValue] = {}
    baseline_deltas: dict[str, JsonValue] = {}
    for key, prior in _PRIOR_246_BASELINE.items():
        current = population_counts[key]
        prior_counts[key] = prior
        current_baseline_counts[key] = current
        baseline_deltas[key] = current - prior
    prior_evidence: dict[str, JsonValue] = {
        "source": "issue_246_prior_evidence",
        "is_current_truth": False,
        "counts": prior_counts,
        "fresh_authoritative_counts": current_baseline_counts,
        "deltas": baseline_deltas,
    }
    return {
        "schema_version": 1,
        "execution_allowed": False,
        "inventory_digest": inventory_digest(items),
        "population_counts": dict(population_counts),
        "condition_equations": condition_equations,
        "lifecycle_counts": lifecycle_counts,
        "closure_counts": closure_counts,
        "prior_246_evidence": prior_evidence,
    }


def _representative_replay(
    items: tuple[RepairInventoryItem, ...],
    *,
    limit: int,
) -> dict[str, JsonValue]:
    ordered = sorted(items, key=lambda item: item.inventory_key)
    selected: dict[str, str] = {}
    for item in ordered:
        for condition in item.repair_conditions:
            selected.setdefault("condition:" + condition, item.inventory_key)
        policy = item.payload.get("lifecycle_policy_evidence")
        if isinstance(policy, dict):
            classification = policy.get("classification")
            if isinstance(classification, str):
                selected.setdefault("lifecycle:" + classification, item.inventory_key)
    keys = list(sorted(set(selected.values()))[:limit])
    selected_keys = set(keys)
    for item in ordered:
        if len(keys) >= limit:
            break
        if item.inventory_key not in selected_keys:
            keys.append(item.inventory_key)
            selected_keys.add(item.inventory_key)
    rows: list[JsonValue] = [{"inventory_key": key, "execution_allowed": False} for key in keys]
    return {"schema_version": 1, "execution_allowed": False, "inventory_keys": rows}


def _compensation_guidance(items: tuple[RepairInventoryItem, ...]) -> dict[str, JsonValue]:
    rows: list[JsonValue] = []
    for item in sorted(items, key=lambda value: value.inventory_key):
        links = item.payload.get("linked_people")
        if not isinstance(links, list):
            raise ValueError("repair inventory direct-link evidence is invalid")
        active = sum(
            isinstance(link, dict) and link.get("is_active") is not False for link in links
        )
        rows.append(
            {
                "inventory_key": item.inventory_key,
                "expected_before_active_link_multiplicity": active,
                "planned_after_active_link_multiplicity": None,
                "guidance": "review_before_separate_execution_issue",
                "rollback_prerequisites": ["fresh_graph_inventory", "approved_execution_scope"],
                "execution_allowed": False,
            }
        )
    return {"schema_version": 1, "execution_allowed": False, "rows": rows}


def _clean_boundary(
    impact: Mapping[str, JsonValue],
    replay: Mapping[str, JsonValue],
    stale_run_evidence: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    digest = hashlib.sha256(canonical_json_bytes(dict(impact))).hexdigest()
    replay_ids = replay.get("inventory_keys")
    if not isinstance(replay_ids, list):
        raise ValueError("repair representative replay keys must be a list")
    return {
        "schema_version": 1,
        "execution_allowed": False,
        "inventory_digest": impact["inventory_digest"],
        "impact_digest": "sha256:" + digest,
        "replay_id_count": len(replay_ids),
        "stale_run_state": stale_run_evidence.get("state"),
        "checklist": [
            "re-run read-only graph inventory immediately before #255",
            "resolve every review or investigate condition in #255 scope",
            "re-check stale-run control evidence before any terminal action",
            "derive executable mutations only in the separate #255 issue",
        ],
    }


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
    if population_counts["active_distinct_owner_count"] > population_counts["active_link_count"]:
        raise ValueError("repair distinct owner count cannot exceed active link count")
    if (
        population_counts["maximum_distinct_owners_per_deal"]
        > population_counts["maximum_links_per_deal"]
    ):
        raise ValueError("repair maximum distinct owners cannot exceed maximum links")
