"""Celery producer client used by the API to enqueue ingestion-worker tasks.

The API is not a Celery worker — it only needs to submit tasks to the shared
Redis broker so the ingestion service can execute them. ``get_celery_app``
creates a lazy producer-only Celery app wired from ``config.celery_broker_url``.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from celery import Celery

from src.config import config

logger = logging.getLogger(__name__)

_RECALCULATE_PAIR_AUDIT_TASK = "src.tasks.recalculate_pair_audit_match_task"


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
    """Queue the ingestion worker to re-score each person-pair review case.

    Safe to call with an empty list. Tasks are sent to the shared Redis broker
    and consumed by the ingestion Celery worker; failures here are logged but
    do not fail the API request.
    """
    if not review_case_ids:
        return
    try:
        app = get_celery_app()
        for review_case_id in review_case_ids:
            app.send_task(_RECALCULATE_PAIR_AUDIT_TASK, args=(review_case_id,))
    except Exception:
        logger.exception(
            "Failed to enqueue match recalculation tasks for %d review case(s)",
            len(review_case_ids),
        )
