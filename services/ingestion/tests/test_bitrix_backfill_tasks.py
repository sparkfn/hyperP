"""Corrective task publication is stable and strictly ordered."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from celery.canvas import Signature, _chain
from src.bitrix_backfill_models import BackfillInventoryEntry
from src.bitrix_backfill_tasks import (
    build_generation_canvas,
    corrective_task_id,
    dispatch_generation_canvas,
    live_task_id,
)
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


def test_live_resume_changes_worker_task_id_but_preserves_idempotency_key() -> None:
    canvas = build_generation_canvas(
        generation_id="successor-1",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:config",
        entries=(_entry("crm_deals"),),
        task_kind="live",
        occurrence="2026-08-11T13:18:52Z",
        resume_generation=4,
    )
    task = canvas.tasks[0]
    original_id = live_task_id(
        "2026-08-11T13:18:52Z",
        "crm_deals",
        "sha256:config",
    )
    assert task.kwargs["idempotency_key"] == original_id
    assert task.options["task_id"] == f"{original_id}:resume:4"


def test_scheduled_live_canvas_marks_every_delayed_step_as_cancellable() -> None:
    canvas = build_generation_canvas(
        generation_id="successor-1",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:config",
        entries=(_entry("crm_deals"), _entry("crm_activities")),
        task_kind="live",
        occurrence="2026-08-11",
        scheduled_dispatch=True,
    )

    assert all(task.kwargs["scheduled_dispatch"] is True for task in canvas.tasks)


def test_nondefault_canvas_scopes_only_new_task_identity_and_payload() -> None:
    canvas = build_generation_canvas(
        generation_id="same-generation",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:config",
        entries=(_entry("crm_deals"),),
        control_instance_id="portal-two",
    )
    task = canvas.tasks[0]
    assert task.options["task_id"] == corrective_task_id(
        "same-generation",
        "crm_deals",
        "sha256:boundary",
        "sha256:config",
        control_instance_id="portal-two",
    )
    assert task.kwargs["idempotency_key"] == corrective_task_id(
        "same-generation",
        "crm_deals",
        "sha256:boundary",
        "sha256:config",
        control_instance_id="portal-two",
    )
    assert task.kwargs["control_instance_id"] == "portal-two"


def test_legacy_canvas_payload_does_not_add_control_instance_id() -> None:
    canvas = build_generation_canvas(
        generation_id="corrective-1",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:config",
        entries=(_entry("crm_deals"),),
    )
    assert "control_instance_id" not in canvas.tasks[0].kwargs


def _reservation() -> object:
    from src.graph.crm_deal_identity_repair_publication import RepairPublicationReservation

    return RepairPublicationReservation(
        "legacy-default",
        "crm_deals",
        "sha256:" + "a" * 64,
        "test",
        "token-test",
        "pending",
        None,
        False,
    )



def test_generation_publication_admission_precedes_canvas_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Canvas:
        def apply_async(self) -> object:
            raise AssertionError("canvas must not publish after rejected admission")

    admission = Mock(side_effect=RuntimeError("blocked"))
    gate = Mock()
    monkeypatch.setattr(
        "src.graph.bitrix_source_instances.admit_configured_bitrix_control",
        admission,
    )
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks._assert_repair_dispatch_unblocked",
        gate,
    )
    monkeypatch.setattr("src.config.get_settings", lambda: object())
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.reserve_generation_publication",
        lambda **_kwargs: _reservation(),
    )
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks._begin_generation_publication",
        lambda _reservation: None,
    )
    monkeypatch.setattr("src.bitrix_backfill_tasks._mark_generation_published", lambda *_args: None)
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.build_generation_canvas", lambda **_kwargs: _Canvas()
    )

    with pytest.raises(RuntimeError, match="blocked"):
        dispatch_generation_canvas(
            generation_id="corrective-1",
            boundary_digest="sha256:boundary",
            configuration_digest="sha256:config",
            entries=(_entry("crm_deals"),),
        )

    gate.assert_called_once_with("legacy-default")
    admission.assert_called_once()


def test_generation_repair_block_precedes_canvas_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = Mock(side_effect=RuntimeError("repair blocked"))
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks._assert_repair_dispatch_unblocked",
        blocked,
    )
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.reserve_generation_publication",
        lambda **_kwargs: _reservation(),
    )
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.build_generation_canvas",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("canvas must not be built")),
    )

    with pytest.raises(RuntimeError, match="repair blocked"):
        dispatch_generation_canvas(
            generation_id="corrective-1",
            boundary_digest="sha256:boundary",
            configuration_digest="sha256:config",
            entries=(_entry("crm_deals"),),
        )

    blocked.assert_called_once_with("legacy-default")


def _capture_canvas_entries(captured: dict[str, object], entries: object) -> object:
    captured["entries"] = entries
    result_type = type("Result", (), {"id": "repair-publication"})
    canvas_type = type("Canvas", (), {"apply_async": lambda self: result_type()})
    return canvas_type()


def _stage_entry() -> BackfillInventoryEntry:
    return BackfillInventoryEntry(
        gap_id="gap-stage-history",
        stream_key="crm_stage_history",
        bounded_population=10,
        current_count=0,
        source_basis="standalone stage artifact",
        expected_repair="not repair-owned",
        replay_mode="strict_keyset",
        source_window={"artifact_id": "stage-artifact"},
        completion_equation="stage artifact accounting",
        max_calls=100,
        max_rows=100,
        max_runtime_seconds=100,
        max_storage_bytes=1000,
        max_lock_seconds=10,
        max_lag_seconds=60,
        rollback_path="preserve evidence",
    )


def test_stage_history_only_dispatch_bypasses_repair_gate_and_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Canvas:
        def apply_async(self) -> object:
            return type("Result", (), {"id": "stage-publication"})()

    gate = Mock(side_effect=AssertionError("stage-only publication must bypass repair gate"))
    reserve = Mock(side_effect=AssertionError("stage-only publication must not reserve"))
    monkeypatch.setattr("src.bitrix_backfill_tasks._assert_repair_dispatch_unblocked", gate)
    monkeypatch.setattr("src.bitrix_backfill_tasks.reserve_generation_publication", reserve)
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.build_generation_canvas",
        lambda **_kwargs: _Canvas(),
    )
    monkeypatch.setattr("src.config.get_settings", lambda: object())
    monkeypatch.setattr(
        "src.graph.bitrix_source_instances.admit_configured_bitrix_control",
        lambda *_args: None,
    )

    publication_id = dispatch_generation_canvas(
        generation_id="stage-only",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:config",
        entries=(_stage_entry(),),
    )

    assert publication_id == "stage-publication"
    gate.assert_not_called()
    reserve.assert_not_called()


def test_mixed_generation_reserves_only_repair_streams_and_filters_stage_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Canvas:
        def apply_async(self) -> object:
            return type("Result", (), {"id": "repair-publication"})()

    captured: dict[str, object] = {}
    reservation = _reservation()
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks._assert_repair_dispatch_unblocked",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.reserve_generation_publication",
        lambda **_kwargs: reservation,
    )
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks._begin_generation_publication",
        lambda *_args: None,
    )
    monkeypatch.setattr("src.bitrix_backfill_tasks._mark_generation_published", lambda *_args: None)
    monkeypatch.setattr("src.config.get_settings", lambda: object())
    monkeypatch.setattr(
        "src.graph.bitrix_source_instances.admit_configured_bitrix_control",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.build_generation_canvas",
        lambda **kwargs: _capture_canvas_entries(captured, kwargs["entries"]),
    )

    publication_id = dispatch_generation_canvas(
        generation_id="mixed",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:config",
        entries=(_entry("crm_deals"), _stage_entry()),
    )

    assert publication_id == "repair-publication"
    canvas_entries = captured["entries"]
    assert isinstance(canvas_entries, tuple)
    assert [entry.stream_key for entry in canvas_entries] == ["crm_deals"]
