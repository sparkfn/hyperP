"""Run the read-only Bitrix deal and global stage-history capability re-gate.

The command uses only three read-only Bitrix endpoints: ``crm.deal.list``,
``crm.status.list``, and ``crm.stagehistory.list``. It never imports an ingestion
pipeline, dispatches Celery, writes graph data, creates SourceRecords, or advances
checkpoints.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, cast

import redis

from src.bitrix_stage_capability_report import (
    catalog_machine_qualified as _catalog_machine_qualified,
)
from src.bitrix_stage_capability_report import (
    evidence_summary as _evidence_summary,
)
from src.bitrix_stage_capability_report import (
    passes_machine_qualified as _passes_machine_qualified,
)
from src.bitrix_stage_capability_report import (
    recommendation,
)
from src.bitrix_stage_capability_report import (
    write_failure_manifest as _write_failure_manifest,
)
from src.bitrix_stage_capability_report import (
    write_json as _write_json,
)
from src.config import get_settings
from src.connectors.bitrix_openlines.client import BitrixOpenLinesClient
from src.connectors.bitrix_stage_history.canonical import normalize_source_contract_id
from src.connectors.bitrix_stage_history.capability_provenance import (
    effective_config_fingerprint,
    normalize_image_digest,
    portal_fingerprint,
)
from src.connectors.bitrix_stage_history.capability_run_lock import acquire_capability_run_lock
from src.connectors.bitrix_stage_history.catalog_probe import collect_current_stage_catalog
from src.connectors.bitrix_stage_history.deal_probe import (
    DealPassManifest,
    RestrictedOwnerManifest,
    collect_deal_owner_pass,
    deal_manifests_are_identical,
    freeze_deal_upper_id,
)
from src.connectors.bitrix_stage_history.models import ProbeLimits
from src.connectors.bitrix_stage_history.probe import (
    PassManifest,
    TraversalMode,
    collect_stage_history_pass,
    freeze_stage_history_upper_id,
    manifests_are_identical,
)
from src.connectors.bitrix_stage_history.reconciliation_spool import (
    CapabilityReconciliationSpool,
    RedactionKey,
    new_redaction_key,
)
from src.connectors.bitrix_stage_history.spool import RestrictedSpool, _prepare_restricted_directory
from src.ingestion_config import get_ingestion_config
from src.models import JsonValue

_recommendation = recommendation


class _RestrictedArtifact(Protocol):
    path: Path

    def delete(self) -> None: ...


class _RedactionKeyArtifact:
    """Persist a per-run HMAC key only in the restricted artifact directory."""

    def __init__(self, directory: Path, key: RedactionKey) -> None:
        self.path = directory / "capability-redaction-key.bin"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                fd = -1
                output.write(key)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            if fd >= 0:
                os.close(fd)
            self.path.unlink(missing_ok=True)
            raise

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return parsed


def _contract_id(value: str) -> str:
    try:
        return normalize_source_contract_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-contract-id", required=True, type=_contract_id)
    parser.add_argument("--entity-type-id", type=_positive_int, default=2)
    parser.add_argument(
        "--owner-id",
        action="append",
        default=[],
        help="Retained for compatibility; re-gate runs reject source owner filters.",
    )
    parser.add_argument(
        "--category-id",
        action="append",
        default=[],
        help="Included current deal categories used only by the deal-owner census.",
    )
    parser.add_argument("--restricted-output-dir", required=True, type=Path)
    parser.add_argument("--max-calls-per-pass", type=_positive_int, required=True)
    parser.add_argument("--max-rows-per-pass", type=_positive_int, required=True)
    parser.add_argument("--max-spool-bytes-per-pass", type=_positive_int, required=True)
    parser.add_argument("--max-runtime-seconds-per-pass", type=_positive_float, required=True)
    parser.add_argument("--max-passes", type=_positive_int, default=2)
    parser.add_argument("--required-identical-passes", type=_positive_int, default=2)
    parser.add_argument("--traversal-mode", choices=("offset", "id_keyset"), default="id_keyset")
    parser.add_argument(
        "--deployment-image-digest",
        default=None,
        help="Immutable OCI sha256 digest; required for an approvable recommendation.",
    )
    parser.add_argument(
        "--expected-cadence-seconds",
        type=_positive_float,
        default=None,
        help="Production scan cadence used to calculate review headroom.",
    )
    parser.add_argument("--retain-spool", action="store_true")
    return parser


def _limits(args: argparse.Namespace) -> ProbeLimits:
    return ProbeLimits(
        max_calls=args.max_calls_per_pass,
        max_rows=args.max_rows_per_pass,
        max_spool_bytes=args.max_spool_bytes_per_pass,
        max_runtime_seconds=args.max_runtime_seconds_per_pass,
        max_passes=args.max_passes,
        required_identical_passes=args.required_identical_passes,
    )


def _client() -> BitrixOpenLinesClient:
    settings = get_settings()
    return BitrixOpenLinesClient(
        base_url=settings.bitrix_openlines_api_base_url.get_secret_value(),
        timeout_seconds=settings.bitrix_openlines_api_timeout_seconds,
        max_attempts=1,
        request_delay_seconds=settings.bitrix_openlines_api_request_delay_seconds,
    )


def _capability_lock_lease_seconds(limits: ProbeLimits) -> int:
    """Cover all bounded passes plus fixed startup/finalization slack."""
    pass_count = 1 + (2 * limits.max_passes)
    return math.ceil(limits.max_runtime_seconds * pass_count) + 300


@contextmanager
def _capability_run_lock(lease_seconds: int) -> Iterator[None]:
    """Reserve read-only re-gate exclusion without using Celery/task code."""
    settings = get_settings()
    with redis.Redis.from_url(settings.celery_broker_url) as lock_client:
        with acquire_capability_run_lock(lock_client, lease_seconds=lease_seconds):
            yield


def _included_categories(raw_values: Sequence[str]) -> tuple[str, ...]:
    supplied = tuple(value for value in raw_values if value)
    if not supplied:
        raise ValueError("re-gate requires at least one --category-id for the deal owner census")
    if any(not value.isdigit() for value in supplied):
        raise ValueError("re-gate category IDs must be numeric")
    return tuple(dict.fromkeys(str(int(value)) for value in supplied))


def _has_stage_converged(manifests: Sequence[PassManifest], limits: ProbeLimits) -> bool:
    if len(manifests) < limits.required_identical_passes:
        return False
    window = manifests[-limits.required_identical_passes :]
    return all(manifests_are_identical(window[0], item) for item in window[1:])


def _has_deal_converged(manifests: Sequence[DealPassManifest], limits: ProbeLimits) -> bool:
    if len(manifests) < limits.required_identical_passes:
        return False
    window = manifests[-limits.required_identical_passes :]
    return all(deal_manifests_are_identical(window[0], item) for item in window[1:])


def _collect_manifests(
    client: BitrixOpenLinesClient,
    *,
    source_contract_id: str,
    entity_type_id: int,
    filters: dict[str, JsonValue],
    limits: ProbeLimits,
    output_directory: Path,
    traversal_mode: TraversalMode,
) -> tuple[list[PassManifest], list[RestrictedSpool]]:
    """Compatibility helper retained for the original stage-only unit tests."""
    manifests: list[PassManifest] = []
    spools: list[RestrictedSpool] = []
    try:
        for pass_number in range(1, limits.max_passes + 1):
            manifest, spool = collect_stage_history_pass(
                client,
                source_contract_id=source_contract_id,
                entity_type_id=entity_type_id,
                filters=filters,
                limits=limits,
                spool_directory=output_directory,
                pass_number=pass_number,
                traversal_mode=traversal_mode,
            )
            if not isinstance(spool, RestrictedSpool):
                raise RuntimeError("legacy collection unexpectedly created a reconciliation spool")
            manifests.append(manifest)
            spools.append(spool)
            if manifest.duplicate_conflict_rows or _has_stage_converged(manifests, limits):
                break
    except BaseException:
        for spool in spools:
            spool.delete()
        raise
    return manifests, spools


def _collect_deal_passes(
    client: BitrixOpenLinesClient,
    *,
    categories: tuple[str, ...],
    upper_deal_id: int,
    limits: ProbeLimits,
    output_directory: Path,
    redaction_key: RedactionKey,
) -> tuple[list[DealPassManifest], list[RestrictedOwnerManifest]]:
    manifests: list[DealPassManifest] = []
    spools: list[RestrictedOwnerManifest] = []
    try:
        for pass_number in range(1, limits.max_passes + 1):
            manifest, spool = collect_deal_owner_pass(
                client,
                category_ids=categories,
                upper_deal_id=upper_deal_id,
                limits=limits,
                spool_directory=output_directory,
                pass_number=pass_number,
                redaction_key=redaction_key,
            )
            manifests.append(manifest)
            spools.append(spool)
            if manifest.duplicate_rows or _has_deal_converged(manifests, limits):
                break
    except BaseException:
        for spool in spools:
            spool.delete()
        raise
    return manifests, spools


def _collect_global_stage_passes(
    client: BitrixOpenLinesClient,
    *,
    source_contract_id: str,
    entity_type_id: int,
    upper_history_id: int,
    owner_manifest: RestrictedOwnerManifest,
    owner_manifest_digest: str,
    limits: ProbeLimits,
    output_directory: Path,
    redaction_key: RedactionKey,
    current_catalog_stage_keys: tuple[tuple[str, str], ...],
) -> tuple[list[PassManifest], list[CapabilityReconciliationSpool]]:
    manifests: list[PassManifest] = []
    spools: list[CapabilityReconciliationSpool] = []
    try:
        for pass_number in range(1, limits.max_passes + 1):
            manifest, spool = collect_stage_history_pass(
                client,
                source_contract_id=source_contract_id,
                entity_type_id=entity_type_id,
                filters={},
                limits=limits,
                spool_directory=output_directory,
                pass_number=pass_number,
                traversal_mode="id_keyset",
                upper_history_id=upper_history_id,
                owner_manifest_path=owner_manifest.path,
                owner_manifest_digest=owner_manifest_digest,
                redaction_key=redaction_key,
                current_catalog_stage_keys=current_catalog_stage_keys,
            )
            if not isinstance(spool, CapabilityReconciliationSpool):
                raise RuntimeError("global re-gate did not create a reconciliation spool")
            manifests.append(manifest)
            spools.append(spool)
            if manifest.duplicate_conflict_rows or _has_stage_converged(manifests, limits):
                break
    except BaseException:
        for spool in spools:
            spool.delete()
        raise
    return manifests, spools


def _run_locked(
    args: argparse.Namespace,
    *,
    categories: tuple[str, ...],
    limits: ProbeLimits,
    image_digest: str | None,
) -> int:
    _prepare_restricted_directory(args.restricted_output_dir)
    client = _client()
    redaction_key = new_redaction_key()
    artifacts: list[_RestrictedArtifact] = []
    try:
        key_artifact = _RedactionKeyArtifact(args.restricted_output_dir, redaction_key)
        artifacts.append(key_artifact)
        settings = get_settings()
        portal_digest = portal_fingerprint(
            redaction_key, settings.bitrix_openlines_api_base_url.get_secret_value()
        )
        config_digest = effective_config_fingerprint(
            redaction_key, get_ingestion_config().bitrix_openlines, categories
        )
        catalog_manifest, catalog_keys = collect_current_stage_catalog(
            client,
            category_ids=categories,
            limits=limits,
            redaction_key=redaction_key,
        )
        if not _catalog_machine_qualified(catalog_manifest):
            _write_failure_manifest(
                args.restricted_output_dir,
                reason="current_stage_catalog_not_qualified",
            )
            raise RuntimeError(
                "current deal-stage catalog is incomplete or internally inconsistent"
            )
        upper_deal_id = freeze_deal_upper_id(client, categories)
        deal_manifests, owner_spools = _collect_deal_passes(
            client,
            categories=categories,
            upper_deal_id=upper_deal_id,
            limits=limits,
            output_directory=args.restricted_output_dir,
            redaction_key=redaction_key,
        )
        artifacts.extend(owner_spools)
        if not _has_deal_converged(deal_manifests, limits) or not _passes_machine_qualified(
            deal_manifests, stage=False
        ):
            _write_failure_manifest(
                args.restricted_output_dir,
                reason="deal_owner_census_not_converged_or_not_qualified",
            )
            raise RuntimeError(
                "deal owner manifests did not converge into a complete frozen census"
            )
        selected_owner = owner_spools[-1]
        upper_history_id = freeze_stage_history_upper_id(client, args.entity_type_id)
        stage_manifests, stage_spools = _collect_global_stage_passes(
            client,
            source_contract_id=args.source_contract_id,
            entity_type_id=args.entity_type_id,
            upper_history_id=upper_history_id,
            owner_manifest=selected_owner,
            owner_manifest_digest=deal_manifests[-1].owner_manifest_digest,
            limits=limits,
            output_directory=args.restricted_output_dir,
            redaction_key=redaction_key,
            current_catalog_stage_keys=catalog_keys,
        )
        artifacts.extend(stage_spools)
        if not _has_stage_converged(stage_manifests, limits) or not _passes_machine_qualified(
            stage_manifests, stage=True
        ):
            _write_failure_manifest(
                args.restricted_output_dir,
                reason="global_stage_history_not_converged_or_not_qualified",
            )
            raise RuntimeError(
                "global stage-history manifests did not converge into a complete frozen census"
            )
        summary = _evidence_summary(
            source_contract_id=args.source_contract_id,
            entity_type_id=args.entity_type_id,
            categories=categories,
            limits=limits,
            deal_manifests=deal_manifests,
            stage_manifests=stage_manifests,
            catalog_manifest=catalog_manifest,
            portal_digest=portal_digest,
            config_digest=config_digest,
            image_digest=image_digest,
            expected_cadence_seconds=args.expected_cadence_seconds,
            retained_verification_material=args.retain_spool,
        )
        _write_json(args.restricted_output_dir / "final-evidence-summary.json", summary)
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
        return 0
    finally:
        if not args.retain_spool:
            for artifact in artifacts:
                artifact.delete()
        client.close()


def run(arguments: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    if args.owner_id:
        raise ValueError(
            "re-gate rejects --owner-id because stage history must be scanned globally"
        )
    traversal_mode = cast(TraversalMode, args.traversal_mode)
    if traversal_mode != "id_keyset":
        raise ValueError("re-gate requires --traversal-mode id_keyset")
    categories = _included_categories(args.category_id)
    limits = _limits(args)
    image_digest = normalize_image_digest(args.deployment_image_digest)
    with _capability_run_lock(_capability_lock_lease_seconds(limits)):
        return _run_locked(args, categories=categories, limits=limits, image_digest=image_digest)


def main() -> None:
    try:
        raise SystemExit(run())
    except (OSError, RuntimeError, ValueError, redis.RedisError) as exc:
        print(f"bitrix stage capability failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
