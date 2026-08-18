"""Operator CLI for the #148 authoritative CRM stage backfill and release."""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from src.config import get_settings
from src.connectors.bitrix_stage_history.artifact_connector import (
    load_qualification_evidence,
)
from src.connectors.bitrix_stage_history.artifact_runtime import (
    stage_history_store_from_settings,
)
from src.crm_stage_mapping import build_mapping_report, load_mapping_policy
from src.graph.client import Neo4jClient
from src.graph.crm_stage_backfill import CrmStageBackfillRepository
from src.ingestion_config import get_ingestion_config
from src.models import JsonValue
from src.stage_history_control import (
    _verified_artifact,
    collect_authoritative_backfill,
    collect_authoritative_catch_up,
)
from src.stage_history_tasks import replay_stage_history_artifact_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    for name in ("backfill", "catch-up"):
        command = commands.add_parser(name)
        command.add_argument("--artifact-id")
        command.add_argument("--authorization-reference")
    mapping = commands.add_parser("map-report")
    mapping.add_argument("--mapping-file", type=Path, required=True)
    commands.add_parser("reconcile")
    retain = commands.add_parser("retain-parent-retries")
    retain.add_argument("--expected-count", type=int, required=True)
    retain.add_argument("--accepted-by", required=True)
    retain.add_argument("--reason", required=True)
    invalidation = commands.add_parser("invalidate-status")
    invalidation.add_argument("--mapping-file", type=Path)
    invalidation.add_argument("--rebuild", action="store_true")
    invalidation.add_argument("--rehearse-rollback", action="store_true")
    release = commands.add_parser("release-status")
    release.add_argument("--mapping-file", type=Path)
    release.add_argument("--enable", action="store_true")
    release.add_argument("--accepted-by")
    release.add_argument("--boundary-digest")
    return parser


def run(arguments: list[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    config = get_ingestion_config().stage_history_ingestion
    if args.command == "preflight":
        _print(_preflight())
        return 0
    if args.command in {"backfill", "catch-up"}:
        config.assert_dispatch_enabled(now=datetime.now(UTC))
        if args.artifact_id is None:
            result = (
                collect_authoritative_backfill(config)
                if args.command == "backfill"
                else collect_authoritative_catch_up(config)
            )
            _print(result)
            return 0
        if not args.authorization_reference:
            raise ValueError("dispatch requires --authorization-reference")
        _print(
            _dispatch(
                cast(str, args.artifact_id),
                cast(str, args.authorization_reference),
                command=cast(str, args.command),
            )
        )
        return 0
    client = Neo4jClient(get_settings())
    try:
        repository = CrmStageBackfillRepository(client, entity_type_id=config.entity_type_id)
        if args.command == "map-report":
            policy = load_mapping_policy(args.mapping_file)
            _print(asdict(build_mapping_report(repository.inventory(), policy)))
            return 0
        if args.command == "reconcile":
            report = repository.reconcile()
            _print(asdict(report))
            return 0 if report.complete else 2
        if args.command == "retain-parent-retries":
            if args.accepted_by != config.authorized_actor:
                raise PermissionError("CRM stage retry retention actor changed")
            retention = repository.retain_pending_parent_retries(
                expected_count=args.expected_count,
                accepted_by=args.accepted_by,
                reason=args.reason,
                decision_id=uuid.uuid4().hex,
            )
            _print(asdict(retention) | {"complete": retention.complete})
            return 0 if retention.complete else 2
        if args.command == "invalidate-status":
            invalidation_result: dict[str, JsonValue] = {
                "before": cast(JsonValue, asdict(repository.invalidation_status()))
            }
            if args.rebuild or args.rehearse_rollback:
                if args.mapping_file is None:
                    raise ValueError("rebuild and rollback rehearsal require --mapping-file")
                policy = load_mapping_policy(args.mapping_file)
                mapping_report = build_mapping_report(repository.inventory(), policy)
                if not mapping_report.complete:
                    raise RuntimeError("observed CRM stage tuples are not completely mapped")
                if args.rebuild:
                    invalidation_result["rebuild"] = cast(
                        JsonValue, asdict(repository.rebuild(policy))
                    )
                if args.rehearse_rollback:
                    probe_id = uuid.uuid4().hex
                    candidate_count, leaked_count = repository.rehearse_rollback(
                        policy.mapping_version, probe_id
                    )
                    invalidation_result["rollback_rehearsal"] = {
                        "candidate_count": candidate_count,
                        "leaked_probe_count": leaked_count,
                        "passed": leaked_count == 0,
                    }
                invalidation_result["after"] = cast(
                    JsonValue, asdict(repository.invalidation_status())
                )
            _print(invalidation_result)
            return 0
        if args.command == "release-status":
            if not args.enable:
                _print(asdict(repository.release_status()))
                return 0
            if args.mapping_file is None or not args.accepted_by or not args.boundary_digest:
                raise ValueError(
                    "release enable requires mapping file, accepted actor, and boundary digest"
                )
            policy = load_mapping_policy(args.mapping_file)
            mapping_report = build_mapping_report(repository.inventory(), policy)
            reconciliation = repository.reconcile()
            invalidation = repository.invalidation_status()
            if not mapping_report.complete:
                raise RuntimeError("CRM stage mapping review is incomplete")
            if not reconciliation.complete:
                raise RuntimeError("CRM stage reconciliation is incomplete")
            if not invalidation.rebuilt:
                raise RuntimeError("CRM stage analytical inputs are not rebuilt")
            status = repository.enable_release(
                policy,
                boundary_digest=args.boundary_digest,
                reconciliation_digest=reconciliation.digest,
                accepted_by=args.accepted_by,
            )
            _print(asdict(status))
            return 0
    finally:
        client.close()
    raise RuntimeError("unsupported CRM stage backfill command")


def _preflight() -> dict[str, JsonValue]:
    config = get_ingestion_config().stage_history_ingestion
    config.assert_dispatch_enabled(now=datetime.now(UTC))
    settings = get_settings()
    store = stage_history_store_from_settings(settings)
    try:
        evidence = load_qualification_evidence(
            store,
            owner_artifact_id=config.owner_artifact_id,
            stage_artifact_id=config.stage_artifact_id,
            expected_qualification_evidence_digest=config.qualification_evidence_digest,
            expected_source_contract_uuid=config.source_contract_uuid,
            expected_configuration_digest=config.accepted_configuration_digest,
            entity_type_id=config.entity_type_id,
        )
    finally:
        store.close()
    required_calls = (len(evidence.expected_rows) + 49) // 50
    return {
        "status": "ready"
        if config.max_calls >= required_calls and config.max_rows >= len(evidence.expected_rows)
        else "limits_insufficient",
        "authorization_active": True,
        "owner_artifact_verified": True,
        "stage_artifact_verified": True,
        "primary_backup_required": True,
        "expected_rows": len(evidence.expected_rows),
        "required_calls": required_calls,
        "configured_max_calls": config.max_calls,
        "configured_max_rows": config.max_rows,
        "repository_sha_present": bool(settings.stage_history_repository_sha),
        "image_digest_present": bool(settings.stage_history_image_digest),
    }


def _dispatch(
    artifact_id: str,
    authorization_reference: str,
    *,
    command: str,
) -> dict[str, JsonValue]:
    config = get_ingestion_config().stage_history_ingestion
    artifact = _verified_artifact(artifact_id, authorization_reference, config)
    expected_mode = "collect-backfill" if command == "backfill" else "collect-catch-up"
    if artifact.manifest.artifact_kind != "stage-ingestion":
        raise ValueError("authoritative replay requires a qualified stage-ingestion artifact")
    if artifact.manifest.metadata.get("mode") != expected_mode:
        raise ValueError("stage ingestion artifact capture mode does not match the operation")
    task_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"hyperp:{command}:{artifact_id}:{authorization_reference}",
    ).hex
    replay_stage_history_artifact_task.apply_async(
        args=(artifact_id, authorization_reference),
        task_id=task_id,
        queue="ingestion",
    )
    return {
        "status": "queued",
        "command": command,
        "artifact_id": artifact_id,
        "task_id": task_id,
    }


def _print(payload: object) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
