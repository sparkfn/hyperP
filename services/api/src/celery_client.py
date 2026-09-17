"""Celery producer client used by the API to enqueue background tasks.

The API is not a Celery worker. It only submits tasks to the shared Redis broker
for the dedicated ingestion or lifecycle/miscellaneous worker. ``get_celery_app``
creates a lazy producer-only Celery app wired from ``config.celery_broker_url``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

from celery import Celery

from src.config import config

logger = logging.getLogger(__name__)

_RECALCULATE_PAIR_AUDIT_TASK = "src.tasks.recalculate_pair_audit_match_task"
_RUN_INGESTION_TASK = "src.tasks.run_ingestion_task"
_RECOVER_BOUNDED_LOGICAL_RUN_TASK = "src.tasks.recover_bounded_logical_run_task"
_INGESTION_QUEUE = "ingestion"
_MISCELLANEOUS_QUEUE = "miscellaneous"


@lru_cache(maxsize=1)
def get_celery_app() -> Celery:
    """Return a producer-only Celery app pointed at the shared broker."""
    return Celery(
        "profile_unifier_api_producer",
        broker=config.celery_broker_url,
        backend=None,
        include=[],
    )


def enqueue_match_recalculation(review_case_ids: list[str]) -> None:
    """Queue the miscellaneous worker to re-score person-pair review cases.

    Safe to call with an empty list. Tasks are sent to the shared Redis broker
    and consumed by the lifecycle/miscellaneous Celery worker; failures here
    are logged but do not fail the API request.
    """
    if not review_case_ids:
        return
    try:
        app = get_celery_app()
        for review_case_id in review_case_ids:
            app.send_task(
                _RECALCULATE_PAIR_AUDIT_TASK,
                args=(review_case_id,),
                queue=_MISCELLANEOUS_QUEUE,
            )
    except Exception:
        logger.exception(
            "Failed to enqueue match recalculation tasks for %d review case(s)",
            len(review_case_ids),
        )


@dataclass(frozen=True)
class BoundedLogicalRunRecoveryRequest:
    """Minimal recovery publication identity for a persisted bounded run.

    The ingestion worker must re-read the durable logical run and apply its
    normal disabled/window/reset/capability guards. This producer never carries
    a cursor, source boundary, occurrence, payload, or fencing token.
    """

    logical_run_id: str
    source_key: str
    control_instance_id: str
    reset_generation: int


def enqueue_bounded_logical_run_recovery(
    request: BoundedLogicalRunRecoveryRequest,
) -> bool:
    """Best-effort publish one idempotent bounded-run recovery request.

    The durable publication intent is written before this method is called.
    A broker failure is deliberately non-fatal: a later operator action or the
    scheduler may re-attempt publication from that persisted intent.
    """
    try:
        get_celery_app().send_task(
            _RECOVER_BOUNDED_LOGICAL_RUN_TASK,
            kwargs={
                "logical_run_id": request.logical_run_id,
                "source_key": request.source_key,
                "control_instance_id": request.control_instance_id,
                "reset_generation": request.reset_generation,
            },
            queue=_INGESTION_QUEUE,
        )
    except Exception:
        logger.exception(
            "Failed to publish bounded logical-run recovery logical_run_id=%s",
            request.logical_run_id,
        )
        return False
    return True


def enqueue_ingestion_run(
    source_key: str,
    mode: str,
    *,
    dump_path: str | None,
    ingest_run_id: str,
) -> None:
    """Queue ingestion while preserving the run created by the API."""
    get_celery_app().send_task(
        _RUN_INGESTION_TASK,
        args=(source_key, mode, dump_path),
        kwargs={"ingest_run_id": ingest_run_id},
        queue=_INGESTION_QUEUE,
    )
