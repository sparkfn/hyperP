"""Manifest authentication tests for restricted Bitrix artifacts."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import src.connectors.bitrix_stage_history.artifact_manifest as manifest_module
from _bitrix_artifact_store_support import KEY_ONE as _KEY_ONE
from _bitrix_artifact_store_support import KEY_TWO as _KEY_TWO
from _bitrix_artifact_store_support import close_all_stores
from _bitrix_artifact_store_support import key_provider as _provider
from _bitrix_artifact_store_support import object_path as _object_path
from _bitrix_artifact_store_support import provenance as _provenance
from _bitrix_artifact_store_support import rewrite_manifest as _rewrite_manifest
from _bitrix_artifact_store_support import seal as _seal
from _bitrix_artifact_store_support import store as _store
from src.connectors.bitrix_stage_history.artifact_manifest import (
    canonical_json_bytes,
    compute_manifest_hmac,
    parse_manifest_bytes,
)
from src.connectors.bitrix_stage_history.artifact_provenance import ArtifactProvenanceInput
from src.connectors.bitrix_stage_history.artifact_store import ArtifactSigningKey


@pytest.fixture(autouse=True)
def _close_open_stores() -> Iterator[None]:
    yield
    close_all_stores()


def test_seal_authenticates_replicates_and_verifies_immutable_artifact(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    manifest = _seal(store)

    primary = _object_path(tmp_path, "primary", manifest.artifact_id)
    backup = _object_path(tmp_path, "backup", manifest.artifact_id)
    assert manifest.backup_verified is True
    assert manifest.backup_path == str(backup)
    assert manifest.signing_key_id == "key-1"
    assert manifest.manifest_hmac == compute_manifest_hmac(manifest, _provider().current().secret)
    assert store.verify(manifest.artifact_id) == manifest
    assert primary.stat().st_mode & 0o222 == 0
    assert backup.stat().st_mode & 0o222 == 0
    assert (primary / "summary.json").stat().st_mode & 0o222 == 0
    assert set(manifest.provenance.to_dict()) == {
        "artifact_path",
        "primary_device",
        "primary_inode",
        "backup_device",
        "backup_inode",
        "owner_uid",
        "group_gid",
        "directory_mode",
        "source_contract_uuid",
        "repository_sha",
        "image_digest",
        "configuration_digest",
        "restricted_boundaries",
        "counts",
        "total_bytes",
    }
    assert manifest.provenance.artifact_path == str(primary)
    assert manifest.provenance.total_bytes == sum(item.byte_count for item in manifest.files)


def _raw_provenance(
    *,
    source_contract_uuid: str = "12345678-1234-5678-9234-567812345678",
    repository_sha: str = "a" * 40,
    image_digest: str = f"sha256:{'b' * 64}",
    configuration_digest: str = f"sha256:{'c' * 64}",
    restricted_boundaries_json: str = '{"upper_id":"100"}',
    counts_json: str = '{"records":2}',
) -> ArtifactProvenanceInput:
    return ArtifactProvenanceInput(
        source_contract_uuid=source_contract_uuid,
        repository_sha=repository_sha,
        image_digest=image_digest,
        configuration_digest=configuration_digest,
        restricted_boundaries_json=restricted_boundaries_json,
        counts_json=counts_json,
    )


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (lambda: _raw_provenance(source_contract_uuid=""), "UUID"),
        (lambda: _raw_provenance(repository_sha="abc"), "repository SHA"),
        (lambda: _raw_provenance(image_digest="sha256:abc"), "image digest"),
        (
            lambda: _raw_provenance(configuration_digest="sha256:abc"),
            "configuration digest",
        ),
        (lambda: _raw_provenance(restricted_boundaries_json="{}"), "boundaries"),
        (lambda: _raw_provenance(counts_json="{}"), "counts"),
        (lambda: _raw_provenance(counts_json='{"records":-1}'), "counts"),
    ],
)
def test_provenance_rejects_missing_or_invalid_required_fields(
    factory: Callable[[], ArtifactProvenanceInput], match: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=match):
        factory()


def test_verify_rejects_filesystem_provenance_mismatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _seal(store)
    primary = _object_path(tmp_path, "primary", manifest.artifact_id)
    os.chmod(primary, 0o700)
    with pytest.raises(RuntimeError, match="primary provenance"):
        store.verify(manifest.artifact_id)


@pytest.mark.parametrize("copy_name", ["primary", "backup"])
def test_verify_fails_closed_when_artifact_data_is_tampered(tmp_path: Path, copy_name: str) -> None:
    store = _store(tmp_path)
    manifest = _seal(store)
    evidence = _object_path(tmp_path, copy_name, manifest.artifact_id) / "summary.json"
    os.chmod(evidence, 0o600)
    evidence.write_text('{"rows":3}\n', encoding="utf-8")
    os.chmod(evidence, 0o400)

    with pytest.raises(RuntimeError, match="digest verification failed"):
        store.verify(manifest.artifact_id)


def test_simultaneous_copy_and_manifest_tampering_cannot_bypass_hmac(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _seal(store)
    changed_metadata = {**manifest.metadata, "generation_id": "attacker"}
    forged = replace(
        manifest,
        metadata_json=json.dumps(changed_metadata, sort_keys=True, separators=(",", ":")) + "\n",
    )
    for copy_name in ("primary", "backup"):
        path = _object_path(tmp_path, copy_name, manifest.artifact_id) / "artifact-manifest.json"
        _rewrite_manifest(path, forged)

    with pytest.raises(RuntimeError, match="HMAC verification failed"):
        store.verify(manifest.artifact_id)


def test_noncanonical_manifest_bytes_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _seal(store)
    noncanonical = json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")
    for copy_name in ("primary", "backup"):
        path = _object_path(tmp_path, copy_name, manifest.artifact_id) / "artifact-manifest.json"
        os.chmod(path, 0o600)
        path.write_bytes(noncanonical)
        os.chmod(path, 0o400)

    with pytest.raises(RuntimeError, match="manifest is not canonical"):
        store.verify(manifest.artifact_id)


def test_noncanonical_marker_bytes_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _seal(store)
    marker = {
        "artifact_id": manifest.artifact_id,
        "manifest_hmac": manifest.manifest_hmac,
    }
    noncanonical = json.dumps(marker, indent=2).encode("utf-8")
    for copy_name in ("primary", "backup"):
        path = tmp_path / copy_name / "sealed" / f"{manifest.artifact_id}.json"
        os.chmod(path, 0o600)
        path.write_bytes(noncanonical)
        os.chmod(path, 0o400)

    with pytest.raises(RuntimeError, match="marker does not match manifest"):
        store.verify(manifest.artifact_id)


def test_wrong_key_fails_but_retained_rotation_key_verifies(tmp_path: Path) -> None:
    old_store = _store(tmp_path)
    manifest = _seal(old_store)
    old_store.close()

    wrong = _store(tmp_path, provider=_provider(current="key-2", keys={"key-2": _KEY_TWO}))
    with pytest.raises(RuntimeError, match="signing key is unavailable"):
        wrong.verify(manifest.artifact_id)
    wrong.close()

    rotated = _store(
        tmp_path,
        provider=_provider(current="key-2", keys={"key-1": _KEY_ONE, "key-2": _KEY_TWO}),
    )
    assert rotated.verify(manifest.artifact_id) == manifest


def test_changed_hmac_domain_fails_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    manifest = _seal(store)
    monkeypatch.setattr(
        manifest_module,
        "MANIFEST_HMAC_DOMAIN",
        b"different-artifact-domain\x00",
    )

    with pytest.raises(RuntimeError, match="HMAC verification failed"):
        store.verify(manifest.artifact_id)


def test_metadata_is_canonical_finite_and_not_shared_with_caller(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tags = ["first"]
    metadata = {"nested": {"tags": tags}}
    with store.begin(artifact_kind="owner-manifest") as artifact:
        artifact.write_json("summary.json", {"rows": 1})
        manifest = artifact.seal(
            metadata=metadata,
            provenance=_provenance(),
            retention_expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    tags.append("mutated")
    returned = manifest.metadata
    assert returned == {"nested": {"tags": ["first"]}}
    nested = returned["nested"]
    assert isinstance(nested, dict)
    nested["tags"] = []
    assert manifest.metadata == {"nested": {"tags": ["first"]}}

    for value in (math.nan, math.inf, -math.inf):
        with store.begin(artifact_kind="owner-manifest") as artifact:
            with pytest.raises(ValueError, match="finite JSON"):
                artifact.write_json("invalid.json", {"nested": {"value": value}})


def test_seal_rejects_non_finite_metadata_and_non_future_retention(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.begin(artifact_kind="owner-manifest") as artifact:
        artifact.write_json("summary.json", {"rows": 1})
        with pytest.raises(ValueError, match="finite JSON"):
            artifact.seal(
                metadata={"value": math.nan},
                provenance=_provenance(),
                retention_expires_at=datetime.now(UTC) + timedelta(days=1),
            )

    with store.begin(artifact_kind="owner-manifest") as artifact:
        artifact.write_json("summary.json", {"rows": 1})
        with pytest.raises(ValueError, match="future"):
            artifact.seal(
                metadata={},
                provenance=_provenance(),
                retention_expires_at=datetime.now(UTC),
            )


def test_manifest_parser_rejects_unknown_fields_and_expiry_is_checkable(tmp_path: Path) -> None:
    manifest = _seal(_store(tmp_path))
    raw = manifest.to_dict()
    raw["unexpected"] = True
    with pytest.raises(RuntimeError, match="fields are invalid"):
        parse_manifest_bytes(canonical_json_bytes(raw))
    assert manifest.is_expired(now=datetime.now(UTC) + timedelta(days=31))


def test_artifact_signing_key_validation() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        ArtifactSigningKey("", _KEY_ONE)
    with pytest.raises(ValueError, match="at least 32"):
        ArtifactSigningKey("short", b"too-short")

    signing_key = ArtifactSigningKey("redacted", _KEY_ONE)
    assert "secret" not in repr(signing_key)
    assert repr(_KEY_ONE) not in repr(signing_key)
