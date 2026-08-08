"""Corrective inventory is complete, bounded, and Bitrix-only."""

from __future__ import annotations

from typing import cast

import pytest
from src.bitrix_backfill_models import (
    BackfillInventoryEntry,
    BackfillInventoryManifest,
)
from src.bitrix_ingestion_models import BitrixStreamKey
from src.models import JsonValue


def _entry(stream_key: str) -> BackfillInventoryEntry:
    windows: dict[str, dict[str, JsonValue]] = {
        "crm_deals": {
            "upper_deal_id": 900,
            "included_category_digest": "sha256:categories",
            "owner_artifact_id": None,
        },
        "crm_activities": {
            "upper_activity_id": 1200,
            "owner_artifact_id": None,
        },
    }
    return BackfillInventoryEntry(
        gap_id=f"gap-{stream_key}",
        stream_key=cast(BitrixStreamKey, stream_key),
        bounded_population=100,
        current_count=20,
        source_basis="frozen strict keyset upper bound",
        expected_repair="ingest every terminal bounded source unit",
        replay_mode="strict_keyset",
        source_window=windows[stream_key],
        completion_equation="coverage == checkpoint == bounded population",
        max_calls=1000,
        max_rows=10000,
        max_runtime_seconds=3600,
        max_storage_bytes=1000000,
        max_lock_seconds=30,
        max_lag_seconds=300,
        rollback_path="tested backup restore or reconciled compensation",
    )


def _manifest(entries: tuple[BackfillInventoryEntry, ...]) -> BackfillInventoryManifest:
    return BackfillInventoryManifest(
        source_key="bitrix_chat",
        reviewed_by="operator@example.test",
        backup_id="neo4j-backup-20260808",
        backup_restore_evidence_digest="sha256:restore",
        minimum_fence_image_digest="sha256:image",
        legacy_dispatch_paused=True,
        predecessor_quiescent=True,
        entries=entries,
    )


def test_manifest_requires_deals_before_activities_and_has_stable_digest() -> None:
    manifest = _manifest((_entry("crm_deals"), _entry("crm_activities")))

    assert manifest.digest == manifest.digest
    assert manifest.canonical_json.count("stage_domain") == 0
    assert [entry.stream_key for entry in manifest.executable_entries] == [
        "crm_deals",
        "crm_activities",
    ]


def test_manifest_rejects_missing_prerequisites_or_reversed_stream_order() -> None:
    with pytest.raises(ValueError, match="quiescent"):
        BackfillInventoryManifest(
            source_key="bitrix_chat",
            reviewed_by="operator@example.test",
            backup_id="backup",
            backup_restore_evidence_digest="sha256:restore",
            minimum_fence_image_digest="sha256:image",
            legacy_dispatch_paused=False,
            predecessor_quiescent=True,
            entries=(_entry("crm_deals"), _entry("crm_activities")),
        )
    with pytest.raises(ValueError, match="precede"):
        _manifest((_entry("crm_activities"), _entry("crm_deals")))
