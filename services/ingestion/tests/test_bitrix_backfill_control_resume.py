"""Corrective resume generation selection regression tests."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pytest import MonkeyPatch
from src.bitrix_backfill_control import BitrixBackfillControl
from src.bitrix_backfill_models import CoverageReconciliation, GenerationChildRun


@pytest.fixture(autouse=True)
def _admit_legacy_control(monkeypatch: MonkeyPatch) -> None:
    def admit(_settings: object, control_instance_id: str) -> None:
        assert control_instance_id == "legacy-default"

    monkeypatch.setattr("src.bitrix_backfill_control.admit_configured_bitrix_control", admit)


def test_start_reserves_before_the_corrective_canvas_is_built(
    monkeypatch: MonkeyPatch,
) -> None:
    repository = Mock()
    repository.get_generation.return_value = SimpleNamespace(
        status="allocated",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:configuration",
        control_instance_id="legacy-default",
    )
    control = object.__new__(BitrixBackfillControl)
    control._repository = repository
    control._manifest_for = Mock(
        return_value=SimpleNamespace(
            digest="sha256:inventory",
            operational_entries=(SimpleNamespace(stream_key="crm_deals"),),
        )
    )
    reservation = Mock(name="reservation")
    gate = Mock(name="publication_gate")
    order: list[str] = []

    def prepare(control_instance_id: str, publication_key: str) -> tuple[Mock, Mock]:
        assert control_instance_id == "legacy-default"
        assert publication_key == "corrective-generation:corrective"
        order.append("reserve")
        return reservation, gate

    def dispatch(**kwargs: object) -> str:
        order.append("build-and-publish")
        assert kwargs["publication_reservation"] is reservation
        assert kwargs["publication_gate"] is gate
        return "canvas-id"

    control._prepare_publication = Mock(side_effect=prepare)  # type: ignore[method-assign]
    monkeypatch.setattr("src.bitrix_backfill_tasks.dispatch_generation_canvas", dispatch)

    assert control.start("corrective-generation", actor="operator") == "canvas-id"
    assert order == ["reserve", "build-and-publish"]


def test_resume_advances_past_historical_worker_generation(
    monkeypatch: MonkeyPatch,
) -> None:
    repository = Mock()
    repository.get_generation.return_value = SimpleNamespace(
        status="backfilling",
        generation_kind="corrective",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:configuration",
        control_instance_id="legacy-default",
    )
    repository.get_max_resume_worker_generation.return_value = 13
    repository.list_child_runs.return_value = [
        GenerationChildRun(
            stream_key="crm_deals",
            logical_run_id="deal-run",
            logical_status="failed",
            attempt_generation=11,
            stream_status="completed",
        ),
        GenerationChildRun(
            stream_key="crm_activities",
            logical_run_id="activity-run",
            logical_status="failed",
            attempt_generation=10,
            stream_status="terminated",
        ),
    ]
    control = object.__new__(BitrixBackfillControl)
    control._repository = repository
    control._manifest_for = Mock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            operational_entries=(SimpleNamespace(stream_key="crm_deals"),),
        )
    )
    prepared = (Mock(name="reservation"), Mock(name="publication_gate"))
    control._prepare_publication = Mock(return_value=prepared)  # type: ignore[method-assign]
    dispatched: dict[str, object] = {}

    def fake_dispatch_generation_canvas(**kwargs: object) -> str:
        dispatched.update(kwargs)
        return "canvas-id"

    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.dispatch_generation_canvas",
        fake_dispatch_generation_canvas,
    )

    assert control.resume("corrective-generation") == "canvas-id"
    assert dispatched["resume_generation"] == 14
    assert dispatched["entries"] == control._manifest_for.return_value.operational_entries
    assert dispatched["publication_reservation"] is prepared[0]
    control._prepare_publication.assert_called_once_with(
        "legacy-default", "corrective-generation:corrective:resume:14"
    )


def test_resume_materialized_successor_uses_live_identity_and_new_worker_task(
    monkeypatch: MonkeyPatch,
) -> None:
    repository = Mock()
    repository.get_generation.return_value = SimpleNamespace(
        status="active",
        generation_kind="live_successor",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:configuration",
        control_instance_id="legacy-default",
    )
    repository.get_successor_publication_occurrence.return_value = "2026-08-11T13:18:52Z"
    repository.get_max_resume_worker_generation.return_value = 2
    repository.list_child_runs.return_value = [
        GenerationChildRun(
            stream_key="crm_deals",
            logical_run_id="deal-run",
            logical_status="failed",
            attempt_generation=3,
            stream_status="terminated",
        )
    ]
    control = object.__new__(BitrixBackfillControl)
    control._repository = repository
    control._manifest_for = Mock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(operational_entries=(SimpleNamespace(stream_key="crm_deals"),))
    )
    prepared = (Mock(name="reservation"), Mock(name="publication_gate"))
    control._prepare_publication = Mock(return_value=prepared)  # type: ignore[method-assign]
    dispatched: dict[str, object] = {}

    def fake_dispatch_generation_canvas(**kwargs: object) -> str:
        dispatched.update(kwargs)
        return "live-resume-canvas"

    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.dispatch_generation_canvas",
        fake_dispatch_generation_canvas,
    )

    assert (
        control.resume("successor-generation", occurrence="2026-08-11T13:18:52Z")
        == "live-resume-canvas"
    )
    assert dispatched["task_kind"] == "live"
    assert dispatched["occurrence"] == "2026-08-11T13:18:52Z"
    assert dispatched["resume_generation"] == 4
    assert dispatched["publication_reservation"] is prepared[0]
    control._prepare_publication.assert_called_once_with(
        "legacy-default", "successor-generation:successor:resume:4"
    )


def test_successor_resume_loads_the_original_occurrence_from_activation_evidence(
    monkeypatch: MonkeyPatch,
) -> None:
    repository = Mock()
    repository.get_generation.return_value = SimpleNamespace(
        status="active",
        generation_kind="live_successor",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:configuration",
        control_instance_id="legacy-default",
    )
    repository.get_successor_publication_occurrence.return_value = "2026-08-11T13:18:52Z"
    repository.get_max_resume_worker_generation.return_value = 3
    repository.list_child_runs.return_value = [
        GenerationChildRun(
            stream_key="crm_deals",
            logical_run_id="deal-run",
            logical_status="failed",
            attempt_generation=3,
            stream_status="terminated",
        )
    ]
    control = object.__new__(BitrixBackfillControl)
    control._repository = repository
    control._manifest_for = Mock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(operational_entries=(SimpleNamespace(stream_key="crm_deals"),))
    )
    prepared = (Mock(name="reservation"), Mock(name="publication_gate"))
    control._prepare_publication = Mock(return_value=prepared)  # type: ignore[method-assign]
    dispatched: dict[str, object] = {}

    def fake_dispatch_generation_canvas(**kwargs: object) -> str:
        dispatched.update(kwargs)
        return "live-resume-canvas"

    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.dispatch_generation_canvas",
        fake_dispatch_generation_canvas,
    )

    assert control.resume("successor-generation") == "live-resume-canvas"
    assert dispatched["occurrence"] == "2026-08-11T13:18:52Z"
    assert dispatched["publication_reservation"] is prepared[0]
    control._prepare_publication.assert_called_once_with(
        "legacy-default", "successor-generation:successor:resume:4"
    )


def test_successor_resume_rejects_a_changed_occurrence() -> None:
    repository = Mock()
    repository.get_generation.return_value = SimpleNamespace(
        status="active",
        generation_kind="live_successor",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:configuration",
        control_instance_id="legacy-default",
    )
    repository.get_successor_publication_occurrence.return_value = "2026-08-11T13:18:52Z"
    control = object.__new__(BitrixBackfillControl)
    control._repository = repository

    with pytest.raises(ValueError, match="does not match activation evidence"):
        control.resume("successor-generation", occurrence="2026-08-12T13:18:52Z")


def test_resume_rejects_retired_only_paused_children_without_publication(
    monkeypatch: MonkeyPatch,
) -> None:
    repository = Mock()
    repository.get_generation.return_value = SimpleNamespace(
        status="backfilling",
        generation_kind="corrective",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:configuration",
        control_instance_id="legacy-default",
    )
    repository.list_child_runs.return_value = [
        GenerationChildRun(
            stream_key="crm_activities",
            logical_run_id="activity-run",
            logical_status="paused_with_checkpoint",
            attempt_generation=1,
            stream_status="paused",
        )
    ]
    control = object.__new__(BitrixBackfillControl)
    control._repository = repository
    control._manifest_for = Mock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(operational_entries=(SimpleNamespace(stream_key="crm_deals"),))
    )
    control._prepare_publication = Mock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.dispatch_generation_canvas",
        lambda **_kwargs: pytest.fail("retired-only children must not publish"),
    )

    with pytest.raises(RuntimeError, match="no operational paused or failed"):
        control.resume("corrective-generation")

    control._prepare_publication.assert_not_called()


def test_reconcile_ignores_completed_historical_activity_children() -> None:
    repository = Mock()
    repository.get_generation.return_value = SimpleNamespace(status="backfilling")
    repository.list_child_runs.return_value = [
        GenerationChildRun(
            stream_key="crm_deals",
            logical_run_id="deal-run",
            logical_status="completed",
            attempt_generation=1,
            stream_status="completed",
        ),
        GenerationChildRun(
            stream_key="crm_activities",
            logical_run_id="activity-run",
            logical_status="completed",
            attempt_generation=1,
            stream_status="completed",
        ),
    ]
    repository.reconcile_coverage.return_value = CoverageReconciliation(
        stream_key="crm_deals",
        coverage_count=10,
        terminal_count=10,
        created_count=10,
        duplicate_count=0,
        projection_count=0,
        unchanged_count=0,
        excluded_count=0,
        quarantine_count=0,
        conflict_count=0,
        failed_count=0,
        checkpoint_committed_count=10,
        checkpoint_duplicate_count=0,
        checkpoint_excluded_count=0,
        checkpoint_retry_count=0,
    )
    control = object.__new__(BitrixBackfillControl)
    control._repository = repository
    entry = SimpleNamespace(stream_key="crm_deals", bounded_population=10, max_rows=10)
    control._manifest_for = Mock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(operational_entries=(entry,))
    )

    digest = control.reconcile("corrective-generation", actor="operator")

    assert digest.startswith("sha256:")
    repository.record_reconciliation.assert_called_once()
    assert repository.record_reconciliation.call_args.kwargs["stream_keys"] == ("crm_deals",)
