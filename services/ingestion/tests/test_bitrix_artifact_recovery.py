"""Persisted crash-state recovery tests for restricted Bitrix artifacts."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from _bitrix_artifact_store_support import close_all_stores
from _bitrix_artifact_store_support import object_path as _object_path
from _bitrix_artifact_store_support import seal as _seal
from _bitrix_artifact_store_support import store as _store
from src.connectors.bitrix_stage_history.artifact_filesystem import ArtifactFilesystem


@pytest.fixture(autouse=True)
def _close_open_stores() -> Iterator[None]:
    yield
    close_all_stores()


def test_startup_recovers_persisted_uncommitted_objects_and_backup_marker(
    tmp_path: Path,
) -> None:
    filesystem = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    session = filesystem.create_session("1" * 32)
    filesystem.write_session_file(session, "summary.json", b"{}\n")
    primary, files = filesystem.snapshot_session(session)
    backup = filesystem.copy_primary_to_backup(primary, session.artifact_id, files)
    primary.close()
    backup.close()
    filesystem.publish_backup_marker(session.artifact_id, b"{}\n")
    session.close()
    filesystem.close()

    recovered = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    assert list((tmp_path / "primary" / ".objects").iterdir()) == []
    assert list((tmp_path / "backup" / ".objects").iterdir()) == []
    assert list((tmp_path / "backup" / "sealed").iterdir()) == []
    assert list((tmp_path / "primary" / ".sessions").iterdir()) == []
    recovered.close()


def test_startup_removes_persisted_temporary_markers(tmp_path: Path) -> None:
    filesystem = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    temporary = tmp_path / "primary" / ".preparing" / f"{'1' * 32}.commit"
    temporary.write_bytes(b"{}\n")
    os.chmod(temporary, 0o400)
    filesystem.close()

    recovered = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    assert list((tmp_path / "primary" / ".preparing").iterdir()) == []
    recovered.close()


def test_startup_rejects_primary_only_commit_marker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _seal(store)
    store.close()
    (tmp_path / "backup" / "sealed" / f"{manifest.artifact_id}.json").unlink()

    with pytest.raises(RuntimeError, match="missing its backup marker"):
        ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")


def test_startup_preserves_complete_matching_commit_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _seal(store)
    store.close()

    recovered = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    assert _object_path(tmp_path, "primary", manifest.artifact_id).is_dir()
    assert _object_path(tmp_path, "backup", manifest.artifact_id).is_dir()
    recovered.close()


def test_startup_rejects_mismatched_commit_markers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _seal(store)
    store.close()
    backup_marker = tmp_path / "backup" / "sealed" / f"{manifest.artifact_id}.json"
    os.chmod(backup_marker, 0o600)
    backup_marker.write_bytes(b'{"copy":"backup"}\n')
    os.chmod(backup_marker, 0o400)

    with pytest.raises(RuntimeError, match="markers do not match"):
        ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")


def test_startup_rejects_committed_marker_with_missing_object(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _seal(store)
    store.close()
    backup_object = _object_path(tmp_path, "backup", manifest.artifact_id)
    os.chmod(backup_object, 0o700)
    shutil.rmtree(backup_object)

    with pytest.raises(RuntimeError, match="missing its immutable object"):
        ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")


def test_startup_rejects_writable_or_missing_manifest_committed_state(
    tmp_path: Path,
) -> None:
    writable_store = _store(tmp_path / "writable")
    writable = _seal(writable_store)
    writable_store.close()
    writable_object = _object_path(tmp_path / "writable", "primary", writable.artifact_id)
    os.chmod(writable_object, 0o700)
    with pytest.raises(RuntimeError, match="writable|provenance"):
        ArtifactFilesystem(
            tmp_path / "writable" / "primary",
            tmp_path / "writable" / "backup",
        )

    missing_store = _store(tmp_path / "missing")
    missing = _seal(missing_store)
    missing_store.close()
    for copy_name in ("primary", "backup"):
        object_path = _object_path(tmp_path / "missing", copy_name, missing.artifact_id)
        os.chmod(object_path, 0o700)
        (object_path / "artifact-manifest.json").unlink()
    with pytest.raises((FileNotFoundError, RuntimeError)):
        ArtifactFilesystem(
            tmp_path / "missing" / "primary",
            tmp_path / "missing" / "backup",
        )
