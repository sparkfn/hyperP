"""Celery application for the ingestion service.

The worker and beat scheduler share this module. Concurrency at the *worker
process* level is controlled by ``CELERY_WORKER_CONCURRENCY``; the maximum
number of *concurrent ingestion runs* across the cluster is enforced by a
Redis-backed semaphore inside the task itself (see :mod:`src.tasks`) and is
configured via ``MAX_CONCURRENT_INGESTIONS``.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from src.config import get_settings

settings = get_settings()

celery_app = Celery(
    "profile_unifier_ingestion",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["src.tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=settings.celery_worker_concurrency,
    task_time_limit=60 * 60 * 6,  # 6h hard limit per ingestion
    task_soft_time_limit=60 * 60 * 6 - 60,
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
)


def _parse_cron(expr: str) -> crontab | None:
    parts = expr.strip().split()
    if len(parts) != 5:
        return None
    minute, hour, day_of_month, month_of_year, day_of_week = parts
    return crontab(
        minute=minute,
        hour=hour,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        day_of_week=day_of_week,
    )


# Beat schedule — entries are only registered when their feature flag is on.
_beat_schedule: dict[str, dict[str, object]] = {}

_fundbox_cron = _parse_cron(settings.fundbox_ingest_cron)
if _fundbox_cron is not None:
    _beat_schedule["fundbox-ingest"] = {
        "task": "src.tasks.run_ingestion_task",
        "schedule": _fundbox_cron,
        "args": ("fundbox", "batch"),
    }

_speedzone_cron = _parse_cron(settings.speedzone_ingest_cron)
if _speedzone_cron is not None:
    _beat_schedule["speedzone-ingest"] = {
        "task": "src.tasks.run_ingestion_task",
        "schedule": _speedzone_cron,
        "args": ("speedzone", "batch"),
    }

if settings.birthday_task_enabled:
    _beat_schedule["birthday-greetings"] = {
        "task": "src.tasks.send_birthday_messages_task",
        "schedule": crontab(
            minute=str(settings.birthday_task_minute),
            hour=str(settings.birthday_task_hour),
        ),
        "args": (),
    }

if _beat_schedule:
    celery_app.conf.beat_schedule = _beat_schedule
