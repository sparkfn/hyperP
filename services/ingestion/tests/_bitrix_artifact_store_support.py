"""Shared fixtures and builders for restricted artifact-store tests."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.connectors.bitrix_stage_history.artifact_filesystem import (
    ArtifactFilesystem,
    ArtifactStorageLimits,
)
from src.connectors.bitrix_stage_history.artifact_manifest import (
    ArtifactManifest,
    canonical_json_bytes,
)
from src.connectors.bitrix_stage_history.artifact_provenance import ArtifactProvenanceInput
from src.connectors.bitrix_stage_history.artifact_store import (
    ArtifactSigningKeyProvider,
    LocalRestrictedArtifactStore,
    StaticArtifactSigningKeyProvider,
)

KEY_ONE = b"a" * 32
KEY_TWO = b"b" * 32
_OPEN_STORES: list[LocalRestrictedArtifactStore] = []


def close_all_stores() -> None:
    while _OPEN_STORES:
        _OPEN_STORES.pop().close()


def key_provider(
    *, current: str = "key-1", keys: dict[str, bytes] | None = None
) -> StaticArtifactSigningKeyProvider:
    return StaticArtifactSigningKeyProvider(current, keys or {"key-1": KEY_ONE})


def store(
    tmp_path: Path,
    *,
    provider: StaticArtifactSigningKeyProvider | None = None,
    filesystem: ArtifactFilesystem | None = None,
) -> LocalRestrictedArtifactStore:
    return new_store(
        tmp_path / "primary",
        tmp_path / "backup",
        provider or key_provider(),
        filesystem=filesystem,
    )


def new_store(
    root: Path,
    backup_root: Path,
    signing_keys: ArtifactSigningKeyProvider,
    *,
    filesystem: ArtifactFilesystem | None = None,
    limits: ArtifactStorageLimits | None = None,
) -> LocalRestrictedArtifactStore:
    artifact_store = LocalRestrictedArtifactStore(
        root,
        backup_root,
        signing_keys,
        filesystem=filesystem,
        limits=limits,
    )
    _OPEN_STORES.append(artifact_store)
    return artifact_store


def seal(artifact_store: LocalRestrictedArtifactStore) -> ArtifactManifest:
    with artifact_store.begin(artifact_kind="owner-manifest") as artifact:
        artifact.write_json("summary.json", {"rows": 2, "digest": "hmac-sha256:test"})
        return artifact.seal(
            metadata={"generation_id": "generation-1", "source_calls": 0},
            provenance=provenance(),
            retention_expires_at=datetime.now(UTC) + timedelta(days=30),
        )


def provenance() -> ArtifactProvenanceInput:
    return ArtifactProvenanceInput.create(
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        repository_sha="a" * 40,
        image_digest=f"sha256:{'b' * 64}",
        configuration_digest=f"sha256:{'c' * 64}",
        restricted_boundaries={"upper_id": "100"},
        counts={"records": 2},
    )


def object_path(tmp_path: Path, copy_name: str, artifact_id: str) -> Path:
    return tmp_path / copy_name / ".objects" / artifact_id


def rewrite_manifest(path: Path, manifest: ArtifactManifest) -> None:
    os.chmod(path, 0o600)
    path.write_bytes(canonical_json_bytes(manifest.to_dict()))
    os.chmod(path, 0o400)
