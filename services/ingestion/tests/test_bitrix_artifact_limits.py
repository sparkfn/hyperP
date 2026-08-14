"""Key-provider and resource-limit tests for restricted Bitrix artifacts."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _bitrix_artifact_store_support import KEY_ONE as _KEY_ONE
from _bitrix_artifact_store_support import KEY_TWO as _KEY_TWO
from _bitrix_artifact_store_support import close_all_stores
from _bitrix_artifact_store_support import key_provider as _provider
from _bitrix_artifact_store_support import new_store as _new_store
from _bitrix_artifact_store_support import object_path as _object_path
from _bitrix_artifact_store_support import provenance as _provenance
from _bitrix_artifact_store_support import seal as _seal
from src.connectors.bitrix_stage_history.artifact_filesystem import (
    ArtifactFilesystem,
    ArtifactStorageLimits,
    PreparedObject,
)
from src.connectors.bitrix_stage_history.artifact_store import ArtifactSigningKey


@pytest.fixture(autouse=True)
def _close_open_stores() -> Iterator[None]:
    yield
    close_all_stores()


class _InconsistentProvider:
    def __init__(self, *, mismatch_id: bool = False, mismatch_secret: bool = False) -> None:
        self._mismatch_id = mismatch_id
        self._mismatch_secret = mismatch_secret

    def current(self) -> ArtifactSigningKey:
        return ArtifactSigningKey("key-1", _KEY_ONE)

    def get(self, key_id: str) -> ArtifactSigningKey | None:
        if self._mismatch_id:
            return ArtifactSigningKey("different-key", _KEY_ONE)
        if self._mismatch_secret:
            return ArtifactSigningKey(key_id, _KEY_TWO)
        return None


@pytest.mark.parametrize(
    "provider",
    [
        _InconsistentProvider(),
        _InconsistentProvider(mismatch_id=True),
        _InconsistentProvider(mismatch_secret=True),
    ],
)
def test_inconsistent_current_key_provider_fails_before_snapshot(
    tmp_path: Path, provider: _InconsistentProvider
) -> None:
    store = _new_store(tmp_path / "primary", tmp_path / "backup", provider)
    with pytest.raises(RuntimeError, match="consistently resolvable"):
        with store.begin(artifact_kind="owner-manifest") as artifact:
            artifact_id = artifact.artifact_id
            artifact.write_json("summary.json", {"rows": 1})
            artifact.seal(
                metadata={},
                provenance=_provenance(),
                retention_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
    assert not (_object_path(tmp_path, "primary", artifact_id)).exists()
    assert not (_object_path(tmp_path, "backup", artifact_id)).exists()


@pytest.mark.parametrize(
    ("limits", "files", "match"),
    [
        (ArtifactStorageLimits(max_files=1), (b"a", b"b"), "file-count"),
        (ArtifactStorageLimits(max_file_bytes=3), (b"abcd",), "byte limit"),
        (
            ArtifactStorageLimits(max_file_bytes=4, max_total_bytes=5),
            (b"abc", b"def"),
            "byte limit",
        ),
    ],
)
def test_snapshot_resource_limits_fail_without_visible_artifacts(
    tmp_path: Path,
    limits: ArtifactStorageLimits,
    files: tuple[bytes, ...],
    match: str,
) -> None:
    store = _new_store(tmp_path / "primary", tmp_path / "backup", _provider(), limits=limits)
    with pytest.raises(RuntimeError, match=match):
        with store.begin(artifact_kind="owner-manifest") as artifact:
            artifact_id = artifact.artifact_id
            for index, content in enumerate(files):
                path = artifact.path / f"file-{index}.bin"
                path.write_bytes(content)
                os.chmod(path, 0o600)
            artifact.seal(
                metadata={},
                provenance=_provenance(),
                retention_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
    assert not _object_path(tmp_path, "primary", artifact_id).exists()
    assert not _object_path(tmp_path, "backup", artifact_id).exists()


@pytest.mark.parametrize(
    "named_files",
    [
        (("a-full.bin", b"abc"), ("z-empty.bin", b"")),
        (("a-empty.bin", b""), ("z-full.bin", b"abc")),
    ],
)
def test_total_byte_limit_allows_empty_files_in_either_name_order(
    tmp_path: Path, named_files: tuple[tuple[str, bytes], ...]
) -> None:
    store = _new_store(
        tmp_path / "primary",
        tmp_path / "backup",
        _provider(),
        limits=ArtifactStorageLimits(max_total_bytes=3),
    )
    with store.begin(artifact_kind="owner-manifest") as artifact:
        for name, content in named_files:
            path = artifact.path / name
            path.write_bytes(content)
            os.chmod(path, 0o600)
        manifest = artifact.seal(
            metadata={},
            provenance=_provenance(),
            retention_expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    assert sum(item.byte_count for item in manifest.files) == 3
    assert store.verify(manifest.artifact_id) == manifest


def test_manifest_and_marker_limits_fail_with_artifact_specific_cleanup(tmp_path: Path) -> None:
    manifest_limited = _new_store(
        tmp_path / "manifest-primary",
        tmp_path / "manifest-backup",
        _provider(),
        limits=ArtifactStorageLimits(max_manifest_bytes=32),
    )
    with pytest.raises(RuntimeError, match="manifest byte limit"):
        _seal(manifest_limited)
    assert list((tmp_path / "manifest-primary" / ".objects").iterdir()) == []

    marker_limited = _new_store(
        tmp_path / "marker-primary",
        tmp_path / "marker-backup",
        _provider(),
        limits=ArtifactStorageLimits(max_marker_bytes=16),
    )
    with pytest.raises(RuntimeError, match="marker byte limit"):
        _seal(marker_limited)
    assert list((tmp_path / "marker-primary" / ".objects").iterdir()) == []


@pytest.mark.parametrize("failure_call", [3, 8])
def test_guard_failure_inside_immutability_cleans_all_publication_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    filesystem = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    store = _new_store(
        tmp_path / "primary",
        tmp_path / "backup",
        _provider(),
        filesystem=filesystem,
    )
    original = filesystem.make_immutable

    def fail_inside_immutability(
        artifact: PreparedObject,
        *,
        guard: Callable[[], None] | None = None,
    ) -> None:
        calls = 0

        def internal_guard() -> None:
            nonlocal calls
            calls += 1
            if calls == failure_call:
                raise RuntimeError("injected internal immutability guard failure")
            if guard is not None:
                guard()

        original(artifact, guard=internal_guard)

    monkeypatch.setattr(filesystem, "make_immutable", fail_inside_immutability)
    with pytest.raises(RuntimeError, match="internal immutability guard"):
        with store.begin(artifact_kind="owner-manifest") as artifact:
            artifact_id = artifact.artifact_id
            artifact.write_json("summary.json", {"rows": 1})
            artifact.seal(
                metadata={},
                provenance=_provenance(),
                retention_expires_at=datetime.now(UTC) + timedelta(days=1),
                guard=lambda: None,
            )

    for copy_name in ("primary", "backup"):
        root = tmp_path / copy_name
        assert list((root / ".preparing").iterdir()) == []
        assert list((root / ".objects").iterdir()) == []
        assert list((root / "sealed").iterdir()) == []
        assert not _object_path(tmp_path, copy_name, artifact_id).exists()


@pytest.mark.parametrize(
    ("named_files", "limits", "match"),
    [
        (
            (("a.bin", b""), ("b.bin", b"")),
            ArtifactStorageLimits(max_files=1),
            "file-count",
        ),
        (
            (("a.bin", b"abcd"),),
            ArtifactStorageLimits(max_file_bytes=3),
            "file exceeds",
        ),
        (
            (("a.bin", b"abc"), ("b.bin", b"def")),
            ArtifactStorageLimits(max_file_bytes=4, max_total_bytes=5),
            "total-byte",
        ),
    ],
)
def test_verification_enforces_current_resource_limits_before_data_reads(
    tmp_path: Path,
    named_files: tuple[tuple[str, bytes], ...],
    limits: ArtifactStorageLimits,
    match: str,
) -> None:
    original = _new_store(tmp_path / "primary", tmp_path / "backup", _provider())
    with original.begin(artifact_kind="owner-manifest") as artifact:
        for name, content in named_files:
            path = artifact.path / name
            path.write_bytes(content)
            os.chmod(path, 0o600)
        artifact.seal(
            metadata={},
            provenance=_provenance(),
            retention_expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    original.close()
    with pytest.raises(RuntimeError, match=match):
        _new_store(tmp_path / "primary", tmp_path / "backup", _provider(), limits=limits)
