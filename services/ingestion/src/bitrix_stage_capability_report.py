"""Redacted reporting and qualification for the Bitrix capability re-gate."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from src.connectors.bitrix_stage_history.catalog_probe import CatalogManifest
from src.connectors.bitrix_stage_history.deal_probe import DealPassManifest
from src.connectors.bitrix_stage_history.models import ProbeLimits, TraversalOutcome
from src.connectors.bitrix_stage_history.probe import PassManifest
from src.models import JsonValue


def repository_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, check=True, text=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    return value if value else None


def write_json(path: Path, value: dict[str, JsonValue]) -> None:
    encoded = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            fd = -1
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if fd >= 0:
            os.close(fd)
        path.unlink(missing_ok=True)
        raise


def catalog_machine_qualified(manifest: CatalogManifest) -> bool:
    return (
        manifest.conflict_rows == 0
        and manifest.source_total_consistent
        and manifest.source_total_matches_rows is not False
    )


def passes_machine_qualified(
    manifests: Sequence[DealPassManifest | PassManifest], *, stage: bool
) -> bool:
    if not manifests:
        return False
    for manifest in manifests:
        duplicates = (
            manifest.duplicate_conflict_rows
            if isinstance(manifest, PassManifest)
            else manifest.duplicate_rows
        )
        if duplicates or not manifest.source_total_consistent:
            return False
        if manifest.source_total_matches_rows is False:
            return False
        if stage and not _stage_manifest_complete(manifest):
            return False
    return True


def _stage_manifest_complete(manifest: DealPassManifest | PassManifest) -> bool:
    if not isinstance(manifest, PassManifest):
        return False
    return all(
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


def recommendation(
    converged: bool,
    qualified: bool,
    approval_evidence_complete: bool,
    supported_outcome: TraversalOutcome,
) -> TraversalOutcome:
    if converged and qualified and approval_evidence_complete:
        return supported_outcome
    return "unsupported"


def write_failure_manifest(
    output_directory: Path,
    *,
    phase: str,
    reason: str,
    exception_type: str,
    portal_digest: str | None,
    config_digest: str | None,
    image_digest: str | None,
    included_deal_category_count: int,
) -> None:
    """Write one redacted, restricted artifact for a failed capability run."""
    write_json(
        output_directory / "failure-manifest.json",
        {
            "report_schema_version": "bitrix-source-capability-v2",
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "repository_sha": repository_sha(),
            "failure_phase": phase,
            "traversal_outcome": "unsupported",
            "human_approval_required": True,
            "failure_reason": reason,
            "exception_type": exception_type,
            "provenance": {
                "portal_origin_digest": portal_digest,
                "effective_ingestion_config_digest": config_digest,
                "deployment_image_digest": image_digest,
            },
            "included_deal_category_count": included_deal_category_count,
            "writes": {
                "graph": False,
                "celery": False,
                "source_records": False,
                "checkpoints": False,
                "stage_history_writes_enabled": False,
            },
        },
    )


def evidence_summary(
    *,
    source_contract_id: str,
    entity_type_id: int,
    categories: tuple[str, ...],
    limits: ProbeLimits,
    deal_manifests: Sequence[DealPassManifest],
    stage_manifests: Sequence[PassManifest],
    catalog_manifest: CatalogManifest,
    portal_digest: str,
    config_digest: str,
    image_digest: str | None,
    expected_cadence_seconds: float | None,
    retained_verification_material: bool,
) -> dict[str, JsonValue]:
    deal_converged = _deal_converged(deal_manifests, limits)
    stage_converged = _stage_converged(stage_manifests, limits)
    repo_sha = repository_sha()
    resources = _resource_evidence(
        deal_manifests, stage_manifests, catalog_manifest, expected_cadence_seconds
    )
    predicates = {
        "catalog_qualified": catalog_machine_qualified(catalog_manifest),
        "provenance_complete": image_digest is not None and repo_sha is not None,
        "cadence_qualified": resources["cadence_qualified"] is True,
        "operating_metadata_observed": resources["bitrix_operating_samples"] != 0,
        "restricted_verification_material_retained": retained_verification_material,
    }
    complete = all(predicates.values())
    final_deal = deal_manifests[-1]
    final_stage = stage_manifests[-1]
    return cast(
        dict[str, JsonValue],
        {
            "report_schema_version": "bitrix-source-capability-v2",
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "source_contract_id": source_contract_id,
            "entity_type_id": entity_type_id,
            "included_deal_category_count": len(categories),
            "provenance": {
                "portal_origin_digest": portal_digest,
                "effective_ingestion_config_digest": config_digest,
                "repository_sha": repo_sha,
                "deployment_image_digest": image_digest,
                "complete": image_digest is not None,
            },
            "restricted_verification_material_retained": retained_verification_material,
            "source_filters": {
                "deal_category_filter_applied": True,
                "stage_history_owner_filter_applied": False,
                "stage_history_category_filter_applied": False,
            },
            **_limit_evidence(limits),
            "resource_and_cadence": resources,
            "machine_approval_predicates": predicates,
            "machine_approval_evidence_complete": complete,
            "current_stage_catalog": {
                "selected_category_count": len(categories),
                "manifest": catalog_manifest.to_dict(),
                "machine_qualified": catalog_machine_qualified(catalog_manifest),
            },
            "deal_owner_census": {
                "pass_manifests": [item.to_dict() for item in deal_manifests],
                "converged_identical_passes": deal_converged,
                "owner_manifest_digest": final_deal.owner_manifest_digest,
                "category_inventory_digest": final_deal.category_inventory_digest,
                "recommended_traversal_outcome": recommendation(
                    deal_converged,
                    passes_machine_qualified(deal_manifests, stage=False),
                    complete,
                    "verified_keyset",
                ),
            },
            "global_stage_history": {
                "pass_manifests": [item.to_dict() for item in stage_manifests],
                "converged_identical_passes": stage_converged,
                "frozen_owner_manifest_digest": final_stage.owner_manifest_digest,
                "global_rows": final_stage.global_rows,
                "in_scope_rows": final_stage.in_scope_rows,
                "out_of_scope_rows": final_stage.out_of_scope_rows,
                "owners_without_history": final_stage.owners_without_history,
                "current_catalog_stage_count": final_stage.current_catalog_stage_count,
                "in_scope_historical_stage_count": final_stage.in_scope_historical_stage_count,
                "in_scope_historical_stage_missing_catalog_count": (
                    final_stage.in_scope_historical_stage_missing_catalog_count
                ),
                "in_scope_rows_missing_stage_identity": (
                    final_stage.in_scope_rows_missing_stage_identity
                ),
                "recommended_traversal_outcome": recommendation(
                    stage_converged,
                    passes_machine_qualified(stage_manifests, stage=True),
                    complete,
                    "bounded_spool_reconcile",
                ),
            },
            "human_approval_required": True,
            "approved_traversal_outcome": None,
            "decision_boundary": (
                "This command recommends only. A human must approve each stream before #147."
            ),
            "writes": {
                "graph": False,
                "celery": False,
                "source_records": False,
                "checkpoints": False,
                "stage_history_writes_enabled": False,
            },
        },
    )


def _deal_converged(manifests: Sequence[DealPassManifest], limits: ProbeLimits) -> bool:
    if len(manifests) < limits.required_identical_passes:
        return False
    window = manifests[-limits.required_identical_passes :]
    first = window[0]
    from src.connectors.bitrix_stage_history.deal_probe import deal_manifests_are_identical

    return all(deal_manifests_are_identical(first, item) for item in window[1:])


def _stage_converged(manifests: Sequence[PassManifest], limits: ProbeLimits) -> bool:
    if len(manifests) < limits.required_identical_passes:
        return False
    window = manifests[-limits.required_identical_passes :]
    first = window[0]
    from src.connectors.bitrix_stage_history.probe import manifests_are_identical

    return all(manifests_are_identical(first, item) for item in window[1:])


def _limit_evidence(limits: ProbeLimits) -> dict[str, JsonValue]:
    pass_calls = limits.max_calls * limits.max_passes
    pass_rows = limits.max_rows * limits.max_passes
    return {
        "per_pass_limits": {
            "max_calls": limits.max_calls,
            "max_rows": limits.max_rows,
            "max_spool_bytes": limits.max_spool_bytes,
            "max_runtime_seconds": limits.max_runtime_seconds,
        },
        "convergence_policy": {
            "max_passes": limits.max_passes,
            "required_identical_passes": limits.required_identical_passes,
        },
        "http_attempts_per_call": 1,
        "run_upper_bounds": {
            "max_logical_calls": 2 + limits.max_calls + (2 * pass_calls),
            "max_rows": limits.max_rows + (2 * pass_rows),
            "max_spool_bytes": 2 * limits.max_spool_bytes * limits.max_passes,
            "max_collection_runtime_seconds": (
                limits.max_runtime_seconds * (1 + (2 * limits.max_passes))
            ),
        },
    }


def _resource_evidence(
    deals: Sequence[DealPassManifest],
    stages: Sequence[PassManifest],
    catalog: CatalogManifest,
    cadence: float | None,
) -> dict[str, JsonValue]:
    passes: tuple[DealPassManifest | PassManifest, ...] = (*deals, *stages)
    calls = 2 + catalog.calls + sum(item.calls for item in passes)
    runtime = catalog.runtime_seconds + sum(item.runtime_seconds for item in passes)
    resets = [
        item.latest_operating_reset_at
        for item in passes
        if item.latest_operating_reset_at is not None
    ]
    if catalog.latest_operating_reset_at is not None:
        resets.append(catalog.latest_operating_reset_at)
    headroom = cadence - runtime if cadence is not None else None
    return {
        "logical_endpoint_calls": calls,
        "http_request_count": calls,
        "boundary_calls": 2,
        "aggregate_runtime_seconds": runtime,
        "maximum_spool_bytes": max((item.spool_bytes for item in passes), default=0),
        "total_spool_bytes": sum(item.spool_bytes for item in passes),
        "logical_call_rate_per_second": calls / runtime if runtime > 0 else None,
        "bitrix_operating_seconds": catalog.operating_seconds
        + sum(item.operating_seconds for item in passes),
        "bitrix_operating_samples": catalog.operating_samples
        + sum(item.operating_samples for item in passes),
        "latest_operating_reset_at": max(resets, default=None),
        "expected_cadence_seconds": cadence,
        "cadence_headroom_seconds": headroom,
        "cadence_qualified": headroom is not None and headroom > 0,
    }
