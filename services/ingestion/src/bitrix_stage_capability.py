"""Run a bounded, read-only Bitrix deal-stage-history capability pass.

This command never imports the ingestion pipeline, creates SourceRecords, updates
checkpoints, or dispatches Celery tasks.  Its only upstream operation is the
hard-coded read-only ``crm.stagehistory.list`` client method.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from src.config import get_settings
from src.connectors.bitrix_openlines.client import BitrixOpenLinesClient
from src.connectors.bitrix_stage_history.canonical import normalize_source_contract_id
from src.connectors.bitrix_stage_history.models import ProbeLimits
from src.connectors.bitrix_stage_history.probe import (
    PassManifest,
    TraversalMode,
    collect_stage_history_pass,
    manifests_are_identical,
)
from src.connectors.bitrix_stage_history.spool import RestrictedSpool
from src.models import JsonValue


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
    parser.add_argument("--owner-id", action="append", default=[])
    parser.add_argument("--category-id", action="append", default=[])
    parser.add_argument("--restricted-output-dir", required=True, type=Path)
    parser.add_argument("--max-calls-per-pass", type=_positive_int, required=True)
    parser.add_argument("--max-rows-per-pass", type=_positive_int, required=True)
    parser.add_argument("--max-spool-bytes-per-pass", type=_positive_int, required=True)
    parser.add_argument("--max-runtime-seconds-per-pass", type=_positive_float, required=True)
    parser.add_argument("--max-passes", type=_positive_int, default=2)
    parser.add_argument("--required-identical-passes", type=_positive_int, default=2)
    parser.add_argument("--traversal-mode", choices=("offset", "id_keyset"), default="offset")
    parser.add_argument("--retain-spool", action="store_true")
    return parser


def _filters(owner_ids: list[str], category_ids: list[str]) -> dict[str, JsonValue]:
    filters: dict[str, JsonValue] = {}
    normalized_owners = list(dict.fromkeys(value for value in owner_ids if value))
    normalized_categories = list(dict.fromkeys(value for value in category_ids if value))
    if normalized_owners:
        filters["@OWNER_ID"] = cast(JsonValue, normalized_owners)
    if normalized_categories:
        filters["@CATEGORY_ID"] = cast(JsonValue, normalized_categories)
    return filters


def _repository_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    return value if value else None


def _write_json(path: Path, value: dict[str, JsonValue]) -> None:
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


def _manifest_rows(manifests: list[PassManifest]) -> list[dict[str, JsonValue]]:
    return [cast(dict[str, JsonValue], manifest.to_dict()) for manifest in manifests]


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
            manifests.append(manifest)
            spools.append(spool)
            if manifest.duplicate_conflict_rows or _has_converged(manifests, limits):
                break
    except BaseException:
        for spool in spools:
            spool.delete()
        raise
    return manifests, spools


def _has_converged(manifests: list[PassManifest], limits: ProbeLimits) -> bool:
    if len(manifests) < limits.required_identical_passes:
        return False
    window = manifests[-limits.required_identical_passes :]
    return all(manifests_are_identical(window[0], item) for item in window[1:])


def _evidence_manifest(
    *,
    source_contract_id: str,
    entity_type_id: int,
    filters_applied: bool,
    traversal_mode: TraversalMode,
    limits: ProbeLimits,
    manifests: list[PassManifest],
) -> dict[str, JsonValue]:
    return {
        "report_schema_version": "bitrix-stage-capability-v1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "repository_sha": _repository_sha(),
        "source_contract_id": source_contract_id,
        "entity_type_id": entity_type_id,
        "filters_applied": filters_applied,
        "traversal_mode": traversal_mode,
        **_limit_evidence(limits),
        "pass_manifests": cast(JsonValue, _manifest_rows(manifests)),
        "converged_identical_passes": _has_converged(manifests, limits),
        "conflict_free": all(item.duplicate_conflict_rows == 0 for item in manifests),
        "traversal_outcome": "unsupported",
        "decision_boundary": (
            "This probe does not approve traversal. A human must reconcile owner scope, "
            "permissions, endpoint semantics, and live evidence before selecting an outcome."
        ),
    }


def _limit_evidence(limits: ProbeLimits) -> dict[str, JsonValue]:
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
        "aggregate_collection_pass_upper_bounds": {
            "max_calls": limits.max_calls * limits.max_passes,
            "max_rows": limits.max_rows * limits.max_passes,
            "max_spool_bytes": limits.max_spool_bytes * limits.max_passes,
            "max_runtime_seconds": limits.max_runtime_seconds * limits.max_passes,
            "scope": "Collection passes only; excludes client setup and report writing.",
        },
        "http_attempts_per_call": 1,
    }


def run(arguments: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    limits = _limits(args)
    filters = _filters(args.owner_id, args.category_id)
    traversal_mode = cast(TraversalMode, args.traversal_mode)
    client = _client()
    spools: list[RestrictedSpool] = []
    try:
        manifests, spools = _collect_manifests(
            client,
            source_contract_id=args.source_contract_id,
            entity_type_id=args.entity_type_id,
            filters=filters,
            limits=limits,
            output_directory=args.restricted_output_dir,
            traversal_mode=traversal_mode,
        )
        evidence_manifest = _evidence_manifest(
            source_contract_id=args.source_contract_id,
            entity_type_id=args.entity_type_id,
            filters_applied=bool(filters),
            traversal_mode=traversal_mode,
            limits=limits,
            manifests=manifests,
        )
        _write_json(args.restricted_output_dir / "evidence-manifest.json", evidence_manifest)
        print(json.dumps(evidence_manifest, sort_keys=True, separators=(",", ":")))
        return 0
    finally:
        if not args.retain_spool:
            for spool in spools:
                spool.delete()
        client.close()


def main() -> None:
    try:
        raise SystemExit(run())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"bitrix stage capability failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
