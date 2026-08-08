"""Filesystem, locking, and lifecycle tests for restricted Bitrix artifacts."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import pytest
from _bitrix_artifact_store_support import close_all_stores
from _bitrix_artifact_store_support import key_provider as _provider
from _bitrix_artifact_store_support import new_store as _new_store
from _bitrix_artifact_store_support import provenance as _provenance
from _bitrix_artifact_store_support import seal as _seal
from _bitrix_artifact_store_support import store as _store
from src.connectors.bitrix_openlines.models import CrmDealCapabilityItem
from src.connectors.bitrix_stage_history.artifact_filesystem import (
    ArtifactFilesystem,
    PreparedObject,
    SessionDirectory,
)
from src.connectors.bitrix_stage_history.artifact_manifest import (
    ArtifactFileDigest,
    ArtifactManifest,
)
from src.connectors.bitrix_stage_history.deal_probe import RestrictedOwnerManifest


@pytest.fixture(autouse=True)
def _close_open_stores() -> Iterator[None]:
    yield
    close_all_stores()


def test_store_rejects_overlapping_and_symlinked_roots(tmp_path: Path) -> None:
    primary = tmp_path / "same"
    with pytest.raises(ValueError, match="must not overlap"):
        _new_store(primary, primary, _provider())
    with pytest.raises(ValueError, match="must not overlap"):
        _new_store(primary, primary / "backup", _provider())

    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked ancestors"):
        _new_store(alias / "primary", tmp_path / "backup", _provider())

    with pytest.raises(ValueError, match="parent traversal"):
        _new_store(
            tmp_path / "primary",
            tmp_path / "alias" / ".." / "primary" / "backup",
            _provider(),
        )


def test_store_lock_prevents_concurrent_recovery_and_releases_cleanly(tmp_path: Path) -> None:
    first = _store(tmp_path)
    with pytest.raises(RuntimeError, match="already active"):
        _store(tmp_path)
    first.close()
    reopened = _store(tmp_path)
    reopened.close()


def test_filesystem_close_is_idempotent_after_descriptor_reuse(tmp_path: Path) -> None:
    filesystem = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    stale_descriptors = {
        filesystem._primary.root_fd,
        filesystem._primary.sessions_fd,
        filesystem._primary.preparing_fd,
        filesystem._primary.objects_fd,
        filesystem._primary.sealed_fd,
        filesystem._primary.lock_fd,
        filesystem._backup.root_fd,
        filesystem._backup.sessions_fd,
        filesystem._backup.preparing_fd,
        filesystem._backup.objects_fd,
        filesystem._backup.sealed_fd,
        filesystem._backup.lock_fd,
    }
    filesystem.close()
    unrelated = [os.open("/dev/null", os.O_RDONLY) for _ in range(len(stale_descriptors) + 8)]
    try:
        assert stale_descriptors.intersection(unrelated)
        filesystem.close()
        for descriptor in unrelated:
            os.fstat(descriptor)
    finally:
        for descriptor in unrelated:
            os.close(descriptor)


def test_backup_root_lock_blocks_shared_and_role_reversed_stores(tmp_path: Path) -> None:
    first = _new_store(tmp_path / "primary-a", tmp_path / "shared-backup", _provider())
    with pytest.raises(RuntimeError, match="already active"):
        _new_store(tmp_path / "primary-b", tmp_path / "shared-backup", _provider())
    with pytest.raises(RuntimeError, match="already active"):
        _new_store(tmp_path / "shared-backup", tmp_path / "primary-a", _provider())
    first.close()


def test_backup_root_lock_is_enforced_across_processes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    script = """
from pathlib import Path
from src.connectors.bitrix_stage_history.artifact_filesystem import ArtifactFilesystem
try:
    ArtifactFilesystem(Path(__import__('sys').argv[1]), Path(__import__('sys').argv[2]))
except RuntimeError as exc:
    print(str(exc))
    raise SystemExit(0)
raise SystemExit(1)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "other-primary"), str(tmp_path / "backup")],
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])},
        text=True,
    )
    store.close()
    assert result.returncode == 0
    assert "already active" in result.stdout


def test_close_is_rejected_during_producer_and_keeps_process_locks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.begin(artifact_kind="owner-manifest"):
        with pytest.raises(RuntimeError, match="operation is active"):
            store.close()
        script = """
from pathlib import Path
from src.connectors.bitrix_stage_history.artifact_filesystem import ArtifactFilesystem
try:
    ArtifactFilesystem(Path(__import__('sys').argv[1]), Path(__import__('sys').argv[2]))
except RuntimeError:
    raise SystemExit(0)
raise SystemExit(1)
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(tmp_path / "other-primary"),
                str(tmp_path / "backup"),
            ],
            capture_output=True,
            check=False,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])},
            text=True,
        )
        assert result.returncode == 0


class _BlockingVerifyFilesystem(ArtifactFilesystem):
    def __init__(self, root: Path, backup_root: Path) -> None:
        self.verify_entered = Event()
        self.verify_release = Event()
        self.block_verify = False
        super().__init__(root, backup_root)

    def read_primary_marker(self, artifact_id: str) -> bytes:
        if self.block_verify:
            self.verify_entered.set()
            if not self.verify_release.wait(timeout=5):
                raise TimeoutError("verification release timed out")
        return super().read_primary_marker(artifact_id)


def test_close_is_rejected_during_concurrent_verification(tmp_path: Path) -> None:
    filesystem = _BlockingVerifyFilesystem(tmp_path / "primary", tmp_path / "backup")
    store = _store(tmp_path, filesystem=filesystem)
    manifest = _seal(store)
    filesystem.block_verify = True
    results: list[ArtifactManifest] = []
    failures: list[BaseException] = []

    def verify() -> None:
        try:
            results.append(store.verify(manifest.artifact_id))
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=verify)
    thread.start()
    assert filesystem.verify_entered.wait(timeout=5)
    with pytest.raises(RuntimeError, match="operation is active"):
        store.close()
    filesystem.verify_release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert failures == []
    assert results == [manifest]


def test_source_symlinks_hard_links_and_fifos_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    external = tmp_path / "external.json"
    external.write_text("external", encoding="utf-8")
    os.chmod(external, 0o600)

    with store.begin(artifact_kind="owner-manifest") as artifact:
        (artifact.path / "link.json").symlink_to(external)
        with pytest.raises(OSError):
            artifact.seal(
                metadata={},
                provenance=_provenance(),
                retention_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
    assert external.read_text(encoding="utf-8") == "external"

    with store.begin(artifact_kind="owner-manifest") as artifact:
        os.link(external, artifact.path / "hard-link.json")
        with pytest.raises(ValueError, match="single-link"):
            artifact.seal(
                metadata={},
                provenance=_provenance(),
                retention_expires_at=datetime.now(UTC) + timedelta(days=1),
            )

    with store.begin(artifact_kind="owner-manifest") as artifact:
        os.mkfifo(artifact.path / "fifo", mode=0o600)
        with pytest.raises(ValueError, match="regular files"):
            artifact.seal(
                metadata={},
                provenance=_provenance(),
                retention_expires_at=datetime.now(UTC) + timedelta(days=1),
            )


def test_retained_writable_descriptor_cannot_mutate_published_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.begin(artifact_kind="owner-manifest") as artifact:
        path = artifact.write_json("summary.json", {"rows": 1})
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
        manifest = artifact.seal(
            metadata={},
            provenance=_provenance(),
            retention_expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    try:
        os.write(descriptor, b"attacker-after-seal")
    finally:
        os.close(descriptor)
    assert store.verify(manifest.artifact_id) == manifest


def test_session_root_symlink_replacement_fails_without_touching_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    sentinel = target / "sentinel"
    sentinel.write_text("safe", encoding="utf-8")
    os.chmod(sentinel, 0o600)

    with pytest.raises(RuntimeError, match="replaced by a symlink"):
        with store.begin(artifact_kind="owner-manifest") as artifact:
            artifact.write_json("summary.json", {"rows": 1})
            moved = artifact.path.with_name("moved-session")
            artifact.path.rename(moved)
            artifact.path.symlink_to(target, target_is_directory=True)
            artifact.seal(
                metadata={},
                provenance=_provenance(),
                retention_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
    assert sentinel.read_text(encoding="utf-8") == "safe"
    assert list((tmp_path / "primary" / ".objects").iterdir()) == []
    assert list((tmp_path / "backup" / ".objects").iterdir()) == []
    assert list((tmp_path / "primary" / ".sessions").iterdir()) == []


def test_ambiguous_session_close_still_removes_unsealed_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    detached_descriptors: list[int] = []

    def fail_after_detach(session: SessionDirectory) -> None:
        if session.descriptor >= 0:
            detached_descriptors.append(session.descriptor)
            session.descriptor = -1
            raise OSError("injected session close failure")

    monkeypatch.setattr(SessionDirectory, "close", fail_after_detach)
    try:
        with pytest.raises(OSError, match="session close failure"):
            with store.begin(artifact_kind="owner-manifest") as artifact:
                artifact_id = artifact.artifact_id
                artifact.write_json("summary.json", {"rows": 1})
        assert not (store.filesystem.root / ".sessions" / artifact_id).exists()
    finally:
        for descriptor in detached_descriptors:
            os.close(descriptor)


class _FailingFilesystem(ArtifactFilesystem):
    def __init__(self, root: Path, backup_root: Path, failure: str) -> None:
        self._failure = failure
        self._immutable_calls = 0
        super().__init__(root, backup_root)

    def make_immutable(self, directory: PreparedObject) -> None:
        self._immutable_calls += 1
        if self._failure == "chmod" and self._immutable_calls == 2:
            raise OSError("injected chmod failure")
        super().make_immutable(directory)

    def publish_primary_marker(self, artifact_id: str, content: bytes) -> None:
        if self._failure == "primary-marker":
            raise OSError("injected marker failure")
        super().publish_primary_marker(artifact_id, content)


class _ReplacingPublicationFilesystem(ArtifactFilesystem):
    def publish_backup_object(
        self,
        artifact: PreparedObject,
        artifact_id: str,
        expected: tuple[ArtifactFileDigest, ...],
    ) -> Path:
        moved = artifact.path.with_name(f"moved-{artifact_id}")
        artifact.path.rename(moved)
        artifact.path.mkdir(mode=0o700)
        return super().publish_backup_object(artifact, artifact_id, expected)


def test_directory_replacement_before_publication_fails_without_markers(
    tmp_path: Path,
) -> None:
    filesystem = _ReplacingPublicationFilesystem(tmp_path / "primary", tmp_path / "backup")
    store = _store(tmp_path, filesystem=filesystem)
    with pytest.raises(RuntimeError, match="pathname identity changed"):
        _seal(store)
    for base in (filesystem.root, filesystem.backup_root):
        assert list((base / "sealed").iterdir()) == []
        assert list((base / ".objects").iterdir()) == []
    assert list((filesystem.root / ".preparing").iterdir()) == []
    replacements = list((filesystem.backup_root / ".preparing").iterdir())
    assert len(replacements) == 1
    assert replacements[0].is_dir()


def test_descriptor_close_failure_happens_before_commit_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    original_close = PreparedObject.close
    failed = False
    detached_descriptors: list[int] = []

    def close_with_one_failure(prepared: PreparedObject) -> None:
        nonlocal failed
        if not failed and prepared.descriptor >= 0:
            detached_descriptors.append(prepared.descriptor)
            prepared.descriptor = -1
            failed = True
            raise OSError("injected descriptor close failure")
        original_close(prepared)

    monkeypatch.setattr(PreparedObject, "close", close_with_one_failure)
    try:
        with pytest.raises(OSError, match="descriptor close failure"):
            _seal(store)
    finally:
        for descriptor in detached_descriptors:
            os.close(descriptor)
    for base in (store.filesystem.root, store.filesystem.backup_root):
        assert list((base / "sealed").iterdir()) == []
        assert list((base / ".objects").iterdir()) == []


def test_marker_rename_fsyncs_both_source_and_destination_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filesystem = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    calls: list[int] = []
    original_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        calls.append(descriptor)
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    filesystem.publish_backup_marker("1" * 32, b"{}\n")
    assert filesystem._backup.preparing_fd in calls
    assert filesystem._backup.sealed_fd in calls
    filesystem.close()


@pytest.mark.parametrize("failure", ["chmod", "primary-marker"])
def test_interrupted_seal_leaves_no_visible_or_orphaned_artifact(
    tmp_path: Path, failure: str
) -> None:
    filesystem = _FailingFilesystem(tmp_path / "primary", tmp_path / "backup", failure)
    store = _store(tmp_path, filesystem=filesystem)
    with pytest.raises(OSError, match="injected"):
        with store.begin(artifact_kind="owner-manifest") as artifact:
            artifact_id = artifact.artifact_id
            artifact.write_json("summary.json", {"rows": 1})
            artifact.seal(
                metadata={},
                provenance=_provenance(),
                retention_expires_at=datetime.now(UTC) + timedelta(days=1),
            )

    for base in (filesystem.root, filesystem.backup_root):
        assert list((base / "sealed").iterdir()) == []
        assert list((base / ".objects").iterdir()) == []
        assert list((base / ".preparing").iterdir()) == []
    assert not (filesystem.root / ".sessions" / artifact_id).exists()


def test_session_seals_real_owner_sqlite_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.begin(artifact_kind="owner-manifest") as artifact:
        owner_manifest = RestrictedOwnerManifest(artifact.path, pass_number=1)
        assert owner_manifest.add(CrmDealCapabilityItem("501", "2", "C2:NEW")) == "unique"
        owner_manifest.flush()
        owner_manifest.close()
        manifest = artifact.seal(
            metadata={"owner_manifest_digest": "hmac-sha256:example"},
            provenance=_provenance(),
            retention_expires_at=datetime.now(UTC) + timedelta(days=1),
        )

    assert store.verify(manifest.artifact_id) == manifest
