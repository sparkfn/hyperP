"""Corrective resume generation selection regression tests."""

from types import SimpleNamespace
from unittest.mock import Mock

from src.bitrix_backfill_control import BitrixBackfillControl
from src.bitrix_backfill_models import GenerationChildRun


def test_resume_advances_past_completed_sibling_generation(monkeypatch) -> None:
    repository = Mock()
    repository.get_generation.return_value = SimpleNamespace(
        status="backfilling",
        boundary_digest="sha256:boundary",
        configuration_digest="sha256:configuration",
    )
    repository.list_child_runs.return_value = [
        GenerationChildRun(
            stream_key="crm_deals",
            logical_run_id="deal-run",
            logical_status="completed",
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
    control._manifest_for = Mock(
        return_value=SimpleNamespace(executable_entries=("deals", "activities"))
    )
    dispatched: dict[str, object] = {}

    def fake_dispatch_generation_canvas(**kwargs):
        dispatched.update(kwargs)
        return "canvas-id"

    monkeypatch.setattr(
        "src.bitrix_backfill_tasks.dispatch_generation_canvas",
        fake_dispatch_generation_canvas,
    )

    assert control.resume("corrective-generation") == "canvas-id"
    assert dispatched["resume_generation"] == 12
