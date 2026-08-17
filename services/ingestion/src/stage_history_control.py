"""Operator CLI for the default-off bounded CRM stage-history smoke stream."""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from typing import cast

from pydantic.types import JsonValue

from src.config import Settings, get_settings
from src.connectors.bitrix_openlines.client import BitrixOpenLinesClient
from src.connectors.bitrix_stage_history.artifact_connector import (
    VerifiedStageIngestionArtifact,
    derive_backfill_plan,
    derive_catch_up_plan,
    derive_smoke_plan,
    load_qualification_evidence,
    read_stage_ingestion_artifact,
)
from src.connectors.bitrix_stage_history.artifact_runtime import (
    stage_history_store_from_settings,
)
from src.connectors.bitrix_stage_history.connector import (
    StageCaptureAuthorization,
    StageCaptureLimits,
    collect_stage_history_smoke,
    stage_capture_limits_digest,
)
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import LogicalRunControl
from src.graph.stage_history_status import StageHistoryStatusRepository
from src.ingestion_config import StageHistoryIngestionConfig, get_ingestion_config
from src.stage_history_review_control import _queue_review, _resume_review
from src.stage_history_task_lock import StageHistoryTaskLock, stage_history_task_lock
from src.stage_history_task_runtime import _replay_authorization
from src.stage_history_tasks import (
    record_stage_history_capture_failure_task,
    replay_stage_history_artifact_task,
)

_STAGE_HISTORY_RUN_TYPES = {
    "bounded_smoke_replay",
    "authoritative_backfill_replay",
    "authoritative_catch_up_replay",
    "capture_failure_accounting",
    "parent_reconcile",
    "conflict_review",
    "correction_review",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("collect-smoke")
    for name in ("dispatch-smoke", "record-capture-failure"):
        command = commands.add_parser(name)
        command.add_argument("--artifact-id", required=True)
        command.add_argument("--authorization-reference", required=True)
    resume = commands.add_parser("resume")
    resume_target = resume.add_mutually_exclusive_group(required=True)
    resume_target.add_argument("--artifact-id")
    resume_target.add_argument("--command-id")
    resume.add_argument("--authorization-reference", required=True)
    for name in ("status", "request-stop"):
        command = commands.add_parser(name)
        command.add_argument("--logical-run-id", required=True)
        if name == "request-stop":
            command.add_argument("--actor", required=True)
            command.add_argument("--reason", required=True)
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--logical-run-id", required=True)
    reconcile.add_argument("--artifact-id", required=True)
    reconcile.add_argument("--authorization-reference", required=True)
    for name in ("resolve-parents", "reject-parent", "resolve-conflict", "apply-correction"):
        command = commands.add_parser(name)
        command.add_argument("--command-id")
        command.add_argument("--event-identity", required=True)
        command.add_argument("--occurrence-id", required=True)
        command.add_argument("--reviewer", required=True)
        command.add_argument("--authorization-reference", required=True)
        command.add_argument("--expected-head-version", type=int, required=True)
        command.add_argument("--expected-authority-token", type=int, required=True)
        command.add_argument(
            "--expected-authority-state",
            choices=(
                "effective",
                "withheld_parent",
                "withheld_conflict",
                "rejected",
                "corrected",
            ),
        )
        command.add_argument("--expected-variant-set-digest", required=True)
        command.add_argument("--retry-sequence", type=int)
        command.add_argument("--selected-variant-hash")
        command.add_argument("--selected-association-decision-id")
        command.add_argument("--correction-of-decision-id")
    return parser


def run(arguments: list[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    config = get_ingestion_config().stage_history_ingestion
    if args.command == "collect-smoke":
        _print(_collect_smoke(config))
        return 0
    if args.command in {"dispatch-smoke", "record-capture-failure"}:
        config.assert_dispatch_enabled(now=datetime.now(UTC))
        artifact = _verified_artifact(
            cast(str, args.artifact_id),
            cast(str, args.authorization_reference),
            config,
        )
        expected_kind = (
            "stage-ingestion-failed"
            if args.command == "record-capture-failure"
            else "stage-ingestion"
        )
        if artifact.manifest.artifact_kind != expected_kind:
            raise ValueError("stage-history artifact kind does not match the command")
        task = (
            record_stage_history_capture_failure_task
            if args.command == "record-capture-failure"
            else replay_stage_history_artifact_task
        )
        task_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"hyperp:{args.command}:{args.artifact_id}:{args.authorization_reference}",
        ).hex
        task.apply_async(
            args=(args.artifact_id, args.authorization_reference),
            task_id=task_id,
            queue="ingestion",
        )
        _print(
            {
                "status": "queued",
                "task_id": task_id,
                "artifact_id": args.artifact_id,
                "command": args.command,
            }
        )
        return 0
    if args.command == "resume":
        config.assert_dispatch_enabled(now=datetime.now(UTC))
        if args.command_id is not None:
            _print(
                _resume_review(
                    cast(str, args.command_id),
                    cast(str, args.authorization_reference),
                    config,
                )
            )
            return 0
        artifact = _verified_artifact(
            cast(str, args.artifact_id),
            cast(str, args.authorization_reference),
            config,
        )
        task = (
            record_stage_history_capture_failure_task
            if artifact.manifest.artifact_kind == "stage-ingestion-failed"
            else replay_stage_history_artifact_task
        )
        task_id = uuid.uuid4().hex
        task.apply_async(
            args=(args.artifact_id, args.authorization_reference),
            task_id=task_id,
            queue="ingestion",
        )
        _print(
            {
                "status": "queued",
                "task_id": task_id,
                "artifact_id": args.artifact_id,
                "command": "resume",
            }
        )
        return 0
    if args.command in {
        "resolve-parents",
        "reject-parent",
        "resolve-conflict",
        "apply-correction",
    }:
        config.assert_dispatch_enabled(now=datetime.now(UTC))
        _print(_queue_review(args, config))
        return 0
    if args.command in {"request-stop", "reconcile"}:
        config.assert_dispatch_enabled(now=datetime.now(UTC))
    if args.command == "request-stop" and args.actor != config.authorized_actor:
        raise PermissionError("stage-history stop actor changed")
    client = Neo4jClient(get_settings())
    try:
        if args.command == "status":
            status = StageHistoryStatusRepository(client).status(args.logical_run_id)
            if status is None:
                _print({"status": "not_found", "logical_run_id": args.logical_run_id})
                return 1
            _print(asdict(status))
            return 0
        if args.command == "request-stop":
            logical = LogicalRunControl(client)
            target = logical.get(args.logical_run_id)
            if (
                target is None
                or target.source_key != "bitrix_chat"
                or target.mode not in _STAGE_HISTORY_RUN_TYPES
            ):
                _print({"status": "not_found", "logical_run_id": args.logical_run_id})
                return 1
            state = logical.request_stop(
                logical_run_id=args.logical_run_id,
                requested_by=args.actor,
                reason=args.reason,
            )
            if state is None:
                _print({"status": "not_found", "logical_run_id": args.logical_run_id})
                return 1
            _print(
                {
                    "status": state.status,
                    "logical_run_id": state.logical_run_id,
                    "stop_requested": state.stop_requested,
                }
            )
            return 0
        if args.command == "reconcile":
            artifact = _verified_artifact(
                args.artifact_id,
                args.authorization_reference,
                config,
            )
            report = StageHistoryStatusRepository(client).reconcile(
                args.logical_run_id,
                artifact=artifact,
            )
            _print(asdict(report))
            return 0 if report.complete else 2
    finally:
        client.close()
    raise RuntimeError("unsupported stage-history command")


def _collect_smoke(config: StageHistoryIngestionConfig) -> dict[str, JsonValue]:
    return _collect_capture(config, capture_mode="smoke")


def collect_authoritative_backfill(
    config: StageHistoryIngestionConfig,
) -> dict[str, JsonValue]:
    return _collect_capture(config, capture_mode="backfill")


def collect_authoritative_catch_up(
    config: StageHistoryIngestionConfig,
) -> dict[str, JsonValue]:
    return _collect_capture(config, capture_mode="catch_up")


def _collect_capture(
    config: StageHistoryIngestionConfig,
    *,
    capture_mode: str,
) -> dict[str, JsonValue]:
    now = datetime.now(UTC)
    config.assert_dispatch_enabled(now=now)
    settings = get_settings()
    with stage_history_task_lock(
        settings.celery_broker_url,
        owner=f"collect:{uuid.uuid4().hex}",
    ) as lock:
        lock.assert_owned()
        return _collect_smoke_locked(
            config, settings=settings, lock=lock, capture_mode=capture_mode
        )


def _collect_smoke_locked(
    config: StageHistoryIngestionConfig,
    *,
    settings: Settings,
    lock: StageHistoryTaskLock,
    capture_mode: str = "smoke",
) -> dict[str, JsonValue]:
    store = stage_history_store_from_settings(settings)
    evidence = load_qualification_evidence(
        store,
        owner_artifact_id=config.owner_artifact_id,
        stage_artifact_id=config.stage_artifact_id,
        expected_qualification_evidence_digest=config.qualification_evidence_digest,
        expected_source_contract_uuid=config.source_contract_uuid,
        expected_configuration_digest=config.accepted_configuration_digest,
        entity_type_id=config.entity_type_id,
    )
    limits = StageCaptureLimits(
        max_calls=config.max_calls,
        max_rows=config.max_rows,
        max_spool_bytes=config.max_spool_bytes,
        max_runtime_seconds=config.max_runtime_seconds,
    )
    limits_digest = stage_capture_limits_digest(limits)
    authorization = StageCaptureAuthorization(
        enabled=config.enabled,
        reference=config.authorization_reference,
        actor=config.authorized_actor,
        expires_at=cast(datetime, config.authorization_expires_at),
        owner_artifact_id=config.owner_artifact_id,
        owner_manifest_hmac=config.owner_manifest_hmac,
        stage_artifact_id=config.stage_artifact_id,
        stage_manifest_hmac=config.stage_manifest_hmac,
        qualification_evidence_digest=config.qualification_evidence_digest,
        source_contract_uuid=config.source_contract_uuid,
        entity_type_id=config.entity_type_id,
        configuration_digest=config.accepted_configuration_digest,
        limits_digest=limits_digest,
    )
    client = BitrixOpenLinesClient(
        base_url=settings.bitrix_openlines_api_base_url.get_secret_value(),
        timeout_seconds=settings.bitrix_openlines_api_timeout_seconds,
        max_attempts=settings.bitrix_openlines_api_max_attempts,
        request_delay_seconds=settings.bitrix_openlines_api_request_delay_seconds,
    )
    if capture_mode == "backfill":
        plan = derive_backfill_plan(evidence, max_calls=limits.max_calls, max_rows=limits.max_rows)
    elif capture_mode == "catch_up":
        plan = derive_catch_up_plan(evidence, max_calls=limits.max_calls, max_rows=limits.max_rows)
    elif capture_mode == "smoke":
        plan = derive_smoke_plan(evidence, max_calls=limits.max_calls, max_rows=limits.max_rows)
    else:
        raise ValueError("unsupported stage-history capture mode")
    result = collect_stage_history_smoke(
        client,
        store,
        evidence=evidence,
        plan=plan,
        authorization=authorization,
        limits=limits,
        repository_sha=settings.stage_history_repository_sha,
        image_digest=settings.stage_history_image_digest,
        configuration_digest=config.accepted_configuration_digest,
        limits_digest=limits_digest,
        retention_days=config.retention_days,
        ownership_guard=lock.assert_owned,
    )
    lock.assert_owned()
    return {
        "status": "qualified" if result.qualified else "failed",
        "artifact_id": result.manifest.artifact_id,
        "artifact_kind": result.manifest.artifact_kind,
        "pages": result.pages,
        "rows": result.rows,
        "valid_rows": result.valid_rows,
        "malformed_rows": result.malformed_rows,
        "failure_reason": result.failure_reason,
    }


def _verified_artifact(
    artifact_id: str,
    authorization_reference: str,
    config: StageHistoryIngestionConfig,
) -> VerifiedStageIngestionArtifact:
    settings = get_settings()
    store = stage_history_store_from_settings(settings)
    try:
        manifest = store.verify(artifact_id)
        return read_stage_ingestion_artifact(
            store,
            artifact_id=artifact_id,
            authorization=_replay_authorization(
                artifact_id,
                authorization_reference,
                manifest,
                config,
                repository_sha=settings.stage_history_repository_sha,
                image_digest=settings.stage_history_image_digest,
            ),
        )
    finally:
        store.close()


def _print(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
