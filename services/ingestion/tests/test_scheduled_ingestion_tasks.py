"""Idempotent group-chain dispatch contracts."""

from __future__ import annotations

import pytest
from _test_helpers import NullContext, TaskSettings
from celery.exceptions import Reject
from pytest import MonkeyPatch
from src.ingestion_config import IngestionConfig, ScheduledIngestionConfig


def test_manual_group_dispatch_defaults_to_full_extraction() -> None:
    from src import scheduled_ingestion_tasks as tasks

    signature = tasks._signature("fundbox", None, False, "run:step:0")

    assert signature.args == ("fundbox", "api")
    assert signature.kwargs == {
        "entity_key": None,
        "incremental": False,
        "wait_for_source": True,
        "require_clean_completion": True,
        "idempotency_key": "run:step:0",
    }
    assert signature.options["queue"] == "ingestion"
    assert signature.immutable


def test_incremental_marker_is_daily_but_manual_marker_is_task_scoped(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import scheduled_ingestion_tasks as tasks

    monkeypatch.setattr(tasks, "_utc_occurrence_date", lambda: "2026-07-29")
    assert tasks._marker_key("fundbox", True, "manual-id").endswith(":incremental:2026-07-29")
    assert tasks._marker_key("fundbox", False, "manual-id").endswith(":full:manual-id")


def test_disabled_scheduled_ingestion_exits_before_resolving_or_claiming(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import scheduled_ingestion_tasks as tasks

    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(scheduled_ingestion=ScheduledIngestionConfig(enabled=False)),
    )
    monkeypatch.setattr(
        tasks,
        "scheduled_ingestion_group",
        lambda _group_key: pytest.fail("disabled dispatch must not resolve a group"),
    )
    monkeypatch.setattr(
        tasks,
        "_claim_dispatch",
        lambda *_args: pytest.fail("disabled dispatch must not claim a marker"),
    )

    result = tasks.dispatch_ingestion_group_task.run("fundbox", incremental=True)

    assert result == {
        "status": "disabled",
        "group_key": "fundbox",
        "incremental": True,
        "workflow_task_id": "",
    }


def test_enabled_scheduled_ingestion_continues_to_idempotent_dispatch(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import scheduled_ingestion_tasks as tasks

    claims: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(scheduled_ingestion=ScheduledIngestionConfig(enabled=True)),
    )

    def claim(marker_key: str, task_id: str) -> tuple[bool, str]:
        claims.append((marker_key, task_id))
        return False, "existing-workflow"

    monkeypatch.setattr(tasks, "_claim_dispatch", claim)

    result = tasks.dispatch_ingestion_group_task.run("fundbox", incremental=True)

    assert result == {
        "status": "already_queued",
        "group_key": "fundbox",
        "incremental": True,
        "workflow_task_id": "existing-workflow",
    }
    assert len(claims) == 1
    assert claims[0][0].startswith("profile_unifier:scheduled-ingestion:fundbox:incremental:")


def test_completed_chain_step_returns_without_running_ingestion(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    monkeypatch.setattr(tasks, "_scheduled_step_completed", lambda _key: True)

    result = tasks.run_ingestion_task.run(
        "fundbox",
        "api",
        idempotency_key="weekly:fundbox:step:0",
    )

    assert result["status"] == "completed"
    assert result["source_key"] == "fundbox"
    assert result["skipped"] == 1


def test_non_clean_chain_step_stops_without_marking_or_queuing_lifecycle(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    marked: list[str] = []
    lifecycle_calls: list[dict[str, object]] = []
    monkeypatch.setattr(tasks, "get_settings", lambda: TaskSettings())
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)
    monkeypatch.setattr(tasks, "initialize_ingestion_graph", lambda: None)
    monkeypatch.setattr(tasks, "_acquire_init_lock", lambda: NullContext())
    monkeypatch.setattr(tasks, "_acquire_source_lock", lambda _key: NullContext())
    monkeypatch.setattr(tasks, "_acquire_ingestion_slot", lambda _cap: NullContext())
    monkeypatch.setattr(tasks, "_renew_ingestion_leases", lambda *_args: NullContext())
    monkeypatch.setattr(tasks, "_scheduled_step_completed", lambda _key: False)
    monkeypatch.setattr(tasks, "_mark_scheduled_step_completed", marked.append)
    monkeypatch.setattr(
        tasks,
        "run_ingestion",
        lambda *_args, **_kwargs: {"status": "completed_with_errors"},
    )
    monkeypatch.setattr(
        tasks.reconcile_lifecycle_task,
        "apply_async",
        lambda **options: lifecycle_calls.append(options),
    )

    with pytest.raises(Reject, match="returned completed_with_errors"):
        tasks.run_ingestion_task.run(
            "fundbox",
            "api",
            require_clean_completion=True,
            idempotency_key="weekly:fundbox:step:0",
        )

    assert marked == []
    assert lifecycle_calls == []


def test_manual_full_chain_step_forwards_disabled_incremental_policy(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    incremental_values: list[bool] = []
    marked: list[str] = []
    monkeypatch.setattr(tasks, "get_settings", lambda: TaskSettings())
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)
    monkeypatch.setattr(tasks, "initialize_ingestion_graph", lambda: None)
    monkeypatch.setattr(tasks, "_acquire_init_lock", lambda: NullContext())
    monkeypatch.setattr(tasks, "_acquire_source_lock", lambda _key: NullContext())
    monkeypatch.setattr(tasks, "_acquire_ingestion_slot", lambda _cap: NullContext())
    monkeypatch.setattr(tasks, "_renew_ingestion_leases", lambda *_args: NullContext())
    monkeypatch.setattr(tasks, "_scheduled_step_completed", lambda _key: False)
    monkeypatch.setattr(tasks, "_mark_scheduled_step_completed", marked.append)
    monkeypatch.setattr(tasks.reconcile_lifecycle_task, "apply_async", lambda **_options: None)

    def run_ingestion(*_args: object, **kwargs: object) -> dict[str, str]:
        incremental = kwargs["incremental"]
        assert isinstance(incremental, bool)
        incremental_values.append(incremental)
        return {"status": "completed"}

    monkeypatch.setattr(tasks, "run_ingestion", run_ingestion)

    result = tasks.run_ingestion_task.run(
        "fundbox",
        "api",
        incremental=False,
        require_clean_completion=True,
        idempotency_key="manual:fundbox:step:0",
    )

    assert result["status"] == "completed"
    assert incremental_values == [False]
    assert marked == ["manual:fundbox:step:0"]


def test_full_snapshot_sources_do_not_claim_incremental_support() -> None:
    from src.scheduled_ingestion_groups import scheduled_ingestion_group

    bankruptcy = scheduled_ingestion_group("sgbankruptcy").tasks[0]
    rental_flats = scheduled_ingestion_group("sgrentalflats").tasks[0]

    assert bankruptcy.supports_incremental is False
    assert rental_flats.supports_incremental is False


def test_active_bitrix_successor_replaces_legacy_weekly_dispatch(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import scheduled_ingestion_tasks as tasks

    class _Redis:
        def __enter__(self) -> _Redis:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def set(self, *_args: object, **_kwargs: object) -> bool:
            return True

    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(scheduled_ingestion=ScheduledIngestionConfig(enabled=True)),
    )
    monkeypatch.setattr(tasks, "_claim_dispatch", lambda *_args: (True, None))
    monkeypatch.setattr(tasks, "_utc_occurrence_date", lambda: "2026-08-08")
    monkeypatch.setattr(
        tasks,
        "_dispatch_active_bitrix_successor",
        lambda occurrence: "split-workflow" if occurrence == "2026-08-08" else None,
    )
    monkeypatch.setattr(
        tasks,
        "_signature",
        lambda *_args: pytest.fail("active successor must not publish legacy Bitrix"),
    )
    monkeypatch.setattr(
        "src.scheduled_ingestion_tasks.redis.Redis.from_url",
        lambda *_args, **_kwargs: _Redis(),
    )

    result = tasks.dispatch_ingestion_group_task.run("bitrix_chat", incremental=True)

    assert result["workflow_task_id"] == "split-workflow"
    assert result["status"] == "queued"
