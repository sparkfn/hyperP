"""Scheduled ingestion dispatch and maintenance contracts."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from pytest import MonkeyPatch
from src.bitrix_backfill_models import BackfillInventoryEntry, BackfillInventoryManifest
from src.bitrix_ingestion_models import BitrixStreamKey
from src.models import JsonValue
from src.scheduled_ingestion_tasks import (
    _drive_group,
    dispatch_ingestion_group_task,
    dispatch_scheduled_maintenance_task,
)

# Monday 02:00 UTC is inside the active scheduled window (01:00-15:00 UTC)
_MONDAY_OPEN = datetime(2026, 9, 21, 2, 0, tzinfo=UTC)
_MONDAY_CLOSED = datetime(2026, 9, 21, 16, 0, tzinfo=UTC)


def test_drive_group_returns_disabled_when_scheduled_ingestion_not_enabled(
    monkeypatch: MonkeyPatch,
) -> None:
    # disabled dispatch must not resolve a group
    monkeypatch.setattr(
        "src.scheduled_ingestion_tasks.get_ingestion_config",
        lambda: SimpleNamespace(scheduled_ingestion=SimpleNamespace(enabled=False)),
    )
    result = _drive_group("fundbox", incremental=False, now=_MONDAY_OPEN)
    assert result["status"] == "disabled"
    assert result["group_key"] == "fundbox"


def test_drive_group_returns_window_status_outside_opening(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.scheduled_ingestion_tasks.get_ingestion_config",
        lambda: SimpleNamespace(scheduled_ingestion=SimpleNamespace(enabled=True)),
    )
    result = _drive_group("fundbox", incremental=False, now=_MONDAY_CLOSED)
    assert result["status"].startswith("window_")


def test_drive_group_publishes_incremental_tasks(
    monkeypatch: MonkeyPatch,
) -> None:
    published: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class DummyTask:
        @staticmethod
        def apply_async(
            args: tuple[object, ...] = (),
            kwargs: dict[str, object] | None = None,
            queue: str = "",
        ) -> None:
            del queue
            published.append((args, kwargs or {}))

    monkeypatch.setattr(
        "src.scheduled_ingestion_tasks.get_ingestion_config",
        lambda: SimpleNamespace(scheduled_ingestion=SimpleNamespace(enabled=True)),
    )
    monkeypatch.setattr("src.tasks.run_incremental_task", DummyTask)

    result = _drive_group("fundbox", incremental=False, now=_MONDAY_OPEN)
    assert result["status"] == "published"
    assert result["group_key"] == "fundbox"
    assert len(published) > 0
    assert published[0][0][0] == "fundbox"


def test_dispatch_ingestion_group_task_calls_drive(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.scheduled_ingestion_tasks.get_ingestion_config",
        lambda: SimpleNamespace(scheduled_ingestion=SimpleNamespace(enabled=False)),
    )
    result = dispatch_ingestion_group_task("fundbox", incremental=True)
    assert result["status"] == "disabled"


def test_dispatch_scheduled_maintenance_task_validation() -> None:
    with pytest.raises(ValueError, match="unknown scheduled maintenance kind"):
        dispatch_scheduled_maintenance_task("invalid")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="KNOWS maintenance requires a supported phase"):
        dispatch_scheduled_maintenance_task("knows", phase="invalid_phase")

    with pytest.raises(ValueError, match="lifecycle maintenance does not accept a phase"):
        dispatch_scheduled_maintenance_task("lifecycle", phase="contacts")


def test_dispatch_scheduled_maintenance_task_disabled_by_config(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.scheduled_ingestion_tasks.get_ingestion_config",
        lambda: SimpleNamespace(scheduled_ingestion=SimpleNamespace(enabled=False)),
    )
    result = dispatch_scheduled_maintenance_task("lifecycle")
    assert result == "disabled"


def test_dispatch_scheduled_maintenance_publishes_when_open(
    monkeypatch: MonkeyPatch,
) -> None:
    lifecycle_calls: list[str] = []
    knows_calls: list[str | None] = []

    class DummyLifecycleTask:
        @staticmethod
        def apply_async(queue: str = "") -> None:
            del queue
            lifecycle_calls.append("lifecycle")

    class DummyKnowsTask:
        @staticmethod
        def apply_async(args: tuple[object, ...] = (), queue: str = "") -> None:
            del queue
            knows_calls.append(cast(str | None, args[0]))

    monkeypatch.setattr(
        "src.scheduled_ingestion_tasks.get_ingestion_config",
        lambda: SimpleNamespace(scheduled_ingestion=SimpleNamespace(enabled=True)),
    )
    monkeypatch.setattr("src.scheduled_ingestion_tasks._utc_now", lambda: _MONDAY_OPEN)
    monkeypatch.setattr("src.tasks.reconcile_lifecycle_task", DummyLifecycleTask)
    monkeypatch.setattr("src.tasks.materialize_knows_task", DummyKnowsTask)

    assert dispatch_scheduled_maintenance_task("lifecycle") == "published"
    assert lifecycle_calls == ["lifecycle"]

    assert dispatch_scheduled_maintenance_task("knows", phase="contacts") == "published"
    assert knows_calls == ["contacts"]


def test_successor_filters_executable_historical_activity_before_probing_or_publication(
    monkeypatch: MonkeyPatch,
) -> None:
    # active successor must not publish legacy Bitrix
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
