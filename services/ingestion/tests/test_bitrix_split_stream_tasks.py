"""Canonical split Bitrix task dispatch regression coverage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest
from pytest import MonkeyPatch
from src import tasks
from src.bitrix_ingestion_models import FenceContext
from src.graph.ingestion_control_models import BitrixStreamAdmission, LogicalRunAttempt
from src.main import IngestionSummary


@dataclass
class _Client:
    closed: bool = False

    def close(self) -> None:
        self.closed = True


class _LogicalControl:
    created: list[str] = []
    finalized: list[str] = []
    failed: list[str] = []

    def __init__(self, _client: object) -> None:
        pass

    def create_or_reuse(self, **parameters: object) -> LogicalRunAttempt:
        self.created.append(cast(str, parameters["idempotency_key"]))
        return LogicalRunAttempt(
            logical_run_id="logical-1",
            ingest_run_id="ingest-1",
            worker_task_id="task-1",
            generation=1,
            logical_status="queued",
            created=True,
        )

    def claim(self, **_parameters: object) -> bool:
        return True

    def finalize_fenced(self, **parameters: object) -> None:
        self.finalized.append(cast(str, parameters["status"]))

    def fail_fenced(self, **_parameters: object) -> None:
        raise AssertionError("successful task must not fail the logical run")

    def fail(self, **parameters: object) -> bool:
        self.failed.append(cast(str, parameters["failure_category"]))
        return True


class _StreamControl:
    def __init__(self, _client: object) -> None:
        pass

    def admit_or_coalesce(self, **_parameters: object) -> BitrixStreamAdmission:
        return BitrixStreamAdmission(
            outcome="admitted",
            fence_context=FenceContext(
                logical_run_id="logical-1",
                ingest_run_id="ingest-1",
                source_key="bitrix_chat",
                stream_key="crm_deals",
                stream_generation=1,
                fencing_token=1,
                attempt_generation=1,
            ),
            worker_task_id="task-1",
        )


def test_split_helper_uses_one_control_plane_run_and_passes_execution_context(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _Client()
    observed: dict[str, object] = {}
    _LogicalControl.created.clear()
    _LogicalControl.finalized.clear()
    _LogicalControl.failed.clear()
    monkeypatch.setattr(tasks, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(tasks, "LogicalRunControl", _LogicalControl)
    monkeypatch.setattr(tasks, "BitrixStreamControl", _StreamControl)
    monkeypatch.setattr(tasks, "get_settings", lambda: object())

    def run_ingestion(*_args: object, **kwargs: object) -> IngestionSummary:
        observed.update(kwargs)
        return {
            "ingest_run_id": "ingest-1",
            "status": "completed",
            "succeeded": 3,
            "errors": 0,
            "skipped": 1,
            "source_key": "bitrix_chat",
            "mode": "backfill",
            "dump_path": None,
            "entity_key": None,
        }

    monkeypatch.setattr(tasks, "run_ingestion", run_ingestion)

    summary = tasks._run_split_bitrix_ingestion(
        source_key="bitrix_chat",
        mode="backfill",
        dump_path=None,
        incremental=False,
        idempotency_key="bitrix-backfill:generation-1:crm_deals:boundary:config",
        stream_key="crm_deals",
        worker_task_id="task-1",
    )

    assert _LogicalControl.created == ["bitrix-backfill:generation-1:crm_deals:boundary:config"]
    assert observed["bitrix_execution_stream"] == "crm_deals"
    assert observed["execution_context"] is not None
    assert "task_id" not in observed
    assert "existing_ingest_run_id" not in observed
    assert _LogicalControl.finalized == ["completed"]
    assert summary["succeeded"] == 3
    assert client.closed is True


def test_admission_failure_fails_the_claimed_attempt(monkeypatch: MonkeyPatch) -> None:
    class FailingStreamControl:
        def __init__(self, _client: object) -> None:
            pass

        def admit_or_coalesce(self, **_parameters: object) -> BitrixStreamAdmission:
            raise RuntimeError("active stream belongs to another attempt")

    _LogicalControl.failed.clear()
    monkeypatch.setattr(tasks, "Neo4jClient", lambda _settings: _Client())
    monkeypatch.setattr(tasks, "LogicalRunControl", _LogicalControl)
    monkeypatch.setattr(tasks, "BitrixStreamControl", FailingStreamControl)
    monkeypatch.setattr(tasks, "get_settings", lambda: object())

    with pytest.raises(RuntimeError, match="another attempt"):
        tasks._run_split_bitrix_ingestion(
            source_key="bitrix_chat",
            mode="backfill",
            dump_path=None,
            incremental=False,
            idempotency_key="bitrix-backfill:generation-1:crm_deals:boundary:config",
            stream_key="crm_deals",
            worker_task_id="task-1",
        )

    assert _LogicalControl.failed == ["stream_admission_failed"]


def test_split_task_requires_stable_idempotency_key() -> None:
    with pytest.raises(ValueError, match="stable idempotency_key"):
        tasks.run_ingestion_task.run(
            "bitrix_chat",
            "backfill",
            bitrix_execution_stream="crm_deals",
        )
