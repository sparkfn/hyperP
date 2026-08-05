"""Tests for ordered Celery execution of all-source ingestion."""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

import pytest
from _orchestration_test_helpers import manifest_json
from celery.canvas import Signature
from celery.exceptions import Reject
from pytest import MonkeyPatch
from src import ingestion_orchestrator as orchestrator


class _FakeGroupResult:
    id = "dependent-group-123"


class _FakeGroup:
    def __init__(self, signatures: list[Signature]) -> None:
        self.signatures = signatures
        self.queue: str | None = None
        self.priority: int | None = None

    def apply_async(self, *, queue: str, priority: int) -> _FakeGroupResult:
        self.queue = queue
        self.priority = priority
        return _FakeGroupResult()


class _FakeWorkflowResult:
    id = "workflow-123"


class _FakeWorkflow:
    def __init__(self) -> None:
        self.queue: str | None = None
        self.priority: int | None = None

    def apply_async(self, *, queue: str, priority: int) -> _FakeWorkflowResult:
        self.queue = queue
        self.priority = priority
        return _FakeWorkflowResult()


def test_start_task_builds_identity_group_before_the_dependent_callback(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import ingestion_orchestration_tasks as tasks

    captured_groups: list[_FakeGroup] = []
    captured_steps: list[object] = []
    workflow = _FakeWorkflow()

    def fake_group(signatures: Iterable[Signature]) -> _FakeGroup:
        result = _FakeGroup(list(signatures))
        captured_groups.append(result)
        return result

    def fake_chain(*steps: object) -> _FakeWorkflow:
        captured_steps.extend(steps)
        return workflow

    monkeypatch.setattr(tasks, "group", fake_group)
    monkeypatch.setattr(tasks, "chain", fake_chain)
    monkeypatch.setattr(
        tasks,
        "_queued_orchestration_result_id",
        lambda _phase, _orchestration_id: None,
    )

    def mark_queued(_phase: str, _orchestration_id: str, _result_id: str) -> None:
        assert workflow.queue == "ingestion"

    monkeypatch.setattr(tasks, "_mark_orchestration_phase_queued", mark_queued)

    manifest = orchestrator.parse_manifest(manifest_json())
    result = tasks.start_orchestrated_ingestion_task.run(manifest.to_payload())

    assert result["status"] == "identity_queued"
    assert result["workflow_task_id"] == "workflow-123"
    assert workflow.queue == "ingestion"
    assert workflow.priority == 0
    assert captured_steps[0] is captured_groups[0]
    callback = cast(Signature, captured_steps[1])
    assert callback.task == "src.ingestion_orchestration_tasks.start_dependent_ingestions_task"
    assert [signature.options["priority"] for signature in captured_groups[0].signatures] == [1] * 6
    assert all(
        signature.kwargs["wait_for_source"] is True
        for signature in captured_groups[0].signatures
    )


def test_dependent_phase_is_only_queued_after_clean_identity_results(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import ingestion_orchestration_tasks as tasks

    captured: list[_FakeGroup] = []

    def fake_group(signatures: Iterable[Signature]) -> _FakeGroup:
        result = _FakeGroup(list(signatures))
        captured.append(result)
        return result

    monkeypatch.setattr(tasks, "group", fake_group)
    monkeypatch.setattr(
        tasks,
        "_queued_orchestration_result_id",
        lambda _phase, _orchestration_id: None,
    )
    monkeypatch.setattr(
        tasks,
        "_mark_orchestration_phase_queued",
        lambda _phase, _orchestration_id, _result_id: None,
    )
    manifest = orchestrator.parse_manifest(manifest_json())
    identity_results = [
        {
            "ingest_run_id": "run",
            "status": "completed",
            "succeeded": 1,
            "errors": 0,
            "skipped": 0,
            "source_key": spec.source_key,
            "mode": spec.mode,
            "dump_path": spec.dump_path,
            "entity_key": spec.entity_key,
        }
        for spec in manifest.identity
    ]

    result = tasks.start_dependent_ingestions_task.run(
        identity_results,
        manifest.to_payload(),
        "orchestration-123",
    )

    assert result == {
        "status": "dependent_queued",
        "dependent_task_count": 9,
        "dependent_group_id": "dependent-group-123",
    }
    assert captured[0].queue == "ingestion"
    assert captured[0].priority == 0
    assert [signature.options["priority"] for signature in captured[0].signatures] == [5] * 9


def test_dependent_phase_is_rejected_after_an_identity_error() -> None:
    from src import ingestion_orchestration_tasks as tasks

    manifest = orchestrator.parse_manifest(manifest_json())
    failed_identity = [
        {
            "ingest_run_id": "run",
            "status": "completed_with_errors" if spec.source_key == "fundbox" else "completed",
            "succeeded": 1,
            "errors": 1 if spec.source_key == "fundbox" else 0,
            "skipped": 0,
            "source_key": spec.source_key,
            "mode": spec.mode,
            "dump_path": spec.dump_path,
            "entity_key": spec.entity_key,
        }
        for spec in manifest.identity
    ]

    with pytest.raises(Reject, match="fundbox=completed_with_errors"):
        tasks.start_dependent_ingestions_task.run(
            failed_identity,
            manifest.to_payload(),
            "orchestration-123",
        )


def test_dependent_phase_rejects_incomplete_identity_results() -> None:
    from src import ingestion_orchestration_tasks as tasks

    manifest = orchestrator.parse_manifest(manifest_json())
    incomplete_results = [
        {
            "ingest_run_id": "run",
            "status": "completed",
            "succeeded": 1,
            "errors": 0,
            "skipped": 0,
            "source_key": "fundbox",
            "mode": "api",
            "dump_path": None,
            "entity_key": None,
        }
    ]

    with pytest.raises(Reject, match="incomplete or duplicate"):
        tasks.start_dependent_ingestions_task.run(
            incomplete_results,
            manifest.to_payload(),
            "orchestration-123",
        )


def test_dependent_phase_redelivery_does_not_queue_children_twice(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import ingestion_orchestration_tasks as tasks

    manifest = orchestrator.parse_manifest(manifest_json())
    identity_results = [
        {
            "ingest_run_id": "run",
            "status": "completed",
            "succeeded": 1,
            "errors": 0,
            "skipped": 0,
            "source_key": spec.source_key,
            "mode": spec.mode,
            "dump_path": spec.dump_path,
            "entity_key": spec.entity_key,
        }
        for spec in manifest.identity
    ]
    monkeypatch.setattr(
        tasks,
        "_queued_orchestration_result_id",
        lambda _phase, _id: "dependent-group-existing",
    )
    monkeypatch.setattr(
        tasks,
        "group",
        lambda _signatures: (_ for _ in ()).throw(AssertionError("children queued twice")),
    )

    result = tasks.start_dependent_ingestions_task.run(
        identity_results,
        manifest.to_payload(),
        "orchestration-123",
    )

    assert result == {
        "status": "already_queued",
        "dependent_task_count": 9,
        "dependent_group_id": "dependent-group-existing",
    }
