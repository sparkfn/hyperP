"""Idempotent group-chain dispatch contracts."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import cast

import pytest
from _test_helpers import NullContext, TaskSettings
from celery.exceptions import Reject
from pytest import MonkeyPatch
from src.bitrix_backfill_models import BackfillInventoryEntry, BackfillInventoryManifest
from src.bitrix_ingestion_models import BitrixStreamKey
from src.ingestion_config import IngestionConfig, ScheduledIngestionConfig
from src.models import JsonValue


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


def test_successor_filters_executable_historical_activity_before_probing_or_publication(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import scheduled_ingestion_tasks as tasks

    # The control module is Linux-oriented because artifact evidence uses
    # advisory file locks.  This test exercises no artifact filesystem path.
    monkeypatch.setitem(
        sys.modules,
        "fcntl",
        SimpleNamespace(LOCK_EX=0, LOCK_UN=0, flock=lambda *_args: None),
    )

    def entry(stream_key: BitrixStreamKey) -> BackfillInventoryEntry:
        windows: dict[str, dict[str, JsonValue]] = {
            "crm_deals": {
                "upper_deal_id": "900",
                "included_category_digest": "sha256:categories",
                "owner_artifact_id": None,
            },
            "crm_activities": {"upper_activity_id": "1200", "owner_artifact_id": None},
            "openlines_conversations": {
                "discovery_boundary_digest": "sha256:discovery",
                "selected_config_digest": "sha256:selection",
            },
        }
        return BackfillInventoryEntry(
            gap_id=f"gap-{stream_key}",
            stream_key=stream_key,
            bounded_population=10,
            current_count=0,
            source_basis="frozen historical inventory",
            expected_repair="replay bounded rows",
            replay_mode="strict_keyset",
            source_window=windows[stream_key],
            completion_equation="coverage equals bounded population",
            max_calls=10,
            max_rows=10,
            max_runtime_seconds=10,
            max_storage_bytes=10,
            max_lock_seconds=10,
            max_lag_seconds=10,
            rollback_path="restore",
        )

    manifest = BackfillInventoryManifest(
        source_key="bitrix_chat",
        reviewed_by="operator@example.test",
        backup_id="backup",
        backup_restore_evidence_digest="sha256:restore",
        minimum_fence_image_digest="sha256:image",
        legacy_dispatch_paused=True,
        predecessor_quiescent=True,
        entries=(entry("crm_deals"), entry("crm_activities"), entry("openlines_conversations")),
    )
    closes: list[str] = []

    class Graph:
        def __init__(self, _settings: object) -> None:
            pass

        def execute_read(self, _reader: object) -> tuple[str, str, str, str]:
            return ("successor-1", "sha256:config", manifest.canonical_json, "legacy-default")

        def close(self) -> None:
            closes.append("graph")

    class ReservationRepository:
        def __init__(self, _graph: object) -> None:
            pass

        def prepare_publication(self, *_args: object) -> object:
            return object()

    class Source:
        def close(self) -> None:
            closes.append("source")

    published_entries: tuple[BackfillInventoryEntry, ...] | None = None

    def dispatch(**kwargs: object) -> str:
        nonlocal published_entries
        entries = kwargs["entries"]
        assert isinstance(entries, tuple)
        published_entries = cast(tuple[BackfillInventoryEntry, ...], entries)
        return "workflow-1"

    monkeypatch.setattr(tasks, "Neo4jClient", Graph)
    monkeypatch.setattr(tasks, "get_settings", lambda: object())
    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: SimpleNamespace(bitrix_openlines=SimpleNamespace(included_crm_category_ids=["1"])),
    )
    monkeypatch.setattr(tasks, "admit_configured_bitrix_control", lambda *_args: None)
    monkeypatch.setattr(
        "src.graph.crm_deal_identity_repair_control.CrmDealRepairControlRepository",
        ReservationRepository,
    )
    monkeypatch.setattr("src.main.create_bitrix_known_owner_client", Source)
    monkeypatch.setattr(
        "src.connectors.bitrix_stage_history.deal_probe.freeze_deal_upper_id",
        lambda _source, categories: (
            901 if categories == ("1",) else pytest.fail("wrong categories")
        ),
    )
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.dispatch_generation_canvas",
        dispatch,
    )

    assert tasks._dispatch_active_bitrix_successor("2026-09-05") == "workflow-1"
    assert published_entries is not None
    assert [entry.stream_key for entry in published_entries] == [
        "crm_deals",
        "openlines_conversations",
    ]
    assert published_entries[0].source_window is not None
    assert published_entries[0].source_window["upper_deal_id"] == 901
    assert closes == ["graph", "source", "graph"]
