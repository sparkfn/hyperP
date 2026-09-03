"""Fixed Celery worker topology and queue-routing contracts."""

from __future__ import annotations

from pathlib import Path

from src.celery_app import celery_app
from src.tasks import materialize_knows_task

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"


def test_worker_concurrency_and_task_routes_are_fixed_in_code() -> None:
    assert celery_app.conf.worker_concurrency == 2
    assert celery_app.conf.task_routes == {
        "src.tasks.run_ingestion_task": {"queue": "ingestion"},
        "src.ingestion_orchestration_tasks.*": {"queue": "ingestion"},
        "src.scheduled_ingestion_tasks.*": {"queue": "ingestion"},
        "src.standalone_crm_schedule_tasks.*": {"queue": "ingestion"},
        "src.standalone_crm_census_tasks.*": {"queue": "ingestion"},
        "src.crm_tenant_operator_tasks.*": {"queue": "ingestion"},
        "src.crm_deal_identity_repair_tasks.*": {"queue": "ingestion"},
        "src.stage_history_tasks.*": {"queue": "ingestion"},
        "src.tasks.reconcile_lifecycle_task": {"queue": "lifecycle"},
        "src.tasks.materialize_knows_task": {"queue": "lifecycle"},
        "src.tasks.send_birthday_messages_task": {"queue": "miscellaneous"},
        "src.tasks.recalculate_pair_audit_match_task": {"queue": "miscellaneous"},
    }
    assert materialize_knows_task.soft_time_limit == 300
    assert materialize_knows_task.time_limit == 330


def test_compose_workers_are_exclusive_and_use_code_concurrency() -> None:
    compose = _COMPOSE_PATH.read_text(encoding="utf-8")
    ingestion_worker = compose.split("  ingestion-worker:", 1)[1].split("  lifecycle-worker:", 1)[0]
    lifecycle_worker = compose.split("  lifecycle-worker:", 1)[1].split("  beat:", 1)[0]

    assert "--queues=ingestion" in ingestion_worker
    assert "--queues=lifecycle,miscellaneous" in lifecycle_worker
    assert "--queues=celery" not in ingestion_worker
    assert "--queues=celery" not in lifecycle_worker
    assert "--concurrency" not in ingestion_worker
    assert "--concurrency" not in lifecycle_worker
    assert "CELERY_WORKER_CONCURRENCY" not in compose
    assert "MAX_CONCURRENT_INGESTIONS" not in compose
    assert "stop_grace_period: 5m" in ingestion_worker
    assert "stop_grace_period: 5m" in lifecycle_worker


def test_crm_tenant_operator_commands_are_registered_and_routed() -> None:
    import src.crm_tenant_operator_tasks  # noqa: F401

    expected = {
        "src.crm_tenant_operator_tasks.prepare",
        "src.crm_tenant_operator_tasks.project",
        "src.crm_tenant_operator_tasks.activate",
        "src.crm_tenant_operator_tasks.rollback",
        "src.crm_tenant_operator_tasks.status",
        "src.crm_tenant_operator_tasks.reconcile",
        "src.crm_tenant_operator_tasks.source_sync",
    }
    assert expected <= set(celery_app.tasks)
    assert celery_app.conf.task_routes["src.crm_tenant_operator_tasks.*"] == {"queue": "ingestion"}
