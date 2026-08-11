"""Stable Celery canvas construction for corrective and successor Bitrix generations."""

from __future__ import annotations

from collections.abc import Sequence

from celery import chain
from celery.canvas import Signature

from src.bitrix_backfill_models import BackfillInventoryEntry
from src.bitrix_ingestion_models import BitrixStreamKey
from src.celery_app import INGESTION_QUEUE, celery_app


def corrective_task_id(
    generation_id: str,
    stream_key: BitrixStreamKey,
    boundary_digest: str,
    configuration_digest: str,
    *,
    resume_generation: int | None = None,
) -> str:
    base = f"bitrix-backfill:{generation_id}:{stream_key}:{boundary_digest}:{configuration_digest}"
    return base if resume_generation is None else f"{base}:resume:{resume_generation}"


def live_task_id(
    occurrence: str,
    stream_key: BitrixStreamKey,
    configuration_digest: str,
) -> str:
    return f"bitrix-live:{occurrence}:{stream_key}:{configuration_digest}"


def build_generation_canvas(
    *,
    generation_id: str,
    boundary_digest: str,
    configuration_digest: str,
    entries: Sequence[BackfillInventoryEntry],
    task_kind: str = "corrective",
    occurrence: str | None = None,
    resume_generation: int | None = None,
) -> Signature:
    """Build a strict deals -> activities -> optional Open Lines chain."""
    executable = [entry for entry in entries if entry.executes]
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
            )
            idempotency_key = corrective_task_id(
                generation_id,
                entry.stream_key,
                boundary_digest,
                configuration_digest,
            )
        elif task_kind == "live" and occurrence is not None:
            task_id = live_task_id(occurrence, entry.stream_key, configuration_digest)
            idempotency_key = task_id
        else:
            raise ValueError("live generation canvases require a UTC occurrence")
        signatures.append(
            celery_app.signature(
                "src.tasks.run_ingestion_task",
                kwargs={
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
                },
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
) -> str:
    canvas = build_generation_canvas(
        generation_id=generation_id,
        boundary_digest=boundary_digest,
        configuration_digest=configuration_digest,
        entries=entries,
        task_kind=task_kind,
        occurrence=occurrence,
        resume_generation=resume_generation,
    )
    result = canvas.apply_async()
    return str(result.id)
