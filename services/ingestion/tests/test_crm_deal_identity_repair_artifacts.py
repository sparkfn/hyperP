"""Tests for deterministic CRM-deal repair inventory artifacts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from src.connectors.bitrix_stage_history.artifact_runtime import ArtifactStoreConfiguration
from src.crm_deal_identity_repair.artifacts import (
    CRM_DEAL_IDENTITY_REPAIR_MANIFEST_HMAC_DOMAIN,
    RepairArtifactContext,
    seal_inventory_artifact,
)
from src.crm_deal_identity_repair.digests import inventory_digest
from src.crm_deal_identity_repair.models import RepairInventoryItem, RepairPartition
from src.models import JsonValue


def _item(source_record_id: str, partition: RepairPartition) -> RepairInventoryItem:
    return RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id=source_record_id,
        source_record_pk=f"pk-{source_record_id}",
        deal_id=source_record_id.removeprefix("bitrix-crm-deal-"),
        partition=partition,
        graph_fingerprint="sha256:" + "a" * 64,
        stored_payload_fingerprint="sha256:" + "b" * 64,
        payload={"crm_deal_id": source_record_id.removeprefix("bitrix-crm-deal-")},
    )


def _context() -> RepairArtifactContext:
    return RepairArtifactContext(
        repair_id="issue251-staging-20260825-v1",
        environment="staging",
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        repository_sha="c" * 40,
        image_digest="sha256:" + "d" * 64,
        configuration_digest="sha256:" + "e" * 64,
        boundary={"bitrix_dispatch_blocked": False, "source_system": "bitrix_chat"},
        retention_expires_at=datetime.now(UTC) + timedelta(days=1),
    )


def _auxiliary_row(item: RepairInventoryItem, *, kind: str) -> dict[str, JsonValue]:
    identity: dict[str, JsonValue] = {
        "source_system": item.source_system,
        "source_record_id": item.source_record_id,
        "source_record_pk": item.source_record_pk,
    }
    if kind == "graph_snapshot":
        return {
            **identity,
            "status": "requires_live_bitrix_hydration",
            "live_source_fingerprint": None,
            "stored_payload_fingerprint": item.stored_payload_fingerprint,
            "execution_allowed": False,
        }
    if kind == "unhydrated_placeholder":
        return {
            **identity,
            "status": "requires_bitrix_source_hydration",
            "execution_allowed": False,
        }
    if kind == "graph_rollback_discovery":
        return {
            **identity,
            "graph_fingerprint": item.graph_fingerprint,
            "captured_relationships": item.payload,
        }
    raise AssertionError(f"unsupported test auxiliary kind: {kind}")


def _population_counts(items: tuple[RepairInventoryItem, ...]) -> dict[str, int]:
    active_deal_count = len(items)
    ownership_count = sum(
        "ownership_repair" in item.repair_conditions for item in items
    )
    cleanup_count = sum(
        "projection_cleanup" in item.repair_conditions for item in items
    )
    clean_count = sum("negative_control" in item.repair_conditions for item in items)
    return {
        "active_deal_count": active_deal_count,
        "authoritative_version_count": active_deal_count,
        "active_link_count": active_deal_count,
        "active_distinct_owner_count": active_deal_count,
        "multi_linked_deal_count": ownership_count,
        "maximum_links_per_deal": 1 if active_deal_count else 0,
        "maximum_distinct_owners_per_deal": 1 if active_deal_count else 0,
        "projection_cleanup_deal_count": cleanup_count,
        "clean_deal_count": clean_count,
    }


def test_inventory_digest_is_order_independent_and_partition_sensitive() -> None:
    ownership = _item("bitrix-crm-deal-10", "ownership_repair")
    cleanup = _item("bitrix-crm-deal-11", "projection_cleanup")

    assert inventory_digest((ownership, cleanup)) == inventory_digest((cleanup, ownership))
    assert inventory_digest((ownership,)) != inventory_digest((cleanup,))


def test_sealed_inventory_uses_repair_specific_hmac_domain(tmp_path: Path) -> None:
    configuration = ArtifactStoreConfiguration(
        primary_root=tmp_path / "primary",
        backup_root=tmp_path / "backup",
        signing_key_id="repair-key",
        signing_key_secret=b"r" * 32,
        hmac_domain=CRM_DEAL_IDENTITY_REPAIR_MANIFEST_HMAC_DOMAIN,
    )
    context = _context()
    items = (
        _item("bitrix-crm-deal-10", "ownership_repair"),
        _item("bitrix-crm-deal-11", "negative_control"),
    )

    with configuration.open() as store:
        manifest = seal_inventory_artifact(
            store,
            context=context,
            items=items,
            source_snapshots=tuple(
                _auxiliary_row(item, kind="graph_snapshot") for item in reversed(items)
            ),
            proposed_versions=tuple(
                _auxiliary_row(item, kind="unhydrated_placeholder") for item in reversed(items)
            ),
            rollback_template=tuple(
                _auxiliary_row(item, kind="graph_rollback_discovery")
                for item in reversed(items)
            ),
            population_counts=_population_counts(items),
        )
        verified = store.verify(manifest.artifact_id)

    assert verified.artifact_kind == "crm-deal-identity-repair-graph-discovery"
    assert verified.metadata["repair_id"] == context.repair_id
    assert verified.metadata["inventory_digest"] == inventory_digest(items)
    assert verified.metadata["execution_ready"] is False
    assert verified.metadata["artifact_scope"] == "graph_discovery_only"
    assert {item.relative_path for item in verified.files} == {
        "inventory.jsonl",
        "graph-source-snapshots.jsonl",
        "unhydrated-v2-placeholders.jsonl",
        "graph-rollback-discovery.jsonl",
        "verification-plan.json",
    }
    artifact_path = Path(verified.provenance.artifact_path)
    for file_name in (
        "graph-source-snapshots.jsonl",
        "unhydrated-v2-placeholders.jsonl",
        "graph-rollback-discovery.jsonl",
    ):
        rows = (
            json.loads(line)
            for line in (artifact_path / file_name).read_text(encoding="utf-8").splitlines()
        )
        assert [row["source_record_id"] for row in rows] == [
            "bitrix-crm-deal-10",
            "bitrix-crm-deal-11",
        ]


def test_seal_rejects_missing_auxiliary_identity(tmp_path: Path) -> None:
    configuration = ArtifactStoreConfiguration(
        primary_root=tmp_path / "primary",
        backup_root=tmp_path / "backup",
        signing_key_id="repair-key",
        signing_key_secret=b"r" * 32,
        hmac_domain=CRM_DEAL_IDENTITY_REPAIR_MANIFEST_HMAC_DOMAIN,
    )
    items = (
        _item("bitrix-crm-deal-10", "ownership_repair"),
        _item("bitrix-crm-deal-11", "negative_control"),
    )

    with configuration.open() as store, pytest.raises(
        ValueError, match="source snapshots identities must exactly match repair inventory"
    ):
        seal_inventory_artifact(
            store,
            context=_context(),
            items=items,
            source_snapshots=(_auxiliary_row(items[0], kind="graph_snapshot"),),
            proposed_versions=tuple(
                _auxiliary_row(item, kind="unhydrated_placeholder") for item in items
            ),
            rollback_template=tuple(
                _auxiliary_row(item, kind="graph_rollback_discovery") for item in items
            ),
            population_counts=_population_counts(items),
        )


def test_seal_rejects_duplicate_auxiliary_identity(tmp_path: Path) -> None:
    configuration = ArtifactStoreConfiguration(
        primary_root=tmp_path / "primary",
        backup_root=tmp_path / "backup",
        signing_key_id="repair-key",
        signing_key_secret=b"r" * 32,
        hmac_domain=CRM_DEAL_IDENTITY_REPAIR_MANIFEST_HMAC_DOMAIN,
    )
    item = _item("bitrix-crm-deal-10", "ownership_repair")
    duplicate = _auxiliary_row(item, kind="unhydrated_placeholder")

    with configuration.open() as store, pytest.raises(
        ValueError, match="proposed version placeholders contain duplicate identity"
    ):
        seal_inventory_artifact(
            store,
            context=_context(),
            items=(item,),
            source_snapshots=(_auxiliary_row(item, kind="graph_snapshot"),),
            proposed_versions=(duplicate, dict(duplicate)),
            rollback_template=(_auxiliary_row(item, kind="graph_rollback_discovery"),),
            population_counts=_population_counts((item,)),
        )


def test_seal_rejects_extra_auxiliary_identity(tmp_path: Path) -> None:
    configuration = ArtifactStoreConfiguration(
        primary_root=tmp_path / "primary",
        backup_root=tmp_path / "backup",
        signing_key_id="repair-key",
        signing_key_secret=b"r" * 32,
        hmac_domain=CRM_DEAL_IDENTITY_REPAIR_MANIFEST_HMAC_DOMAIN,
    )
    item = _item("bitrix-crm-deal-10", "ownership_repair")
    extra = _item("bitrix-crm-deal-99", "negative_control")

    with configuration.open() as store, pytest.raises(
        ValueError, match="rollback discovery rows contain an extra identity"
    ):
        seal_inventory_artifact(
            store,
            context=_context(),
            items=(item,),
            source_snapshots=(_auxiliary_row(item, kind="graph_snapshot"),),
            proposed_versions=(_auxiliary_row(item, kind="unhydrated_placeholder"),),
            rollback_template=(
                _auxiliary_row(item, kind="graph_rollback_discovery"),
                _auxiliary_row(extra, kind="graph_rollback_discovery"),
            ),
            population_counts=_population_counts((item,)),
        )


def test_seal_rejects_incomplete_population_counts(tmp_path: Path) -> None:
    configuration = ArtifactStoreConfiguration(
        primary_root=tmp_path / "primary",
        backup_root=tmp_path / "backup",
        signing_key_id="repair-key",
        signing_key_secret=b"r" * 32,
        hmac_domain=CRM_DEAL_IDENTITY_REPAIR_MANIFEST_HMAC_DOMAIN,
    )
    item = _item("bitrix-crm-deal-10", "negative_control")

    with configuration.open() as store, pytest.raises(
        ValueError, match="population count fields are invalid"
    ):
        seal_inventory_artifact(
            store,
            context=_context(),
            items=(item,),
            source_snapshots=(_auxiliary_row(item, kind="graph_snapshot"),),
            proposed_versions=(_auxiliary_row(item, kind="unhydrated_placeholder"),),
            rollback_template=(_auxiliary_row(item, kind="graph_rollback_discovery"),),
            population_counts={"active_deal_count": 1},
        )


def test_seal_rejects_unexpected_auxiliary_fields(tmp_path: Path) -> None:
    configuration = ArtifactStoreConfiguration(
        primary_root=tmp_path / "primary",
        backup_root=tmp_path / "backup",
        signing_key_id="repair-key",
        signing_key_secret=b"r" * 32,
        hmac_domain=CRM_DEAL_IDENTITY_REPAIR_MANIFEST_HMAC_DOMAIN,
    )
    item = _item("bitrix-crm-deal-10", "negative_control")
    proposal = _auxiliary_row(item, kind="unhydrated_placeholder")
    proposal["mutation_blueprint"] = {"enabled": True}

    with configuration.open() as store, pytest.raises(
        ValueError, match="proposed version placeholder fields are invalid"
    ):
        seal_inventory_artifact(
            store,
            context=_context(),
            items=(item,),
            source_snapshots=(_auxiliary_row(item, kind="graph_snapshot"),),
            proposed_versions=(proposal,),
            rollback_template=(_auxiliary_row(item, kind="graph_rollback_discovery"),),
            population_counts=_population_counts((item,)),
        )


def test_seal_rejects_auxiliary_fingerprint_not_bound_to_inventory(tmp_path: Path) -> None:
    configuration = ArtifactStoreConfiguration(
        primary_root=tmp_path / "primary",
        backup_root=tmp_path / "backup",
        signing_key_id="repair-key",
        signing_key_secret=b"r" * 32,
        hmac_domain=CRM_DEAL_IDENTITY_REPAIR_MANIFEST_HMAC_DOMAIN,
    )
    item = _item("bitrix-crm-deal-10", "negative_control")
    snapshot = _auxiliary_row(item, kind="graph_snapshot")
    snapshot["stored_payload_fingerprint"] = "sha256:" + "f" * 64

    with configuration.open() as store, pytest.raises(
        ValueError, match="source snapshot fingerprint does not match inventory"
    ):
        seal_inventory_artifact(
            store,
            context=_context(),
            items=(item,),
            source_snapshots=(snapshot,),
            proposed_versions=(_auxiliary_row(item, kind="unhydrated_placeholder"),),
            rollback_template=(_auxiliary_row(item, kind="graph_rollback_discovery"),),
            population_counts=_population_counts((item,)),
        )
