"""Contracts for the fixed weekly API-ingestion schedule."""

from __future__ import annotations

from typing import cast

from celery.schedules import crontab
from src.scheduled_ingestion_groups import (
    SCHEDULED_INGESTION_GROUPS,
    scheduled_ingestion_group,
    scheduled_ingestion_group_for_weekday,
)


def test_groups_are_weekly_api_chains_with_identity_first() -> None:
    assert [(group.key, group.weekday) for group in SCHEDULED_INGESTION_GROUPS] == [
        ("fundbox", "monday"),
        ("eko", "tuesday"),
        ("speedzone", "wednesday"),
        ("bitrix_chat", "thursday"),
        ("sgbankruptcy", "friday"),
        ("sgrentalflats", "saturday"),
    ]
    assert [task.source_key for task in scheduled_ingestion_group("fundbox").tasks] == [
        "fundbox",
        "fundbox:contacts",
        "fundbox:sales",
    ]
    assert [task.source_key for task in scheduled_ingestion_group("eko").tasks] == [
        "eko_phppos",
        "eko_phppos:sales",
        "whatsapp_chat",
    ]
    assert scheduled_ingestion_group("eko").tasks[-1].entity_key == "eko"
    assert scheduled_ingestion_group("speedzone").tasks[-1].entity_key == "speedzone"


def test_groups_cover_only_and_all_api_sources() -> None:
    from src.ingestion_orchestrator import _API_SOURCE_KEYS

    scheduled_sources = {
        task.source_key for group in SCHEDULED_INGESTION_GROUPS for task in group.tasks
    }

    assert scheduled_sources == _API_SOURCE_KEYS


def test_groups_follow_parent_entity_and_keep_parentless_sources_standalone() -> None:
    from src.graph.bootstrap import SOURCE_KEY_TO_ENTITY

    for group in SCHEDULED_INGESTION_GROUPS:
        for task in group.tasks:
            if task.source_key == "whatsapp_chat":
                assert task.entity_key == group.key
                continue
            parent_entity = SOURCE_KEY_TO_ENTITY.get(task.source_key)
            if parent_entity is None:
                assert group.key == task.source_key
                assert len(group.tasks) == 1
            else:
                assert group.key == parent_entity


def test_beat_dispatches_every_group_at_one_utc_with_incremental_enabled() -> None:
    from src.celery_app import _beat_schedule

    entries = [
        (name.removeprefix("scheduled-ingestion-"), entry)
        for name, entry in _beat_schedule.items()
        if name.startswith("scheduled-ingestion-")
    ]
    assert len(entries) == len(SCHEDULED_INGESTION_GROUPS)
    expected_weekdays = {group.key: group.weekday for group in SCHEDULED_INGESTION_GROUPS}
    for group_key, entry in entries:
        assert entry["task"] == "src.scheduled_ingestion_tasks.dispatch_ingestion_group_task"
        assert entry["kwargs"] == {"incremental": True}
        assert entry["options"] == {"queue": "ingestion"}
        schedule = cast(crontab, entry["schedule"])
        assert isinstance(schedule, crontab)
        assert schedule._orig_minute == "0"
        assert schedule._orig_hour == "1"
        assert schedule._orig_day_of_week == expected_weekdays[group_key]


def test_hourly_maintenance_is_routed_through_scheduler_authorization() -> None:
    from src.celery_app import _beat_schedule

    assert _beat_schedule["lifecycle-reconciliation"] == {
        "task": "src.scheduled_ingestion_tasks.dispatch_scheduled_maintenance_task",
        "schedule": 60.0 * 60.0,
        "args": ("lifecycle",),
        "options": {"queue": "ingestion"},
    }
    assert _beat_schedule["knows-materialization-contacts"]["args"] == (
        "knows",
        "contacts",
    )
    assert _beat_schedule["knows-materialization-chat-relationships"]["args"] == (
        "knows",
        "chat_relationships",
    )


def test_recurring_mode_keeps_full_snapshot_sources_on_bootstrap() -> None:
    assert scheduled_ingestion_group("fundbox").tasks[0].mode_for(True) == "delta"
    assert scheduled_ingestion_group("eko").tasks[0].mode_for(False) == "bootstrap"
    for group_key in ("sgbankruptcy", "sgrentalflats"):
        assert scheduled_ingestion_group(group_key).tasks[0].mode_for(True) == "bootstrap"


def test_child_keys_are_stable_and_entity_scoped() -> None:
    assert [task.child_key for task in scheduled_ingestion_group("eko").tasks] == [
        "eko_phppos|-",
        "eko_phppos:sales|-",
        "whatsapp_chat|eko",
    ]


def test_weekday_lookup_resolves_each_configured_cadence_day() -> None:
    for group in SCHEDULED_INGESTION_GROUPS:
        resolved = scheduled_ingestion_group_for_weekday(group.weekday)
        assert resolved is not None
        assert resolved.key == group.key


def test_beat_excludes_onediver_and_standalone_census_sources() -> None:
    from src.celery_app import _beat_schedule

    scheduled_sources = {
        spec.source_key for group in SCHEDULED_INGESTION_GROUPS for spec in group.tasks
    }

    assert "onediver" not in scheduled_sources
    assert "onediver:sales" not in scheduled_sources
    assert "bitrix_crm_identity" not in scheduled_sources
    assert not any("onediver" in name or "census" in name for name in _beat_schedule)
