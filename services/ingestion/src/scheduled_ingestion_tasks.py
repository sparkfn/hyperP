"""Scheduled ingestion dispatch for weekly source groups."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal, TypedDict

from celery import Task
from neo4j import ManagedTransaction
from pydantic import TypeAdapter

from src.bitrix_ingestion_models import CRM_ACTIVITY_INGESTION_RETIRED_REASON
from src.celery_app import INGESTION_QUEUE, LIFECYCLE_QUEUE, celery_app
from src.config import get_settings
from src.graph.bitrix_source_instances import admit_configured_bitrix_control
from src.graph.client import Neo4jClient
from src.graph.queries.bitrix_backfill import GET_ACTIVE_BITRIX_SUCCESSOR_SCHEDULE
from src.ingestion_config import get_ingestion_config
from src.models import JsonValue
from src.scheduled_ingestion_groups import (
    ScheduledIngestionGroup,
    scheduled_ingestion_group,
    scheduled_ingestion_group_for_weekday,
)
from src.scheduled_ingestion_policy import (
    ScheduledOccurrence,
    scheduled_group_for_weekday,
    weekly_occurrence,
)
from src.source_instances import LEGACY_DEFAULT_CONTROL_INSTANCE_ID

logger = logging.getLogger(__name__)

MaintenanceKind = Literal["lifecycle", "knows"]

_DRAIN_RESERVE_SECONDS: float = 300.0


class ScheduledGroupDispatchSummary(TypedDict):
    """Outcome of one scheduled group dispatch tick."""

    status: str
    group_key: str
    incremental: bool
    workflow_task_id: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _dispatch_active_bitrix_successor(occurrence: str) -> str | None:
    """Publish one fresh split cadence when cutover is active."""
    from src.bitrix_backfill_control import _manifest_from_payload
    from src.bitrix_backfill_tasks import dispatch_generation_canvas
    from src.connectors.bitrix_stage_history.deal_probe import (
        freeze_deal_upper_id,
    )
    from src.main import create_bitrix_known_owner_client

    graph = Neo4jClient(get_settings())
    try:

        def _read(
            tx: ManagedTransaction,
        ) -> tuple[str, str, str, str] | None:
            record = tx.run(
                GET_ACTIVE_BITRIX_SUCCESSOR_SCHEDULE,
                control_instance_id=(LEGACY_DEFAULT_CONTROL_INSTANCE_ID),
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
    (
        generation_id,
        configuration_digest,
        manifest_json,
        control_instance_id,
    ) = active
    admit_configured_bitrix_control(get_settings(), control_instance_id)
    from src.graph.crm_deal_identity_repair_control import (
        CrmDealRepairControlRepository,
    )

    reservation_graph = Neo4jClient(get_settings())
    try:
        reservation_repository = CrmDealRepairControlRepository(reservation_graph)
        reservation = reservation_repository.prepare_publication(
            control_instance_id,
            f"{generation_id}:pending:{occurrence}",
        )
        payload = TypeAdapter(dict[str, JsonValue]).validate_json(manifest_json)
        manifest = _manifest_from_payload(payload)
        if any(
            entry.stream_key == "crm_activities" and entry.executes for entry in manifest.entries
        ):
            logger.warning(
                "Omitted historical Bitrix activity stream from "
                "successor publication "
                "disposition=retired reason=%s",
                CRM_ACTIVITY_INGESTION_RETIRED_REASON,
            )
        categories = tuple(get_ingestion_config().bitrix_openlines.included_crm_category_ids)
        executable = manifest.operational_entries
        refresh_deals = any(
            entry.stream_key == "crm_deals" and entry.replay_mode != "fixed_keyset"
            for entry in executable
        )
        upper_deal_id = None
        if refresh_deals:
            source = create_bitrix_known_owner_client()
            try:
                if refresh_deals:
                    upper_deal_id = freeze_deal_upper_id(source, categories)
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
            publication_reservation=reservation,
            publication_gate=reservation_repository,
        )
    finally:
        reservation_graph.close()


def _occurrence(group: ScheduledIngestionGroup, now: datetime) -> ScheduledOccurrence:
    return weekly_occurrence(
        group_key=group.key,
        weekday=group.weekday,
        now=now,
        drain_reserve_seconds=_DRAIN_RESERVE_SECONDS,
    )


def _summary(
    status: str,
    group_key: str,
    incremental: bool,
    workflow_task_id: str = "",
) -> ScheduledGroupDispatchSummary:
    return {
        "status": status,
        "group_key": group_key,
        "incremental": incremental,
        "workflow_task_id": workflow_task_id,
    }


def _drive_group(group_key: str, incremental: bool, now: datetime) -> ScheduledGroupDispatchSummary:
    group = scheduled_ingestion_group(group_key)
    if not get_ingestion_config().scheduled_ingestion.enabled:
        return _summary("disabled", group.key, incremental)

    occurrence = _occurrence(group, now)
    if occurrence.eligibility != "open":
        return _summary(
            f"window_{occurrence.eligibility}",
            group.key,
            incremental,
        )
    if group.key == "bitrix_chat":
        successor = _dispatch_active_bitrix_successor(now.date().isoformat())
        if successor is not None:
            return _summary("queued", group.key, incremental, successor)

    from src.tasks import run_incremental_task

    for spec in group.tasks:
        try:
            run_incremental_task.apply_async(
                args=(spec.source_key,),
                kwargs=({"entity_key": spec.entity_key} if spec.entity_key is not None else {}),
                queue=INGESTION_QUEUE,
            )
        except Exception:
            logger.exception(
                "Failed to publish incremental task for %s",
                spec.source_key,
            )
    return _summary("published", group.key, incremental)


@celery_app.task(  # type: ignore[untyped-decorator]
    name=("src.scheduled_ingestion_tasks.dispatch_ingestion_group_task"),
    bind=True,
    max_retries=0,
)
def dispatch_ingestion_group_task(
    self: Task,
    group_key: str,
    incremental: bool = False,
) -> ScheduledGroupDispatchSummary:
    """Publish incremental tasks for a scheduled group."""
    del self
    return _drive_group(group_key, incremental, _utc_now())


@celery_app.task(  # type: ignore[untyped-decorator]
    name=("src.scheduled_ingestion_tasks.reconcile_ingestion_group_task"),
    bind=True,
    max_retries=0,
)
def reconcile_ingestion_group_task(
    self: Task,
    group_key: str,
    incremental: bool = True,
) -> ScheduledGroupDispatchSummary:
    """Re-drive after a child result."""
    del self
    return _drive_group(group_key, incremental, _utc_now())


@celery_app.task(  # type: ignore[untyped-decorator]
    name=("src.scheduled_ingestion_tasks.dispatch_scheduled_maintenance_task"),
    bind=True,
    max_retries=0,
)
def dispatch_scheduled_maintenance_task(
    self: Task,
    kind: MaintenanceKind,
    phase: str | None = None,
) -> str:
    """Publish one hourly maintenance task when scheduling is active."""
    del self
    if kind not in {"lifecycle", "knows"}:
        raise ValueError("unknown scheduled maintenance kind")
    if kind == "knows" and phase not in {
        "contacts",
        "chat_relationships",
    }:
        raise ValueError("KNOWS maintenance requires a supported phase")
    if kind == "lifecycle" and phase is not None:
        raise ValueError("lifecycle maintenance does not accept a phase")
    if not get_ingestion_config().scheduled_ingestion.enabled:
        return "disabled"
    now = _utc_now()
    weekday = scheduled_group_for_weekday(now)
    group = None if weekday is None else scheduled_ingestion_group_for_weekday(weekday)
    if group is None:
        return "window_closed"
    occurrence = _occurrence(group, now)
    if occurrence.eligibility != "open":
        return f"window_{occurrence.eligibility}"
    if kind == "lifecycle":
        from src.tasks import reconcile_lifecycle_task

        reconcile_lifecycle_task.apply_async(queue=LIFECYCLE_QUEUE)
        return "published"
    from src.tasks import materialize_knows_task

    materialize_knows_task.apply_async(args=(phase,), queue=LIFECYCLE_QUEUE)
    return "published"
