"""Artifact qualification and pure boundary comparison for issue #300."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.connectors.bitrix_stage_history.artifact_manifest import ArtifactManifest
from src.crm_deal_identity_repair.digests import inventory_digest_from_bytes
from src.crm_deal_identity_repair.execution_models import RepairExecutionBoundaryManifest
from src.crm_deal_identity_repair.qualification_inventory import (
    canonical_json_object,
    inventory_source_record_pks,
    validate_artifact_count_boundary,
)


class RepairArtifactStore(Protocol):
    def verify(self, artifact_id: str) -> ArtifactManifest: ...


_EXPECTED_FILES = frozenset(
    {
        "inventory.jsonl",
        "impact-summary.json",
        "representative-replay-plan.json",
        "compensation-guidance.json",
        "stale-run-evidence.json",
        "clean-boundary-plan.json",
    }
)
_ARTIFACT_KIND = "crm-deal-identity-repair-graph-discovery"


@dataclass(frozen=True)
class VerifiedRepairArtifact:
    manifest: ArtifactManifest
    inventory_source_record_pks: tuple[str, ...]
    inventory_digest: str
    inventory_row_count: int
    eligible_unit_count: int
    negative_control_count: int


def verify_repair_artifact(
    store: RepairArtifactStore,
    *,
    artifact_id: str,
    repair_id: str,
    source_contract_uuid: str,
    repository_sha: str,
    image_digest: str,
    configuration_digest: str,
) -> VerifiedRepairArtifact:
    """Verify exact #254 bytes, HMAC, primary/backup, provenance and non-executability."""
    manifest = store.verify(artifact_id)
    _validate_authenticated_manifest(
        manifest,
        repair_id=repair_id,
        source_contract_uuid=source_contract_uuid,
        repository_sha=repository_sha,
        image_digest=image_digest,
        configuration_digest=configuration_digest,
    )
    documents = _verified_artifact_documents(
        Path(manifest.provenance.artifact_path), Path(manifest.backup_path)
    )
    inventory_bytes = documents["inventory.jsonl"]
    digest = inventory_digest_from_bytes(inventory_bytes)
    if manifest.metadata.get("inventory_digest") != digest:
        raise RuntimeError("repair inventory digest does not match sealed bytes")
    pks, eligible_count, negative_count = inventory_source_record_pks(inventory_bytes)
    validate_artifact_count_boundary(
        manifest,
        documents,
        digest,
        len(pks),
        eligible_count,
        negative_count,
    )
    return VerifiedRepairArtifact(manifest, pks, digest, len(pks), eligible_count, negative_count)


def _validate_authenticated_manifest(
    manifest: ArtifactManifest,
    *,
    repair_id: str,
    source_contract_uuid: str,
    repository_sha: str,
    image_digest: str,
    configuration_digest: str,
) -> None:
    if manifest.artifact_kind != _ARTIFACT_KIND:
        raise RuntimeError("repair artifact kind is not eligible for qualification")
    if {item.relative_path for item in manifest.files} != _EXPECTED_FILES:
        raise RuntimeError("repair artifact file set is not eligible for qualification")
    metadata = manifest.metadata
    if set(metadata) != {
        "repair_id",
        "environment",
        "artifact_scope",
        "execution_allowed",
        "inventory_digest",
        "population_counts",
        "stale_run_state",
    }:
        raise RuntimeError("repair artifact metadata fields are invalid")
    if metadata.get("repair_id") != repair_id:
        raise RuntimeError("repair artifact repair ID does not match qualification")
    if metadata.get("execution_allowed") is not False:
        raise RuntimeError("repair artifact must remain non-executable")
    if (
        metadata.get("environment") != "staging"
        or metadata.get("artifact_scope") != "graph_discovery_only"
    ):
        raise RuntimeError("repair artifact metadata is not staging graph-discovery evidence")
    provenance = manifest.provenance
    expected = (source_contract_uuid, repository_sha, image_digest, configuration_digest)
    actual = (
        provenance.source_contract_uuid,
        provenance.repository_sha,
        provenance.image_digest,
        provenance.configuration_digest,
    )
    if actual != expected:
        raise RuntimeError("repair artifact provenance does not match qualification boundary")


def _verified_artifact_documents(primary_root: Path, backup_root: Path) -> dict[str, bytes]:
    documents: dict[str, bytes] = {}
    for file_name in _EXPECTED_FILES:
        primary = (primary_root / file_name).read_bytes()
        backup = (backup_root / file_name).read_bytes()
        if primary != backup:
            raise RuntimeError("repair artifact primary and backup bytes do not match")
        if file_name == "inventory.jsonl":
            documents[file_name] = primary
            continue
        decoded = canonical_json_object(primary, "repair artifact JSON evidence")
        if decoded.get("execution_allowed") is not False:
            raise RuntimeError("repair artifact evidence must remain non-executable")
        documents[file_name] = primary
    return documents


def build_execution_manifest(
    artifact: VerifiedRepairArtifact,
    *,
    repair_id: str,
    source_contract_uuid: str,
    repository_sha: str,
    image_digest: str,
    configuration_digest: str,
    approval_reference: str,
    unit_ceiling: int,
    stop_conditions: tuple[str, ...],
    source_instance_id: str,
    control_instance_id: str,
    rollback_authority_reference: str,
    rollback_authority_policy: str,
    graph_boundary_digest: str,
) -> RepairExecutionBoundaryManifest:
    if unit_ceiling > artifact.eligible_unit_count:
        raise ValueError("repair unit ceiling exceeds eligible inventory population")
    return RepairExecutionBoundaryManifest(
        repair_id=repair_id,
        artifact_id=artifact.manifest.artifact_id,
        artifact_manifest_hmac=artifact.manifest.manifest_hmac,
        execution_allowed=False,
        inventory_digest=artifact.inventory_digest,
        repository_sha=repository_sha,
        image_digest=image_digest,
        configuration_digest=configuration_digest,
        source_contract_uuid=source_contract_uuid,
        environment="staging",
        approval_reference=approval_reference,
        unit_ceiling=unit_ceiling,
        stop_conditions=stop_conditions,
        source_instance_id=source_instance_id,
        control_instance_id=control_instance_id,
        rollback_authority_reference=rollback_authority_reference,
        rollback_authority_policy=rollback_authority_policy,
        graph_boundary_digest=graph_boundary_digest,
        inventory_row_count=artifact.inventory_row_count,
        eligible_unit_count=artifact.eligible_unit_count,
        negative_control_count=artifact.negative_control_count,
    )
