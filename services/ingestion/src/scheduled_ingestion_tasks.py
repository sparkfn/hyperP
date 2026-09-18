"""Durable one-child-at-a-time publication for scheduled ingestion groups."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal, Protocol, TypedDict

from celery import Task
from celery.canvas import Signature
from neo4j import ManagedTransaction
from pydantic import TypeAdapter

from src.bitrix_ingestion_models import CRM_ACTIVITY_INGESTION_RETIRED_REASON
from src.bounded_ingestion_models import BoundedMode, OccurrenceContext
from src.bounded_ingestion_window import occurrence_to_payload
from src.celery_app import INGESTION_QUEUE, LIFECYCLE_QUEUE, celery_app
from src.config import get_settings
from src.graph.bitrix_source_instances import admit_configured_bitrix_control
from src.graph.client import Neo4jClient
from src.graph.queries.bitrix_backfill import GET_ACTIVE_BITRIX_SUCCESSOR_SCHEDULE
from src.ingestion_config import get_ingestion_config
from src.models import JsonValue
from src.scheduled_ingestion_control import (
    ScheduledChildContext,
    ScheduledIngestionControl,
    ScheduledMaintenanceContext,
)
from src.scheduled_ingestion_groups import (
    ScheduledIngestionGroup,
    ScheduledIngestionSpec,
    scheduled_ingestion_group,
    scheduled_ingestion_group_for_weekday,
)
from src.scheduled_ingestion_policy import (
    ScheduledOccurrence,
    occurrence_hour_bucket,
    scheduled_group_for_weekday,
    weekly_occurrence,
)
from src.source_instances import LEGACY_DEFAULT_CONTROL_INSTANCE_ID

logger = logging.getLogger(__name__)

MaintenanceKind = Literal["lifecycle", "knows"]


class ScheduledGroupDispatchSummary(TypedDict):
    """A durable scheduler-drive outcome rather than a Celery-chain result."""

    status: str
    group_key: str
    incremental: bool
    workflow_task_id: str


@dataclass(frozen=True)
class SchedulerBlocked:
    """A redacted, durable-safe reason for withholding publication."""

    reason: str


class SchedulerReadiness(Protocol):
    """Boundary for accepted seed and source capability evidence owned elsewhere."""

    def resolve_child(
        self,
        *,
        group: ScheduledIngestionGroup,
        spec: ScheduledIngestionSpec,
        mode: BoundedMode,
        environment: str,
        reset_generation: int,
        occurrence: OccurrenceContext,
    ) -> ScheduledChildContext | SchedulerBlocked: ...

    def resolve_maintenance(
        self,
        *,
        environment: str,
        reset_generation: int,
        occurrence: OccurrenceContext,
    ) -> ScheduledMaintenanceContext | SchedulerBlocked: ...


class FailClosedSchedulerReadiness:
    """Default gate until #432-#438 provide verifiable bounded evidence readers."""

    def resolve_child(
        self,
        *,
        group: ScheduledIngestionGroup,
        spec: ScheduledIngestionSpec,
        mode: BoundedMode,
        environment: str,
        reset_generation: int,
        occurrence: OccurrenceContext,
    ) -> ScheduledChildContext | SchedulerBlocked:
        del group, spec, mode, environment, reset_generation, occurrence
        return SchedulerBlocked("accepted_seed_evidence_unavailable")

    def resolve_maintenance(
        self,
        *,
        environment: str,
        reset_generation: int,
        occurrence: OccurrenceContext,
    ) -> ScheduledMaintenanceContext | SchedulerBlocked:
        del environment, reset_generation, occurrence
        return SchedulerBlocked("accepted_seed_evidence_unavailable")


def build_scheduler_readiness() -> SchedulerReadiness:
    """Return the conservative readiness boundary; source owners replace it later."""
    return FailClosedSchedulerReadiness()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _dispatch_active_bitrix_successor(occurrence: str) -> str | None:
    """Publish one fresh bounded split cadence when cutover is active."""
    from src.bitrix_backfill_control import _manifest_from_payload
    from src.bitrix_backfill_tasks import dispatch_generation_canvas
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
        if any(
            entry.stream_key == "crm_activities" and entry.executes for entry in manifest.entries
        ):
            logger.warning(
                "Omitted historical Bitrix activity stream from successor publication "
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


def _occurrence(group: ScheduledIngestionGroup, now: datetime) -> ScheduledOccurrence:
    return weekly_occurrence(
        group_key=group.key,
        weekday=group.weekday,
        now=now,
        drain_reserve_seconds=get_ingestion_config().bounded_ingestion.drain_reserve_seconds,
    )


def _child_signature(
    child: ScheduledChildContext,
    occurrence: OccurrenceContext,
    publication_id: str,
    reset_generation: int,
) -> Signature:
    """Build the complete #430 bounded payload; no legacy fallback is possible."""
    return celery_app.signature(
        "src.tasks.run_ingestion_task",
        args=(child.source_key, "api"),
        kwargs={
            "entity_key": child.entity_key,
            "incremental": child.mode == "delta",
            "wait_for_source": True,
            "require_clean_completion": True,
            "idempotency_key": publication_id,
            "scheduled_dispatch": True,
            "bounded_environment": get_settings().deployment_environment,
            "bounded_reset_generation": reset_generation,
            "bounded_mode": child.mode,
            "bounded_configuration_fingerprint": child.configuration_fingerprint,
            "bounded_connector_version": child.connector_version,
            "bounded_configuration_version": child.configuration_version,
            "bounded_checkpoint_schema_version": child.checkpoint_schema_version,
            "bounded_source_window": child.source_window,
            "bounded_occurrence": occurrence_to_payload(occurrence),
            "bounded_stream_key": child.stream_key,
            "control_instance_id": child.control_instance_id,
        },
        immutable=True,
        queue=INGESTION_QUEUE,
    )


def _reconcile_signature(group_key: str, incremental: bool) -> Signature:
    return celery_app.signature(
        "src.scheduled_ingestion_tasks.reconcile_ingestion_group_task",
        args=(group_key,),
        kwargs={"incremental": incremental},
        immutable=True,
        queue=INGESTION_QUEUE,
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


def _record_disabled_group(
    group: ScheduledIngestionGroup,
    incremental: bool,
    now: datetime,
) -> ScheduledGroupDispatchSummary:
    """Persist a disabled latch for an existing/new durable workflow safely."""
    occurrence = _occurrence(group, now)
    settings = get_settings()
    graph = Neo4jClient(settings)
    try:
        control = ScheduledIngestionControl(graph)
        reset_generation = control.active_reset_generation(settings.deployment_environment)
        if reset_generation is None:
            return _summary("disabled", group.key, incremental)
        workflow = control.ensure_workflow(
            environment=settings.deployment_environment,
            reset_generation=reset_generation,
            group_key=group.key,
            control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
            child_keys=tuple(spec.child_key for spec in group.tasks),
            occurrence=occurrence.context,
            now=now,
        )
        if workflow is not None:
            control.block_workflow(
                environment=settings.deployment_environment,
                reset_generation=reset_generation,
                group_key=group.key,
                control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
                reason="disabled",
                next_eligible_at=occurrence.context.next_eligible_at,
            )
        workflow_id = workflow.workflow_id if workflow is not None else ""
        return _summary("disabled", group.key, incremental, workflow_id)
    finally:
        graph.close()


def _drive_group(group_key: str, incremental: bool, now: datetime) -> ScheduledGroupDispatchSummary:
    group = scheduled_ingestion_group(group_key)
    if not get_ingestion_config().scheduled_ingestion.enabled:
        return _record_disabled_group(group, incremental, now)

    occurrence = _occurrence(group, now)
    settings = get_settings()
    graph = Neo4jClient(settings)
    try:
        control = ScheduledIngestionControl(graph)
        reset_generation = control.active_reset_generation(settings.deployment_environment)
        if reset_generation is None:
            return _summary("blocked_reset_generation", group.key, incremental)
        workflow = control.ensure_workflow(
            environment=settings.deployment_environment,
            reset_generation=reset_generation,
            group_key=group.key,
            control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
            child_keys=tuple(spec.child_key for spec in group.tasks),
            occurrence=occurrence.context,
            now=now,
        )
        if workflow is None:
            return _summary("blocked_workflow", group.key, incremental)
        if workflow.manual_pause:
            return _summary("manual_pause", group.key, incremental, workflow.workflow_id)
        if occurrence.eligibility != "open":
            if occurrence.eligibility == "closed":
                control.block_workflow(
                    environment=settings.deployment_environment,
                    reset_generation=reset_generation,
                    group_key=group.key,
                    control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
                    reason="schedule_window_closed",
                    next_eligible_at=occurrence.context.next_eligible_at,
                )
            return _summary(
                f"window_{occurrence.eligibility}",
                group.key,
                incremental,
                workflow.workflow_id,
            )
        if group.key == "bitrix_chat":
            successor = _dispatch_active_bitrix_successor(now.date().isoformat())
            if successor is not None:
                # An operator-activated successor generation owns this cadence, so
                # the legacy bounded child is withheld rather than double-published.
                control.block_workflow(
                    environment=settings.deployment_environment,
                    reset_generation=reset_generation,
                    group_key=group.key,
                    control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
                    reason="bitrix_successor_schedule_active",
                    next_eligible_at=occurrence.context.next_eligible_at,
                )
                return _summary("queued", group.key, incremental, successor)

        progress = control.reconcile_current_child(
            environment=settings.deployment_environment,
            reset_generation=reset_generation,
            group_key=group.key,
            control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
        )
        if progress is not None and progress.status == "completed":
            return _summary("completed", group.key, incremental, workflow.workflow_id)
        index = (
            progress.current_child_index if progress is not None else workflow.current_child_index
        )
        if index >= len(group.tasks):
            return _summary("completed", group.key, incremental, workflow.workflow_id)

        spec = group.tasks[index]
        mode = spec.mode_for(incremental)
        ready = build_scheduler_readiness().resolve_child(
            group=group,
            spec=spec,
            mode=mode,
            environment=settings.deployment_environment,
            reset_generation=reset_generation,
            occurrence=occurrence.context,
        )
        if isinstance(ready, SchedulerBlocked):
            control.block_workflow(
                environment=settings.deployment_environment,
                reset_generation=reset_generation,
                group_key=group.key,
                control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
                reason=ready.reason,
                next_eligible_at=occurrence.context.next_eligible_at,
            )
            return _summary("blocked_readiness", group.key, incremental, workflow.workflow_id)
        if ready.control_instance_id != LEGACY_DEFAULT_CONTROL_INSTANCE_ID:
            control.block_workflow(
                environment=settings.deployment_environment,
                reset_generation=reset_generation,
                group_key=group.key,
                control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
                reason="group_control_instance_mismatch",
                next_eligible_at=occurrence.context.next_eligible_at,
            )
            return _summary(
                "blocked_control_instance",
                group.key,
                incremental,
                workflow.workflow_id,
            )
        publication = control.claim_current_child(
            environment=settings.deployment_environment,
            reset_generation=reset_generation,
            group_key=group.key,
            occurrence=occurrence.context,
            now=now,
            child_index=index,
            child=ready,
            budget=get_ingestion_config().bounded_ingestion,
        )
        if publication is None:
            # Coalesced (duplicate tick, in-flight intent), budget-exhausted, or
            # awaiting durable completion of an accepted intent. All three yield
            # to the group's next same-weekday occurrence with a durable reason.
            control.block_workflow(
                environment=settings.deployment_environment,
                reset_generation=reset_generation,
                group_key=group.key,
                control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
                reason="child_publication_deferred",
                next_eligible_at=occurrence.context.next_eligible_at,
            )
            return _summary("coalesced", group.key, incremental, workflow.workflow_id)
        signature = _child_signature(
            ready,
            occurrence.context,
            publication.publication_id,
            reset_generation,
        )
        # Only a durable clean completion advances the group. A failed, paused,
        # or budget-yielded child stays unfinished and waits for the group's
        # next same-weekday occurrence instead of looping inside the window.
        signature.apply_async(
            task_id=publication.publication_id,
            link=_reconcile_signature(group.key, incremental),
        )
        control.confirm_child_publication(
            environment=settings.deployment_environment,
            reset_generation=reset_generation,
            group_key=group.key,
            control_instance_id=ready.control_instance_id,
            publication_id=publication.publication_id,
        )
        return _summary("published", group.key, incremental, publication.publication_id)
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
    """Coalesce one Beat/manual tick into the durable group workflow."""
    del self
    return _drive_group(group_key, incremental, _utc_now())


@celery_app.task(  # type: ignore[untyped-decorator]
    name="src.scheduled_ingestion_tasks.reconcile_ingestion_group_task",
    bind=True,
    max_retries=0,
)
def reconcile_ingestion_group_task(
    self: Task,
    group_key: str,
    incremental: bool = True,
) -> ScheduledGroupDispatchSummary:
    """Wake after a child result, trusting graph completion rather than Celery state."""
    del self
    return _drive_group(group_key, incremental, _utc_now())


def _publish_maintenance(
    kind: MaintenanceKind,
    phase: str | None,
    obligation_id: str,
    occurrence: OccurrenceContext,
) -> None:
    """Publish one bounded maintenance delivery under the obligation's identity."""
    kwargs: dict[str, object] = {
        "bounded_occurrence": occurrence_to_payload(occurrence),
        "bounded_logical_run_id": obligation_id,
    }
    if kind == "lifecycle":
        from src.tasks import reconcile_lifecycle_task

        reconcile_lifecycle_task.apply_async(
            kwargs=kwargs,
            task_id=obligation_id,
            queue=LIFECYCLE_QUEUE,
        )
        return
    from src.tasks import materialize_knows_task

    materialize_knows_task.apply_async(
        args=(phase,),
        kwargs=kwargs,
        task_id=obligation_id,
        queue=LIFECYCLE_QUEUE,
    )


@celery_app.task(  # type: ignore[untyped-decorator]
    name="src.scheduled_ingestion_tasks.dispatch_scheduled_maintenance_task",
    bind=True,
    max_retries=0,
)
def dispatch_scheduled_maintenance_task(
    self: Task,
    kind: MaintenanceKind,
    phase: str | None = None,
) -> str:
    """Publish one hourly maintenance obligation only through scheduler authority.

    ``src.tasks`` still owns after-ingestion and continuation call sites. Those
    paths cannot be made scheduler-authoritative in this issue without the
    explicitly excluded core hook, so this task is deliberately a narrow safe
    entry point for Beat-owned maintenance only.
    """
    del self
    if kind not in {"lifecycle", "knows"}:
        raise ValueError("unknown scheduled maintenance kind")
    if kind == "knows" and phase not in {"contacts", "chat_relationships"}:
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
    bucket = occurrence_hour_bucket(now)
    settings = get_settings()
    graph = Neo4jClient(settings)
    try:
        control = ScheduledIngestionControl(graph)
        reset_generation = control.active_reset_generation(settings.deployment_environment)
        if reset_generation is None:
            return "blocked_reset_generation"
        readiness = build_scheduler_readiness().resolve_maintenance(
            environment=settings.deployment_environment,
            reset_generation=reset_generation,
            occurrence=occurrence.context,
        )
        if isinstance(readiness, SchedulerBlocked):
            return f"blocked_{readiness.reason}"
        obligation = control.claim_maintenance_obligation(
            environment=settings.deployment_environment,
            reset_generation=reset_generation,
            kind=kind,
            phase=phase,
            occurrence=occurrence.context,
            bucket=bucket,
            context=readiness,
            budget=get_ingestion_config().bounded_ingestion,
        )
        if obligation is None:
            return "coalesced"
        _publish_maintenance(kind, phase, obligation.obligation_id, occurrence.context)
        control.confirm_maintenance_obligation(
            environment=settings.deployment_environment,
            reset_generation=reset_generation,
            kind=kind,
            phase=phase,
            occurrence=occurrence.context,
            bucket=bucket,
        )
        return "published"
    finally:
        graph.close()
