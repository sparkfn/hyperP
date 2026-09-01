"""Stable Celery canvas construction for corrective and successor Bitrix generations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from celery import chain
from celery.canvas import Signature

from src.bitrix_backfill_models import BackfillInventoryEntry
from src.bitrix_ingestion_models import BitrixStreamKey
from src.celery_app import INGESTION_QUEUE, celery_app
from src.crm_deal_identity_repair.control_models import RepairPublicationReservation
from src.source_instances import (
    LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
    effective_control_instance_id,
    scope_control_identity,
)


class PublicationReservationGate(Protocol):
    """Narrow durable reservation seam; keeps legacy canvas construction pure."""

    def mark_publishing(
        self, reservation: RepairPublicationReservation
    ) -> RepairPublicationReservation: ...

    def confirm_publication(
        self, reservation: RepairPublicationReservation, workflow_task_id: str
    ) -> RepairPublicationReservation: ...


def corrective_task_id(
    generation_id: str,
    stream_key: BitrixStreamKey,
    boundary_digest: str,
    configuration_digest: str,
    *,
    resume_generation: int | None = None,
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
) -> str:
    base = f"bitrix-backfill:{generation_id}:{stream_key}:{boundary_digest}:{configuration_digest}"
    base = base if resume_generation is None else f"{base}:resume:{resume_generation}"
    return scope_control_identity(base, control_instance_id)


def live_task_id(
    occurrence: str,
    stream_key: BitrixStreamKey,
    configuration_digest: str,
    *,
    resume_generation: int | None = None,
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
) -> str:
    base = f"bitrix-live:{occurrence}:{stream_key}:{configuration_digest}"
    base = base if resume_generation is None else f"{base}:resume:{resume_generation}"
    return scope_control_identity(base, control_instance_id)


def build_generation_canvas(
    *,
    generation_id: str,
    boundary_digest: str,
    configuration_digest: str,
    entries: Sequence[BackfillInventoryEntry],
    task_kind: str = "corrective",
    occurrence: str | None = None,
    resume_generation: int | None = None,
    scheduled_dispatch: bool = False,
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
) -> Signature:
    """Build a strict deals -> activities -> optional Open Lines chain."""
    control_instance_id = effective_control_instance_id(control_instance_id)
    executable = [entry for entry in entries if entry.executes]
    if any(entry.stream_key == "crm_stage_history" for entry in executable):
        raise ValueError("stage history cannot join a Bitrix backfill generation canvas")
    order = {"crm_deals": 0, "crm_activities": 1, "openlines_conversations": 2}
    executable.sort(key=lambda entry: order[entry.stream_key])
    streams = [entry.stream_key for entry in executable]
    valid_orders = (
        ["crm_deals"],
        ["crm_deals", "crm_activities"],
        ["crm_deals", "crm_activities", "openlines_conversations"],
    )
    if streams not in valid_orders:
        raise ValueError(
            "generation canvas requires deals first; activities may be reviewed-excluded"
        )
    if len(set(streams)) != len(streams):
        raise ValueError("generation canvas accepts one executable entry per stream")
    signatures: list[Signature] = []
    for entry in executable:
        assert entry.source_window is not None
        if task_kind == "corrective":
            task_id = corrective_task_id(
                generation_id,
                entry.stream_key,
                boundary_digest,
                configuration_digest,
                resume_generation=resume_generation,
                control_instance_id=control_instance_id,
            )
            idempotency_key = corrective_task_id(
                generation_id,
                entry.stream_key,
                boundary_digest,
                configuration_digest,
                control_instance_id=control_instance_id,
            )
        elif task_kind == "live" and occurrence is not None:
            task_id = live_task_id(
                occurrence,
                entry.stream_key,
                configuration_digest,
                resume_generation=resume_generation,
                control_instance_id=control_instance_id,
            )
            idempotency_key = live_task_id(
                occurrence,
                entry.stream_key,
                configuration_digest,
                control_instance_id=control_instance_id,
            )
        else:
            raise ValueError("live generation canvases require a UTC occurrence")
        signatures.append(
            celery_app.signature(
                "src.tasks.run_ingestion_task",
                kwargs=_task_kwargs(
                    task_kind=task_kind,
                    entry=entry,
                    idempotency_key=idempotency_key,
                    generation_id=generation_id,
                    boundary_digest=boundary_digest,
                    configuration_digest=configuration_digest,
                    scheduled_dispatch=scheduled_dispatch,
                    control_instance_id=control_instance_id,
                ),
                immutable=True,
                queue=INGESTION_QUEUE,
                task_id=task_id,
            )
        )
    signatures[0].set(countdown=2)
    return chain(*signatures)


def dispatch_generation_canvas(
    *,
    generation_id: str,
    boundary_digest: str,
    configuration_digest: str,
    entries: Sequence[BackfillInventoryEntry],
    task_kind: str = "corrective",
    occurrence: str | None = None,
    resume_generation: int | None = None,
    scheduled_dispatch: bool = False,
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
    publication_reservation: RepairPublicationReservation | None = None,
    publication_gate: PublicationReservationGate | None = None,
) -> str:
    control_instance_id = effective_control_instance_id(control_instance_id)
    if publication_reservation is None or publication_gate is None:
        raise ValueError("repair publication reservation and gate are required")
    reservation = publication_gate.mark_publishing(publication_reservation)
    canvas = build_generation_canvas(
        generation_id=generation_id,
        boundary_digest=boundary_digest,
        configuration_digest=configuration_digest,
        entries=entries,
        task_kind=task_kind,
        occurrence=occurrence,
        resume_generation=resume_generation,
        scheduled_dispatch=scheduled_dispatch,
        control_instance_id=control_instance_id,
    )
    from src.config import get_settings
    from src.graph.bitrix_source_instances import admit_configured_bitrix_control

    admit_configured_bitrix_control(get_settings(), control_instance_id)
    result = canvas.apply_async()
    workflow_task_id = str(result.id)
    publication_gate.confirm_publication(reservation, workflow_task_id)
    return workflow_task_id


def _task_kwargs(
    *,
    task_kind: str,
    entry: BackfillInventoryEntry,
    idempotency_key: str,
    generation_id: str,
    boundary_digest: str,
    configuration_digest: str,
    scheduled_dispatch: bool,
    control_instance_id: str,
) -> dict[str, object]:
    """Keep deployed legacy task payloads byte-for-byte unchanged."""
    payload: dict[str, object] = {
        "source_key": "bitrix_chat",
        "mode": "backfill" if task_kind == "corrective" else "api",
        "incremental": task_kind == "live",
        "idempotency_key": idempotency_key,
        "bitrix_execution_stream": entry.stream_key,
        "bitrix_generation_id": generation_id,
        "bitrix_boundary_digest": boundary_digest,
        "bitrix_configuration_digest": configuration_digest,
        "bitrix_source_window": entry.source_window,
        "bitrix_max_calls": entry.max_calls,
        "bitrix_max_rows": entry.max_rows,
        "bitrix_max_runtime_seconds": entry.max_runtime_seconds,
        "scheduled_dispatch": scheduled_dispatch,
    }
    if control_instance_id != LEGACY_DEFAULT_CONTROL_INSTANCE_ID:
        payload["control_instance_id"] = control_instance_id
    return payload
