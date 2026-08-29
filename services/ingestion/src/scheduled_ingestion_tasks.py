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
from src.graph.crm_deal_identity_repair_publication import (
    CrmDealRepairPublicationRepository,
    RepairPublicationReservation,
)
from src.graph.queries.bitrix_backfill import GET_ACTIVE_BITRIX_SUCCESSOR_SCHEDULE
from src.graph.queries.crm_deal_identity_repair_ledger import GET_REPAIR_DISPATCH_BLOCK
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


def _read_dispatch_marker(marker_key: str) -> str | None:
    """Read a Redis idempotency marker without making publication authoritative."""
    with redis.Redis.from_url(get_settings().celery_broker_url, decode_responses=True) as client:
        return client.get(marker_key)


def _claim_dispatch(marker_key: str, task_id: str) -> tuple[bool, str | None]:
    """Claim a dispatch key; an existing placeholder is never republished."""
    with redis.Redis.from_url(get_settings().celery_broker_url, decode_responses=True) as client:
        while True:
            if client.set(marker_key, task_id, nx=True, ex=_MARKER_TTL_SECONDS):
                return True, None
            prior = client.get(marker_key)
            if prior is not None:
                return False, prior


def _record_dispatch_marker(marker_key: str, workflow_task_id: str) -> None:
    """Record an already-authorized workflow ID while retaining the marker TTL."""
    with redis.Redis.from_url(get_settings().celery_broker_url, decode_responses=True) as client:
        client.set(marker_key, workflow_task_id, xx=True, ex=_MARKER_TTL_SECONDS)


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


def _repair_dispatch_blocked(control_instance_id: str) -> bool:
    """Fail closed before source-window freezing or canvas publication."""
    graph = Neo4jClient(get_settings())
    try:
        def _read(tx: ManagedTransaction) -> bool:
            return tx.run(
                GET_REPAIR_DISPATCH_BLOCK,
                control_instance_id=control_instance_id,
            ).single() is not None

        return graph.execute_read(_read)
    finally:
        graph.close()


def _dispatch_active_bitrix_successor(occurrence: str) -> str | None:
    """Publish one fresh bounded split cadence when cutover is active."""
    from src.bitrix_backfill_control import _manifest_from_payload
    from src.bitrix_backfill_tasks import (
        dispatch_generation_canvas,
        reserve_generation_publication,
    )
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
    if _repair_dispatch_blocked(control_instance_id):
        raise RuntimeError("Bitrix successor publication is blocked by CRM repair quiescence")
    admit_configured_bitrix_control(get_settings(), control_instance_id)
    payload = TypeAdapter(dict[str, JsonValue]).validate_json(manifest_json)
    manifest = _manifest_from_payload(payload)
    categories = tuple(get_ingestion_config().bitrix_openlines.included_crm_category_ids)
    executable = manifest.executable_entries
    # This admission happens before any source-window probe.  Its identity is stable over
    # the cadence occurrence and manifest routing, while the later frozen-window digest is
    # retained in the actual task payload.
    reservation = reserve_generation_publication(
        generation_id=generation_id,
        boundary_digest=f"preflight:{configuration_digest}",
        configuration_digest=configuration_digest,
        entries=executable,
        task_kind="live",
        occurrence=occurrence,
        resume_generation=None,
        control_instance_id=control_instance_id,
    )
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
        {"occurrence": occurrence, "windows": windows},
        sort_keys=True,
        separators=(",", ":"),
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
        reservation=reservation,
    )


def _legacy_reservation_identity(marker_key: str) -> tuple[str, str]:
    routing_digest = "sha256:" + hashlib.sha256(
        f"legacy-bitrix:{marker_key}".encode("utf-8")
    ).hexdigest()
    return routing_digest, f"legacy:{marker_key}"


def _reserve_legacy_bitrix_publication(
    *, control_instance_id: str, marker_key: str, task_id: str
) -> RepairPublicationReservation:
    """Reserve the stable marker identity before mutating the Redis marker."""
    del task_id
    routing_digest, occurrence_identity = _legacy_reservation_identity(marker_key)
    graph = Neo4jClient(get_settings())
    try:
        return CrmDealRepairPublicationRepository(graph, control_instance_id).reserve(
            stream_scope="crm_deals,crm_activities,openlines_conversations",
            routing_identity_digest=routing_digest,
            occurrence_generation_identity=occurrence_identity,
        )
    finally:
        graph.close()


def _read_legacy_bitrix_publication(
    *, control_instance_id: str, marker_key: str
) -> RepairPublicationReservation | None:
    """Find an existing legacy reservation without manufacturing an uncertain row."""
    routing_digest, occurrence_identity = _legacy_reservation_identity(marker_key)
    graph = Neo4jClient(get_settings())
    try:
        return CrmDealRepairPublicationRepository(graph, control_instance_id).get_by_identity(
            stream_scope="crm_deals,crm_activities,openlines_conversations",
            routing_identity_digest=routing_digest,
            occurrence_generation_identity=occurrence_identity,
        )
    finally:
        graph.close()


def _reconcile_legacy_marker(
    reservation: RepairPublicationReservation,
    marker_value: str,
) -> str:
    """Accept only an exact published ID or an explicitly uncertain placeholder."""
    if reservation.status == "published":
        if reservation.publication_id != marker_value:
            raise RuntimeError("legacy Redis marker disagrees with the published reservation")
        return marker_value
    if reservation.status in {"pending", "publishing"}:
        if reservation.publication_id is not None:
            raise RuntimeError("unpublished legacy reservation has a workflow ID")
        return marker_value
    raise RuntimeError("legacy publication reservation has an invalid status")


def _begin_legacy_bitrix_publication(reservation: RepairPublicationReservation) -> None:
    graph = Neo4jClient(get_settings())
    try:
        CrmDealRepairPublicationRepository(graph, reservation.control_instance_id).begin_publishing(
            reservation
        )
    finally:
        graph.close()


def _publish_legacy_bitrix_publication(
    reservation: RepairPublicationReservation, workflow_task_id: str
) -> None:
    graph = Neo4jClient(get_settings())
    try:
        CrmDealRepairPublicationRepository(graph, reservation.control_instance_id).mark_published(
            reservation, workflow_task_id
        )
    finally:
        graph.close()


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
    if group.key == "bitrix_chat" and _repair_dispatch_blocked(LEGACY_DEFAULT_CONTROL_INSTANCE_ID):
        logger.warning("Skipped Bitrix scheduled publication because repair dispatch is blocked")
        return {
            "status": "repair_blocked",
            "group_key": group.key,
            "incremental": incremental,
            "workflow_task_id": "",
        }
    task_id = str(self.request.id or "manual")
    marker_key = _marker_key(group.key, incremental, task_id)
    reservation: RepairPublicationReservation | None = None
    if group.key == "bitrix_chat":
        # A marker read is non-authoritative. It prevents a pre-rollout marker from
        # creating a new fail-closed reservation when no corresponding row exists.
        existing_marker = _read_dispatch_marker(marker_key)
        if existing_marker is not None:
            existing_reservation = _read_legacy_bitrix_publication(
                control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
                marker_key=marker_key,
            )
            if existing_reservation is None:
                return {
                    "status": "already_queued",
                    "group_key": group.key,
                    "incremental": incremental,
                    "workflow_task_id": existing_marker,
                }
            return {
                "status": "already_queued",
                "group_key": group.key,
                "incremental": incremental,
                "workflow_task_id": _reconcile_legacy_marker(
                    existing_reservation,
                    existing_marker,
                ),
            }
        reservation = _reserve_legacy_bitrix_publication(
            control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
            marker_key=marker_key,
            task_id=task_id,
        )
    claimed, existing = _claim_dispatch(marker_key, task_id)
    if not claimed:
        if reservation is None:
            return {
                "status": "already_queued",
                "group_key": group.key,
                "incremental": incremental,
                "workflow_task_id": existing or task_id,
            }
        if existing is None:
            raise RuntimeError("legacy marker claim did not return its existing value")
        return {
            "status": "already_queued",
            "group_key": group.key,
            "incremental": incremental,
            "workflow_task_id": _reconcile_legacy_marker(reservation, existing),
        }
    if reservation is not None and reservation.status == "published":
        if reservation.publication_id is None:
            raise RuntimeError("published legacy reservation is missing its workflow ID")
        _record_dispatch_marker(marker_key, reservation.publication_id)
        return {
            "status": "already_queued",
            "group_key": group.key,
            "incremental": incremental,
            "workflow_task_id": reservation.publication_id,
        }
    if reservation is not None and reservation.status == "publishing":
        return {
            "status": "already_queued",
            "group_key": group.key,
            "incremental": incremental,
            "workflow_task_id": task_id,
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
            if reservation is not None:
                _begin_legacy_bitrix_publication(reservation)
            result = workflow.apply_async(queue=INGESTION_QUEUE)
            workflow_task_id = str(result.id)
            if reservation is not None:
                _publish_legacy_bitrix_publication(reservation, workflow_task_id)
        else:
            workflow_task_id = split_workflow_id
            if reservation is not None:
                _begin_legacy_bitrix_publication(reservation)
                _publish_legacy_bitrix_publication(reservation, workflow_task_id)
    except Exception:
        _release_claim(marker_key, task_id)
        raise
    _record_dispatch_marker(marker_key, workflow_task_id)

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
