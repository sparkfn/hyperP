"""Focused graph-only CRM-deal repair artifact tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.connectors.bitrix_stage_history.artifact_runtime import ArtifactStoreConfiguration
from src.crm_deal_identity_repair.artifacts import (
    CRM_DEAL_IDENTITY_REPAIR_MANIFEST_HMAC_DOMAIN,
    RepairArtifactContext,
    seal_inventory_artifact,
)
from src.crm_deal_identity_repair.models import RepairInventoryItem, RepairPartition


def _item(
    record_id: str,
    *,
    policy: str,
    partition: RepairPartition = "projection_cleanup",
) -> RepairInventoryItem:
    return RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id=record_id,
        source_record_pk="pk-" + record_id,
        deal_id=record_id.removeprefix("bitrix-crm-deal-"),
        partition=partition,
        repair_conditions=(partition,),
        graph_fingerprint="sha256:" + "a" * 64,
        stored_payload_fingerprint="sha256:" + "b" * 64,
        payload={
            "linked_people": [],
            "lifecycle_policy_evidence": {"classification": policy},
            "descendants": [],
            "decisions_and_reviews": [],
            "owner_impacts": [],
        },
    )


def _population_counts(*, cleanup: int, clean: int) -> dict[str, int]:
    return {
        "active_deal_count": 1,
        "authoritative_version_count": 1,
        "active_link_count": 0,
        "active_distinct_owner_count": 0,
        "multi_linked_deal_count": 0,
        "maximum_links_per_deal": 0,
        "maximum_distinct_owners_per_deal": 0,
        "projection_cleanup_deal_count": cleanup,
        "clean_deal_count": clean,
    }


def test_graph_discovery_artifact_contains_only_non_executable_handoff_documents(
    tmp_path: Path,
) -> None:
    config = ArtifactStoreConfiguration(
        primary_root=tmp_path / "primary",
        backup_root=tmp_path / "backup",
        signing_key_id="repair-key",
        signing_key_secret=b"r" * 32,
        hmac_domain=CRM_DEAL_IDENTITY_REPAIR_MANIFEST_HMAC_DOMAIN,
    )
    context = RepairArtifactContext(
        repair_id="issue-254-graph-inventory",
        environment="staging",
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        repository_sha="a" * 40,
        image_digest="sha256:" + "b" * 64,
        configuration_digest="sha256:" + "c" * 64,
        boundary={"source_system": "bitrix_chat", "execution_allowed": False},
        retention_expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    counts = _population_counts(cleanup=1, clean=0)
    stale = {
        "stale_run_id": "e5deb1d6-7333-4660-be4f-c44fcf5af686",
        "state": "unknown",
        "disposition": "investigate",
        "execution_allowed": False,
    }

    with config.open() as store:
        manifest = seal_inventory_artifact(
            store,
            context=context,
            items=(_item("bitrix-crm-deal-10", policy="pre_policy"),),
            population_counts=counts,
            stale_run_evidence=stale,
        )
        verified = store.verify(manifest.artifact_id)

    assert verified.metadata["execution_allowed"] is False
    assert {item.relative_path for item in verified.files} == {
        "inventory.jsonl",
        "impact-summary.json",
        "representative-replay-plan.json",
        "compensation-guidance.json",
        "stale-run-evidence.json",
        "clean-boundary-plan.json",
    }
    artifact_path = Path(verified.provenance.artifact_path)
    inventory_row = json.loads((artifact_path / "inventory.jsonl").read_text(encoding="utf-8"))
    impact = json.loads((artifact_path / "impact-summary.json").read_text(encoding="utf-8"))
    assert inventory_row["execution_allowed"] is False
    assert impact["prior_246_evidence"] == {
        "source": "issue_246_prior_evidence",
        "is_current_truth": False,
        "counts": {
            "active_deal_count": 133146,
            "active_link_count": 134975,
            "multi_linked_deal_count": 1255,
            "maximum_distinct_owners_per_deal": 5,
        },
        "fresh_authoritative_counts": {
            "active_deal_count": 1,
            "active_link_count": 0,
            "multi_linked_deal_count": 0,
            "maximum_distinct_owners_per_deal": 0,
        },
        "deltas": {
            "active_deal_count": -133145,
            "active_link_count": -134975,
            "multi_linked_deal_count": -1255,
            "maximum_distinct_owners_per_deal": -5,
        },
    }


def test_impact_equations_include_negative_control_partition(tmp_path: Path) -> None:
    config = ArtifactStoreConfiguration(
        primary_root=tmp_path / "primary",
        backup_root=tmp_path / "backup",
        signing_key_id="repair-key",
        signing_key_secret=b"r" * 32,
        hmac_domain=CRM_DEAL_IDENTITY_REPAIR_MANIFEST_HMAC_DOMAIN,
    )
    context = RepairArtifactContext(
        repair_id="issue-254-negative-control",
        environment="staging",
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        repository_sha="a" * 40,
        image_digest="sha256:" + "b" * 64,
        configuration_digest="sha256:" + "c" * 64,
        boundary={"source_system": "bitrix_chat", "execution_allowed": False},
        retention_expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    stale = {"state": "unknown", "execution_allowed": False}

    with config.open() as store:
        manifest = seal_inventory_artifact(
            store,
            context=context,
            items=(
                _item(
                    "bitrix-crm-deal-11",
                    policy="pre_policy",
                    partition="negative_control",
                ),
            ),
            population_counts=_population_counts(cleanup=0, clean=1),
            stale_run_evidence=stale,
        )
        verified = store.verify(manifest.artifact_id)

    impact = (Path(verified.provenance.artifact_path) / "impact-summary.json").read_text(
        encoding="utf-8"
    )
    assert '"negative_control":1' in impact
