"""Pinned-directory state machine for restricted Bitrix artifacts."""

from __future__ import annotations

import hmac
import os
import stat
from pathlib import Path

from src.connectors.bitrix_stage_history import artifact_fs_primitives as fs
from src.connectors.bitrix_stage_history.artifact_fs_models import (
    ArtifactStorageLimits,
    PreparedObject,
    PublishedMarker,
    PublishedObjectIdentity,
    SessionDirectory,
)
from src.connectors.bitrix_stage_history.artifact_fs_roots import (
    OBJECTS_DIRECTORY,
    PREPARING_DIRECTORY,
    SESSIONS_DIRECTORY,
    RootState,
    acquire_root_locks,
    close_root_state,
    open_root_state,
    validate_distinct_states,
)
from src.connectors.bitrix_stage_history.artifact_manifest import (
    MANIFEST_NAME,
    ArtifactFileDigest,
    canonical_json_bytes,
    canonical_marker_bytes,
    parse_manifest_bytes,
)

__all__ = [
    "ArtifactStorageLimits",
    "PreparedObject",
    "PublishedMarker",
    "PublishedObjectIdentity",
    "SessionDirectory",
]


class ArtifactFilesystem:
    """Own snapshot, commit-marker publication, verification, and recovery."""

    def __init__(
        self,
        root: Path,
        backup_root: Path,
        limits: ArtifactStorageLimits | None = None,
    ) -> None:
        self.limits = limits or ArtifactStorageLimits()
        self._primary = open_root_state(root)
        try:
            self._backup = open_root_state(backup_root)
            validate_distinct_states(self._primary, self._backup)
            acquire_root_locks(self._primary, self._backup)
            self.recover_uncommitted()
        except BaseException:
            backup = getattr(self, "_backup", None)
            states = [self._primary]
            if isinstance(backup, RootState):
                states.append(backup)
            for state in states:
                try:
                    close_root_state(state)
                except OSError:
                    pass
            raise
        self.root = self._primary.path
        self.backup_root = self._backup.path
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[OSError] = []
        for state in (self._primary, self._backup):
            try:
                close_root_state(state)
            except OSError as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def create_session(self, artifact_id: str) -> SessionDirectory:
        descriptor = fs.create_private_directory(self._primary.sessions_fd, artifact_id)
        try:
            details = os.fstat(descriptor)
            return SessionDirectory(
                artifact_id=artifact_id,
                path=self.root / SESSIONS_DIRECTORY / artifact_id,
                descriptor=descriptor,
                device=details.st_dev,
                inode=details.st_ino,
            )
        except BaseException:
            _remove_new_directory(self._primary.sessions_fd, artifact_id, descriptor)
            raise

    def assert_session_path_identity(self, session: SessionDirectory) -> None:
        details = os.stat(
            session.artifact_id,
            dir_fd=self._primary.sessions_fd,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(details.st_mode):
            raise RuntimeError("artifact session path was replaced by a symlink")
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_dev != session.device
            or details.st_ino != session.inode
        ):
            raise RuntimeError("artifact session pathname identity changed")

    def write_session_file(self, session: SessionDirectory, name: str, content: bytes) -> Path:
        fs.write_new_file(session.descriptor, name, content, mode=0o600)
        return session.path / name

    def snapshot_session(
        self, session: SessionDirectory
    ) -> tuple[PreparedObject, tuple[ArtifactFileDigest, ...]]:
        self.assert_session_path_identity(session)
        destination = _create_object(self._primary, session.artifact_id)
        try:
            names = tuple(sorted(os.listdir(session.descriptor)))
            if not names:
                raise ValueError("restricted artifacts must contain at least one data file")
            if len(names) > self.limits.max_files:
                raise RuntimeError("artifact file-count limit exceeded")
            collected: list[ArtifactFileDigest] = []
            total_bytes = 0
            for name in names:
                remaining = self.limits.max_total_bytes - total_bytes
                if remaining < 0:
                    raise RuntimeError("artifact total-byte limit exceeded")
                digest = fs.snapshot_file(
                    session.descriptor,
                    destination.descriptor,
                    name,
                    max_file_bytes=min(self.limits.max_file_bytes, remaining),
                )
                collected.append(digest)
                total_bytes += digest.byte_count
            digests = tuple(collected)
            if tuple(sorted(os.listdir(session.descriptor))) != names:
                raise RuntimeError("artifact source directory changed during snapshot")
            os.fsync(destination.descriptor)
            return destination, digests
        except BaseException as exc:
            try:
                _discard_prepared_in_state(self._primary, destination)
            except BaseException as cleanup_error:
                exc.add_note(f"artifact snapshot cleanup failed: {cleanup_error!r}")
            try:
                destination.close()
            except BaseException as cleanup_error:
                exc.add_note(f"artifact snapshot descriptor cleanup failed: {cleanup_error!r}")
            raise

    def copy_primary_to_backup(
        self,
        primary: PreparedObject,
        artifact_id: str,
        expected: tuple[ArtifactFileDigest, ...],
    ) -> PreparedObject:
        backup = _create_object(self._backup, artifact_id)
        try:
            for item in expected:
                fs.copy_verified_file(primary.descriptor, backup.descriptor, item)
            os.fsync(backup.descriptor)
            return backup
        except BaseException as exc:
            try:
                _discard_prepared_in_state(self._backup, backup)
            except BaseException as cleanup_error:
                exc.add_note(f"artifact backup cleanup failed: {cleanup_error!r}")
            try:
                backup.close()
            except BaseException as cleanup_error:
                exc.add_note(f"artifact backup descriptor cleanup failed: {cleanup_error!r}")
            raise

    def write_manifest(self, artifact: PreparedObject, content: bytes) -> None:
        if len(content) > self.limits.max_manifest_bytes:
            raise RuntimeError("artifact manifest byte limit exceeded")
        fs.write_new_file(artifact.descriptor, MANIFEST_NAME, content, mode=0o600)

    def make_immutable(self, artifact: PreparedObject) -> None:
        fs.make_directory_immutable(artifact.descriptor)

    def publish_backup_object(
        self,
        artifact: PreparedObject,
        artifact_id: str,
        expected: tuple[ArtifactFileDigest, ...],
    ) -> Path:
        return self._publish_object(self._backup, artifact, artifact_id, expected)

    def publish_primary_object(
        self,
        artifact: PreparedObject,
        artifact_id: str,
        expected: tuple[ArtifactFileDigest, ...],
    ) -> Path:
        return self._publish_object(self._primary, artifact, artifact_id, expected)

    def verify_published_object(
        self,
        artifact: PreparedObject,
        expected: tuple[ArtifactFileDigest, ...],
        manifest_bytes: bytes,
    ) -> None:
        object_fd = fs.open_private_directory(artifact.parent_descriptor, artifact.artifact_id)
        try:
            details = os.fstat(object_fd)
            if (details.st_dev, details.st_ino) != (artifact.device, artifact.inode):
                raise RuntimeError("artifact published pathname identity changed")
            fs.validate_immutable_directory(object_fd, expected, MANIFEST_NAME)
            stored_manifest = fs.read_file(
                object_fd,
                MANIFEST_NAME,
                max_bytes=self.limits.max_manifest_bytes,
                immutable=True,
            )
            if not hmac.compare_digest(stored_manifest, manifest_bytes):
                raise RuntimeError("artifact published manifest bytes changed before commit")
        finally:
            os.close(object_fd)

    def prepared_owner_group(self, artifact: PreparedObject) -> tuple[int, int]:
        details = os.fstat(artifact.descriptor)
        if (details.st_dev, details.st_ino) != (artifact.device, artifact.inode):
            raise RuntimeError("artifact prepared object identity changed")
        return details.st_uid, details.st_gid

    def published_identity(self, artifact_id: str, *, backup: bool) -> PublishedObjectIdentity:
        state = self._backup if backup else self._primary
        descriptor = fs.open_private_directory(state.objects_fd, artifact_id)
        try:
            details = os.fstat(descriptor)
            return PublishedObjectIdentity(
                path=state.path / OBJECTS_DIRECTORY / artifact_id,
                device=details.st_dev,
                inode=details.st_ino,
                owner_uid=details.st_uid,
                group_gid=details.st_gid,
                mode=stat.S_IMODE(details.st_mode),
            )
        finally:
            os.close(descriptor)

    def publish_backup_marker(self, artifact_id: str, content: bytes) -> PublishedMarker:
        return self._publish_marker(self._backup, artifact_id, content)

    def publish_primary_marker(self, artifact_id: str, content: bytes) -> PublishedMarker:
        return self._publish_marker(self._primary, artifact_id, content)

    def read_primary_marker(self, artifact_id: str) -> bytes:
        return fs.read_file(
            self._primary.sealed_fd,
            f"{artifact_id}.json",
            max_bytes=self.limits.max_marker_bytes,
            immutable=True,
        )

    def read_backup_marker(self, artifact_id: str) -> bytes:
        return fs.read_file(
            self._backup.sealed_fd,
            f"{artifact_id}.json",
            max_bytes=self.limits.max_marker_bytes,
            immutable=True,
        )

    def read_primary_manifest(self, artifact_id: str) -> bytes:
        return self._read_manifest(self._primary, artifact_id)

    def read_backup_manifest(self, artifact_id: str) -> bytes:
        return self._read_manifest(self._backup, artifact_id)

    def verify_primary_files(
        self, artifact_id: str, expected: tuple[ArtifactFileDigest, ...]
    ) -> None:
        self._verify_files(self._primary, artifact_id, expected)

    def verify_backup_files(
        self, artifact_id: str, expected: tuple[ArtifactFileDigest, ...]
    ) -> None:
        self._verify_files(self._backup, artifact_id, expected)

    def abandon_session(self, session: SessionDirectory) -> None:
        close_error: OSError | None = None
        try:
            session.close()
        except OSError as exc:
            close_error = exc
        self._remove_session_inode(session)
        if close_error is not None:
            raise close_error

    def cleanup_artifact(
        self,
        artifact_id: str,
        session: SessionDirectory | None,
        *,
        primary_marker: PublishedMarker | None = None,
        backup_marker: PublishedMarker | None = None,
    ) -> None:
        errors: list[Exception] = []
        for state, marker in (
            (self._primary, primary_marker),
            (self._backup, backup_marker),
        ):
            if marker is not None:
                try:
                    _remove_marker_identity(state, marker)
                except Exception as exc:
                    errors.append(exc)
        if session is not None:
            try:
                session.close()
            except Exception as exc:
                errors.append(exc)
            try:
                self._remove_session_inode(session, strict=False)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("artifact cleanup failed", errors)

    def discard_prepared_object(self, artifact: PreparedObject) -> None:
        for state in (self._primary, self._backup):
            for parent_fd in (state.preparing_fd, state.objects_fd):
                for name in os.listdir(parent_fd):
                    details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    if (details.st_dev, details.st_ino) == (artifact.device, artifact.inode):
                        fs.remove_flat_directory(parent_fd, name)
                        return

    def validate_manifest_limits(self, files: tuple[ArtifactFileDigest, ...]) -> None:
        if len(files) > self.limits.max_files:
            raise RuntimeError("artifact manifest exceeds the file-count limit")
        total_bytes = 0
        for item in files:
            if item.byte_count > self.limits.max_file_bytes:
                raise RuntimeError("artifact manifest file exceeds its byte limit")
            total_bytes += item.byte_count
            if total_bytes > self.limits.max_total_bytes:
                raise RuntimeError("artifact manifest exceeds the total-byte limit")

    def recover_uncommitted(self) -> None:
        for state in (self._primary, self._backup):
            fs.clear_flat_directory(state.sessions_fd)
            fs.clear_flat_directory(state.preparing_fd)
        primary_markers = _marker_ids(self._primary)
        backup_markers = _marker_ids(self._backup)
        if primary_markers - backup_markers:
            raise RuntimeError("committed primary artifact is missing its backup marker")
        for artifact_id in backup_markers - primary_markers:
            fs.remove_file(self._backup.sealed_fd, f"{artifact_id}.json")
        _remove_uncommitted_objects(self._primary, primary_markers)
        _remove_uncommitted_objects(self._backup, primary_markers)
        for artifact_id in primary_markers:
            self._validate_committed_artifact(artifact_id)

    def _validate_committed_artifact(self, artifact_id: str) -> None:
        primary_marker = self.read_primary_marker(artifact_id)
        backup_marker = self.read_backup_marker(artifact_id)
        if not hmac.compare_digest(primary_marker, backup_marker):
            raise RuntimeError("committed artifact markers do not match")
        primary_manifest = self.read_primary_manifest(artifact_id)
        backup_manifest = self.read_backup_manifest(artifact_id)
        if not hmac.compare_digest(primary_manifest, backup_manifest):
            raise RuntimeError("committed artifact manifests do not match")
        manifest = parse_manifest_bytes(primary_manifest)
        if manifest.artifact_id != artifact_id:
            raise RuntimeError("committed artifact manifest ID does not match marker")
        if not hmac.compare_digest(primary_manifest, canonical_json_bytes(manifest.to_dict())):
            raise RuntimeError("committed artifact manifest is not canonical")
        if not hmac.compare_digest(primary_marker, canonical_marker_bytes(manifest)):
            raise RuntimeError("committed artifact marker is not canonical")
        self.validate_manifest_limits(manifest.files)
        self.verify_primary_files(artifact_id, manifest.files)
        self.verify_backup_files(artifact_id, manifest.files)
        primary = self.published_identity(artifact_id, backup=False)
        backup = self.published_identity(artifact_id, backup=True)
        provenance = manifest.provenance
        if (
            primary.path != Path(provenance.artifact_path)
            or (primary.device, primary.inode)
            != (
                provenance.primary_device,
                provenance.primary_inode,
            )
            or (backup.device, backup.inode)
            != (
                provenance.backup_device,
                provenance.backup_inode,
            )
            or (primary.owner_uid, primary.group_gid, primary.mode)
            != (provenance.owner_uid, provenance.group_gid, provenance.directory_mode)
            or (backup.owner_uid, backup.group_gid, backup.mode)
            != (provenance.owner_uid, provenance.group_gid, provenance.directory_mode)
        ):
            raise RuntimeError("committed artifact provenance does not match filesystem identity")

    def _publish_marker(
        self, state: RootState, artifact_id: str, content: bytes
    ) -> PublishedMarker:
        if len(content) > self.limits.max_marker_bytes:
            raise RuntimeError("artifact marker byte limit exceeded")
        temporary = f"{artifact_id}.commit"
        final = f"{artifact_id}.json"
        descriptor = -1
        marker: PublishedMarker | None = None
        try:
            descriptor = fs.open_new_file(state.preparing_fd, temporary, 0o400)
            try:
                pinned = os.fstat(descriptor)
            except OSError:
                pinned = os.stat(f"/proc/self/fd/{descriptor}")
            marker = PublishedMarker(pinned.st_dev, pinned.st_ino)
            details = os.fstat(descriptor)
            if (details.st_dev, details.st_ino) != (marker.device, marker.inode):
                raise RuntimeError("artifact temporary marker identity changed")
            fs.write_all(descriptor, content)
            os.fsync(descriptor)
            closing = descriptor
            descriptor = -1
            os.close(closing)
            fs.rename_entry_no_replace(
                state.preparing_fd,
                temporary,
                state.sealed_fd,
                final,
            )
            published = os.stat(final, dir_fd=state.sealed_fd, follow_symlinks=False)
            if (published.st_dev, published.st_ino) != (marker.device, marker.inode):
                raise RuntimeError("artifact published marker identity changed")
            return marker
        except BaseException:
            if descriptor >= 0:
                closing = descriptor
                descriptor = -1
                try:
                    os.close(closing)
                except OSError:
                    pass
            if marker is not None:
                _remove_marker_identity(state, marker)
            raise

    @staticmethod
    def _publish_object(
        state: RootState,
        artifact: PreparedObject,
        artifact_id: str,
        expected: tuple[ArtifactFileDigest, ...],
    ) -> Path:
        _validate_object(artifact, state, artifact_id)
        current = os.stat(artifact_id, dir_fd=state.preparing_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (artifact.device, artifact.inode):
            raise RuntimeError("artifact prepared pathname identity changed")
        fs.rename_entry_no_replace(
            state.preparing_fd,
            artifact_id,
            state.objects_fd,
            artifact_id,
        )
        artifact.parent_descriptor = state.objects_fd
        artifact.path = state.path / OBJECTS_DIRECTORY / artifact_id
        published = fs.open_private_directory(state.objects_fd, artifact_id)
        try:
            details = os.fstat(published)
            if (details.st_dev, details.st_ino) != (artifact.device, artifact.inode):
                raise RuntimeError("artifact published pathname identity changed")
        finally:
            os.close(published)
        return artifact.path

    def _read_manifest(self, state: RootState, artifact_id: str) -> bytes:
        object_fd = fs.open_private_directory(state.objects_fd, artifact_id)
        try:
            return fs.read_file(
                object_fd,
                MANIFEST_NAME,
                max_bytes=self.limits.max_manifest_bytes,
                immutable=True,
            )
        finally:
            os.close(object_fd)

    @staticmethod
    def _verify_files(
        state: RootState,
        artifact_id: str,
        expected: tuple[ArtifactFileDigest, ...],
    ) -> None:
        object_fd = fs.open_private_directory(state.objects_fd, artifact_id)
        try:
            fs.validate_immutable_directory(object_fd, expected, MANIFEST_NAME)
        finally:
            os.close(object_fd)

    def _remove_session_inode(self, session: SessionDirectory, *, strict: bool = True) -> None:
        matching_name: str | None = None
        for name in os.listdir(self._primary.sessions_fd):
            details = os.stat(name, dir_fd=self._primary.sessions_fd, follow_symlinks=False)
            if (details.st_dev, details.st_ino) == (session.device, session.inode):
                matching_name = name
                break
        if matching_name is not None:
            fs.remove_flat_directory(self._primary.sessions_fd, matching_name)
        try:
            current = os.stat(
                session.artifact_id,
                dir_fd=self._primary.sessions_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if stat.S_ISLNK(current.st_mode):
            os.unlink(session.artifact_id, dir_fd=self._primary.sessions_fd)
            os.fsync(self._primary.sessions_fd)
            if strict:
                raise RuntimeError("artifact session path was replaced by a symlink")
            return
        if strict:
            raise RuntimeError("artifact session pathname identity changed")


def _create_object(state: RootState, artifact_id: str) -> PreparedObject:
    descriptor = fs.create_private_directory(state.preparing_fd, artifact_id)
    try:
        details = os.fstat(descriptor)
        return PreparedObject(
            artifact_id=artifact_id,
            path=state.path / PREPARING_DIRECTORY / artifact_id,
            descriptor=descriptor,
            parent_descriptor=state.preparing_fd,
            device=details.st_dev,
            inode=details.st_ino,
        )
    except BaseException:
        _remove_new_directory(state.preparing_fd, artifact_id, descriptor)
        raise


def _remove_new_directory(parent_fd: int, name: str, descriptor: int) -> None:
    details = os.stat(f"/proc/self/fd/{descriptor}")
    try:
        os.close(descriptor)
    finally:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (details.st_dev, details.st_ino):
            raise RuntimeError("new artifact directory identity changed during rollback")
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)


def _validate_object(artifact: PreparedObject, state: RootState, artifact_id: str) -> Path:
    if artifact.artifact_id != artifact_id or artifact.parent_descriptor != state.preparing_fd:
        raise RuntimeError("artifact immutable object identity is invalid")
    return artifact.path


def _discard_prepared_in_state(state: RootState, artifact: PreparedObject) -> None:
    for name in os.listdir(state.preparing_fd):
        details = os.stat(name, dir_fd=state.preparing_fd, follow_symlinks=False)
        if (details.st_dev, details.st_ino) == (artifact.device, artifact.inode):
            fs.remove_flat_directory(state.preparing_fd, name)
            return


def _remove_marker_identity(state: RootState, marker: PublishedMarker) -> None:
    for parent_fd in (state.preparing_fd, state.sealed_fd):
        for name in os.listdir(parent_fd):
            details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (details.st_dev, details.st_ino) == (marker.device, marker.inode):
                fs.remove_file_identity(parent_fd, name, marker.device, marker.inode)
                return


def _marker_ids(state: RootState) -> set[str]:
    artifact_ids: set[str] = set()
    for name in os.listdir(state.sealed_fd):
        if not name.endswith(".json"):
            raise RuntimeError("sealed artifact marker directory contains an invalid entry")
        descriptor = fs.open_regular_file(state.sealed_fd, name)
        try:
            if stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o222:
                raise RuntimeError("sealed artifact marker is writable")
        finally:
            os.close(descriptor)
        artifact_ids.add(name.removesuffix(".json"))
    return artifact_ids


def _remove_uncommitted_objects(state: RootState, committed: set[str]) -> None:
    object_ids = set(os.listdir(state.objects_fd))
    if committed - object_ids:
        raise RuntimeError("committed artifact marker is missing its immutable object")
    for artifact_id in object_ids - committed:
        fs.remove_flat_directory(state.objects_fd, artifact_id)
