"""Stable Celery canvas construction for corrective and successor Bitrix generations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from celery import chain
from celery.canvas import Signature

from src.bitrix_backfill_models import BackfillInventoryEntry
from src.bitrix_ingestion_models import BitrixStreamKey
from src.celery_app import INGESTION_QUEUE, celery_app
from src.graph.crm_deal_identity_repair_publication import (
    CrmDealRepairPublicationRepository,
    RepairPublicationReservation,
)
from src.source_instances import (
    LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
    effective_control_instance_id,
    scope_control_identity,
)


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
    reservation: RepairPublicationReservation | None = None,
) -> str:
    control_instance_id = effective_control_instance_id(control_instance_id)
    repair_scope = _repair_stream_scope(entries)
    if repair_scope:
        _assert_repair_dispatch_unblocked(control_instance_id)
        owned_reservation = reservation or reserve_generation_publication(
            generation_id=generation_id,
            boundary_digest=boundary_digest,
            configuration_digest=configuration_digest,
            entries=entries,
            task_kind=task_kind,
            occurrence=occurrence,
            resume_generation=resume_generation,
            control_instance_id=control_instance_id,
        )
        _assert_generation_reservation(
            owned_reservation,
            generation_id,
            configuration_digest,
            task_kind,
            occurrence,
            resume_generation,
            entries,
            control_instance_id,
        )
    else:
        if reservation is not None:
            raise ValueError("stage-history-only generation cannot carry a repair reservation")
        owned_reservation = None
    # Stage-history work is a separate artifact-replay cadence. It is neither
    # admitted nor fenced by #310, so a mixed caller contributes only repair-owned
    # entries to this generation canvas.
    canvas_entries = tuple(entry for entry in entries if entry.stream_key != "crm_stage_history")
    canvas = build_generation_canvas(
        generation_id=generation_id,
        boundary_digest=boundary_digest,
        configuration_digest=configuration_digest,
        entries=canvas_entries,
        task_kind=task_kind,
        occurrence=occurrence,
        resume_generation=resume_generation,
        scheduled_dispatch=scheduled_dispatch,
        control_instance_id=control_instance_id,
    )
    from src.config import get_settings
    from src.graph.bitrix_source_instances import admit_configured_bitrix_control

    admit_configured_bitrix_control(get_settings(), control_instance_id)
    if owned_reservation is not None:
        _begin_generation_publication(owned_reservation)
    result = canvas.apply_async()
    publication_id = str(result.id)
    if owned_reservation is not None:
        _mark_generation_published(owned_reservation, publication_id)
    return publication_id


def reserve_generation_publication(
    *,
    generation_id: str,
    boundary_digest: str,
    configuration_digest: str,
    entries: Sequence[BackfillInventoryEntry],
    task_kind: str,
    occurrence: str | None,
    resume_generation: int | None,
    control_instance_id: str,
) -> RepairPublicationReservation | None:
    """Reserve only the repair-owned stream subset; stage-history-only publication bypasses #310."""
    del boundary_digest  # Frozen source windows are an allowed post-reservation refinement.
    control_instance_id = effective_control_instance_id(control_instance_id)
    scope = _repair_stream_scope(entries)
    if not scope:
        return None
    routing_digest = _generation_routing_digest(
        generation_id,
        configuration_digest,
        task_kind,
        occurrence,
        resume_generation,
        scope,
    )
    identity = f"{generation_id}:{occurrence or 'corrective'}:{resume_generation or 0}"
    from src.config import get_settings
    from src.graph.client import Neo4jClient

    client = Neo4jClient(get_settings())
    try:
        return CrmDealRepairPublicationRepository(client, control_instance_id).reserve(
            stream_scope=scope,
            routing_identity_digest=routing_digest,
            occurrence_generation_identity=identity,
        )
    finally:
        client.close()


def _repair_stream_scope(entries: Sequence[BackfillInventoryEntry]) -> str:
    affected = sorted(
        entry.stream_key
        for entry in entries
        if entry.executes
        and entry.stream_key in {"crm_deals", "crm_activities", "openlines_conversations"}
    )
    return ",".join(affected)


def _generation_routing_digest(
    generation_id: str,
    configuration_digest: str,
    task_kind: str,
    occurrence: str | None,
    resume_generation: int | None,
    stream_scope: str,
) -> str:
    payload = {
        "generation_id": generation_id,
        "configuration_digest": configuration_digest,
        "task_kind": task_kind,
        "occurrence": occurrence,
        "resume_generation": resume_generation,
        "stream_scope": stream_scope,
    }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def _assert_generation_reservation(
    reservation: RepairPublicationReservation | None,
    generation_id: str,
    configuration_digest: str,
    task_kind: str,
    occurrence: str | None,
    resume_generation: int | None,
    entries: Sequence[BackfillInventoryEntry],
    control_instance_id: str,
) -> None:
    scope = _repair_stream_scope(entries)
    if not scope:
        if reservation is not None:
            raise ValueError("stage-history-only generation cannot carry a repair reservation")
        return
    if reservation is None:
        raise RuntimeError("repair-owned generation publication is missing a reservation")
    identity = f"{generation_id}:{occurrence or 'corrective'}:{resume_generation or 0}"
    expected = _generation_routing_digest(
        generation_id,
        configuration_digest,
        task_kind,
        occurrence,
        resume_generation,
        scope,
    )
    if (
        reservation.control_instance_id != control_instance_id
        or reservation.stream_scope != scope
        or reservation.routing_identity_digest != expected
        or reservation.occurrence_generation_identity != identity
    ):
        raise RuntimeError("publication reservation is not bound to this generation routing")


def _begin_generation_publication(reservation: RepairPublicationReservation) -> None:
    from src.config import get_settings
    from src.graph.client import Neo4jClient

    client = Neo4jClient(get_settings())
    try:
        CrmDealRepairPublicationRepository(
            client, reservation.control_instance_id
        ).begin_publishing(reservation)
    finally:
        client.close()


def _mark_generation_published(
    reservation: RepairPublicationReservation, publication_id: str
) -> None:
    from src.config import get_settings
    from src.graph.client import Neo4jClient

    client = Neo4jClient(get_settings())
    try:
        CrmDealRepairPublicationRepository(client, reservation.control_instance_id).mark_published(
            reservation, publication_id
        )
    finally:
        client.close()


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


def _assert_repair_dispatch_unblocked(control_instance_id: str) -> None:
    """Reject all deal/activity/Open Lines canvas construction while #310 owns the block."""
    from neo4j import ManagedTransaction

    from src.config import get_settings
    from src.graph.client import Neo4jClient
    from src.graph.queries.crm_deal_identity_repair_ledger import GET_REPAIR_DISPATCH_BLOCK

    client = Neo4jClient(get_settings())
    try:
        def _read(tx: ManagedTransaction) -> bool:
            return (
                tx.run(
                    GET_REPAIR_DISPATCH_BLOCK,
                    control_instance_id=control_instance_id,
                ).single()
                is not None
            )

        if client.execute_read(_read):
            raise RuntimeError("Bitrix generation publication is blocked by CRM repair quiescence")
    finally:
        client.close()
