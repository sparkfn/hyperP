"""Authenticated restricted artifact store for Bitrix qualification evidence."""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from types import TracebackType
from typing import Protocol, Self

from src.connectors.bitrix_stage_history.artifact_filesystem import (
    ArtifactFilesystem,
    ArtifactStorageLimits,
    PreparedObject,
    PublishedMarker,
    SessionDirectory,
)
from src.connectors.bitrix_stage_history.artifact_manifest import (
    MANIFEST_SCHEMA_VERSION,
    ArtifactFileDigest,
    ArtifactManifest,
    canonical_json_bytes,
    canonical_marker_bytes,
    canonical_metadata_json,
    compute_manifest_hmac,
    parse_manifest_bytes,
)
from src.connectors.bitrix_stage_history.artifact_provenance import (
    ArtifactProvenance,
    ArtifactProvenanceInput,
)
from src.connectors.bitrix_stage_history.artifact_signing import (
    ArtifactSigningKey,
    ArtifactSigningKeyProvider,
    StaticArtifactSigningKeyProvider,
)
from src.models import JsonValue

__all__ = [
    "ArtifactSigningKey",
    "ArtifactSigningKeyProvider",
    "StaticArtifactSigningKeyProvider",
]

_EMPTY_HMAC = "0" * 64


class ArtifactStore(Protocol):
    """Protocol used by capability and corrective-backfill workflows."""

    @contextmanager
    def begin(self, *, artifact_kind: str) -> Iterator[RestrictedArtifactSession]: ...

    def verify(self, artifact_id: str) -> ArtifactManifest: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class RestrictedArtifactSession:
    """Private producer workspace that can be snapshotted and sealed exactly once."""

    def __init__(
        self,
        store: LocalRestrictedArtifactStore,
        *,
        artifact_kind: str,
        directory: SessionDirectory,
    ) -> None:
        self._store = store
        self._directory = directory
        self.artifact_id = directory.artifact_id
        self.artifact_kind = artifact_kind
        self.path = directory.path
        self._closed = False

    def write_json(self, file_name: str, value: Mapping[str, JsonValue]) -> Path:
        """Write one canonical flat JSON file without following filesystem links."""
        self._ensure_open()
        return self._store.filesystem.write_session_file(
            self._directory,
            file_name,
            canonical_json_bytes(value),
        )

    def write_bytes(self, file_name: str, content: bytes) -> Path:
        """Write one private flat evidence file into the producer workspace."""
        self._ensure_open()
        if not isinstance(content, bytes):
            raise TypeError("restricted artifact content must be bytes")
        return self._store.filesystem.write_session_file(
            self._directory,
            file_name,
            content,
        )

    def seal(
        self,
        *,
        metadata: Mapping[str, JsonValue],
        provenance: ArtifactProvenanceInput,
        retention_expires_at: datetime,
    ) -> ArtifactManifest:
        """Snapshot producer files, authenticate both copies, and commit atomically."""
        self._ensure_open()
        manifest = self._store._seal(
            directory=self._directory,
            artifact_kind=self.artifact_kind,
            metadata=metadata,
            provenance=provenance,
            retention_expires_at=retention_expires_at,
        )
        self._closed = True
        return manifest

    def abandon(self) -> None:
        if self._closed:
            return
        self._store.filesystem.abandon_session(self._directory)
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("restricted artifact session is closed")


class LocalRestrictedArtifactStore:
    """Host-backed primary and verified-backup store for trusted producers.

    The service UID is the storage trust boundary: roots must be privately owned
    by that UID, and no untrusted process may run under it. Mode-based sealing
    protects against other identities and accidental mutation; root or the
    storage UID can always bypass local filesystem permissions.
    """

    def __init__(
        self,
        root: Path,
        backup_root: Path,
        signing_keys: ArtifactSigningKeyProvider,
        *,
        filesystem: ArtifactFilesystem | None = None,
        limits: ArtifactStorageLimits | None = None,
    ) -> None:
        if filesystem is not None and limits is not None:
            raise ValueError("artifact limits cannot override an injected filesystem")
        self.filesystem = filesystem or ArtifactFilesystem(root, backup_root, limits)
        if self.filesystem.root != root.absolute():
            raise ValueError("artifact filesystem primary root does not match store configuration")
        if self.filesystem.backup_root != backup_root.absolute():
            raise ValueError("artifact filesystem backup root does not match store configuration")
        self._signing_keys = signing_keys
        self._lifecycle_lock = Lock()
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        if not self._lifecycle_lock.acquire(blocking=False):
            raise RuntimeError("artifact store cannot close while an operation is active")
        try:
            if self._closed:
                return
            try:
                self.filesystem.close()
            finally:
                self._closed = True
        finally:
            self._lifecycle_lock.release()

    def __enter__(self) -> LocalRestrictedArtifactStore:
        if self._closed:
            raise RuntimeError("artifact store is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def begin(self, *, artifact_kind: str) -> Iterator[RestrictedArtifactSession]:
        """Open a producer session for trusted in-process connector code."""
        _validate_artifact_kind(artifact_kind)
        if not self._lifecycle_lock.acquire(blocking=False):
            raise RuntimeError("artifact store already has an active producer session")
        try:
            self._ensure_open()
            directory = self.filesystem.create_session(uuid.uuid4().hex)
            session = RestrictedArtifactSession(
                self,
                artifact_kind=artifact_kind,
                directory=directory,
            )
            try:
                yield session
            except BaseException as exc:
                if not session._closed:
                    try:
                        session.abandon()
                    except BaseException as cleanup_error:
                        exc.add_note(f"artifact session abandonment failed: {cleanup_error!r}")
                raise
            else:
                if not session._closed:
                    session.abandon()
        finally:
            self._lifecycle_lock.release()

    def verify(self, artifact_id: str) -> ArtifactManifest:
        """Authenticate marker, manifest, primary snapshot, and backup snapshot."""
        _validate_artifact_id(artifact_id)
        if not self._lifecycle_lock.acquire(blocking=False):
            raise RuntimeError("artifact store already has an active operation")
        try:
            self._ensure_open()
            return self._verify_locked(artifact_id)
        finally:
            self._lifecycle_lock.release()

    def _verify_locked(self, artifact_id: str) -> ArtifactManifest:
        primary_marker = self.filesystem.read_primary_marker(artifact_id)
        backup_marker = self.filesystem.read_backup_marker(artifact_id)
        if not hmac.compare_digest(primary_marker, backup_marker):
            raise RuntimeError("artifact backup marker does not match primary")
        primary_manifest_bytes = self.filesystem.read_primary_manifest(artifact_id)
        backup_manifest_bytes = self.filesystem.read_backup_manifest(artifact_id)
        if not hmac.compare_digest(primary_manifest_bytes, backup_manifest_bytes):
            raise RuntimeError("artifact backup manifest does not match primary")
        manifest = parse_manifest_bytes(primary_manifest_bytes)
        canonical_manifest = canonical_json_bytes(manifest.to_dict())
        if not hmac.compare_digest(primary_manifest_bytes, canonical_manifest):
            raise RuntimeError("sealed artifact manifest is not canonical")
        self._authenticate_manifest(manifest)
        if manifest.artifact_id != artifact_id:
            raise RuntimeError("artifact manifest ID does not match requested artifact")
        expected_backup = self.filesystem.backup_root / ".objects" / artifact_id
        if Path(manifest.backup_path) != expected_backup:
            raise RuntimeError("artifact manifest backup path does not match configured store")
        if manifest.is_expired():
            raise RuntimeError("sealed artifact retention period has expired")
        _verify_marker(primary_marker, manifest)
        self.filesystem.validate_manifest_limits(manifest.files)
        self._verify_provenance_identity(manifest)
        self.filesystem.verify_primary_files(artifact_id, manifest.files)
        self.filesystem.verify_backup_files(artifact_id, manifest.files)
        return manifest

    def _seal(
        self,
        *,
        directory: SessionDirectory,
        artifact_kind: str,
        metadata: Mapping[str, JsonValue],
        provenance: ArtifactProvenanceInput,
        retention_expires_at: datetime,
    ) -> ArtifactManifest:
        if retention_expires_at.tzinfo is None:
            raise ValueError("artifact retention expiry must be timezone-aware")
        expiry = retention_expires_at.astimezone(UTC)
        if expiry <= datetime.now(UTC):
            raise ValueError("artifact retention expiry must be in the future")
        metadata_json = canonical_metadata_json(metadata)
        signing_key = self._validated_current_key()
        primary: PreparedObject | None = None
        backup: PreparedObject | None = None
        primary_marker: PublishedMarker | None = None
        backup_marker: PublishedMarker | None = None
        try:
            self.filesystem.assert_session_path_identity(directory)
            primary, files = self.filesystem.snapshot_session(directory)
            backup = self.filesystem.copy_primary_to_backup(primary, directory.artifact_id, files)
            self.filesystem.abandon_session(directory)
            primary_owner = self.filesystem.prepared_owner_group(primary)
            backup_owner = self.filesystem.prepared_owner_group(backup)
            if primary_owner != backup_owner:
                raise RuntimeError("artifact primary and backup ownership do not match")
            stored_provenance = ArtifactProvenance.from_input(
                provenance,
                artifact_path=(self.filesystem.root / ".objects" / directory.artifact_id),
                primary_device=primary.device,
                primary_inode=primary.inode,
                backup_device=backup.device,
                backup_inode=backup.inode,
                owner_uid=primary_owner[0],
                group_gid=primary_owner[1],
                total_bytes=sum(item.byte_count for item in files),
            )
            manifest = _build_manifest(
                artifact_id=directory.artifact_id,
                artifact_kind=artifact_kind,
                retention_expires_at=expiry,
                metadata_json=metadata_json,
                files=files,
                provenance=stored_provenance,
                backup_path=self.filesystem.backup_root / ".objects" / directory.artifact_id,
                signing_key=signing_key,
            )
            manifest_bytes = canonical_json_bytes(manifest.to_dict())
            marker_bytes = _marker_bytes(manifest)
            self.filesystem.write_manifest(primary, manifest_bytes)
            self.filesystem.write_manifest(backup, manifest_bytes)
            self.filesystem.publish_backup_object(backup, directory.artifact_id, files)
            self.filesystem.publish_primary_object(primary, directory.artifact_id, files)
            self.filesystem.make_immutable(backup)
            self.filesystem.make_immutable(primary)
            self.filesystem.verify_published_object(backup, files, manifest_bytes)
            self.filesystem.verify_published_object(primary, files, manifest_bytes)
            self._verify_provenance_identity(manifest)
            backup.close()
            primary.close()
            backup_marker = self.filesystem.publish_backup_marker(
                directory.artifact_id, marker_bytes
            )
            self._verify_publication_state(
                manifest,
                manifest_bytes,
                marker_bytes,
                primary_marker_published=False,
            )
            primary_marker = self.filesystem.publish_primary_marker(
                directory.artifact_id, marker_bytes
            )
            self._verify_publication_state(
                manifest,
                manifest_bytes,
                marker_bytes,
                primary_marker_published=True,
            )
        except BaseException as exc:
            for prepared in (primary, backup):
                if prepared is not None:
                    try:
                        self.filesystem.discard_prepared_object(prepared)
                    except BaseException as cleanup_error:
                        exc.add_note(f"artifact object cleanup failed: {cleanup_error!r}")
                    try:
                        prepared.close()
                    except BaseException as cleanup_error:
                        exc.add_note(f"artifact descriptor cleanup failed: {cleanup_error!r}")
            try:
                self.filesystem.cleanup_artifact(
                    directory.artifact_id,
                    directory,
                    primary_marker=primary_marker,
                    backup_marker=backup_marker,
                )
            except BaseException as cleanup_error:
                exc.add_note(f"artifact cleanup failed: {cleanup_error!r}")
            raise
        return manifest

    def _authenticate_manifest(self, manifest: ArtifactManifest) -> None:
        signing_key = self._signing_keys.get(manifest.signing_key_id)
        if signing_key is None or signing_key.key_id != manifest.signing_key_id:
            raise RuntimeError("artifact manifest signing key is unavailable")
        expected = compute_manifest_hmac(manifest, signing_key.secret)
        if not hmac.compare_digest(expected, manifest.manifest_hmac):
            raise RuntimeError("artifact manifest HMAC verification failed")

    def _validated_current_key(self) -> ArtifactSigningKey:
        signing_key = self._signing_keys.current()
        resolved = self._signing_keys.get(signing_key.key_id)
        if (
            resolved is None
            or resolved.key_id != signing_key.key_id
            or not hmac.compare_digest(resolved.secret, signing_key.secret)
        ):
            raise RuntimeError("current artifact signing key is not consistently resolvable")
        return signing_key

    def _verify_provenance_identity(self, manifest: ArtifactManifest) -> None:
        primary = self.filesystem.published_identity(manifest.artifact_id, backup=False)
        backup = self.filesystem.published_identity(manifest.artifact_id, backup=True)
        expected = (
            Path(manifest.provenance.artifact_path),
            manifest.provenance.primary_device,
            manifest.provenance.primary_inode,
            manifest.provenance.owner_uid,
            manifest.provenance.group_gid,
            manifest.provenance.directory_mode,
        )
        primary_actual = (
            primary.path,
            primary.device,
            primary.inode,
            primary.owner_uid,
            primary.group_gid,
            primary.mode,
        )
        if primary_actual != expected:
            raise RuntimeError("artifact primary provenance does not match filesystem identity")
        backup_expected = (
            manifest.provenance.backup_device,
            manifest.provenance.backup_inode,
            manifest.provenance.owner_uid,
            manifest.provenance.group_gid,
            manifest.provenance.directory_mode,
        )
        backup_actual = (
            backup.device,
            backup.inode,
            backup.owner_uid,
            backup.group_gid,
            backup.mode,
        )
        if backup_actual != backup_expected:
            raise RuntimeError("artifact backup provenance does not match filesystem identity")

    def _verify_publication_state(
        self,
        manifest: ArtifactManifest,
        manifest_bytes: bytes,
        marker_bytes: bytes,
        *,
        primary_marker_published: bool,
    ) -> None:
        self._verify_provenance_identity(manifest)
        if not hmac.compare_digest(
            self.filesystem.read_backup_marker(manifest.artifact_id), marker_bytes
        ):
            raise RuntimeError("artifact backup marker bytes changed during commit")
        if primary_marker_published and not hmac.compare_digest(
            self.filesystem.read_primary_marker(manifest.artifact_id), marker_bytes
        ):
            raise RuntimeError("artifact primary marker bytes changed during commit")
        for stored_manifest in (
            self.filesystem.read_primary_manifest(manifest.artifact_id),
            self.filesystem.read_backup_manifest(manifest.artifact_id),
        ):
            if not hmac.compare_digest(stored_manifest, manifest_bytes):
                raise RuntimeError("artifact manifest bytes changed during commit")
        self.filesystem.verify_primary_files(manifest.artifact_id, manifest.files)
        self.filesystem.verify_backup_files(manifest.artifact_id, manifest.files)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("artifact store is closed")


def _build_manifest(
    *,
    artifact_id: str,
    artifact_kind: str,
    retention_expires_at: datetime,
    metadata_json: str,
    files: tuple[ArtifactFileDigest, ...],
    provenance: ArtifactProvenance,
    backup_path: Path,
    signing_key: ArtifactSigningKey,
) -> ArtifactManifest:
    unsigned = ArtifactManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        retention_expires_at=retention_expires_at.isoformat().replace("+00:00", "Z"),
        metadata_json=metadata_json,
        files=files,
        provenance=provenance,
        backup_path=str(backup_path),
        backup_verified=True,
        signing_key_id=signing_key.key_id,
        manifest_hmac=_EMPTY_HMAC,
    )
    return replace(unsigned, manifest_hmac=compute_manifest_hmac(unsigned, signing_key.secret))


def _marker_bytes(manifest: ArtifactManifest) -> bytes:
    return canonical_marker_bytes(manifest)


def _verify_marker(content: bytes, manifest: ArtifactManifest) -> None:
    if not hmac.compare_digest(content, _marker_bytes(manifest)):
        raise RuntimeError("sealed artifact marker does not match manifest")


def _validate_artifact_kind(artifact_kind: str) -> None:
    normalized = artifact_kind.replace("-", "").replace("_", "")
    if not artifact_kind or not normalized.isalnum():
        raise ValueError(
            "artifact kind must contain only letters, numbers, hyphens, or underscores"
        )


def _validate_artifact_id(artifact_id: str) -> None:
    if len(artifact_id) != 32 or any(
        character not in "0123456789abcdef" for character in artifact_id
    ):
        raise ValueError("artifact ID must be a lowercase UUID hex value")
