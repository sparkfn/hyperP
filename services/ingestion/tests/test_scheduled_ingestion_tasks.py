"""Idempotent group-chain dispatch contracts."""

from __future__ import annotations

from types import SimpleNamespace

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
        "scheduled_dispatch": True,
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


def test_disabled_checker_cancels_an_already_published_chain_step(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(scheduled_ingestion=ScheduledIngestionConfig(enabled=False)),
    )
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: pytest.fail("disabled chain step must exit before runtime setup"),
    )

    result = tasks.run_ingestion_task.run(
        "fundbox",
        "api",
        idempotency_key="weekly:fundbox:step:0",
        scheduled_dispatch=True,
    )

    assert result["status"] == "disabled"
    assert result["skipped"] == 1


def test_disabled_checker_cancels_legacy_same_id_live_delivery(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    logical_task_id = "bitrix-live:2026-08-11:crm_deals:sha256:config"
    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(scheduled_ingestion=ScheduledIngestionConfig(enabled=False)),
    )
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: pytest.fail("disabled delayed delivery must exit before runtime setup"),
    )
    tasks.run_ingestion_task.push_request(id=logical_task_id, retries=4)
    try:
        result = tasks.run_ingestion_task.run(
            "bitrix_chat",
            "api",
            idempotency_key=logical_task_id,
            bitrix_execution_stream="crm_deals",
        )
    finally:
        tasks.run_ingestion_task.pop_request()

    assert result["status"] == "disabled"
    assert result["skipped"] == 1


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
    monkeypatch.setattr(tasks, "_read_dispatch_marker", lambda _marker: None)
    monkeypatch.setattr(tasks, "_claim_dispatch", lambda *_args: (True, None))
    monkeypatch.setattr(
        tasks,
        "_reserve_legacy_bitrix_publication",
        lambda **_kwargs: SimpleNamespace(
            control_instance_id="legacy-default",
            reservation_token="token",
            status="pending",
            publication_id=None,
            is_exact_replay=False,
        ),
    )
    monkeypatch.setattr(tasks, "_begin_legacy_bitrix_publication", lambda _reservation: None)
    monkeypatch.setattr(tasks, "_publish_legacy_bitrix_publication", lambda *_args: None)
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


def test_repair_block_precedes_legacy_bitrix_marker_and_publication(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import scheduled_ingestion_tasks as tasks

    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(scheduled_ingestion=ScheduledIngestionConfig(enabled=True)),
    )
    monkeypatch.setattr(tasks, "_repair_dispatch_blocked", lambda _control: True)
    monkeypatch.setattr(
        tasks,
        "_claim_dispatch",
        lambda *_args: pytest.fail("blocked repair must not claim a marker"),
    )
    monkeypatch.setattr(
        tasks,
        "_dispatch_active_bitrix_successor",
        lambda _occurrence: pytest.fail("blocked repair must not publish successor work"),
    )

    result = tasks.dispatch_ingestion_group_task.run("bitrix_chat", incremental=True)

    assert result == {
        "status": "repair_blocked",
        "group_key": "bitrix_chat",
        "incremental": True,
        "workflow_task_id": "",
    }


def test_repair_gate_does_not_block_unrelated_scheduled_ingestion(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import scheduled_ingestion_tasks as tasks

    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(scheduled_ingestion=ScheduledIngestionConfig(enabled=True)),
    )
    monkeypatch.setattr(
        tasks,
        "_repair_dispatch_blocked",
        lambda _control: pytest.fail("repair gate must not inspect unrelated groups"),
    )
    monkeypatch.setattr(tasks, "_claim_dispatch", lambda *_args: (False, "existing"))

    result = tasks.dispatch_ingestion_group_task.run("fundbox", incremental=False)

    assert result["status"] == "already_queued"


def test_successor_repair_block_precedes_source_window_freezing(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import scheduled_ingestion_tasks as tasks

    monkeypatch.setattr(tasks, "_repair_dispatch_blocked", lambda _control: True)
    monkeypatch.setattr(
        tasks,
        "admit_configured_bitrix_control",
        lambda *_args: pytest.fail("blocked successor must not admit a control"),
    )

    class _Graph:
        def execute_read(self, work: object) -> tuple[str, str, str, str]:
            del work
            return ("generation", "sha256:config", "{}", "control-310")

        def close(self) -> None:
            return None

    monkeypatch.setattr(tasks, "Neo4jClient", lambda _settings: _Graph())
    monkeypatch.setattr(tasks, "get_settings", lambda: object())

    with pytest.raises(RuntimeError, match="blocked"):
        tasks._dispatch_active_bitrix_successor("2026-08-29")


def _legacy_reservation(
    *,
    status: str = "pending",
    publication_id: str | None = None,
    replay: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        control_instance_id="legacy-default",
        reservation_token="reservation-310",
        status=status,
        publication_id=publication_id,
        is_exact_replay=replay,
    )


def test_legacy_reservation_is_created_before_marker_claim(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import scheduled_ingestion_tasks as tasks

    events: list[str] = []
    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(scheduled_ingestion=ScheduledIngestionConfig(enabled=True)),
    )
    monkeypatch.setattr(tasks, "_repair_dispatch_blocked", lambda _control: False)
    monkeypatch.setattr(
        tasks,
        "_read_dispatch_marker",
        lambda _marker: events.append("read") or None,
    )
    monkeypatch.setattr(
        tasks,
        "_reserve_legacy_bitrix_publication",
        lambda **_kwargs: events.append("reserve") or _legacy_reservation(),
    )
    monkeypatch.setattr(
        tasks,
        "_claim_dispatch",
        lambda *_args: events.append("claim") or (True, None),
    )
    monkeypatch.setattr(
        tasks,
        "_dispatch_active_bitrix_successor",
        lambda _day: events.append("successor") or "workflow-310",
    )
    monkeypatch.setattr(
        tasks,
        "_begin_legacy_bitrix_publication",
        lambda _reservation: events.append("begin"),
    )
    monkeypatch.setattr(
        tasks,
        "_publish_legacy_bitrix_publication",
        lambda *_args: events.append("published"),
    )
    monkeypatch.setattr(tasks, "_record_dispatch_marker", lambda *_args: events.append("marker"))

    result = tasks.dispatch_ingestion_group_task.run("bitrix_chat", incremental=True)

    assert result["workflow_task_id"] == "workflow-310"
    assert events.index("reserve") < events.index("claim")
    assert events == ["read", "reserve", "claim", "successor", "begin", "published", "marker"]


def test_legacy_published_marker_exact_replay_never_claims_or_creates(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import scheduled_ingestion_tasks as tasks

    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(scheduled_ingestion=ScheduledIngestionConfig(enabled=True)),
    )
    monkeypatch.setattr(tasks, "_repair_dispatch_blocked", lambda _control: False)
    monkeypatch.setattr(tasks, "_read_dispatch_marker", lambda _marker: "workflow-310")
    monkeypatch.setattr(
        tasks,
        "_read_legacy_bitrix_publication",
        lambda **_kwargs: _legacy_reservation(
            status="published",
            publication_id="workflow-310",
            replay=True,
        ),
    )
    monkeypatch.setattr(
        tasks,
        "_reserve_legacy_bitrix_publication",
        lambda **_kwargs: pytest.fail("duplicate marker must not reserve"),
    )
    monkeypatch.setattr(
        tasks,
        "_claim_dispatch",
        lambda *_args: pytest.fail("duplicate marker must not claim"),
    )

    result = tasks.dispatch_ingestion_group_task.run("bitrix_chat", incremental=True)

    assert result["status"] == "already_queued"
    assert result["workflow_task_id"] == "workflow-310"


def test_legacy_pending_placeholder_replay_remains_fail_closed_without_republish(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import scheduled_ingestion_tasks as tasks

    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(scheduled_ingestion=ScheduledIngestionConfig(enabled=True)),
    )
    monkeypatch.setattr(tasks, "_repair_dispatch_blocked", lambda _control: False)
    monkeypatch.setattr(tasks, "_read_dispatch_marker", lambda _marker: "in-flight-task-310")
    monkeypatch.setattr(
        tasks,
        "_read_legacy_bitrix_publication",
        lambda **_kwargs: _legacy_reservation(status="pending", replay=True),
    )
    monkeypatch.setattr(
        tasks,
        "_reserve_legacy_bitrix_publication",
        lambda **_kwargs: pytest.fail("placeholder must not reserve"),
    )
    monkeypatch.setattr(
        tasks,
        "_claim_dispatch",
        lambda *_args: pytest.fail("placeholder must not claim"),
    )
    monkeypatch.setattr(
        tasks,
        "_dispatch_active_bitrix_successor",
        lambda _day: pytest.fail("placeholder must not republish"),
    )

    result = tasks.dispatch_ingestion_group_task.run("bitrix_chat", incremental=True)

    assert result["status"] == "already_queued"
    assert result["workflow_task_id"] == "in-flight-task-310"


def test_legacy_claim_race_reconciles_exact_replay_without_broker_publication(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import scheduled_ingestion_tasks as tasks

    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(scheduled_ingestion=ScheduledIngestionConfig(enabled=True)),
    )
    monkeypatch.setattr(tasks, "_repair_dispatch_blocked", lambda _control: False)
    monkeypatch.setattr(tasks, "_read_dispatch_marker", lambda _marker: None)
    monkeypatch.setattr(
        tasks,
        "_reserve_legacy_bitrix_publication",
        lambda **_kwargs: _legacy_reservation(replay=True),
    )
    monkeypatch.setattr(tasks, "_claim_dispatch", lambda *_args: (False, "in-flight-task-310"))
    monkeypatch.setattr(
        tasks,
        "_dispatch_active_bitrix_successor",
        lambda _day: pytest.fail("claim race must not republish"),
    )

    result = tasks.dispatch_ingestion_group_task.run("bitrix_chat", incremental=True)

    assert result["status"] == "already_queued"
    assert result["workflow_task_id"] == "in-flight-task-310"


def test_legacy_marker_without_reservation_never_creates_an_uncertain_row(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import scheduled_ingestion_tasks as tasks

    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(scheduled_ingestion=ScheduledIngestionConfig(enabled=True)),
    )
    monkeypatch.setattr(tasks, "_repair_dispatch_blocked", lambda _control: False)
    monkeypatch.setattr(tasks, "_read_dispatch_marker", lambda _marker: "pre-rollout-workflow")
    monkeypatch.setattr(tasks, "_read_legacy_bitrix_publication", lambda **_kwargs: None)
    monkeypatch.setattr(
        tasks,
        "_reserve_legacy_bitrix_publication",
        lambda **_kwargs: pytest.fail("existing marker must not create a pending reservation"),
    )
    monkeypatch.setattr(
        tasks,
        "_claim_dispatch",
        lambda *_args: pytest.fail("existing marker must not be claimed"),
    )

    result = tasks.dispatch_ingestion_group_task.run("bitrix_chat", incremental=True)

    assert result == {
        "status": "already_queued",
        "group_key": "bitrix_chat",
        "incremental": True,
        "workflow_task_id": "pre-rollout-workflow",
    }


def test_legacy_published_marker_mismatch_fails_closed(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import scheduled_ingestion_tasks as tasks

    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(scheduled_ingestion=ScheduledIngestionConfig(enabled=True)),
    )
    monkeypatch.setattr(tasks, "_repair_dispatch_blocked", lambda _control: False)
    monkeypatch.setattr(tasks, "_read_dispatch_marker", lambda _marker: "marker-workflow")
    monkeypatch.setattr(
        tasks,
        "_read_legacy_bitrix_publication",
        lambda **_kwargs: _legacy_reservation(
            status="published",
            publication_id="different-workflow",
            replay=True,
        ),
    )

    with pytest.raises(RuntimeError, match="disagrees"):
        tasks.dispatch_ingestion_group_task.run("bitrix_chat", incremental=True)
