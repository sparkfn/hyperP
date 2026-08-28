"""Cross-platform boundary tests for #300 qualification, not artifact-store HMAC tests."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from src.connectors.bitrix_stage_history.artifact_manifest import (
    ArtifactFileDigest,
    ArtifactManifest,
    canonical_json_bytes,
)
from src.connectors.bitrix_stage_history.artifact_provenance import ArtifactProvenance
from src.crm_deal_identity_repair.digests import inventory_digest_from_bytes, object_digest
from src.crm_deal_identity_repair.models import RepairInventoryItem, RepairPartition
from src.crm_deal_identity_repair.qualification import (
    VerifiedRepairArtifact,
    build_execution_manifest,
    verify_repair_artifact,
)
from src.models import JsonValue


class _Store:
    def __init__(self, manifest: ArtifactManifest | Exception) -> None:
        self._manifest = manifest

    def verify(self, artifact_id: str) -> ArtifactManifest:
        if isinstance(self._manifest, Exception):
            raise self._manifest
        return self._manifest


_ARTIFACT_KIND = "crm-deal-identity-repair-graph-discovery"
_POPULATION_COUNTS: dict[str, JsonValue] = {
    "active_deal_count": 0,
    "authoritative_version_count": 0,
    "active_link_count": 0,
    "active_distinct_owner_count": 0,
    "multi_linked_deal_count": 0,
    "maximum_links_per_deal": 0,
    "maximum_distinct_owners_per_deal": 0,
    "projection_cleanup_deal_count": 0,
    "clean_deal_count": 0,
}


def _inventory(rows: tuple[dict[str, JsonValue], ...]) -> bytes:
    return b"".join(canonical_json_bytes(row) for row in rows)


def _row(
    *,
    deal_id: str,
    source_record_pk: str,
    partition: RepairPartition,
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "source_record_version": "1",
        "lifecycle_status": "active",
        "is_latest": True,
        "record_hash": "record-hash-" + source_record_pk,
        "observed_at": None,
        "raw_payload": {},
        "normalized_payload": {},
        "linked_people": [],
        "projections": [],
        "logical_version_evidence": {},
        "lifecycle_policy_evidence": {},
        "descendants": [],
        "decisions_and_reviews": [],
        "owner_impacts": [],
    }
    conditions = (partition,)
    return RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-deal-" + deal_id,
        source_record_pk=source_record_pk,
        deal_id=deal_id,
        partition=partition,
        repair_conditions=conditions,
        graph_fingerprint=object_digest(b"crm-deal-repair-graph-v1\x00", payload),
        stored_payload_fingerprint=object_digest(
            b"crm-deal-repair-source-v1\x00",
            {
                "record_hash": payload["record_hash"],
                "raw_payload": payload["raw_payload"],
                "normalized_payload": payload["normalized_payload"],
            },
        ),
        payload=payload,
    ).to_dict()


def _manifest(
    tmp_path: Path, *, rows: tuple[dict[str, JsonValue], ...] | None = None
) -> ArtifactManifest:
    primary, backup = tmp_path / "primary", tmp_path / "backup"
    primary.mkdir()
    backup.mkdir()
    inventory = _inventory(
        rows
        or (
            _row(deal_id="1", source_record_pk="pk-1", partition="ownership_repair"),
            _row(deal_id="2", source_record_pk="pk-2", partition="negative_control"),
        )
    )
    documents: dict[str, bytes] = {
        "inventory.jsonl": inventory,
        "impact-summary.json": canonical_json_bytes(
            {
                "execution_allowed": False,
                "inventory_digest": inventory_digest_from_bytes(inventory),
                "population_counts": _POPULATION_COUNTS,
            }
        ),
        "clean-boundary-plan.json": canonical_json_bytes(
            {
                "execution_allowed": False,
                "inventory_digest": inventory_digest_from_bytes(inventory),
            }
        ),
    }
    for name in (
        "representative-replay-plan.json",
        "compensation-guidance.json",
        "stale-run-evidence.json",
    ):
        documents[name] = canonical_json_bytes({"execution_allowed": False})
    for name, content in documents.items():
        (primary / name).write_bytes(content)
        (backup / name).write_bytes(content)
    metadata = {
        "repair_id": "repair-300",
        "environment": "staging",
        "artifact_scope": "graph_discovery_only",
        "execution_allowed": False,
        "inventory_digest": inventory_digest_from_bytes(inventory),
        "population_counts": _POPULATION_COUNTS,
        "stale_run_state": "clean",
    }
    files = tuple(
        ArtifactFileDigest(name, hashlib.sha256(content).hexdigest(), len(content))
        for name, content in sorted(documents.items())
    )
    provenance = ArtifactProvenance(
        artifact_path=str(primary.absolute()),
        primary_device=1,
        primary_inode=1,
        backup_device=2,
        backup_inode=2,
        owner_uid=1,
        group_gid=1,
        directory_mode=0o500,
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        repository_sha="a" * 40,
        image_digest="sha256:" + "b" * 64,
        configuration_digest="sha256:" + "c" * 64,
        restricted_boundaries_json='{"scope":"graph"}\n',
        counts_json=canonical_json_bytes({"inventory_rows": 2, **_POPULATION_COUNTS}).decode(),
        total_bytes=sum(len(content) for content in documents.values()),
    )
    return ArtifactManifest(
        1,
        "d" * 32,
        _ARTIFACT_KIND,
        datetime.now(UTC).isoformat(),
        (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        canonical_json_bytes(metadata).decode(),
        files,
        provenance,
        str(backup.absolute()),
        True,
        "key",
        "e" * 64,
    )


def _verify(manifest: ArtifactManifest) -> VerifiedRepairArtifact:
    return verify_repair_artifact(
        _Store(manifest),
        artifact_id=manifest.artifact_id,
        repair_id="repair-300",
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        repository_sha="a" * 40,
        image_digest="sha256:" + "b" * 64,
        configuration_digest="sha256:" + "c" * 64,
    )


def _with_metadata(manifest: ArtifactManifest, **changes: JsonValue) -> ArtifactManifest:
    metadata = manifest.metadata
    metadata.update(changes)
    return replace(manifest, metadata_json=canonical_json_bytes(metadata).decode())


def _replace_document(manifest: ArtifactManifest, name: str, content: bytes) -> ArtifactManifest:
    for root in (Path(manifest.provenance.artifact_path), Path(manifest.backup_path)):
        (root / name).write_bytes(content)
    if name != "inventory.jsonl":
        return manifest
    return _with_metadata(manifest, inventory_digest=inventory_digest_from_bytes(content))


def test_qualification_accepts_authenticated_canonical_boundary(tmp_path: Path) -> None:
    verified = _verify(_manifest(tmp_path))
    assert (
        verified.inventory_source_record_pks,
        verified.eligible_unit_count,
        verified.negative_control_count,
    ) == (("pk-1", "pk-2"), 1, 1)


def test_qualification_uses_inventory_key_order_and_returns_sorted_pks(tmp_path: Path) -> None:
    verified = _verify(
        _manifest(
            tmp_path,
            rows=(
                _row(deal_id="1", source_record_pk="pk-z", partition="ownership_repair"),
                _row(deal_id="2", source_record_pk="pk-a", partition="negative_control"),
            ),
        )
    )
    assert verified.inventory_source_record_pks == ("pk-a", "pk-z")


@pytest.mark.parametrize(
    "mutator",
    (
        lambda manifest: replace(manifest, artifact_kind="wrong"),
        lambda manifest: replace(manifest, files=manifest.files[:-1]),
        lambda manifest: replace(
            manifest,
            files=manifest.files + (ArtifactFileDigest("unexpected.json", "0" * 64, 0),),
        ),
        lambda manifest: replace(
            manifest, metadata_json=canonical_json_bytes({"repair_id": "other"}).decode()
        ),
        lambda manifest: replace(
            manifest, provenance=replace(manifest.provenance, repository_sha="f" * 40)
        ),
        lambda manifest: replace(
            manifest, provenance=replace(manifest.provenance, counts_json='{"inventory_rows":1}\n')
        ),
    ),
)
def test_qualification_rejects_authenticated_boundary_mismatch(
    tmp_path: Path,
    mutator: Callable[[ArtifactManifest], ArtifactManifest],
) -> None:
    with pytest.raises((RuntimeError, ValueError)):
        _verify(mutator(_manifest(tmp_path)))


def test_store_verification_failure_and_expiry_propagate(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="store tamper"):
        verify_repair_artifact(
            _Store(RuntimeError("store tamper")),
            artifact_id="d" * 32,
            repair_id="repair-300",
            source_contract_uuid="12345678-1234-5678-9234-567812345678",
            repository_sha="a" * 40,
            image_digest="sha256:" + "b" * 64,
            configuration_digest="sha256:" + "c" * 64,
        )
    with pytest.raises(RuntimeError, match="expired"):
        verify_repair_artifact(
            _Store(RuntimeError("artifact expired")),
            artifact_id="d" * 32,
            repair_id="repair-300",
            source_contract_uuid="12345678-1234-5678-9234-567812345678",
            repository_sha="a" * 40,
            image_digest="sha256:" + "b" * 64,
            configuration_digest="sha256:" + "c" * 64,
        )


def test_qualification_rejects_primary_backup_tamper(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    (Path(manifest.backup_path) / "inventory.jsonl").write_bytes(b"tampered\n")
    with pytest.raises(RuntimeError, match="primary and backup"):
        _verify(manifest)


def test_qualification_rejects_authenticated_execution_and_inventory_mismatch(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    with pytest.raises(RuntimeError):
        _verify(_with_metadata(manifest, execution_allowed=True))
    with pytest.raises(RuntimeError):
        _verify(_with_metadata(manifest, inventory_digest="sha256:" + "0" * 64))


def test_qualification_rejects_signed_internal_count_and_document_mismatch(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    with pytest.raises(RuntimeError):
        _verify(
            _with_metadata(
                manifest, population_counts={**_POPULATION_COUNTS, "clean_deal_count": 1}
            )
        )
    _replace_document(
        manifest,
        "impact-summary.json",
        canonical_json_bytes(
            {
                "execution_allowed": False,
                "inventory_digest": "sha256:" + "0" * 64,
                "population_counts": _POPULATION_COUNTS,
            }
        ),
    )
    with pytest.raises(RuntimeError):
        _verify(manifest)


@pytest.mark.parametrize(
    "content",
    (
        canonical_json_bytes({"execution_allowed": True}),
        b"{",
        b'{"execution_allowed": false}\n',
    ),
)
def test_qualification_rejects_nonexecutable_unreadable_or_noncanonical_document(
    tmp_path: Path,
    content: bytes,
) -> None:
    manifest = _replace_document(_manifest(tmp_path), "impact-summary.json", content)
    with pytest.raises(RuntimeError):
        _verify(manifest)


@pytest.mark.parametrize(
    "content",
    (
        b"{\n",
        b'{"execution_allowed":false,"partition":"ownership_repair","source_record_pk":"pk-1"} \n',
    ),
)
def test_qualification_rejects_unreadable_or_noncanonical_inventory_jsonl(
    tmp_path: Path,
    content: bytes,
) -> None:
    manifest = _replace_document(_manifest(tmp_path), "inventory.jsonl", content)
    with pytest.raises(RuntimeError):
        _verify(manifest)


@pytest.mark.parametrize(
    "rows",
    (
        ({"execution_allowed": True, "partition": "ownership_repair", "source_record_pk": "pk-1"},),
        ({"execution_allowed": False, "partition": "ownership_repair", "source_record_pk": ""},),
        (
            {
                "execution_allowed": False,
                "partition": "ownership_repair",
                "source_record_pk": "pk-2",
            },
            {
                "execution_allowed": False,
                "partition": "negative_control",
                "source_record_pk": "pk-1",
            },
        ),
        (
            {
                "execution_allowed": False,
                "partition": "unknown_partition",
                "source_record_pk": "pk-1",
            },
        ),
        (
            {
                "execution_allowed": False,
                "partition": "ownership_repair",
                "source_record_pk": "pk-1",
            },
            {
                "execution_allowed": False,
                "partition": "negative_control",
                "source_record_pk": "pk-1",
            },
        ),
    ),
)
def test_qualification_rejects_invalid_inventory_boundary(
    tmp_path: Path, rows: tuple[dict[str, JsonValue], ...]
) -> None:
    with pytest.raises(RuntimeError):
        _verify(_manifest(tmp_path, rows=rows))


def test_build_manifest_enforces_ceiling_instances_and_stop_conditions(tmp_path: Path) -> None:
    artifact = _verify(_manifest(tmp_path))
    kwargs = dict(
        repair_id="repair-300",
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        repository_sha="a" * 40,
        image_digest="sha256:" + "b" * 64,
        configuration_digest="sha256:" + "c" * 64,
        approval_reference="approval",
        unit_ceiling=2,
        stop_conditions=("boundary_drift",),
        source_instance_id="portal-a",
        control_instance_id="control-a",
        rollback_authority_reference="rollback",
        rollback_authority_policy="reviewed",
        graph_boundary_digest="sha256:" + "f" * 64,
    )
    with pytest.raises(ValueError, match="ceiling"):
        build_execution_manifest(artifact, **kwargs)
    kwargs["unit_ceiling"] = 1
    kwargs["stop_conditions"] = ("boundary_drift", "boundary_drift")
    with pytest.raises(ValueError, match="unique"):
        build_execution_manifest(artifact, **kwargs)
    kwargs["stop_conditions"] = ("unknown_stop",)
    with pytest.raises(ValueError, match="unknown"):
        build_execution_manifest(artifact, **kwargs)
    kwargs["stop_conditions"] = ("boundary_drift",)
    kwargs["source_instance_id"] = "invalid source"
    with pytest.raises(ValueError):
        build_execution_manifest(artifact, **kwargs)
    kwargs["source_instance_id"] = "portal-a"
    kwargs["control_instance_id"] = "invalid control"
    with pytest.raises(ValueError):
        build_execution_manifest(artifact, **kwargs)
