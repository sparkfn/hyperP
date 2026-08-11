"""Corrective task publication is stable and strictly ordered."""

from __future__ import annotations

from celery.canvas import Signature, _chain
from src.bitrix_backfill_models import BackfillInventoryEntry
from src.bitrix_backfill_tasks import build_generation_canvas, corrective_task_id, live_task_id
from src.bitrix_ingestion_models import BitrixStreamKey
from src.models import JsonValue


def _entry(stream_key: BitrixStreamKey) -> BackfillInventoryEntry:
    source_window: dict[str, JsonValue] = (
        {
            "upper_deal_id": 900,
            "included_category_digest": "sha256:categories",
            "owner_artifact_id": None,
        }
        if stream_key == "crm_deals"
        else {"upper_activity_id": 1200, "owner_artifact_id": None}
    )
    return BackfillInventoryEntry(
        gap_id=f"gap-{stream_key}",
        stream_key=stream_key,
        bounded_population=10,
        current_count=0,
        source_basis="bounded keyset",
        expected_repair="all rows",
        replay_mode="strict_keyset",
        source_window=source_window,
        completion_equation="terminal coverage equals checkpoint accounting",
        max_calls=100,
        max_rows=100,
        max_runtime_seconds=100,
        max_storage_bytes=1000,
        max_lock_seconds=10,
        max_lag_seconds=60,
        rollback_path="restore",
    )


def test_corrective_canvas_orders_deals_before_activities_with_stable_ids() -> None:
    canvas = build_generation_canvas(
        generation_id="corrective-1",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:config",
        entries=(_entry("crm_activities"), _entry("crm_deals")),
    )
    assert isinstance(canvas, _chain)
    tasks: tuple[Signature, ...] = tuple(canvas.tasks)

    assert [task.kwargs["bitrix_execution_stream"] for task in tasks] == [
        "crm_deals",
        "crm_activities",
    ]
    assert tasks[0].options["task_id"] == corrective_task_id(
        "corrective-1",
        "crm_deals",
        "sha256:boundary",
        "sha256:config",
    )
    assert all("stage" not in str(task.kwargs["bitrix_execution_stream"]) for task in tasks)


def test_live_canvas_allows_deal_only_when_activities_are_reviewed_excluded() -> None:
    canvas = build_generation_canvas(
        generation_id="successor-1",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:config",
        entries=(_entry("crm_deals"),),
        task_kind="live",
        occurrence="2026-08-11",
    )
    assert isinstance(canvas, _chain)
    assert len(canvas.tasks) == 1
    task = canvas.tasks[0]
    assert task.kwargs["bitrix_execution_stream"] == "crm_deals"
    assert task.options["task_id"] == live_task_id(
        "2026-08-11",
        "crm_deals",
        "sha256:config",
    )


def test_canvas_rejects_activity_without_deal() -> None:
    import pytest

    with pytest.raises(ValueError, match="requires deals first"):
        build_generation_canvas(
            generation_id="successor-1",
            boundary_digest="sha256:boundary",
            configuration_digest="sha256:config",
            entries=(_entry("crm_activities"),),
            task_kind="live",
            occurrence="2026-08-11",
        )
