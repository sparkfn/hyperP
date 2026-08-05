"""Celery tasks for ordered, agent-triggered all-source ingestion."""

from __future__ import annotations

import json
import logging
import uuid
from typing import TypedDict

import redis
from celery import Task, chain, group
from celery.canvas import Signature
from celery.exceptions import Reject
from pydantic.types import JsonValue

from src.celery_app import celery_app
from src.config import get_settings
from src.ingestion_orchestrator import (
    INGESTION_QUEUE,
    ORCHESTRATION_PRIORITY,
    IngestionManifest,
    IngestionTaskSpec,
    parse_manifest,
)
from src.main import IngestionSummary

logger = logging.getLogger(__name__)

_ORCHESTRATION_MARKER_PREFIX = "profile_unifier:ingestion:orchestration"
_ORCHESTRATION_MARKER_SECONDS = 60 * 60 * 24


class OrchestratedIngestionSummary(TypedDict):
    """Task IDs emitted by the all-source Celery orchestration."""

    status: str
    identity_task_count: int
    dependent_task_count: int
    orchestration_id: str
    workflow_task_id: str


class DependentPhaseSummary(TypedDict):
    """Task IDs submitted after a successful identity phase."""

    status: str
    dependent_task_count: int
    dependent_group_id: str


def _orchestration_manifest(payload: dict[str, JsonValue]) -> IngestionManifest:
    """Revalidate an untrusted Celery JSON payload before scheduling child tasks."""
    return parse_manifest(json.dumps(payload))


def _orchestration_marker_key(phase: str, orchestration_id: str) -> str:
    return f"{_ORCHESTRATION_MARKER_PREFIX}:{phase}:{orchestration_id}"


def _queued_orchestration_result_id(phase: str, orchestration_id: str) -> str | None:
    """Return the previously published phase result ID when one is recorded."""
    try:
        with redis.Redis.from_url(get_settings().celery_broker_url) as client:
            result_id = client.get(_orchestration_marker_key(phase, orchestration_id))
        if isinstance(result_id, bytes):
            return result_id.decode("utf-8")
        if isinstance(result_id, str):
            return result_id
        return None
    except Exception:
        logger.warning(
            "Could not read the %s orchestration marker; continuing without deduplication",
            phase,
        )
        return None


def _mark_orchestration_phase_queued(
    phase: str,
    orchestration_id: str,
    result_id: str,
) -> None:
    """Record a published phase without risking loss when Redis marking fails."""
    try:
        with redis.Redis.from_url(get_settings().celery_broker_url) as client:
            client.set(
                _orchestration_marker_key(phase, orchestration_id),
                result_id,
                nx=True,
                ex=_ORCHESTRATION_MARKER_SECONDS,
            )
    except Exception:
        # Publishing has already succeeded. Raising here would redeliver this
        # short task and could duplicate child messages; source locks still
        # protect ingestion if a later redelivery cannot observe the marker.
        logger.warning(
            "Could not write the %s orchestration marker; child tasks remain queued",
            phase,
        )


def _ingestion_signature(spec: IngestionTaskSpec) -> Signature:
    """Build a priority-routed child signature without bypassing task safeguards."""
    return celery_app.signature(
        "src.tasks.run_ingestion_task",
        args=(spec.source_key, spec.mode, spec.dump_path),
        kwargs={"entity_key": spec.entity_key, "wait_for_source": True},
        queue=INGESTION_QUEUE,
        priority=spec.priority,
    )


@celery_app.task(
    name="src.ingestion_orchestration_tasks.start_orchestrated_ingestion_task",
    bind=True,
    max_retries=0,
)
def start_orchestrated_ingestion_task(
    self: Task,
    manifest_payload: dict[str, JsonValue],
) -> OrchestratedIngestionSummary:
    """Queue identity tasks, then release dependent tasks only after they succeed."""
    manifest = _orchestration_manifest(manifest_payload)
    orchestration_id = str(self.request.id or uuid.uuid4())
    existing_workflow_id = _queued_orchestration_result_id("identity", orchestration_id)
    if existing_workflow_id is not None:
        return {
            "status": "already_queued",
            "identity_task_count": len(manifest.identity),
            "dependent_task_count": len(manifest.dependent),
            "orchestration_id": orchestration_id,
            "workflow_task_id": existing_workflow_id,
        }
    identity_phase = group(_ingestion_signature(spec) for spec in manifest.identity)
    callback = start_dependent_ingestions_task.s(manifest.to_payload(), orchestration_id).set(
        queue=INGESTION_QUEUE,
        priority=ORCHESTRATION_PRIORITY,
    )
    result = chain(identity_phase, callback).apply_async(
        queue=INGESTION_QUEUE,
        priority=ORCHESTRATION_PRIORITY,
    )
    workflow_task_id = str(result.id)
    _mark_orchestration_phase_queued("identity", orchestration_id, workflow_task_id)
    return {
        "status": "identity_queued",
        "identity_task_count": len(manifest.identity),
        "dependent_task_count": len(manifest.dependent),
        "orchestration_id": orchestration_id,
        "workflow_task_id": workflow_task_id,
    }


@celery_app.task(
    name="src.ingestion_orchestration_tasks.start_dependent_ingestions_task",
    bind=True,
    max_retries=0,
)
def start_dependent_ingestions_task(
    self: Task,
    identity_summaries: list[IngestionSummary],
    manifest_payload: dict[str, JsonValue],
    orchestration_id: str,
) -> DependentPhaseSummary:
    """Gate dependent work on a cleanly completed identity phase."""
    del self
    manifest = _orchestration_manifest(manifest_payload)
    expected_sources = [spec.source_key for spec in manifest.identity]
    observed_sources = [summary["source_key"] for summary in identity_summaries]
    if len(observed_sources) != len(expected_sources) or set(observed_sources) != set(
        expected_sources
    ):
        raise Reject(
            "Identity phase returned incomplete or duplicate source results",
            requeue=False,
        )
    failed_statuses = {
        summary["source_key"]: summary["status"]
        for summary in identity_summaries
        if summary["status"] != "completed"
    }
    if failed_statuses:
        details = ", ".join(
            f"{source}={status}" for source, status in sorted(failed_statuses.items())
        )
        raise Reject(f"Identity phase did not complete cleanly: {details}", requeue=False)
    existing_group_id = _queued_orchestration_result_id("dependent", orchestration_id)
    if existing_group_id is not None:
        return {
            "status": "already_queued",
            "dependent_task_count": len(manifest.dependent),
            "dependent_group_id": existing_group_id,
        }
    result = group(_ingestion_signature(spec) for spec in manifest.dependent).apply_async(
        queue=INGESTION_QUEUE,
        priority=ORCHESTRATION_PRIORITY,
    )
    dependent_group_id = str(result.id)
    _mark_orchestration_phase_queued("dependent", orchestration_id, dependent_group_id)
    return {
        "status": "dependent_queued",
        "dependent_task_count": len(manifest.dependent),
        "dependent_group_id": dependent_group_id,
    }
