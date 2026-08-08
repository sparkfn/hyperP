"""Failure-path descriptor and directory cleanup tests for Bitrix artifacts."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from _bitrix_artifact_store_support import close_all_stores
from _bitrix_artifact_store_support import seal as _seal
from _bitrix_artifact_store_support import store as _store
from src.connectors.bitrix_stage_history import artifact_fs_primitives as fs
from src.connectors.bitrix_stage_history import artifact_fs_roots as roots
from src.connectors.bitrix_stage_history.artifact_filesystem import (
    ArtifactFilesystem,
    PreparedObject,
    SessionDirectory,
)


@pytest.fixture(autouse=True)
def _close_open_stores() -> Iterator[None]:
    yield
    close_all_stores()


@pytest.mark.parametrize("failure", ["unlock", "lock-close"])
def test_root_close_failure_still_releases_both_process_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    store = _store(tmp_path)
    primary_lock = store.filesystem._primary.lock_fd
    original_flock = roots.fcntl.flock
    original_close = roots.os.close
    injected = False

    def failing_flock(descriptor: int, operation: int) -> None:
        nonlocal injected
        if failure == "unlock" and operation == roots.fcntl.LOCK_UN and not injected:
            injected = True
            raise OSError("injected unlock failure")
        original_flock(descriptor, operation)

    def failing_close(descriptor: int) -> None:
        nonlocal injected
        original_close(descriptor)
        if failure == "lock-close" and descriptor == primary_lock and not injected:
            injected = True
            raise OSError("injected lock close failure")

    monkeypatch.setattr(roots.fcntl, "flock", failing_flock)
    monkeypatch.setattr(roots.os, "close", failing_close)
    with pytest.raises(OSError, match="injected"):
        store.close()
    monkeypatch.setattr(roots.fcntl, "flock", original_flock)
    monkeypatch.setattr(roots.os, "close", original_close)

    script = """
from pathlib import Path
from src.connectors.bitrix_stage_history.artifact_filesystem import ArtifactFilesystem
filesystem = ArtifactFilesystem(Path(__import__('sys').argv[1]), Path(__import__('sys').argv[2]))
filesystem.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "primary"), str(tmp_path / "backup")],
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])},
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_session_directory_creation_rolls_back_when_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filesystem = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    artifact_id = "1" * 32
    original_open = fs.os.open

    def failing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == artifact_id and dir_fd == filesystem._primary.sessions_fd:
            raise OSError("injected directory open failure")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(fs.os, "open", failing_open)
    with pytest.raises(OSError, match="directory open failure"):
        filesystem.create_session(artifact_id)
    assert not (tmp_path / "primary" / ".sessions" / artifact_id).exists()
    filesystem.close()


def test_snapshot_destination_open_failure_closes_source_and_removes_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filesystem = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    session = filesystem.create_session("1" * 32)
    filesystem.write_session_file(session, "summary.json", b"{}\n")
    descriptor_count = len(os.listdir("/proc/self/fd"))

    def fail_open_new(parent_fd: int, name: str, mode: int) -> int:
        raise OSError("injected destination open failure")

    monkeypatch.setattr(fs, "open_new_file", fail_open_new)
    with pytest.raises(OSError, match="destination open failure"):
        filesystem.snapshot_session(session)
    assert len(os.listdir("/proc/self/fd")) == descriptor_count
    assert list((tmp_path / "primary" / ".objects").iterdir()) == []
    filesystem.abandon_session(session)
    filesystem.close()


def test_snapshot_first_close_failure_still_closes_source_and_removes_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filesystem = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    session = filesystem.create_session("1" * 32)
    filesystem.write_session_file(session, "summary.json", b"{}\n")
    descriptor_count = len(os.listdir("/proc/self/fd"))
    original_close = fs.os.close
    injected = False

    def fail_first_close(descriptor: int) -> None:
        nonlocal injected
        original_close(descriptor)
        if not injected:
            injected = True
            raise OSError("injected first close failure")

    monkeypatch.setattr(fs.os, "close", fail_first_close)
    with pytest.raises(OSError, match="first close failure"):
        filesystem.snapshot_session(session)
    monkeypatch.setattr(fs.os, "close", original_close)
    assert len(os.listdir("/proc/self/fd")) == descriptor_count
    assert list((tmp_path / "primary" / ".objects").iterdir()) == []
    filesystem.abandon_session(session)
    filesystem.close()


@pytest.mark.parametrize("failure", ["destination-open", "first-close"])
def test_backup_copy_failure_closes_descriptors_and_removes_staging_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    filesystem = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    session = filesystem.create_session("1" * 32)
    filesystem.write_session_file(session, "summary.json", b"{}\n")
    primary, files = filesystem.snapshot_session(session)
    descriptor_count = len(os.listdir("/proc/self/fd"))
    original_open_new = fs.open_new_file
    original_close = fs.os.close
    injected = False

    def fail_open_new(parent_fd: int, name: str, mode: int) -> int:
        raise OSError("injected backup destination open failure")

    def fail_first_close(descriptor: int) -> None:
        nonlocal injected
        original_close(descriptor)
        if not injected:
            injected = True
            raise OSError("injected backup first close failure")

    if failure == "destination-open":
        monkeypatch.setattr(fs, "open_new_file", fail_open_new)
    else:
        monkeypatch.setattr(fs.os, "close", fail_first_close)
    with pytest.raises(OSError, match="injected backup"):
        filesystem.copy_primary_to_backup(primary, session.artifact_id, files)
    monkeypatch.setattr(fs, "open_new_file", original_open_new)
    monkeypatch.setattr(fs.os, "close", original_close)
    assert len(os.listdir("/proc/self/fd")) == descriptor_count
    assert list((tmp_path / "backup" / ".preparing").iterdir()) == []
    filesystem.discard_prepared_object(primary)
    primary.close()
    filesystem.abandon_session(session)
    filesystem.close()


def test_snapshot_prepared_close_failure_does_not_skip_directory_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filesystem = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    session = filesystem.create_session("1" * 32)
    (session.path / "bad").mkdir(mode=0o700)
    detached: list[int] = []

    def fail_after_detach(prepared: PreparedObject) -> None:
        detached.append(prepared.descriptor)
        prepared.descriptor = -1
        raise OSError("injected prepared close failure")

    monkeypatch.setattr(PreparedObject, "close", fail_after_detach)
    try:
        with pytest.raises(ValueError, match="single-link regular files"):
            filesystem.snapshot_session(session)
        assert list((tmp_path / "primary" / ".preparing").iterdir()) == []
    finally:
        for descriptor in detached:
            os.close(descriptor)
    (session.path / "bad").rmdir()
    filesystem.abandon_session(session)
    filesystem.close()


@pytest.mark.parametrize("helper", ["directory", "file"])
def test_post_open_fstat_failure_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper: str
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    child = parent / "child"
    if helper == "directory":
        child.mkdir(mode=0o700)
    else:
        child.write_bytes(b"data")
        os.chmod(child, 0o600)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    descriptor_count = len(os.listdir("/proc/self/fd"))
    original_fstat = fs.os.fstat

    def failing_fstat(descriptor: int) -> os.stat_result:
        raise OSError("injected fstat failure")

    monkeypatch.setattr(fs.os, "fstat", failing_fstat)
    try:
        with pytest.raises(OSError, match="fstat failure"):
            if helper == "directory":
                fs.open_private_directory(parent_fd, "child")
            else:
                fs.open_regular_file(parent_fd, "child")
    finally:
        monkeypatch.setattr(fs.os, "fstat", original_fstat)
    assert len(os.listdir("/proc/self/fd")) == descriptor_count
    os.close(parent_fd)


@pytest.mark.parametrize("failure", ["fstat", "flock"])
def test_lock_acquisition_failure_closes_descriptor_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    descriptor_count = len(os.listdir("/proc/self/fd"))
    original_fstat = roots.os.fstat
    original_flock = roots.fcntl.flock

    def failing_fstat(descriptor: int) -> os.stat_result:
        raise OSError("injected lock fstat failure")

    def failing_flock(descriptor: int, operation: int) -> None:
        raise OSError("injected flock failure")

    if failure == "fstat":
        monkeypatch.setattr(roots.os, "fstat", failing_fstat)
    else:
        monkeypatch.setattr(roots.fcntl, "flock", failing_flock)
    with pytest.raises(OSError, match="injected"):
        roots._acquire_lock(root_fd)
    monkeypatch.setattr(roots.os, "fstat", original_fstat)
    monkeypatch.setattr(roots.fcntl, "flock", original_flock)
    assert len(os.listdir("/proc/self/fd")) == descriptor_count
    lock_fd = roots._acquire_lock(root_fd)
    roots.fcntl.flock(lock_fd, roots.fcntl.LOCK_UN)
    os.close(lock_fd)
    os.close(root_fd)


@pytest.mark.parametrize("creation", ["session", "object"])
def test_high_level_creation_fstat_failure_removes_directory_and_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, creation: str
) -> None:
    filesystem = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    session: SessionDirectory | None = None
    if creation == "object":
        session = filesystem.create_session("1" * 32)
        filesystem.write_session_file(session, "summary.json", b"{}\n")
    descriptor_count = len(os.listdir("/proc/self/fd"))
    original_fstat = os.fstat

    def failing_fstat(descriptor: int) -> os.stat_result:
        raise OSError("injected creation fstat failure")

    monkeypatch.setattr(os, "fstat", failing_fstat)
    with pytest.raises(OSError, match="creation fstat failure"):
        if creation == "session":
            filesystem.create_session("1" * 32)
        else:
            assert session is not None
            filesystem.snapshot_session(session)
    monkeypatch.setattr(os, "fstat", original_fstat)
    assert len(os.listdir("/proc/self/fd")) == descriptor_count
    directory = ".sessions" if creation == "session" else ".preparing"
    assert list((tmp_path / "primary" / directory).iterdir()) == (
        [session.path] if session is not None and directory == ".sessions" else []
    )
    if session is not None:
        filesystem.abandon_session(session)
    filesystem.close()


def test_producer_exception_remains_primary_when_abandonment_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    detached: list[int] = []

    def failing_close(session: SessionDirectory) -> None:
        detached.append(session.descriptor)
        session.descriptor = -1
        raise OSError("injected abandonment failure")

    monkeypatch.setattr(SessionDirectory, "close", failing_close)
    try:
        with pytest.raises(ValueError, match="producer failed") as captured:
            with store.begin(artifact_kind="owner-manifest"):
                raise ValueError("producer failed")
        assert any("abandonment failed" in note for note in captured.value.__notes__)
    finally:
        for descriptor in detached:
            os.close(descriptor)
    assert list((tmp_path / "primary" / ".sessions").iterdir()) == []


@pytest.mark.parametrize("phase", ["snapshot", "backup"])
def test_failure_cleanup_removes_attempt_inode_without_deleting_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    filesystem = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    session = filesystem.create_session("1" * 32)
    filesystem.write_session_file(session, "summary.json", b"{}\n")
    primary: PreparedObject | None = None
    original_snapshot = fs.snapshot_file
    original_copy = fs.copy_verified_file
    base = tmp_path / ("primary" if phase == "snapshot" else "backup") / ".preparing"

    def replace_and_fail(*args: object, **kwargs: object) -> object:
        current = base / session.artifact_id
        moved = current.with_name(f"moved-{session.artifact_id}")
        current.rename(moved)
        current.mkdir(mode=0o700)
        raise OSError("injected replacement failure")

    try:
        if phase == "snapshot":
            monkeypatch.setattr(fs, "snapshot_file", replace_and_fail)
            with pytest.raises(OSError, match="replacement failure"):
                filesystem.snapshot_session(session)
        else:
            primary, files = filesystem.snapshot_session(session)
            monkeypatch.setattr(fs, "copy_verified_file", replace_and_fail)
            with pytest.raises(OSError, match="replacement failure"):
                filesystem.copy_primary_to_backup(primary, session.artifact_id, files)
        assert not (base / f"moved-{session.artifact_id}").exists()
        assert (base / session.artifact_id).is_dir()
    finally:
        monkeypatch.setattr(fs, "snapshot_file", original_snapshot)
        monkeypatch.setattr(fs, "copy_verified_file", original_copy)
        replacement = base / session.artifact_id
        if replacement.exists():
            replacement.rmdir()
        if primary is not None:
            filesystem.discard_prepared_object(primary)
            primary.close()
        filesystem.abandon_session(session)
        filesystem.close()


@pytest.mark.parametrize("phase", ["snapshot", "backup"])
def test_seal_failure_preserves_foreign_preparing_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    store = _store(tmp_path)
    original = fs.snapshot_file if phase == "snapshot" else fs.copy_verified_file
    base = tmp_path / ("primary" if phase == "snapshot" else "backup") / ".preparing"
    replacement: Path | None = None

    def replace_and_fail(*args: object, **kwargs: object) -> object:
        nonlocal replacement
        current = next(base.iterdir())
        current.rename(current.with_name(f"moved-{current.name}"))
        current.mkdir(mode=0o700)
        replacement = current
        (current / "foreign.txt").write_text("preserve me", encoding="utf-8")
        raise OSError("injected replacement failure")

    monkeypatch.setattr(
        fs,
        "snapshot_file" if phase == "snapshot" else "copy_verified_file",
        replace_and_fail,
    )
    with pytest.raises(OSError, match="replacement failure"):
        _seal(store)
    monkeypatch.setattr(
        fs,
        "snapshot_file" if phase == "snapshot" else "copy_verified_file",
        original,
    )
    assert replacement is not None
    assert (replacement / "foreign.txt").read_text(encoding="utf-8") == "preserve me"


@pytest.mark.parametrize("failure", ["fstat", "write", "fsync", "close"])
def test_marker_construction_failure_removes_temp_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    filesystem = ArtifactFilesystem(tmp_path / "primary", tmp_path / "backup")
    original_fstat = os.fstat
    original_write = fs.write_all
    original_fsync = os.fsync
    original_close = os.close
    injected = False

    def fail_fstat(descriptor: int) -> os.stat_result:
        raise OSError("injected marker fstat failure")

    def fail_write(descriptor: int, content: bytes) -> None:
        raise OSError("injected marker write failure")

    def fail_fsync(descriptor: int) -> None:
        raise OSError("injected marker fsync failure")

    def fail_close(descriptor: int) -> None:
        nonlocal injected
        original_close(descriptor)
        if not injected:
            injected = True
            raise OSError("injected marker close failure")

    if failure == "fstat":
        monkeypatch.setattr(os, "fstat", fail_fstat)
    elif failure == "write":
        monkeypatch.setattr(fs, "write_all", fail_write)
    elif failure == "fsync":
        monkeypatch.setattr(os, "fsync", fail_fsync)
    else:
        monkeypatch.setattr(os, "close", fail_close)
    descriptor_count = len(os.listdir("/proc/self/fd"))
    with pytest.raises(OSError, match="injected marker"):
        filesystem.publish_backup_marker("1" * 32, b"{}\n")
    monkeypatch.setattr(os, "fstat", original_fstat)
    monkeypatch.setattr(fs, "write_all", original_write)
    monkeypatch.setattr(os, "fsync", original_fsync)
    monkeypatch.setattr(os, "close", original_close)
    assert len(os.listdir("/proc/self/fd")) == descriptor_count
    assert list((tmp_path / "backup" / ".preparing").iterdir()) == []
    assert list((tmp_path / "backup" / "sealed").iterdir()) == []
    filesystem.publish_backup_marker("1" * 32, b"{}\n")
    filesystem.close()
