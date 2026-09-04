"""Artifact qualification and pure boundary comparison for issue #300."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.connectors.bitrix_stage_history.artifact_manifest import ArtifactManifest
from src.crm_deal_identity_repair.digests import INVENTORY_DIGEST_DOMAIN
from src.crm_deal_identity_repair.execution_models import (
    RepairExecutionBoundaryManifest,
    RepairQualificationRun,
)
from src.crm_deal_identity_repair.qualification_inventory import (
    canonical_json_object,
    inventory_source_record_pks_from_lines,
    validate_artifact_count_boundary,
)


class RepairArtifactStore(Protocol):
    def verify(self, artifact_id: str) -> ArtifactManifest: ...


_EVIDENCE_FILES = frozenset(
    {
        "impact-summary.json",
        "representative-replay-plan.json",
        "compensation-guidance.json",
        "stale-run-evidence.json",
        "clean-boundary-plan.json",
    }
)
_LEGACY_INVENTORY_FILE = "inventory.jsonl"
_INVENTORY_PART_PATTERN = re.compile(r"inventory-([0-9]{5})\.jsonl")
_ARTIFACT_KIND = "crm-deal-identity-repair-graph-discovery"
_RESTRICTED_BOUNDARY = {
    "artifact_scope": "graph_discovery_only",
    "execution_allowed": False,
    "inventory_mode": "graph_only_read_only",
    "source_system": "bitrix_chat",
}


@dataclass(frozen=True)
class VerifiedRepairArtifact:
    manifest: ArtifactManifest
    inventory_file_names: tuple[str, ...]
    inventory_source_record_pks: tuple[str, ...]
    inventory_digest: str
    inventory_row_count: int
    eligible_unit_count: int
    negative_control_count: int


def read_qualified_stale_run_id(artifact: VerifiedRepairArtifact) -> str:
    """Read the sealed #300 stale-run target only after artifact authentication."""
    evidence = canonical_json_object(
        (Path(artifact.manifest.provenance.artifact_path) / "stale-run-evidence.json").read_bytes(),
        "repair sealed stale-run evidence",
    )
    stale_run_id = evidence.get("stale_run_id")
    if (
        not isinstance(stale_run_id, str)
        or not stale_run_id
        or evidence.get("state") != "unknown"
        or evidence.get("execution_allowed") is not False
    ):
        raise RuntimeError("repair sealed stale-run evidence is invalid")
    return stale_run_id


def verify_qualified_repair_artifact(
    store: RepairArtifactStore,
    *,
    run: RepairQualificationRun,
) -> VerifiedRepairArtifact:
    """Re-authenticate the immutable artifact bound to an already-qualified run.

    Allocation must not reconstruct qualification from CLI arguments: the graph
    ledger is the authority for every #300 binding.
    """
    manifest = run.manifest
    return verify_repair_artifact(
        store,
        artifact_id=run.artifact_id,
        repair_id=run.repair_id,
        source_contract_uuid=manifest.source_contract_uuid,
        repository_sha=manifest.repository_sha,
        image_digest=manifest.image_digest,
        configuration_digest=manifest.configuration_digest,
    )


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
    inventory_file_names = _inventory_file_names(manifest)
    documents = _verified_evidence_documents(
        Path(manifest.provenance.artifact_path), Path(manifest.backup_path)
    )
    digest_state = hashlib.sha256()
    digest_state.update(INVENTORY_DIGEST_DOMAIN)

    def digesting_lines() -> Iterator[bytes]:
        for line in _verified_inventory_lines(
            Path(manifest.provenance.artifact_path),
            Path(manifest.backup_path),
            inventory_file_names,
        ):
            digest_state.update(line)
            yield line

    pks, eligible_count, negative_count = inventory_source_record_pks_from_lines(digesting_lines())
    digest = "sha256:" + digest_state.hexdigest()
    if manifest.metadata.get("inventory_digest") != digest:
        raise RuntimeError("repair inventory digest does not match sealed bytes")
    validate_artifact_count_boundary(
        manifest,
        documents,
        digest,
        len(pks),
        eligible_count,
        negative_count,
    )
    return VerifiedRepairArtifact(
        manifest,
        inventory_file_names,
        pks,
        digest,
        len(pks),
        eligible_count,
        negative_count,
    )


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
    _inventory_file_names(manifest)
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
    restricted_boundaries = canonical_json_object(
        provenance.restricted_boundaries_json.encode("utf-8"),
        "repair artifact restricted-boundaries provenance",
    )
    if restricted_boundaries != _RESTRICTED_BOUNDARY:
        raise RuntimeError("repair artifact restricted-boundaries provenance is invalid")


def _inventory_file_names(manifest: ArtifactManifest) -> tuple[str, ...]:
    relative_names = tuple(item.relative_path for item in manifest.files)
    names = set(relative_names)
    if len(names) != len(relative_names):
        raise RuntimeError("repair artifact file names must be unique")
    inventory_names = names - _EVIDENCE_FILES
    if inventory_names == {_LEGACY_INVENTORY_FILE}:
        return (_LEGACY_INVENTORY_FILE,)
    parts = tuple(sorted(inventory_names))
    if not parts or names != _EVIDENCE_FILES | set(parts):
        raise RuntimeError("repair artifact file set is not eligible for qualification")
    expected = tuple(f"inventory-{index:05d}.jsonl" for index in range(1, len(parts) + 1))
    if parts != expected or any(_INVENTORY_PART_PATTERN.fullmatch(name) is None for name in parts):
        raise RuntimeError("repair artifact inventory parts are not contiguous")
    return parts


def iter_verified_inventory_lines(artifact: VerifiedRepairArtifact) -> Iterator[bytes]:
    """Read authenticated primary inventory rows in canonical part order."""
    root = Path(artifact.manifest.provenance.artifact_path)
    for file_name in artifact.inventory_file_names:
        with (root / file_name).open("rb") as source:
            yield from source


def _verified_evidence_documents(primary_root: Path, backup_root: Path) -> dict[str, bytes]:
    documents: dict[str, bytes] = {}
    for file_name in _EVIDENCE_FILES:
        primary = (primary_root / file_name).read_bytes()
        backup = (backup_root / file_name).read_bytes()
        if primary != backup:
            raise RuntimeError("repair artifact primary and backup bytes do not match")
        decoded = canonical_json_object(primary, "repair artifact JSON evidence")
        if decoded.get("execution_allowed") is not False:
            raise RuntimeError("repair artifact evidence must remain non-executable")
        documents[file_name] = primary
    return documents


def _verified_inventory_lines(
    primary_root: Path,
    backup_root: Path,
    file_names: tuple[str, ...],
) -> Iterator[bytes]:
    for file_name in file_names:
        observed = False
        with (
            (primary_root / file_name).open("rb") as primary,
            (backup_root / file_name).open("rb") as backup,
        ):
            while True:
                primary_line = primary.readline()
                backup_line = backup.readline()
                if primary_line != backup_line:
                    raise RuntimeError("repair artifact primary and backup bytes do not match")
                if not primary_line:
                    break
                observed = True
                yield primary_line
        if not observed:
            raise RuntimeError("repair artifact inventory parts must not be empty")


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
