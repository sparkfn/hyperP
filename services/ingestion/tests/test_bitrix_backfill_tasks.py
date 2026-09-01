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
from src.crm_deal_identity_repair.control_models import RepairPublicationReservation
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


def test_generation_publication_admission_precedes_canvas_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Canvas:
        def apply_async(self) -> object:
            raise AssertionError("canvas must not publish after rejected admission")

    admission = Mock(side_effect=RuntimeError("blocked"))
    monkeypatch.setattr(
        "src.graph.bitrix_source_instances.admit_configured_bitrix_control",
        admission,
    )
    monkeypatch.setattr("src.config.get_settings", lambda: object())
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

    admission.assert_called_once()


class _PublicationGate:
    def __init__(self, *, fail_confirm: bool = False) -> None:
        self._fail_confirm = fail_confirm
        self.calls: list[str] = []

    def mark_publishing(
        self, reservation: RepairPublicationReservation
    ) -> RepairPublicationReservation:
        self.calls.append("mark")
        return RepairPublicationReservation(
            reservation.reservation_id,
            reservation.control_instance_id,
            reservation.publication_key,
            "publishing",
            reservation.revision + 1,
        )

    def confirm_publication(
        self, reservation: RepairPublicationReservation, workflow_task_id: str
    ) -> RepairPublicationReservation:
        self.calls.append("confirm:" + workflow_task_id)
        if self._fail_confirm:
            raise RuntimeError("ambiguous publish")
        return RepairPublicationReservation(
            reservation.reservation_id,
            reservation.control_instance_id,
            reservation.publication_key,
            "confirmed",
            reservation.revision + 1,
        )


def test_generation_publication_uses_one_reservation_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Result:
        id = "broker-task-1"

    class _Canvas:
        def apply_async(self) -> _Result:
            return _Result()

    monkeypatch.setattr("src.config.get_settings", lambda: object())
    monkeypatch.setattr(
        "src.graph.bitrix_source_instances.admit_configured_bitrix_control", lambda *_args: None
    )
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.build_generation_canvas", lambda **_kwargs: _Canvas()
    )
    reservation = RepairPublicationReservation(
        "reservation-1", "legacy-default", "key-1", "preparing", 1
    )
    gate = _PublicationGate()
    assert (
        dispatch_generation_canvas(
            generation_id="corrective-1",
            boundary_digest="sha256:boundary",
            configuration_digest="sha256:config",
            entries=(_entry("crm_deals"),),
            publication_reservation=reservation,
            publication_gate=gate,
        )
        == "broker-task-1"
    )
    assert gate.calls == ["mark", "confirm:broker-task-1"]


def test_generation_publication_confirmation_failure_is_not_silently_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Result:
        id = "broker-task-ambiguous"

    class _Canvas:
        def apply_async(self) -> _Result:
            return _Result()

    monkeypatch.setattr("src.config.get_settings", lambda: object())
    monkeypatch.setattr(
        "src.graph.bitrix_source_instances.admit_configured_bitrix_control", lambda *_args: None
    )
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.build_generation_canvas", lambda **_kwargs: _Canvas()
    )
    with pytest.raises(RuntimeError, match="ambiguous publish"):
        dispatch_generation_canvas(
            generation_id="corrective-1",
            boundary_digest="sha256:boundary",
            configuration_digest="sha256:config",
            entries=(_entry("crm_deals"),),
            publication_reservation=RepairPublicationReservation(
                "reservation-2", "legacy-default", "key-2", "preparing", 1
            ),
            publication_gate=_PublicationGate(fail_confirm=True),
        )
