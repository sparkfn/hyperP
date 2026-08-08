"""One-pass collection into authenticated restricted capability artifacts."""

from __future__ import annotations

import subprocess
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from src.connectors.bitrix_openlines.models import CrmDealCapabilityItem
from src.connectors.bitrix_stage_history.artifact_manifest import ArtifactManifest
from src.connectors.bitrix_stage_history.artifact_provenance import ArtifactProvenanceInput
from src.connectors.bitrix_stage_history.artifact_store import LocalRestrictedArtifactStore
from src.connectors.bitrix_stage_history.catalog_probe import (
    CatalogManifest,
    DealStageCatalogClient,
    collect_current_stage_catalog,
)
from src.connectors.bitrix_stage_history.deal_probe import (
    DealCapabilityClient,
    DealPassManifest,
    RestrictedOwnerManifest,
    collect_deal_owner_pass,
    freeze_deal_upper_id,
)
from src.connectors.bitrix_stage_history.models import ProbeLimits
from src.connectors.bitrix_stage_history.probe import (
    PassManifest,
    StageHistoryClient,
    collect_stage_history_pass,
    freeze_stage_history_upper_id,
)
from src.connectors.bitrix_stage_history.reconciliation_spool import (
    CapabilityReconciliationSpool,
    new_redaction_key,
)
from src.models import JsonValue

_REDACTION_KEY_FILE = "capability-redaction-key.bin"
_OWNER_DB_FILE = "owner-manifest.sqlite3"
_STAGE_DB_FILE = "stage-reconciliation.sqlite3"


def collect_owner_artifact(
    client: DealCapabilityClient,
    store: LocalRestrictedArtifactStore,
    *,
    source_contract_id: str,
    categories: Collection[str],
    limits: ProbeLimits,
    image_digest: str,
    configuration_digest: str,
    retention_days: int,
) -> tuple[ArtifactManifest, DealPassManifest]:
    """Collect one complete bounded deal keyset pass and seal it."""
    category_ids = tuple(categories)
    if not category_ids:
        raise ValueError("owner collection requires included categories")
    redaction_key = new_redaction_key()
    upper_deal_id = freeze_deal_upper_id(client, category_ids)
    with store.begin(artifact_kind="owner-capability") as artifact:
        artifact.write_bytes(_REDACTION_KEY_FILE, redaction_key)
        collected, owner_db = collect_deal_owner_pass(
            client,
            category_ids=category_ids,
            upper_deal_id=upper_deal_id,
            limits=limits,
            spool_directory=artifact.path,
            pass_number=1,
            redaction_key=redaction_key,
        )
        if not owner_pass_qualified(collected):
            raise RuntimeError("owner capability pass failed machine qualification")
        owner_db.close()
        owner_db.path.rename(artifact.path / _OWNER_DB_FILE)
        artifact.write_json("owner-summary.json", collected.to_dict())
        manifest = artifact.seal(
            metadata={
                "mode": "collect-owner",
                "owner_manifest_file": _OWNER_DB_FILE,
                "redaction_key_file": _REDACTION_KEY_FILE,
                "owner_manifest_digest": collected.owner_manifest_digest,
                "category_inventory_digest": collected.category_inventory_digest,
                "included_category_ids": list(category_ids),
                "recommendation": "verified_keyset",
                "source_calls": collected.calls + 1,
            },
            provenance=ArtifactProvenanceInput.create(
                source_contract_uuid=source_contract_id,
                repository_sha=_repository_sha(),
                image_digest=image_digest,
                configuration_digest=configuration_digest,
                restricted_boundaries={
                    "upper_deal_id": upper_deal_id,
                    "included_category_ids": list(category_ids),
                },
                counts={
                    "raw_rows": collected.raw_rows,
                    "unique_owner_rows": collected.unique_owner_rows,
                    "duplicate_rows": collected.duplicate_rows,
                },
            ),
            retention_expires_at=_retention_expiry(retention_days),
        )
    return manifest, collected


def collect_stage_artifact(
    client: StageHistoryClient,
    store: LocalRestrictedArtifactStore,
    *,
    owner_artifact_id: str,
    source_contract_id: str,
    entity_type_id: int,
    categories: Collection[str],
    limits: ProbeLimits,
    image_digest: str,
    configuration_digest: str,
    retention_days: int,
    catalog_client: DealStageCatalogClient,
) -> tuple[ArtifactManifest, PassManifest, CatalogManifest]:
    """Collect global stage history against exactly one verified owner artifact."""
    owner = store.verify(owner_artifact_id)
    if owner.artifact_kind not in {"owner-capability", "owner-export"}:
        raise ValueError("collect-stage requires a sealed owner artifact")
    if owner.provenance.source_contract_uuid != source_contract_id:
        raise ValueError("owner artifact source contract does not match collection")
    owner_root = Path(owner.provenance.artifact_path)
    owner_db_name = _metadata_string(owner, "owner_manifest_file")
    redaction_key_name = _metadata_string(owner, "redaction_key_file")
    owner_digest = _metadata_string(owner, "owner_manifest_digest")
    redaction_key = (owner_root / redaction_key_name).read_bytes()
    catalog_manifest, catalog_keys = collect_current_stage_catalog(
        catalog_client,
        category_ids=_owner_categories(owner, categories),
        limits=limits,
        redaction_key=redaction_key,
    )
    upper_history_id = freeze_stage_history_upper_id(client, entity_type_id)
    with store.begin(artifact_kind="stage-capability") as artifact:
        collected, stage_db = collect_stage_history_pass(
            client,
            source_contract_id=source_contract_id,
            entity_type_id=entity_type_id,
            filters={},
            limits=limits,
            spool_directory=artifact.path,
            pass_number=1,
            traversal_mode="id_keyset",
            upper_history_id=upper_history_id,
            owner_manifest_path=owner_root / owner_db_name,
            owner_manifest_digest=owner_digest,
            redaction_key=redaction_key,
            current_catalog_stage_keys=catalog_keys,
        )
        if not isinstance(stage_db, CapabilityReconciliationSpool):
            raise RuntimeError("stage collection did not create a reconciliation spool")
        if not stage_pass_qualified(collected, catalog_manifest):
            raise RuntimeError("stage capability pass failed machine qualification")
        stage_db.close()
        stage_db.path.rename(artifact.path / _STAGE_DB_FILE)
        artifact.write_json("stage-summary.json", collected.to_dict())
        artifact.write_json("catalog-summary.json", catalog_manifest.to_dict())
        manifest = artifact.seal(
            metadata={
                "mode": "collect-stage",
                "owner_artifact_id": owner_artifact_id,
                "stage_reconciliation_file": _STAGE_DB_FILE,
                "owner_manifest_digest": owner_digest,
                "global_identity_hash_digest": collected.identity_hash_digest,
                "in_scope_identity_hash_digest": _required_digest(
                    collected.in_scope_identity_hash_digest,
                    "in-scope stage identity",
                ),
                "recommendation": "bounded_spool_reconcile",
                "source_calls": collected.calls + catalog_manifest.calls + 1,
            },
            provenance=ArtifactProvenanceInput.create(
                source_contract_uuid=source_contract_id,
                repository_sha=_repository_sha(),
                image_digest=image_digest,
                configuration_digest=configuration_digest,
                restricted_boundaries={
                    "upper_history_id": upper_history_id,
                    "owner_artifact_id": owner_artifact_id,
                },
                counts={
                    "global_rows": _required_count(collected.global_rows, "global rows"),
                    "in_scope_rows": _required_count(collected.in_scope_rows, "in-scope rows"),
                    "out_of_scope_rows": _required_count(
                        collected.out_of_scope_rows, "out-of-scope rows"
                    ),
                },
            ),
            retention_expires_at=_retention_expiry(retention_days),
        )
    return manifest, collected, catalog_manifest


def export_owner_artifact(
    store: LocalRestrictedArtifactStore,
    *,
    generation_id: str,
    boundary_digest: str,
    owner_set_digest: str,
    rows: Collection[CrmDealCapabilityItem],
    included_category_ids: Collection[str],
    source_contract_id: str,
    image_digest: str,
    configuration_digest: str,
    retention_days: int,
) -> ArtifactManifest:
    """Seal a deterministic, source-free owner export from frozen graph coverage."""
    ordered = tuple(sorted(rows, key=lambda item: (int(item.deal_id), item.deal_id)))
    if not ordered:
        raise ValueError("frozen owner export cannot be empty")
    if len({item.deal_id for item in ordered}) != len(ordered):
        raise ValueError("frozen owner export requires distinct deal IDs")
    category_sequence = tuple(included_category_ids)
    if not category_sequence or len(set(category_sequence)) != len(category_sequence):
        raise ValueError("frozen owner export requires distinct included categories")
    if any(item.category_id not in category_sequence for item in ordered):
        raise ValueError("frozen owner export row is outside the configured categories")
    included_categories: list[JsonValue] = []
    for category_id in category_sequence:
        included_categories.append(category_id)
    redaction_key = new_redaction_key()
    with store.begin(artifact_kind="owner-export") as artifact:
        artifact.write_bytes(_REDACTION_KEY_FILE, redaction_key)
        owner_db = RestrictedOwnerManifest(artifact.path, 1)
        try:
            for item in ordered:
                if owner_db.add(item) != "unique":
                    raise RuntimeError("frozen owner export produced a duplicate row")
            owner_db.flush()
            owner_digest = owner_db.manifest_digest(redaction_key=redaction_key)
            category_digest = owner_db.category_inventory_digest(redaction_key=redaction_key)
        finally:
            owner_db.close()
        owner_db.path.rename(artifact.path / _OWNER_DB_FILE)
        artifact.write_json(
            "owner-summary.json",
            {
                "generation_id": generation_id,
                "owner_rows": len(ordered),
                "raw_rows": len(ordered),
                "unique_owner_rows": len(ordered),
                "duplicate_rows": 0,
                "source_total": len(ordered),
                "source_total_consistent": True,
                "source_total_matches_rows": True,
                "owner_manifest_digest": owner_digest,
                "category_inventory_digest": category_digest,
                "owner_set_digest": owner_set_digest,
                "source_calls": 0,
            },
        )
        return artifact.seal(
            metadata={
                "mode": "export-owner",
                "generation_id": generation_id,
                "owner_manifest_file": _OWNER_DB_FILE,
                "redaction_key_file": _REDACTION_KEY_FILE,
                "owner_manifest_digest": owner_digest,
                "category_inventory_digest": category_digest,
                "included_category_ids": included_categories,
                "recommendation": "verified_keyset",
                "source_calls": 0,
            },
            provenance=ArtifactProvenanceInput.create(
                source_contract_uuid=source_contract_id,
                repository_sha=_repository_sha(),
                image_digest=image_digest,
                configuration_digest=configuration_digest,
                restricted_boundaries={
                    "generation_id": generation_id,
                    "boundary_digest": boundary_digest,
                    "owner_set_digest": owner_set_digest,
                },
                counts={"owner_rows": len(ordered)},
            ),
            retention_expires_at=_retention_expiry(retention_days),
        )


def owner_pass_qualified(manifest: DealPassManifest) -> bool:
    return (
        manifest.duplicate_rows == 0
        and manifest.source_total_consistent
        and manifest.source_total_matches_rows is not False
        and manifest.unique_owner_rows == manifest.raw_rows
        and manifest.owner_manifest_digest.startswith("hmac-sha256:")
        and manifest.category_inventory_digest.startswith("hmac-sha256:")
    )


def stage_pass_qualified(manifest: PassManifest, catalog: CatalogManifest) -> bool:
    complete = all(
        value is not None
        for value in (
            manifest.upper_history_id_digest,
            manifest.owner_manifest_digest,
            manifest.global_rows,
            manifest.in_scope_rows,
            manifest.out_of_scope_rows,
            manifest.owners_without_history,
            manifest.in_scope_identity_hash_digest,
            manifest.current_catalog_stage_count,
            manifest.in_scope_historical_stage_count,
            manifest.in_scope_historical_stage_missing_catalog_count,
            manifest.in_scope_rows_missing_stage_identity,
        )
    )
    accounting = (
        manifest.global_rows == manifest.raw_rows
        and manifest.in_scope_rows is not None
        and manifest.out_of_scope_rows is not None
        and manifest.global_rows == manifest.in_scope_rows + manifest.out_of_scope_rows
    )
    catalog_qualified = (
        catalog.conflict_rows == 0
        and catalog.source_total_consistent
        and catalog.source_total_matches_rows is not False
    )
    return (
        manifest.traversal_mode == "id_keyset"
        and manifest.duplicate_conflict_rows == 0
        and manifest.source_total_consistent
        and manifest.source_total_matches_rows is not False
        and complete
        and accounting
        and catalog_qualified
    )


def owner_summary_qualified(summary: Mapping[str, JsonValue]) -> bool:
    return (
        _summary_int(summary, "duplicate_rows") == 0
        and summary.get("source_total_consistent") is True
        and summary.get("source_total_matches_rows") is not False
        and _summary_int(summary, "unique_owner_rows") == _summary_int(summary, "raw_rows")
        and _summary_digest(summary, "owner_manifest_digest")
        and _summary_digest(summary, "category_inventory_digest")
    )


def stage_summary_qualified(
    summary: Mapping[str, JsonValue], catalog: Mapping[str, JsonValue]
) -> bool:
    required = (
        "upper_history_id_digest",
        "owner_manifest_digest",
        "global_rows",
        "in_scope_rows",
        "out_of_scope_rows",
        "owners_without_history",
        "in_scope_identity_hash_digest",
        "current_catalog_stage_count",
        "in_scope_historical_stage_count",
        "in_scope_historical_stage_missing_catalog_count",
        "in_scope_rows_missing_stage_identity",
    )
    global_rows = _summary_int(summary, "global_rows")
    in_scope = _summary_int(summary, "in_scope_rows")
    out_scope = _summary_int(summary, "out_of_scope_rows")
    return (
        summary.get("traversal_mode") == "id_keyset"
        and _summary_int(summary, "duplicate_conflict_rows") == 0
        and summary.get("source_total_consistent") is True
        and summary.get("source_total_matches_rows") is not False
        and all(summary.get(key) is not None for key in required)
        and global_rows == _summary_int(summary, "raw_rows")
        and global_rows == in_scope + out_scope
        and _summary_int(catalog, "conflict_rows") == 0
        and catalog.get("source_total_consistent") is True
        and catalog.get("source_total_matches_rows") is not False
    )


def _owner_categories(
    owner: ArtifactManifest,
    requested: Collection[str],
) -> tuple[str, ...]:
    value = owner.metadata.get("included_category_ids")
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise RuntimeError("sealed owner artifact omitted its category inventory")
    categories = tuple(cast(str, item) for item in value)
    if tuple(requested) != categories:
        raise ValueError("collect-stage categories do not match the owner artifact")
    return categories


def _summary_int(summary: Mapping[str, JsonValue], key: str) -> int:
    value = summary.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else -1


def _summary_digest(summary: Mapping[str, JsonValue], key: str) -> bool:
    value = summary.get(key)
    return isinstance(value, str) and value.startswith("hmac-sha256:")


def _metadata_string(manifest: ArtifactManifest, key: str) -> str:
    value = manifest.metadata.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"sealed owner artifact metadata omitted {key}")
    return value


def _required_digest(value: str | None, label: str) -> str:
    if value is None:
        raise RuntimeError(f"stage collection omitted {label} digest")
    return value


def _required_count(value: int | None, label: str) -> int:
    if value is None:
        raise RuntimeError(f"stage collection omitted {label}")
    return value


def _repository_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        check=True,
        text=True,
    )
    value = result.stdout.strip()
    if len(value) != 40:
        raise RuntimeError("repository SHA is unavailable for artifact provenance")
    return value


def _retention_expiry(retention_days: int) -> datetime:
    if isinstance(retention_days, bool) or retention_days < 1:
        raise ValueError("artifact retention days must be positive")
    return datetime.now(UTC) + timedelta(days=retention_days)
