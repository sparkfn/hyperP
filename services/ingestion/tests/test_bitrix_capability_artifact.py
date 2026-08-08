"""Integrated frozen-artifact capability and source-free replay tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from _bitrix_artifact_store_support import key_provider, new_store
from src.connectors.bitrix_openlines.models import (
    CrmDealCapabilityItem,
    CrmDealCapabilityPage,
    CrmDealStageCatalogItem,
    CrmDealStageCatalogPage,
)
from src.connectors.bitrix_stage_history.artifact_provenance import ArtifactProvenanceInput
from src.connectors.bitrix_stage_history.artifact_runtime import ArtifactStoreConfiguration
from src.connectors.bitrix_stage_history.capability_artifacts import (
    collect_owner_artifact,
    collect_stage_artifact,
    export_owner_artifact,
    owner_pass_qualified,
    stage_pass_qualified,
)
from src.connectors.bitrix_stage_history.catalog_probe import CatalogManifest
from src.connectors.bitrix_stage_history.deal_probe import DealPassManifest
from src.connectors.bitrix_stage_history.models import (
    ProbeLimits,
    StageHistoryItem,
    StageHistoryPage,
)
from src.connectors.bitrix_stage_history.probe import PassManifest
from src.connectors.bitrix_stage_history.replay import qualify_artifacts

_SOURCE_CONTRACT = "12345678-1234-5678-9234-567812345678"
_IMAGE_DIGEST = f"sha256:{'b' * 64}"
_CONFIG_DIGEST = f"sha256:{'c' * 64}"


class _OwnerClient:
    def __init__(self) -> None:
        self.calls = 0

    def list_crm_deal_capability_page(
        self,
        *,
        category_ids: object,
        greater_than_id: int | None = None,
        less_than_or_equal_to_id: int | None = None,
        order_direction: str = "ASC",
    ) -> CrmDealCapabilityPage:
        _ = category_ids, greater_than_id, less_than_or_equal_to_id
        self.calls += 1
        items = (
            CrmDealCapabilityItem("2", "2", "C2:NEW"),
            CrmDealCapabilityItem("10", "2", "C2:WON"),
        )
        return CrmDealCapabilityPage(
            tuple(reversed(items)) if order_direction == "DESC" else items,
            None,
            2,
            None,
            None,
        )


class _StageClient:
    def __init__(self) -> None:
        self.stage_calls = 0

    def list_crm_deal_stage_catalog_page(
        self, *, category_id: int, start: int = 0
    ) -> CrmDealStageCatalogPage:
        assert category_id == 2
        assert start == 0
        return CrmDealStageCatalogPage(
            (
                CrmDealStageCatalogItem("2", "C2:NEW", "process"),
                CrmDealStageCatalogItem("2", "C2:WON", "success"),
            ),
            None,
            2,
            None,
            None,
        )

    def list_stage_history_page(
        self,
        *,
        entity_type_id: int,
        filters: object = None,
        order_direction: str = "ASC",
        start: int = -1,
    ) -> StageHistoryPage:
        _ = filters, start
        assert entity_type_id == 2
        self.stage_calls += 1
        items = (
            _stage_item("1", "2", "C2:NEW"),
            _stage_item("2", "10", "C2:WON"),
            _stage_item("3", "999", "C2:NEW"),
        )
        return StageHistoryPage(
            tuple(reversed(items)) if order_direction == "DESC" else items,
            None,
            3,
            None,
            None,
        )


def test_collect_and_source_free_replay_sealed_artifacts(tmp_path: Path) -> None:
    store = new_store(tmp_path / "primary", tmp_path / "backup", key_provider())
    limits = ProbeLimits(10, 100, 10_000_000, 10, 2, 2)
    owner, owner_pass = collect_owner_artifact(
        _OwnerClient(),
        store,
        source_contract_id=_SOURCE_CONTRACT,
        categories=("2",),
        limits=limits,
        image_digest=_IMAGE_DIGEST,
        configuration_digest=_CONFIG_DIGEST,
        retention_days=30,
    )
    stage_client = _StageClient()
    stage, stage_pass, _ = collect_stage_artifact(
        stage_client,
        store,
        owner_artifact_id=owner.artifact_id,
        source_contract_id=_SOURCE_CONTRACT,
        entity_type_id=2,
        categories=("2",),
        limits=limits,
        image_digest=_IMAGE_DIGEST,
        configuration_digest=_CONFIG_DIGEST,
        retention_days=30,
        catalog_client=stage_client,
    )

    result = qualify_artifacts(
        store,
        owner_artifact_id=owner.artifact_id,
        stage_artifact_id=stage.artifact_id,
    )

    assert owner_pass.unique_owner_rows == 2
    assert stage_pass.global_rows == 3
    assert stage_pass.in_scope_rows == 2
    assert result["deterministic_replay"] is True
    assert result["source_calls"] == 0
    assert result["graph_writes"] == 0
    assert result["stage_domain_writes"] == 0
    store.close()


def test_one_pass_qualification_rejects_incomplete_source_accounting() -> None:
    owner = DealPassManifest(
        upper_deal_id_digest="hmac-sha256:upper",
        calls=1,
        pages=1,
        raw_rows=1,
        unique_owner_rows=1,
        duplicate_rows=0,
        source_total=1,
        source_total_consistent=True,
        source_total_matches_rows=True,
        owner_manifest_digest="hmac-sha256:owner",
        category_inventory_digest="hmac-sha256:category",
        runtime_seconds=0.1,
        spool_bytes=1,
    )
    assert owner_pass_qualified(owner)
    assert not owner_pass_qualified(replace(owner, duplicate_rows=1))
    assert not owner_pass_qualified(replace(owner, source_total_consistent=False))
    assert not owner_pass_qualified(replace(owner, source_total_matches_rows=False))

    stage = PassManifest(
        traversal_mode="id_keyset",
        calls=1,
        raw_rows=1,
        unique_identity_rows=1,
        duplicate_same_hash_rows=0,
        duplicate_conflict_rows=0,
        pages=1,
        source_total=1,
        source_total_consistent=True,
        source_total_matches_rows=True,
        history_id_ordering="numeric",
        minimum_history_id="1",
        maximum_history_id="1",
        identity_hash_digest="hmac-sha256:global",
        runtime_seconds=0.1,
        spool_bytes=1,
        upper_history_id_digest="hmac-sha256:upper",
        owner_manifest_digest="hmac-sha256:owner",
        global_rows=1,
        in_scope_rows=1,
        out_of_scope_rows=0,
        owners_without_history=0,
        in_scope_identity_hash_digest="hmac-sha256:scope",
        current_catalog_stage_count=1,
        in_scope_historical_stage_count=1,
        in_scope_historical_stage_missing_catalog_count=0,
        in_scope_rows_missing_stage_identity=0,
    )
    catalog = CatalogManifest(1, 1, 1, 1, 0, 0, True, True, "hmac-sha256:cat", 0.1, 0, 0, None)
    assert stage_pass_qualified(stage, catalog)
    assert not stage_pass_qualified(replace(stage, duplicate_conflict_rows=1), catalog)
    assert not stage_pass_qualified(stage, replace(catalog, conflict_rows=1))
    assert not stage_pass_qualified(replace(stage, in_scope_rows=None), catalog)


def test_runtime_configuration_redacts_secret_and_retains_old_key(tmp_path: Path) -> None:
    old = ArtifactStoreConfiguration(
        tmp_path / "primary",
        tmp_path / "backup",
        "old",
        b"o" * 32,
    )
    with old.open() as store:
        with store.begin(artifact_kind="owner-capability") as artifact:
            artifact.write_json("summary.json", {"rows": 1})
            sealed = artifact.seal(
                metadata={"mode": "test"},
                provenance=_artifact_provenance(),
                retention_expires_at=datetime(2026, 9, 8, tzinfo=UTC),
            )
    rotated = ArtifactStoreConfiguration(
        tmp_path / "primary",
        tmp_path / "backup",
        "new",
        b"n" * 32,
        {"old": b"o" * 32},
    )
    assert (b"n" * 32).hex() not in repr(rotated)
    assert "b'nnn" not in repr(rotated)
    with rotated.open() as store:
        assert store.verify(sealed.artifact_id) == sealed


def test_export_owner_collect_stage_and_qualify(tmp_path: Path) -> None:
    store = new_store(tmp_path / "primary", tmp_path / "backup", key_provider())
    exported = export_owner_artifact(
        store,
        generation_id="generation-1",
        boundary_digest=_CONFIG_DIGEST,
        owner_set_digest=_CONFIG_DIGEST,
        rows=(
            CrmDealCapabilityItem("2", "2", "C2:NEW"),
            CrmDealCapabilityItem("10", "2", "C2:WON"),
        ),
        included_category_ids=("2",),
        source_contract_id=_SOURCE_CONTRACT,
        image_digest=_IMAGE_DIGEST,
        configuration_digest=_CONFIG_DIGEST,
        retention_days=30,
    )
    stage_client = _StageClient()
    stage, _, _ = collect_stage_artifact(
        stage_client,
        store,
        owner_artifact_id=exported.artifact_id,
        source_contract_id=_SOURCE_CONTRACT,
        entity_type_id=2,
        categories=("2",),
        limits=ProbeLimits(10, 100, 10_000_000, 10, 2, 2),
        image_digest=_IMAGE_DIGEST,
        configuration_digest=_CONFIG_DIGEST,
        retention_days=30,
        catalog_client=stage_client,
    )

    qualified = qualify_artifacts(
        store,
        owner_artifact_id=exported.artifact_id,
        stage_artifact_id=stage.artifact_id,
    )

    assert qualified["deterministic_replay"] is True
    store.close()


def test_export_preserves_frozen_category_order_and_empty_categories(tmp_path: Path) -> None:
    store = new_store(tmp_path / "primary", tmp_path / "backup", key_provider())
    exported = export_owner_artifact(
        store,
        generation_id="generation-1",
        boundary_digest=_CONFIG_DIGEST,
        owner_set_digest=_CONFIG_DIGEST,
        rows=(CrmDealCapabilityItem("2", "10", "C10:NEW"),),
        included_category_ids=("10", "2"),
        source_contract_id=_SOURCE_CONTRACT,
        image_digest=_IMAGE_DIGEST,
        configuration_digest=_CONFIG_DIGEST,
        retention_days=30,
    )

    assert exported.metadata["included_category_ids"] == ["10", "2"]
    store.close()


def _stage_item(history_id: str, owner_id: str, stage_id: str) -> StageHistoryItem:
    return StageHistoryItem(
        history_id=history_id,
        entity_type_id="2",
        owner_id=owner_id,
        type_id="1",
        created_time=datetime(2026, 8, 8, tzinfo=UTC),
        created_time_source="2026-08-08T00:00:00+00:00",
        category_id="2",
        stage_semantic_id="P",
        stage_id=stage_id,
        raw_payload={"ID": history_id},
    )


def _artifact_provenance() -> ArtifactProvenanceInput:
    return ArtifactProvenanceInput.create(
        source_contract_uuid=_SOURCE_CONTRACT,
        repository_sha="a" * 40,
        image_digest=_IMAGE_DIGEST,
        configuration_digest=_CONFIG_DIGEST,
        restricted_boundaries={"upper_id": 1},
        counts={"rows": 1},
    )
