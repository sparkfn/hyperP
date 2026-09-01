"""Default-off manual standalone CRM source-sync dispatch coverage for #307."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from src import standalone_crm_schedule_tasks as task_module
from src.ingestion_config import BitrixOpenLinesConfig, IngestionConfig
from src.standalone_crm_census_requests import SourceSyncAuthority


@dataclass
class _Result:
    id: str


class _StartTask:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def delay(self, payload: dict[str, object]) -> _Result:
        self.payloads.append(payload)
        return _Result("published-task")


def test_disabled_dispatch_touches_neither_head_capture_nor_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BitrixOpenLinesConfig()
    monkeypatch.setattr(
        task_module, "get_ingestion_config", lambda: IngestionConfig(bitrix_openlines=config)
    )
    monkeypatch.setattr(
        task_module,
        "_scheduled_source_sync_authority_provider",
        lambda _source, _control: (_ for _ in ()).throw(AssertionError("head capture")),
    )
    monkeypatch.setattr(
        task_module,
        "admit_and_run_standalone_crm_census",
        object(),
    )

    assert task_module.dispatch_standalone_crm_source_sync.run() is None


def test_enabled_dispatch_captures_heads_then_publishes_bounded_deterministic_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BitrixOpenLinesConfig(
        source_instance_id="portal-a",
        standalone_crm_identity_enabled=True,
        standalone_crm_identity_schedule_enabled=True,
        standalone_crm_identity_kinds=["lead", "contact"],
        standalone_crm_identity_max_rows_per_attempt=10,
        standalone_crm_identity_max_calls_per_attempt=11,
        standalone_crm_identity_max_runtime_seconds_per_attempt=12.0,
        standalone_crm_identity_max_rows_per_occurrence=100,
        standalone_crm_identity_max_calls_per_occurrence=101,
        standalone_crm_identity_max_attempts_per_occurrence=3,
        standalone_crm_identity_max_wall_clock_seconds_per_occurrence=120.0,
    )
    published = _StartTask()
    monkeypatch.setattr(
        task_module, "get_ingestion_config", lambda: IngestionConfig(bitrix_openlines=config)
    )
    monkeypatch.setattr(
        task_module,
        "_utc_now",
        lambda: datetime(2026, 9, 1, 2, 3, 4, tzinfo=UTC),
    )
    monkeypatch.setattr(
        task_module,
        "_scheduled_source_sync_authority_provider",
        lambda source, control: SourceSyncAuthority(
            f"mapping:{source}:{control}",
            "sha256:" + "a" * 64,
            f"projection:{source}:{control}",
            "sha256:" + "b" * 64,
        ),
    )
    monkeypatch.setattr(task_module, "admit_and_run_standalone_crm_census", published)

    assert task_module.dispatch_standalone_crm_source_sync.run() == "published-task"
    assert len(published.payloads) == 1
    payload = published.payloads[0]
    assert payload["census_kind"] == "source_sync"
    assert payload["occurrence_key"] == "standalone-crm-source-sync-v1:2026-09-01"
    assert payload["selected_kinds"] == ["contact", "lead"]
    assert payload["budget"] == {
        "max_calls_per_attempt": 11,
        "max_rows_per_attempt": 10,
        "max_runtime_seconds_per_attempt": 12,
        "max_calls_per_occurrence": 101,
        "max_rows_per_occurrence": 100,
        "max_attempts_per_occurrence": 3,
        "occurrence_deadline": "2026-09-01T02:05:04Z",
    }


def test_schedule_task_is_discoverable_but_has_no_beat_entry() -> None:
    from src.celery_app import celery_app

    assert task_module.dispatch_standalone_crm_source_sync.name in celery_app.tasks
    assert "src.standalone_crm_schedule_tasks" in celery_app.conf.include
    assert celery_app.conf.task_routes["src.standalone_crm_schedule_tasks.*"] == {
        "queue": "ingestion"
    }
    assert all("standalone_crm_schedule" not in name for name in celery_app.conf.beat_schedule)
    assert all(
        "standalone_crm_schedule" not in str(entry)
        for entry in celery_app.conf.beat_schedule.values()
    )
