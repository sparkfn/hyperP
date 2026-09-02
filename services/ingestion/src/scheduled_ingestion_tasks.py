"""Idempotent weekly API-ingestion chain dispatch."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import TypedDict

import redis
from celery import Task, chain
from celery.canvas import Signature
from neo4j import ManagedTransaction
from pydantic import TypeAdapter

from src.celery_app import INGESTION_QUEUE, celery_app
from src.config import get_settings
from src.graph.bitrix_source_instances import admit_configured_bitrix_control
from src.graph.client import Neo4jClient
from src.graph.queries.bitrix_backfill import GET_ACTIVE_BITRIX_SUCCESSOR_SCHEDULE
from src.ingestion_config import get_ingestion_config
from src.models import JsonValue
from src.scheduled_ingestion_groups import scheduled_ingestion_group
from src.source_instances import LEGACY_DEFAULT_CONTROL_INSTANCE_ID

logger = logging.getLogger(__name__)

_MARKER_PREFIX = "profile_unifier:scheduled-ingestion"
_MARKER_TTL_SECONDS = 60 * 60 * 24 * 8


class ScheduledGroupDispatchSummary(TypedDict):
    """A group-chain publication result."""

    status: str
    group_key: str
    incremental: bool
    workflow_task_id: str


def _utc_occurrence_date() -> str:
    return datetime.now(UTC).date().isoformat()


def _marker_key(group_key: str, incremental: bool, task_id: str) -> str:
    """Return a stable idempotency key for cron or manual dispatch."""
    if incremental:
        occurrence = _utc_occurrence_date()
    else:
        occurrence = task_id
    policy = "incremental" if incremental else "full"
    return f"{_MARKER_PREFIX}:{group_key}:{policy}:{occurrence}"


def _claim_dispatch(marker_key: str, task_id: str) -> tuple[bool, str | None]:
    """Claim a dispatch key or resume a redelivery owned by this task."""
    with redis.Redis.from_url(get_settings().celery_broker_url, decode_responses=True) as client:
        while True:
            if client.set(marker_key, task_id, nx=True, ex=_MARKER_TTL_SECONDS):
                return True, None
            prior = client.get(marker_key)
            if prior is not None:
                break
    if prior == task_id:
        # The worker may have exited after reserving but before recording the
        # workflow ID. Re-publishing is safe because every chain step has its
        # own logical-run completion marker.
        return True, None
    return False, prior


def _release_claim(marker_key: str, task_id: str) -> None:
    """Release a failed publication only when this task owns the claim."""
    script = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
    with redis.Redis.from_url(get_settings().celery_broker_url, decode_responses=True) as client:
        client.eval(script, 1, marker_key, task_id)


def _signature(
    source_key: str,
    entity_key: str | None,
    incremental: bool,
    idempotency_key: str,
) -> Signature:
    """Build an immutable chain step that cannot consume a prior result."""
    return celery_app.signature(
        "src.tasks.run_ingestion_task",
        args=(source_key, "api"),
        kwargs={
            "entity_key": entity_key,
            "incremental": incremental,
            "wait_for_source": True,
            "require_clean_completion": True,
            "idempotency_key": idempotency_key,
            "scheduled_dispatch": True,
        },
        immutable=True,
        queue=INGESTION_QUEUE,
    )


def _dispatch_active_bitrix_successor(occurrence: str) -> str | None:
    """Publish one fresh bounded split cadence when cutover is active."""
    from src.bitrix_backfill_control import _manifest_from_payload
    from src.bitrix_backfill_tasks import dispatch_generation_canvas
    from src.connectors.bitrix_crm.activity_probe import freeze_activity_upper_id
    from src.connectors.bitrix_stage_history.deal_probe import freeze_deal_upper_id
    from src.main import create_bitrix_known_owner_client

    graph = Neo4jClient(get_settings())
    try:

        def _read(tx: ManagedTransaction) -> tuple[str, str, str, str] | None:
            record = tx.run(
                GET_ACTIVE_BITRIX_SUCCESSOR_SCHEDULE,
                control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
            ).single()
            if record is None:
                return None
            return (
                str(record["generation_id"]),
                str(record["configuration_digest"]),
                str(record["manifest_json"]),
                str(record["control_instance_id"]),
            )

        active = graph.execute_read(_read)
    finally:
        graph.close()
    if active is None:
        return None
    generation_id, configuration_digest, manifest_json, control_instance_id = active
    admit_configured_bitrix_control(get_settings(), control_instance_id)
    # A durable reservation closes the race before source-window probes. A
    # repair-owned or ambiguous dispatch block rejects rather than being cleared.
    from src.graph.crm_deal_identity_repair_control import CrmDealRepairControlRepository

    reservation_graph = Neo4jClient(get_settings())
    try:
        reservation_repository = CrmDealRepairControlRepository(reservation_graph)
        reservation = reservation_repository.prepare_publication(
            control_instance_id,
            f"{generation_id}:pending:{occurrence}",
        )
        payload = TypeAdapter(dict[str, JsonValue]).validate_json(manifest_json)
        manifest = _manifest_from_payload(payload)
        categories = tuple(get_ingestion_config().bitrix_openlines.included_crm_category_ids)
        executable = manifest.executable_entries
        refresh_deals = any(
            entry.stream_key == "crm_deals" and entry.replay_mode != "fixed_keyset"
            for entry in executable
        )
        refresh_activities = any(
            entry.stream_key == "crm_activities" and entry.replay_mode != "fixed_keyset"
            for entry in executable
        )
        upper_deal_id = None
        upper_activity_id = None
        if refresh_deals or refresh_activities:
            source = create_bitrix_known_owner_client()
            try:
                if refresh_deals:
                    upper_deal_id = freeze_deal_upper_id(source, categories)
                if refresh_activities:
                    upper_activity_id = freeze_activity_upper_id(source)
            finally:
                source.close()
        entries = []
        windows: list[dict[str, JsonValue]] = []
        for entry in executable:
            window = dict(entry.source_window or {})
            if entry.stream_key == "crm_deals" and refresh_deals:
                assert upper_deal_id is not None
                window["upper_deal_id"] = upper_deal_id
                window["owner_artifact_id"] = None
            elif entry.stream_key == "crm_activities" and refresh_activities:
                assert upper_activity_id is not None
                window["upper_activity_id"] = upper_activity_id
                window["owner_artifact_id"] = None
            entries.append(replace(entry, source_window=window))
            windows.append(window)
        encoded = json.dumps(
            {"occurrence": occurrence, "windows": windows}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        boundary_digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        return dispatch_generation_canvas(
            generation_id=generation_id,
            boundary_digest=boundary_digest,
            configuration_digest=configuration_digest,
            entries=tuple(entries),
            task_kind="live",
            occurrence=occurrence,
            scheduled_dispatch=True,
            control_instance_id=control_instance_id,
            publication_reservation=reservation,
            publication_gate=reservation_repository,
        )
    finally:
        reservation_graph.close()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="src.scheduled_ingestion_tasks.dispatch_ingestion_group_task",
    bind=True,
    max_retries=0,
)
def dispatch_ingestion_group_task(
    self: Task,
    group_key: str,
    incremental: bool = False,
) -> ScheduledGroupDispatchSummary:
    """Publish one ordered API chain, with cron runs deduplicated by UTC day."""
    if not get_ingestion_config().scheduled_ingestion.enabled:
        logger.info(
            "Skipped scheduled ingestion group=%s incremental=%s because scheduling is disabled",
            group_key,
            incremental,
        )
        return {
            "status": "disabled",
            "group_key": group_key,
            "incremental": incremental,
            "workflow_task_id": "",
        }
    group = scheduled_ingestion_group(group_key)
    task_id = str(self.request.id or "manual")
    marker_key = _marker_key(group.key, incremental, task_id)
    claimed, existing = _claim_dispatch(marker_key, task_id)
    if not claimed:
        return {
            "status": "already_queued",
            "group_key": group.key,
            "incremental": incremental,
            "workflow_task_id": existing or task_id,
        }
    try:
        split_workflow_id = (
            _dispatch_active_bitrix_successor(_utc_occurrence_date())
            if group.key == "bitrix_chat"
            else None
        )
        if split_workflow_id is None:
            if group.key == "bitrix_chat":
                admit_configured_bitrix_control(
                    get_settings(),
                    LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
                )
            workflow = chain(
                *(
                    _signature(
                        spec.source_key,
                        spec.entity_key,
                        incremental and spec.supports_incremental,
                        f"{marker_key}:step:{index}",
                    )
                    for index, spec in enumerate(group.tasks)
                )
            )
            result = workflow.apply_async(queue=INGESTION_QUEUE)
            workflow_task_id = str(result.id)
        else:
            workflow_task_id = split_workflow_id
    except Exception:
        _release_claim(marker_key, task_id)
        raise
    # Replace the reservation with the workflow ID while retaining the same TTL.
    with redis.Redis.from_url(get_settings().celery_broker_url, decode_responses=True) as client:
        client.set(marker_key, workflow_task_id, xx=True, ex=_MARKER_TTL_SECONDS)
    logger.info(
        "Queued scheduled ingestion group=%s incremental=%s workflow=%s",
        group.key,
        incremental,
        workflow_task_id,
    )
    return {
        "status": "queued",
        "group_key": group.key,
        "incremental": incremental,
        "workflow_task_id": workflow_task_id,
    }
