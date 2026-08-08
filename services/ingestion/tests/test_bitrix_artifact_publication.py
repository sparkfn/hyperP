"""Atomic publication and collision tests for restricted Bitrix artifacts."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import src.connectors.bitrix_stage_history.artifact_store as store_module
from _bitrix_artifact_store_support import close_all_stores
from _bitrix_artifact_store_support import provenance as _provenance
from _bitrix_artifact_store_support import seal as _seal
from _bitrix_artifact_store_support import store as _store
from src.connectors.bitrix_stage_history.artifact_filesystem import (
    ArtifactFilesystem,
    PreparedObject,
    PublishedMarker,
)
from src.connectors.bitrix_stage_history.artifact_manifest import ArtifactFileDigest


@pytest.fixture(autouse=True)
def _close_open_stores() -> Iterator[None]:
    yield
    close_all_stores()


class _FixedUuid:
    def __init__(self, hexadecimal: str) -> None:
        self.hex = hexadecimal


def _tree_bytes(path: Path) -> dict[str, bytes]:
    return {
        str(file.relative_to(path)): file.read_bytes()
        for file in sorted(path.rglob("*"))
        if file.is_file()
    }


def test_uuid_collision_preserves_existing_committed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    original = _seal(store)
    before = {copy_name: _tree_bytes(tmp_path / copy_name) for copy_name in ("primary", "backup")}
    monkeypatch.setattr(store_module.uuid, "uuid4", lambda: _FixedUuid(original.artifact_id))

    with pytest.raises(FileExistsError):
        with store.begin(artifact_kind="owner-manifest") as artifact:
            artifact.write_json("summary.json", {"rows": 99})
            artifact.seal(
                metadata={},
                provenance=_provenance(),
                retention_expires_at=datetime.now(UTC) + timedelta(days=1),
            )

    assert store.verify(original.artifact_id) == original
    assert {
        copy_name: _tree_bytes(tmp_path / copy_name) for copy_name in ("primary", "backup")
    } == before


class _ManifestTamperingFilesystem(ArtifactFilesystem):
    def make_immutable(self, artifact: PreparedObject) -> None:
        manifest = artifact.path / "artifact-manifest.json"
        manifest.write_bytes(b'{"tampered":true}\n')
        super().make_immutable(artifact)


def test_manifest_change_before_immutability_fails_without_publication(tmp_path: Path) -> None:
    filesystem = _ManifestTamperingFilesystem(tmp_path / "primary", tmp_path / "backup")
    store = _store(tmp_path, filesystem=filesystem)
    with pytest.raises(RuntimeError, match="manifest bytes changed"):
        _seal(store)
    for base in (filesystem.root, filesystem.backup_root):
        assert list((base / "sealed").iterdir()) == []
        assert list((base / ".objects").iterdir()) == []


def _replace_published_directory(path: Path) -> None:
    os.chmod(path, 0o700)
    moved = path.with_name(f"moved-{path.name}")
    path.rename(moved)
    path.mkdir(mode=0o700)


class _AfterVerificationReplacementFilesystem(ArtifactFilesystem):
    def __init__(self, root: Path, backup_root: Path) -> None:
        self._verification_count = 0
        super().__init__(root, backup_root)

    def verify_published_object(
        self,
        artifact: PreparedObject,
        expected: tuple[ArtifactFileDigest, ...],
        manifest_bytes: bytes,
    ) -> None:
        super().verify_published_object(artifact, expected, manifest_bytes)
        self._verification_count += 1
        if self._verification_count == 2:
            _replace_published_directory(artifact.path)


def test_replacement_after_final_verification_fails_before_markers(tmp_path: Path) -> None:
    filesystem = _AfterVerificationReplacementFilesystem(tmp_path / "primary", tmp_path / "backup")
    store = _store(tmp_path, filesystem=filesystem)
    with pytest.raises(RuntimeError, match="provenance"):
        _seal(store)
    for base in (filesystem.root, filesystem.backup_root):
        assert list((base / "sealed").iterdir()) == []


class _BetweenMarkersReplacementFilesystem(ArtifactFilesystem):
    def publish_backup_marker(self, artifact_id: str, content: bytes) -> PublishedMarker:
        marker = super().publish_backup_marker(artifact_id, content)
        _replace_published_directory(self.root / ".objects" / artifact_id)
        return marker


def test_replacement_between_markers_rolls_back_attempt_marker(tmp_path: Path) -> None:
    filesystem = _BetweenMarkersReplacementFilesystem(tmp_path / "primary", tmp_path / "backup")
    store = _store(tmp_path, filesystem=filesystem)
    with pytest.raises(RuntimeError, match="provenance"):
        _seal(store)
    assert list((filesystem.root / "sealed").iterdir()) == []
    assert list((filesystem.backup_root / "sealed").iterdir()) == []


class _BetweenMarkersMutationFilesystem(ArtifactFilesystem):
    def publish_backup_marker(self, artifact_id: str, content: bytes) -> PublishedMarker:
        marker = super().publish_backup_marker(artifact_id, content)
        for root in (self.root, self.backup_root):
            artifact = root / ".objects" / artifact_id
            evidence = artifact / "summary.json"
            os.chmod(artifact, 0o700)
            os.chmod(evidence, 0o600)
            evidence.write_bytes(b'{"tampered":true}\n')
            os.chmod(evidence, 0o400)
            os.chmod(artifact, 0o500)
        return marker


def test_in_place_mutation_between_markers_fails_and_rolls_back(tmp_path: Path) -> None:
    filesystem = _BetweenMarkersMutationFilesystem(tmp_path / "primary", tmp_path / "backup")
    store = _store(tmp_path, filesystem=filesystem)
    with pytest.raises(RuntimeError, match="writable|digest"):
        _seal(store)
    assert list((filesystem.root / "sealed").iterdir()) == []
    assert list((filesystem.backup_root / "sealed").iterdir()) == []
