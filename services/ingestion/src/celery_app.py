"""Celery application and weekly scheduled-ingestion configuration."""

from __future__ import annotations

from typing import Final

from celery import Celery
from celery.schedules import crontab

from src.config import get_settings
from src.scheduled_ingestion_groups import SCHEDULED_INGESTION_GROUPS

WORKER_CONCURRENCY: Final[int] = 2
INGESTION_QUEUE: Final[str] = "ingestion"
LIFECYCLE_QUEUE: Final[str] = "lifecycle"
MISCELLANEOUS_QUEUE: Final[str] = "miscellaneous"

settings = get_settings()

celery_app = Celery(
    "profile_unifier_ingestion",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "src.standalone_crm_census_tasks",
        "src.crm_tenant_operator_tasks",
        "src.tasks",
        "src.ingestion_orchestration_tasks",
        "src.scheduled_ingestion_tasks",
        "src.standalone_crm_schedule_tasks",
        "src.stage_history_tasks",
    ],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=WORKER_CONCURRENCY,
    task_queue_max_priority=9,
    task_routes={
        "src.tasks.run_ingestion_task": {"queue": INGESTION_QUEUE},
        "src.ingestion_orchestration_tasks.*": {"queue": INGESTION_QUEUE},
        "src.scheduled_ingestion_tasks.*": {"queue": INGESTION_QUEUE},
        "src.standalone_crm_schedule_tasks.*": {"queue": INGESTION_QUEUE},
        "src.stage_history_tasks.*": {"queue": INGESTION_QUEUE},
        "src.standalone_crm_census_tasks.*": {"queue": INGESTION_QUEUE},
        "src.crm_tenant_operator_tasks.*": {"queue": INGESTION_QUEUE},
        "src.tasks.reconcile_lifecycle_task": {"queue": LIFECYCLE_QUEUE},
        "src.tasks.materialize_knows_task": {"queue": LIFECYCLE_QUEUE},
        "src.tasks.send_birthday_messages_task": {"queue": MISCELLANEOUS_QUEUE},
        "src.tasks.recalculate_pair_audit_match_task": {"queue": MISCELLANEOUS_QUEUE},
    },
    broker_transport_options={
        "visibility_timeout": settings.celery_broker_visibility_timeout,
        "priority_steps": list(range(10)),
    },
    task_time_limit=None,
    task_soft_time_limit=None,
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    result_backend_transport_options={
        "visibility_timeout": settings.celery_broker_visibility_timeout,
    },
    visibility_timeout=settings.celery_broker_visibility_timeout,
)

_beat_schedule: dict[str, dict[str, object]] = {
    "lifecycle-reconciliation": {
        "task": "src.tasks.reconcile_lifecycle_task",
        "schedule": 60.0 * 60.0,
        "options": {"queue": LIFECYCLE_QUEUE},
    },
    "knows-materialization-contacts": {
        "task": "src.tasks.materialize_knows_task",
        "schedule": 60.0 * 60.0,
        "args": ("contacts",),
        "options": {"queue": LIFECYCLE_QUEUE},
    },
    "knows-materialization-chat-relationships": {
        "task": "src.tasks.materialize_knows_task",
        "schedule": 60.0 * 60.0,
        "args": ("chat_relationships",),
        "options": {"queue": LIFECYCLE_QUEUE},
    },
}

for group in SCHEDULED_INGESTION_GROUPS:
    _beat_schedule[f"scheduled-ingestion-{group.key}"] = {
        "task": "src.scheduled_ingestion_tasks.dispatch_ingestion_group_task",
        "schedule": crontab(minute="0", hour="1", day_of_week=group.weekday),
        "args": (group.key,),
        "kwargs": {"incremental": True},
        "options": {"queue": INGESTION_QUEUE},
    }

if settings.birthday_task_enabled:
    _beat_schedule["birthday-greetings"] = {
        "task": "src.tasks.send_birthday_messages_task",
        "schedule": crontab(
            minute=str(settings.birthday_task_minute),
            hour=str(settings.birthday_task_hour),
        ),
        "args": (),
        "options": {"queue": MISCELLANEOUS_QUEUE},
    }

celery_app.conf.beat_schedule = _beat_schedule
