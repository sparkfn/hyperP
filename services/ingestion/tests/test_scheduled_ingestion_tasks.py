"""Scheduler-owned publication, coalescing, and fail-closed readiness contracts."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from pytest import MonkeyPatch
from src.bitrix_backfill_models import BackfillInventoryEntry, BackfillInventoryManifest
from src.bitrix_ingestion_models import BitrixStreamKey
from src.bounded_ingestion_models import BoundedMode, OccurrenceContext, Usage
from src.graph.client import Neo4jClient
from src.graph.queries.scheduled_ingestion import (
    CLAIM_SCHEDULED_CHILD_PUBLICATION,
    CLAIM_SCHEDULED_MAINTENANCE,
    ENSURE_SCHEDULED_OCCURRENCE_AUTHORITY,
)
from src.models import JsonValue
from src.scheduled_ingestion_control import (
    ScheduledChildContext,
    ScheduledIngestionControl,
    ScheduledMaintenanceContext,
    ScheduledMaintenanceObligation,
    ScheduledProgress,
    ScheduledPublication,
    ScheduledWorkflow,
)
from src.scheduled_ingestion_groups import ScheduledIngestionSpec
from src.scheduled_ingestion_policy import weekly_occurrence

_MONDAY_OPEN = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
_DEFAULT_PUBLICATION = ScheduledPublication("workflow-1", "pub-1")
_DEFAULT_OBLIGATION = ScheduledMaintenanceObligation("obligation-1", "publishing")


def _occurrence(group_key: str = "fundbox", now: datetime = _MONDAY_OPEN) -> OccurrenceContext:
    return weekly_occurrence(
        group_key=group_key,
        weekday="monday",
        now=now,
        drain_reserve_seconds=300,
    ).context


def _child(**overrides: object) -> ScheduledChildContext:
    values: dict[str, object] = {
        "child_key": "fundbox|-",
        "source_key": "fundbox",
        "entity_key": None,
        "control_instance_id": "legacy-default",
        "mode": "delta",
        "configuration_fingerprint": "sha256:fixture",
        "connector_version": "fixture-v1",
        "configuration_version": "fixture-config-v1",
        "checkpoint_schema_version": 1,
        "source_window": {"lower_id": "1"},
        "reserved_usage": Usage(records=1, source_requests=1, pages=1),
    }
    values.update(overrides)
    return ScheduledChildContext(**values)  # type: ignore[arg-type]


class _Ready:
    """An admitting readiness boundary owned by a future evidence reader."""

    def __init__(self) -> None:
        self.block_reason: str | None = None
        self.maintenance_block_reason: str | None = None
        self.control_instance_id = "legacy-default"
        self.maintenance_usage = Usage(records=2)
        self.specs: list[ScheduledIngestionSpec] = []
        self.modes: list[BoundedMode] = []

    def resolve_child(
        self,
        *,
        spec: ScheduledIngestionSpec,
        mode: BoundedMode,
        **_kwargs: object,
    ) -> ScheduledChildContext | object:
        from src.scheduled_ingestion_tasks import SchedulerBlocked

        self.specs.append(spec)
        self.modes.append(mode)
        if self.block_reason is not None:
            return SchedulerBlocked(self.block_reason)
        return _child(
            child_key=spec.child_key,
            source_key=spec.source_key,
            entity_key=spec.entity_key,
            control_instance_id=self.control_instance_id,
            mode=mode,
        )

    def resolve_maintenance(self, **_kwargs: object) -> ScheduledMaintenanceContext | object:
        from src.scheduled_ingestion_tasks import SchedulerBlocked

        if self.maintenance_block_reason is not None:
            return SchedulerBlocked(self.maintenance_block_reason)
        return ScheduledMaintenanceContext(reserved_usage=self.maintenance_usage)


@dataclass
class _Recording:
    workflows: list[dict[str, object]] = field(default_factory=list)
    claims: list[dict[str, object]] = field(default_factory=list)
    blocks: list[dict[str, object]] = field(default_factory=list)
    obligation_claims: list[dict[str, object]] = field(default_factory=list)
    confirmations: list[str] = field(default_factory=list)
    signatures: list[tuple[ScheduledChildContext, OccurrenceContext, str, int]] = field(
        default_factory=list
    )
    published: list[dict[str, object]] = field(default_factory=list)


class _FakeSignature:
    def __init__(self, recording: _Recording) -> None:
        self._recording = recording

    def apply_async(self, **kwargs: object) -> None:
        self._recording.published.append(kwargs)


def _patch(
    monkeypatch: MonkeyPatch,
    recording: _Recording,
    *,
    workflow: ScheduledWorkflow | None = None,
    progress: ScheduledProgress | None = None,
    publication: ScheduledPublication | None = _DEFAULT_PUBLICATION,
    obligation: ScheduledMaintenanceObligation | None = _DEFAULT_OBLIGATION,
    reset_generation: int | None = 1,
    readiness: object | None = None,
    enabled: bool = True,
    successor: str | None = None,
) -> _Recording:
    """Install scheduler fakes with no broker, Redis, or Neo4j dependency."""
    from src import scheduled_ingestion_tasks as tasks
    from src.ingestion_config import IngestionConfig, ScheduledIngestionConfig

    # Captured before patching so readiness=None keeps the production gate.
    fail_closed = tasks.build_scheduler_readiness()
    resolved_workflow = workflow or ScheduledWorkflow(
        "workflow-1", "queued", False, 0, 0, None, None, None
    )

    class _Control:
        def __init__(self, _graph: object) -> None:
            self._workflow = resolved_workflow

        def active_reset_generation(self, _environment: str) -> int | None:
            return reset_generation

        def ensure_workflow(self, **kwargs: object) -> ScheduledWorkflow | None:
            recording.workflows.append(kwargs)
            return self._workflow if reset_generation is not None else None

        def reconcile_current_child(self, **_kwargs: object) -> ScheduledProgress | None:
            return progress

        def claim_current_child(self, **kwargs: object) -> ScheduledPublication | None:
            recording.claims.append(kwargs)
            return publication

        def confirm_child_publication(self, **kwargs: object) -> bool:
            recording.confirmations.append(str(kwargs["publication_id"]))
            return True

        def block_workflow(self, **kwargs: object) -> bool:
            recording.blocks.append(kwargs)
            return True

        def claim_maintenance_obligation(
            self, **kwargs: object
        ) -> ScheduledMaintenanceObligation | None:
            recording.obligation_claims.append(kwargs)
            return obligation

        def confirm_maintenance_obligation(self, **kwargs: object) -> bool:
            recording.confirmations.append(str(kwargs["bucket"]))
            return True

    class _Graph:
        def __init__(self, _settings: object) -> None:
            pass

        def close(self) -> None:
            pass

    def _child_signature(
        child: ScheduledChildContext,
        occurrence: OccurrenceContext,
        publication_id: str,
        generation: int,
    ) -> _FakeSignature:
        recording.signatures.append((child, occurrence, publication_id, generation))
        return _FakeSignature(recording)

    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: IngestionConfig(
            scheduled_ingestion=ScheduledIngestionConfig(enabled=enabled),
        ),
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: _Settings())
    monkeypatch.setattr(tasks, "Neo4jClient", _Graph)
    monkeypatch.setattr(tasks, "ScheduledIngestionControl", _Control)
    monkeypatch.setattr(tasks, "build_scheduler_readiness", lambda: readiness or fail_closed)
    monkeypatch.setattr(tasks, "_child_signature", _child_signature)
    monkeypatch.setattr(tasks, "_reconcile_signature", lambda *_args: object())
    monkeypatch.setattr(tasks, "_dispatch_active_bitrix_successor", lambda _occurrence: successor)
    return recording


class _Settings:
    deployment_environment = "test"


def test_child_signature_carries_the_complete_bounded_context() -> None:
    from src import scheduled_ingestion_tasks as tasks

    signature = tasks._child_signature(_child(stream_key="crm_deals"), _occurrence(), "pub-1", 3)

    assert signature.args == ("fundbox", "api")
    assert signature.kwargs["entity_key"] is None
    assert signature.kwargs["incremental"] is True
    assert signature.kwargs["require_clean_completion"] is True
    assert signature.kwargs["scheduled_dispatch"] is True
    assert signature.kwargs["idempotency_key"] == "pub-1"
    assert signature.kwargs["bounded_reset_generation"] == 3
    assert signature.kwargs["bounded_mode"] == "delta"
    assert signature.kwargs["bounded_stream_key"] == "crm_deals"
    assert signature.kwargs["bounded_source_window"] == {"lower_id": "1"}
    assert signature.kwargs["bounded_occurrence"]["occurrence_id"].startswith("scheduled:fundbox:")
    assert signature.kwargs["bounded_connector_version"] == "fixture-v1"
    assert signature.options["queue"] == "ingestion"
    assert signature.immutable


def test_child_signature_uses_bootstrap_mode_for_full_snapshot_sources() -> None:
    from src import scheduled_ingestion_tasks as tasks

    signature = tasks._child_signature(
        _child(child_key="sgbankruptcy|-", source_key="sgbankruptcy", mode="bootstrap"),
        _occurrence(),
        "pub-2",
        1,
    )

    assert signature.kwargs["incremental"] is False
    assert signature.kwargs["bounded_mode"] == "bootstrap"


def test_default_readiness_withholds_sources_and_maintenance() -> None:
    from src.scheduled_ingestion_groups import scheduled_ingestion_group
    from src.scheduled_ingestion_tasks import (
        FailClosedSchedulerReadiness,
        SchedulerBlocked,
        build_scheduler_readiness,
    )

    readiness = FailClosedSchedulerReadiness()
    group = scheduled_ingestion_group("fundbox")

    assert readiness.resolve_child(
        group=group,
        spec=group.tasks[0],
        mode="delta",
        environment="test",
        reset_generation=1,
        occurrence=_occurrence(),
    ) == SchedulerBlocked("accepted_seed_evidence_unavailable")
    assert readiness.resolve_maintenance(
        environment="test",
        reset_generation=1,
        occurrence=_occurrence(),
    ) == SchedulerBlocked("accepted_seed_evidence_unavailable")
    assert isinstance(build_scheduler_readiness(), FailClosedSchedulerReadiness)


def test_drive_publishes_one_complete_bounded_child(monkeypatch: MonkeyPatch) -> None:
    from src.scheduled_ingestion_tasks import _drive_group

    readiness = _Ready()
    recording = _patch(monkeypatch, _Recording(), readiness=readiness)

    result = _drive_group("fundbox", True, _MONDAY_OPEN)

    assert result == {
        "status": "published",
        "group_key": "fundbox",
        "incremental": True,
        "workflow_task_id": "pub-1",
    }
    assert [spec.source_key for spec in readiness.specs] == ["fundbox"]
    assert readiness.modes == ["delta"]
    assert len(recording.signatures) == 1
    child, occurrence, publication_id, generation = recording.signatures[0]
    assert (child.source_key, publication_id, generation) == ("fundbox", "pub-1", 1)
    assert occurrence == _occurrence()
    assert recording.claims[0]["child_index"] == 0
    published = recording.published
    assert [entry["task_id"] for entry in published] == ["pub-1"]
    assert "link" in published[0]
    assert "link_error" not in published[0]
    assert recording.blocks == []
    assert recording.confirmations == ["pub-1"]


def test_drive_resumes_the_unfinished_child_without_repeating_completed_ones(
    monkeypatch: MonkeyPatch,
) -> None:
    """Covers a lost completion callback: the graph, not Celery, holds progress."""
    from src.scheduled_ingestion_tasks import _drive_group

    readiness = _Ready()
    recording = _patch(
        monkeypatch,
        _Recording(),
        readiness=readiness,
        progress=ScheduledProgress(
            completed=False, current_child_index=2, status="paused_with_checkpoint"
        ),
    )

    result = _drive_group("fundbox", True, _MONDAY_OPEN)

    assert result["status"] == "published"
    assert [spec.source_key for spec in readiness.specs] == ["fundbox:sales"]
    assert [entry[0].source_key for entry in recording.signatures] == ["fundbox:sales"]
    assert recording.claims[0]["child_index"] == 2
    assert len(recording.published) == 1


def test_drive_keeps_full_snapshot_groups_on_bootstrap(monkeypatch: MonkeyPatch) -> None:
    from src.scheduled_ingestion_tasks import _drive_group

    readiness = _Ready()
    _patch(monkeypatch, _Recording(), readiness=readiness)

    result = _drive_group("sgbankruptcy", True, datetime(2026, 9, 18, 1, 0, tzinfo=UTC))

    assert result["status"] == "published"
    assert [spec.source_key for spec in readiness.specs] == ["sgbankruptcy"]
    assert readiness.modes == ["bootstrap"]


def test_manual_group_run_defaults_to_full_extraction(monkeypatch: MonkeyPatch) -> None:
    from src.scheduled_ingestion_tasks import _drive_group

    readiness = _Ready()
    recording = _patch(monkeypatch, _Recording(), readiness=readiness)

    result = _drive_group("fundbox", False, _MONDAY_OPEN)

    assert result["status"] == "published"
    assert readiness.modes == ["bootstrap"]
    assert recording.signatures[0][0].mode == "bootstrap"


_THURSDAY_OPEN = datetime(2026, 9, 17, 1, 0, tzinfo=UTC)


def test_drive_defers_bitrix_to_an_active_successor_schedule(monkeypatch: MonkeyPatch) -> None:
    """An active successor schedule owns the cadence."""
    # Cadence guardrail: an active successor must not publish legacy Bitrix.
    # The assertions below pin zero child publications and one durable block reason.
    from src.scheduled_ingestion_tasks import _drive_group

    recording = _patch(monkeypatch, _Recording(), readiness=_Ready(), successor="split-workflow")

    result = _drive_group("bitrix_chat", True, _THURSDAY_OPEN)

    assert result == {
        "status": "queued",
        "group_key": "bitrix_chat",
        "incremental": True,
        "workflow_task_id": "split-workflow",
    }
    assert [entry["reason"] for entry in recording.blocks] == ["bitrix_successor_schedule_active"]
    assert recording.published == []
    assert recording.signatures == []


def test_drive_publishes_the_bitrix_child_without_an_active_successor(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.scheduled_ingestion_tasks import _drive_group

    recording = _patch(monkeypatch, _Recording(), readiness=_Ready(), successor=None)

    result = _drive_group("bitrix_chat", True, _THURSDAY_OPEN)

    assert result["status"] == "published"
    assert [entry[0].source_key for entry in recording.signatures] == ["bitrix_chat"]
    assert recording.blocks == []


def test_drive_reports_completion_without_republishing(monkeypatch: MonkeyPatch) -> None:
    from src.scheduled_ingestion_tasks import _drive_group

    recording = _patch(
        monkeypatch,
        _Recording(),
        readiness=_Ready(),
        progress=ScheduledProgress(completed=True, current_child_index=3, status="completed"),
    )

    result = _drive_group("fundbox", True, _MONDAY_OPEN)

    assert result["status"] == "completed"
    assert recording.published == []
    assert recording.signatures == []


def test_drive_defers_a_duplicate_tick_with_a_durable_reason(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.scheduled_ingestion_tasks import _drive_group

    recording = _patch(monkeypatch, _Recording(), readiness=_Ready(), publication=None)

    result = _drive_group("fundbox", True, _MONDAY_OPEN)

    assert result["status"] == "coalesced"
    assert result["workflow_task_id"] == "workflow-1"
    assert [entry["reason"] for entry in recording.blocks] == ["child_publication_deferred"]
    assert recording.blocks[0]["next_eligible_at"] == datetime(2026, 9, 28, 1, 0, tzinfo=UTC)
    assert recording.published == []


def test_drive_uses_the_fail_closed_default_readiness(monkeypatch: MonkeyPatch) -> None:
    from src.scheduled_ingestion_tasks import _drive_group

    recording = _patch(monkeypatch, _Recording())

    result = _drive_group("fundbox", True, _MONDAY_OPEN)

    assert result["status"] == "blocked_readiness"
    assert [entry["reason"] for entry in recording.blocks] == ["accepted_seed_evidence_unavailable"]
    assert recording.published == []


def test_drive_never_advances_downstream_when_readiness_blocks(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.scheduled_ingestion_tasks import _drive_group

    readiness = _Ready()
    readiness.block_reason = "connector_capability_unverified"
    recording = _patch(monkeypatch, _Recording(), readiness=readiness)

    result = _drive_group("fundbox", True, _MONDAY_OPEN)

    assert result["status"] == "blocked_readiness"
    assert recording.published == []
    assert [entry["reason"] for entry in recording.blocks] == ["connector_capability_unverified"]
    assert recording.blocks[0]["next_eligible_at"] == datetime(2026, 9, 28, 1, 0, tzinfo=UTC)


def test_drive_persists_manual_pause_without_publication(monkeypatch: MonkeyPatch) -> None:
    from src.scheduled_ingestion_tasks import _drive_group

    paused = ScheduledWorkflow(
        "workflow-1", "paused", True, 0, 0, None, "scheduled:fundbox:x", None
    )
    recording = _patch(monkeypatch, _Recording(), readiness=_Ready(), workflow=paused)

    result = _drive_group("fundbox", True, _MONDAY_OPEN)

    assert result["status"] == "manual_pause"
    assert recording.published == []
    assert recording.blocks == []


def test_drive_records_cutoff_and_waits_for_the_next_weekly_opening(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.scheduled_ingestion_tasks import _drive_group

    recording = _patch(monkeypatch, _Recording(), readiness=_Ready())

    result = _drive_group("fundbox", True, datetime(2026, 9, 21, 15, 0, tzinfo=UTC))

    assert result["status"] == "window_closed"
    assert recording.published == []
    assert [entry["reason"] for entry in recording.blocks] == ["schedule_window_closed"]
    assert recording.blocks[0]["next_eligible_at"] == datetime(2026, 9, 28, 1, 0, tzinfo=UTC)


def test_drive_withholds_before_the_opening_without_blocking(monkeypatch: MonkeyPatch) -> None:
    from src.scheduled_ingestion_tasks import _drive_group

    recording = _patch(monkeypatch, _Recording(), readiness=_Ready())

    result = _drive_group("fundbox", True, datetime(2026, 9, 21, 0, 30, tzinfo=UTC))

    assert result["status"] == "window_before_open"
    assert recording.published == []
    assert recording.blocks == []


def test_drive_persists_a_disabled_latch_for_the_next_occurrence(
    monkeypatch: MonkeyPatch,
) -> None:
    """A disabled schedule latches durably and publishes nothing.

    Group resolution now only names the occurrence for that latch, so the legacy
    guardrail "disabled dispatch must not resolve a group" is asserted here as
    zero publications plus a durable disabled reason.
    """
    from src.scheduled_ingestion_tasks import _drive_group

    recording = _patch(monkeypatch, _Recording(), readiness=_Ready(), enabled=False)

    result = _drive_group("fundbox", True, _MONDAY_OPEN)

    assert result == {
        "status": "disabled",
        "group_key": "fundbox",
        "incremental": True,
        "workflow_task_id": "workflow-1",
    }
    assert [entry["reason"] for entry in recording.blocks] == ["disabled"]
    assert recording.blocks[0]["next_eligible_at"] == datetime(2026, 9, 28, 1, 0, tzinfo=UTC)
    assert recording.published == []


def test_drive_blocks_without_an_active_reset_generation(monkeypatch: MonkeyPatch) -> None:
    from src.scheduled_ingestion_tasks import _drive_group

    recording = _patch(monkeypatch, _Recording(), readiness=_Ready(), reset_generation=None)

    result = _drive_group("fundbox", True, _MONDAY_OPEN)

    assert result["status"] == "blocked_reset_generation"
    assert recording.published == []


def test_drive_blocks_a_child_owned_by_another_control_instance(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.scheduled_ingestion_tasks import _drive_group

    readiness = _Ready()
    readiness.control_instance_id = "tenant-a"
    recording = _patch(monkeypatch, _Recording(), readiness=readiness)

    result = _drive_group("fundbox", True, _MONDAY_OPEN)

    assert result["status"] == "blocked_control_instance"
    assert [entry["reason"] for entry in recording.blocks] == ["group_control_instance_mismatch"]
    assert recording.published == []


def test_maintenance_publishes_one_bounded_delivery_per_hour(monkeypatch: MonkeyPatch) -> None:
    from src import scheduled_ingestion_tasks as tasks

    recording = _patch(monkeypatch, _Recording(), readiness=_Ready())
    monkeypatch.setattr(tasks, "_utc_now", lambda: _MONDAY_OPEN)
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        "src.tasks.reconcile_lifecycle_task.apply_async",
        lambda **kwargs: published.append(kwargs),
    )

    result = tasks.dispatch_scheduled_maintenance_task.run("lifecycle")

    assert result == "published"
    assert len(recording.obligation_claims) == 1
    claim = recording.obligation_claims[0]
    assert claim["bucket"] == "20260921T01"
    assert claim["kind"] == "lifecycle"
    assert claim["context"] == ScheduledMaintenanceContext(reserved_usage=Usage(records=2))
    assert claim["occurrence"].occurrence_id.startswith("scheduled:fundbox:")
    assert [entry["task_id"] for entry in published] == ["obligation-1"]
    kwargs = cast(dict[str, object], published[0]["kwargs"])
    assert kwargs["bounded_logical_run_id"] == "obligation-1"
    occurrence_payload = cast(dict[str, str], kwargs["bounded_occurrence"])
    assert occurrence_payload["occurrence_id"] == claim["occurrence"].occurrence_id
    assert occurrence_payload["timezone"] == "Asia/Singapore"
    assert recording.confirmations == ["20260921T01"]


def test_maintenance_coalesces_duplicate_ticks_in_the_same_hour(monkeypatch: MonkeyPatch) -> None:
    from src import scheduled_ingestion_tasks as tasks

    recording = _patch(monkeypatch, _Recording(), readiness=_Ready(), obligation=None)
    monkeypatch.setattr(tasks, "_utc_now", lambda: _MONDAY_OPEN)

    result = tasks.dispatch_scheduled_maintenance_task.run("knows", "contacts")

    assert result == "coalesced"
    assert recording.confirmations == []


def test_maintenance_stays_fail_closed_without_accepted_evidence(monkeypatch: MonkeyPatch) -> None:
    from src import scheduled_ingestion_tasks as tasks

    recording = _patch(monkeypatch, _Recording())
    monkeypatch.setattr(tasks, "_utc_now", lambda: _MONDAY_OPEN)

    result = tasks.dispatch_scheduled_maintenance_task.run("lifecycle")

    assert result == "blocked_accepted_seed_evidence_unavailable"
    assert recording.obligation_claims == []


def test_maintenance_yields_outside_the_scheduled_window(monkeypatch: MonkeyPatch) -> None:
    from src import scheduled_ingestion_tasks as tasks

    recording = _patch(monkeypatch, _Recording(), readiness=_Ready())
    monkeypatch.setattr(tasks, "_utc_now", lambda: datetime(2026, 9, 20, 3, 0, tzinfo=UTC))

    result = tasks.dispatch_scheduled_maintenance_task.run("lifecycle")

    assert result == "window_closed"
    assert recording.obligation_claims == []


def test_maintenance_rejects_unsupported_arguments(monkeypatch: MonkeyPatch) -> None:
    from src import scheduled_ingestion_tasks as tasks

    monkeypatch.setattr(
        tasks,
        "get_ingestion_config",
        lambda: pytest.fail("invalid maintenance arguments must reject before configuration"),
    )

    with pytest.raises(ValueError, match="unknown scheduled maintenance kind"):
        tasks.dispatch_scheduled_maintenance_task.run("census")
    with pytest.raises(ValueError, match="supported phase"):
        tasks.dispatch_scheduled_maintenance_task.run("knows", "crm_deals")
    with pytest.raises(ValueError, match="does not accept a phase"):
        tasks.dispatch_scheduled_maintenance_task.run("lifecycle", "contacts")


class _FakeResult:
    def __init__(self, record: object | None) -> None:
        self._record = record

    def single(self) -> object | None:
        return self._record


class _FakeTransaction:
    """Record the durable claim parameters without a database."""

    def __init__(self, records: dict[str, object]) -> None:
        self._records = records
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> _FakeResult:
        self.calls.append((query, parameters))
        return _FakeResult(self._records.get(query))


class _FakeClient:
    """Minimal durable-store stand-in that records every claim parameter."""

    def __init__(self, records: dict[str, object]) -> None:
        self.transaction = _FakeTransaction(records)

    def execute_write(self, work: Callable[[_FakeTransaction], object]) -> object:
        return work(self.transaction)


def _control(records: dict[str, object]) -> tuple[ScheduledIngestionControl, _FakeClient]:
    client = _FakeClient(records)
    return ScheduledIngestionControl(cast(Neo4jClient, client)), client


def test_child_claim_ensures_the_shared_ceiling_before_one_deterministic_intent() -> None:
    from src.bounded_ingestion_budget import BoundedIngestionBudget

    budget = BoundedIngestionBudget()
    control, client = _control(
        {
            ENSURE_SCHEDULED_OCCURRENCE_AUTHORITY: object(),
            CLAIM_SCHEDULED_CHILD_PUBLICATION: {
                "workflow_id": "workflow-1",
                "publication_id": "publication-1",
            },
        }
    )
    occurrence = _occurrence()

    publication = control.claim_current_child(
        environment="test",
        reset_generation=1,
        group_key="fundbox",
        occurrence=occurrence,
        now=_MONDAY_OPEN,
        child_index=1,
        child=_child(child_key="fundbox:contacts|"),
        budget=budget,
    )

    assert publication == ScheduledPublication("workflow-1", "publication-1")
    queries = [query for query, _params in client.transaction.calls]
    assert queries == [ENSURE_SCHEDULED_OCCURRENCE_AUTHORITY, CLAIM_SCHEDULED_CHILD_PUBLICATION]
    claim = client.transaction.calls[1][1]
    assert claim["publication_id"] == (
        f"scheduled-group|test|1|legacy-default|fundbox:{occurrence.occurrence_id}:1"
    )
    assert claim["participant_key"] == (
        f"scheduled-occurrence|test|1|{occurrence.occurrence_id}|source|"
        f"scheduled-group|test|1|legacy-default|fundbox"
    )
    assert claim["reserved_records"] == 1
    assert claim["now"] == _MONDAY_OPEN.isoformat()
    authority = client.transaction.calls[0][1]
    assert authority["max_records"] == budget.max_records
    assert authority["max_extraction_calls"] == budget.max_extraction_calls


def test_maintenance_claim_shares_the_occurrence_ceiling_and_hour_bucket() -> None:
    from src.bounded_ingestion_budget import BoundedIngestionBudget

    control, client = _control(
        {
            ENSURE_SCHEDULED_OCCURRENCE_AUTHORITY: object(),
            CLAIM_SCHEDULED_MAINTENANCE: {
                "obligation_id": "obligation-1",
                "publication_state": "publishing",
            },
        }
    )
    occurrence = _occurrence()

    obligation = control.claim_maintenance_obligation(
        environment="test",
        reset_generation=1,
        kind="knows",
        phase="contacts",
        occurrence=occurrence,
        bucket="20260921T01",
        context=ScheduledMaintenanceContext(reserved_usage=Usage(records=7)),
        budget=BoundedIngestionBudget(),
    )

    assert obligation == ScheduledMaintenanceObligation("obligation-1", "publishing")
    queries = [query for query, _params in client.transaction.calls]
    assert queries == [ENSURE_SCHEDULED_OCCURRENCE_AUTHORITY, CLAIM_SCHEDULED_MAINTENANCE]
    claim = client.transaction.calls[1][1]
    assert claim["occurrence_authority_key"] == (
        f"scheduled-occurrence|test|1|{occurrence.occurrence_id}"
    )
    assert claim["participant_key"] == (
        f"scheduled-occurrence|test|1|{occurrence.occurrence_id}|maintenance|knows|contacts"
    )
    assert claim["bucket"] == "20260921T01"
    assert claim["scope_key"].endswith(f"{occurrence.occurrence_id}|20260921T01")
    assert claim["reserved_records"] == 7
    assert claim["phase"] == "contacts"


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
