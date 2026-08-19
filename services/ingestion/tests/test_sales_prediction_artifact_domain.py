"""HMAC-domain separation tests for sales-prediction restricted artifacts.

Sales artifacts are sealed under their own manifest HMAC domain while reusing
the stage-history restricted artifact primitives unchanged: a manifest sealed
by one domain must fail authentication under the other.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from src.connectors.bitrix_stage_history.artifact_manifest import MANIFEST_HMAC_DOMAIN
from src.connectors.bitrix_stage_history.artifact_provenance import ArtifactProvenanceInput
from src.connectors.bitrix_stage_history.artifact_signing import (
    StaticArtifactSigningKeyProvider,
)
from src.connectors.bitrix_stage_history.artifact_store import LocalRestrictedArtifactStore
from src.sales_prediction.contracts import SALES_ARTIFACT_HMAC_DOMAIN

_KEY_PROVIDER = StaticArtifactSigningKeyProvider("sales-key-1", {"sales-key-1": b"s" * 32})


def _provenance() -> ArtifactProvenanceInput:
    return ArtifactProvenanceInput.create(
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        repository_sha="a" * 40,
        image_digest=f"sha256:{'b' * 64}",
        configuration_digest=f"sha256:{'c' * 64}",
        restricted_boundaries={"evidence_cutoff": "bound"},
        counts={"rows": 1},
    )


def _seal_sales_artifact(tmp_path: Path) -> str:
    store = LocalRestrictedArtifactStore(
        tmp_path / "primary",
        tmp_path / "backup",
        _KEY_PROVIDER,
        hmac_domain=SALES_ARTIFACT_HMAC_DOMAIN,
    )
    try:
        with store.begin(artifact_kind="sales-dataset") as session:
            session.write_json("summary.json", {"rows": 1})
            manifest = session.seal(
                metadata={"dataset_schema_version": "issue-125-crm-dataset-v1"},
                provenance=_provenance(),
                retention_expires_at=datetime.now(UTC) + timedelta(days=365),
            )
        verified = store.verify(manifest.artifact_id)
        assert verified.artifact_id == manifest.artifact_id
        return manifest.artifact_id
    finally:
        store.close()


def test_sales_artifact_verifies_under_sales_domain(tmp_path: Path) -> None:
    artifact_id = _seal_sales_artifact(tmp_path)
    store = LocalRestrictedArtifactStore(
        tmp_path / "primary",
        tmp_path / "backup",
        _KEY_PROVIDER,
        hmac_domain=SALES_ARTIFACT_HMAC_DOMAIN,
    )
    try:
        manifest = store.verify(artifact_id)
        assert manifest.artifact_kind == "sales-dataset"
    finally:
        store.close()


def test_sales_artifact_fails_authentication_under_bitrix_domain(tmp_path: Path) -> None:
    artifact_id = _seal_sales_artifact(tmp_path)
    store = LocalRestrictedArtifactStore(
        tmp_path / "primary",
        tmp_path / "backup",
        StaticArtifactSigningKeyProvider("sales-key-1", {"sales-key-1": b"s" * 32}),
        hmac_domain=MANIFEST_HMAC_DOMAIN,
    )
    try:
        with pytest.raises(RuntimeError, match="HMAC verification failed"):
            store.verify(artifact_id)
    finally:
        store.close()


def test_default_domain_still_seals_bitrix_artifacts_unchanged(tmp_path: Path) -> None:
    store = LocalRestrictedArtifactStore(
        tmp_path / "primary",
        tmp_path / "backup",
        _KEY_PROVIDER,
        hmac_domain=MANIFEST_HMAC_DOMAIN,
    )
    try:
        with store.begin(artifact_kind="owner-capability") as session:
            session.write_json("summary.json", {"rows": 2})
            manifest = session.seal(
                metadata={"generation_id": "generation-1"},
                provenance=_provenance(),
                retention_expires_at=datetime.now(UTC) + timedelta(days=30),
            )
        assert store.verify(manifest.artifact_id).artifact_id == manifest.artifact_id
    finally:
        store.close()
