"""Canonical split Bitrix task dispatch regression coverage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest
from pytest import MonkeyPatch
from src import tasks
from src.bitrix_backfill_models import GenerationRunContext, KnownOwnerMembershipSet
from src.bitrix_deal_scope_reconciliation import KnownOwnerRefreshSummary
from src.bitrix_ingestion_models import ExecutionContext, FenceContext
from src.graph.ingestion_control_models import (
    BitrixStreamAdmission,
    LogicalRunAttempt,
    LogicalRunState,
)
from src.main import IngestionSummary
from src.models import JsonValue
from src.resumable import LogicalRunStatus


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

    def get_by_idempotency(self, **_parameters: object) -> LogicalRunAttempt | None:
        return None

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

    def get(self, _logical_run_id: str) -> LogicalRunState:
        return LogicalRunState(
            logical_run_id="logical-1",
            status="running",
            generation=1,
            source_key="bitrix_chat",
            mode="backfill",
            dump_path=None,
            entity_key=None,
            stop_requested=False,
            stop_reason=None,
            ingest_run_id="ingest-1",
            phase="scoped_deal_census_v1",
            cursor={"last_deal_id": None, "census_epoch": 1},
            committed_count=0,
            duplicate_count=0,
            excluded_count=0,
            retry_count=0,
            checkpointed_at="2026-08-08T00:00:00Z",
        )

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
            fence_context=_fence(),
            worker_task_id="task-1",
        )


def _fence() -> FenceContext:
    return FenceContext(
        logical_run_id="logical-1",
        ingest_run_id="ingest-1",
        source_key="bitrix_chat",
        stream_key="crm_deals",
        stream_generation=1,
        fencing_token=1,
        attempt_generation=1,
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
            "http_request_count": 7,
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
        source_window={
            "upper_deal_id": "900",
            "included_category_digest": "sha256:categories",
            "owner_artifact_id": None,
        },
    )

    assert _LogicalControl.created == ["bitrix-backfill:generation-1:crm_deals:boundary:config"]
    assert observed["bitrix_execution_stream"] == "crm_deals"
    assert observed["execution_context"] is not None
    assert "task_id" not in observed
    assert "existing_ingest_run_id" not in observed
    assert _LogicalControl.finalized == ["completed"]
    assert summary["succeeded"] == 3
    assert client.closed is True


def test_duplicate_delivery_coalesces_before_generation_or_domain_mutation(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _Client()
    attached: list[str] = []

    class CoalescedStreamControl(_StreamControl):
        def admit_or_coalesce(self, **_parameters: object) -> BitrixStreamAdmission:
            return BitrixStreamAdmission(
                outcome="coalesced",
                fence_context=_fence(),
                worker_task_id="task-1",
            )

    class Repository:
        def __init__(self, _client: object) -> None:
            pass

        def attach_logical_run(self, **_parameters: object) -> None:
            attached.append("attached")

    monkeypatch.setattr(tasks, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(tasks, "LogicalRunControl", _LogicalControl)
    monkeypatch.setattr(tasks, "BitrixStreamControl", CoalescedStreamControl)
    monkeypatch.setattr(tasks, "BitrixBackfillRepository", Repository)
    monkeypatch.setattr(tasks, "get_settings", lambda: object())
    monkeypatch.setattr(
        tasks,
        "run_ingestion",
        lambda *_args, **_kwargs: pytest.fail("duplicate delivery must not run ingestion"),
    )

    summary = tasks._run_split_bitrix_ingestion(
        source_key="bitrix_chat",
        mode="api",
        dump_path=None,
        incremental=True,
        idempotency_key="bitrix-live:occurrence:crm_deals:config",
        stream_key="crm_deals",
        worker_task_id="task-1",
        generation_context=GenerationRunContext(
            generation_id="successor-1",
            boundary_digest="sha256:boundary",
            configuration_digest="sha256:config",
        ),
        source_window={
            "upper_deal_id": "900",
            "included_category_digest": "sha256:categories",
            "owner_artifact_id": None,
        },
    )

    assert summary["status"] == "already_running"
    assert summary["skipped"] == 1
    assert attached == []
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
            source_window={
                "upper_deal_id": "900",
                "included_category_digest": "sha256:categories",
                "owner_artifact_id": None,
            },
        )

    assert _LogicalControl.failed == ["stream_admission_failed"]


def test_split_task_requires_stable_idempotency_key() -> None:
    with pytest.raises(ValueError, match="stable idempotency_key"):
        tasks.run_ingestion_task.run(
            "bitrix_chat",
            "backfill",
            bitrix_execution_stream="crm_deals",
        )


def test_resume_reuses_existing_sealed_known_owner_set(monkeypatch: MonkeyPatch) -> None:
    existing = KnownOwnerMembershipSet(
        generation_id="generation-1",
        membership_set_id="owners-1",
        digest="sha256:owners",
        deal_ids=("2", "10"),
    )
    calls: list[str] = []

    class Repository:
        def __init__(self, _client: object) -> None:
            pass

        def find_known_owner_set(self, **_parameters: object) -> KnownOwnerMembershipSet | None:
            calls.append("find")
            return existing

        def materialize_known_owner_set(self, **_parameters: object) -> KnownOwnerMembershipSet:
            calls.append("materialize")
            raise AssertionError("a sealed resume set must never be rebuilt from mutable scope")

    monkeypatch.setattr(tasks, "BitrixBackfillRepository", Repository)

    membership = tasks._get_or_materialize_known_owner_set(
        client=object(),
        generation_id="generation-1",
        membership_set_id="owners-1",
        fence_context=_fence(),
    )

    assert membership is existing
    assert calls == ["find"]


def test_first_census_materializes_missing_known_owner_set(
    monkeypatch: MonkeyPatch,
) -> None:
    created = KnownOwnerMembershipSet(
        generation_id="generation-1",
        membership_set_id="owners-1",
        digest="sha256:owners",
        deal_ids=("2", "10"),
    )
    calls: list[str] = []
    materialize_parameters: dict[str, object] = {}

    class Repository:
        def __init__(self, _client: object) -> None:
            pass

        def find_known_owner_set(self, **_parameters: object) -> KnownOwnerMembershipSet | None:
            calls.append("find")
            return None

        def materialize_known_owner_set(self, **_parameters: object) -> KnownOwnerMembershipSet:
            calls.append("materialize")
            materialize_parameters.update(_parameters)
            return created

    monkeypatch.setattr(tasks, "BitrixBackfillRepository", Repository)

    membership = tasks._get_or_materialize_known_owner_set(
        client=object(),
        generation_id="generation-1",
        membership_set_id="owners-1",
        fence_context=_fence(),
    )

    assert membership is created
    assert calls == ["find", "materialize"]
    assert materialize_parameters["fence_context"] == _fence()


def test_known_owner_load_failure_terminates_the_admitted_attempt(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _Client()
    failed: list[str] = []

    class LogicalControl(_LogicalControl):
        def fail_fenced(self, **parameters: object) -> None:
            failed.append(cast(str, parameters["failure_category"]))

    class Repository:
        def __init__(self, _client: object) -> None:
            pass

        def attach_logical_run(self, **_parameters: object) -> None:
            pass

        def find_known_owner_set(self, **_parameters: object) -> KnownOwnerMembershipSet | None:
            raise RuntimeError("sealed owner set unavailable")

        def materialize_known_owner_set(self, **_parameters: object) -> KnownOwnerMembershipSet:
            raise AssertionError("find failure must fail closed")

    monkeypatch.setattr(tasks, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(tasks, "LogicalRunControl", LogicalControl)
    monkeypatch.setattr(tasks, "BitrixStreamControl", _StreamControl)
    monkeypatch.setattr(tasks, "BitrixBackfillRepository", Repository)
    monkeypatch.setattr(tasks, "get_settings", lambda: object())

    with pytest.raises(RuntimeError, match="sealed owner set unavailable"):
        tasks._run_split_bitrix_ingestion(
            source_key="bitrix_chat",
            mode="backfill",
            dump_path=None,
            incremental=False,
            idempotency_key="bitrix-backfill:generation-1:crm_deals:boundary:config",
            stream_key="crm_deals",
            worker_task_id="task-1",
            generation_context=GenerationRunContext(
                generation_id="generation-1",
                boundary_digest="sha256:boundary",
                configuration_digest="sha256:config",
            ),
            source_window={
                "upper_deal_id": "900",
                "included_category_digest": "sha256:categories",
                "owner_artifact_id": None,
            },
        )

    assert failed == ["RuntimeError"]
    assert client.closed is True


def test_failed_refresh_phase_resumes_with_durable_connector_and_cursor(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _Client()
    resume_parameters: dict[str, object] = {}
    refresh_cursor: dict[str, JsonValue] = {}
    finalized: list[tuple[int, int]] = []
    membership = KnownOwnerMembershipSet(
        generation_id="generation-1",
        membership_set_id="generation-1:known-owners:sha256:boundary",
        digest="sha256:owners",
        deal_ids=("2", "10", "901", "950"),
    )

    def state(*, status: LogicalRunStatus, generation: int, ingest_run_id: str) -> LogicalRunState:
        return LogicalRunState(
            logical_run_id="logical-1",
            status=status,
            generation=generation,
            source_key="bitrix_chat",
            mode="api",
            dump_path=None,
            entity_key=None,
            stop_requested=False,
            stop_reason=None,
            ingest_run_id=ingest_run_id,
            phase="known_owner_refresh_v1",
            cursor={"last_known_deal_id": "901", "census_epoch": 1},
            committed_count=149_578,
            duplicate_count=0,
            excluded_count=0,
            retry_count=0,
            checkpointed_at="2026-08-13T00:00:00Z",
        )

    class LogicalControl(_LogicalControl):
        reads = 0

        def create_or_reuse(self, **_parameters: object) -> LogicalRunAttempt:
            return LogicalRunAttempt(
                logical_run_id="logical-1",
                ingest_run_id="ingest-2",
                worker_task_id="task-2",
                generation=2,
                logical_status="failed",
                created=False,
            )

        def get(self, _logical_run_id: str) -> LogicalRunState:
            self.reads += 1
            return state(
                status="failed" if self.reads == 1 else "running",
                generation=2 if self.reads == 1 else 3,
                ingest_run_id="ingest-2" if self.reads == 1 else "ingest-3",
            )

        def resume(self, **parameters: object) -> LogicalRunAttempt:
            resume_parameters.update(parameters)
            return LogicalRunAttempt(
                logical_run_id="logical-1",
                ingest_run_id="ingest-3",
                worker_task_id="task-3",
                generation=3,
                logical_status="queued",
                created=True,
            )

        def finalize_fenced(self, **parameters: object) -> None:
            finalized.append(
                (
                    cast(FenceContext, parameters["context"]).attempt_generation,
                    cast(int, parameters["committed_count"]),
                )
            )

    class StreamControl(_StreamControl):
        def admit_or_coalesce(self, **_parameters: object) -> BitrixStreamAdmission:
            return BitrixStreamAdmission(
                outcome="admitted",
                fence_context=FenceContext(
                    logical_run_id="logical-1",
                    ingest_run_id="ingest-3",
                    source_key="bitrix_chat",
                    stream_key="crm_deals",
                    stream_generation=2,
                    fencing_token=2,
                    attempt_generation=3,
                ),
                worker_task_id="task-3",
            )

    class Repository:
        def __init__(self, _client: object) -> None:
            pass

        def get_known_owner_set(self, **_parameters: object) -> KnownOwnerMembershipSet:
            return membership

        def attach_logical_run(self, **_parameters: object) -> None:
            pass

    class Config:
        included_crm_category_ids = ("1",)
        entity_by_crm_category_id = {"1": "entity-1"}

    class IngestionConfig:
        bitrix_openlines = Config()

    def refresh(*_args: object, **parameters: object) -> KnownOwnerRefreshSummary:
        context = cast(ExecutionContext, parameters["context"])
        refresh_cursor.update(context.checkpoint.cursor)
        return KnownOwnerRefreshSummary(
            refreshed=1,
            moved_out_of_scope=0,
            missing_candidates=0,
            unresolved=0,
            http_request_count=1,
        )

    monkeypatch.setattr(tasks, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(tasks, "LogicalRunControl", LogicalControl)
    monkeypatch.setattr(tasks, "BitrixStreamControl", StreamControl)
    monkeypatch.setattr(tasks, "BitrixBackfillRepository", Repository)
    monkeypatch.setattr(tasks, "get_settings", lambda: object())
    monkeypatch.setattr(tasks, "get_ingestion_config", lambda: IngestionConfig())
    monkeypatch.setattr(tasks, "create_bitrix_known_owner_client", lambda: object())
    monkeypatch.setattr(tasks, "refresh_known_owner_set", refresh)
    monkeypatch.setattr(
        tasks,
        "run_ingestion",
        lambda *_args, **_kwargs: pytest.fail("refresh resume must not replay the census"),
    )

    summary = tasks._run_split_bitrix_ingestion(
        source_key="bitrix_chat",
        mode="api",
        dump_path=None,
        incremental=True,
        idempotency_key="bitrix-live:occurrence:crm_deals:config",
        stream_key="crm_deals",
        worker_task_id="task-3",
        generation_context=GenerationRunContext(
            generation_id="generation-1",
            boundary_digest="sha256:boundary",
            configuration_digest="sha256:config",
        ),
        source_window={
            "upper_deal_id": "900",
            "included_category_digest": "sha256:categories",
            "owner_artifact_id": None,
        },
    )

    assert resume_parameters["logical_connector_version"] == "bitrix-crm-deals-keyset-v1"
    assert resume_parameters["checkpoint_connector_version"] == "bitrix-crm-known-owner-refresh-v1"
    assert refresh_cursor == {"last_known_deal_id": "901", "census_epoch": 1}
    assert finalized == [(3, 149_578)]
    assert summary["succeeded"] == 1
    assert client.closed is True


def test_resume_normalizes_deployed_known_owner_cursor_key() -> None:
    assert tasks._normalized_split_cursor(
        "known_owner_refresh_v1",
        {"last_known_deal_id": None, "last_deal_id": "901", "census_epoch": 1},
    ) == {"last_known_deal_id": "901", "census_epoch": 1}


def test_cursor_normalization_does_not_rewrite_census_state() -> None:
    cursor = {"last_deal_id": "901", "census_epoch": 1}

    assert tasks._normalized_split_cursor("scoped_deal_census_v1", cursor) == cursor


def test_phase_row_ceiling_bounds_each_phase_without_summing_them() -> None:
    tasks._validate_split_limits(
        census_record_count=143_719,
        refresh_population=143_719,
        http_request_count=12_000,
        max_rows=143_719,
        max_calls=300_000,
    )


def test_phase_row_ceiling_rejects_an_oversized_refresh_population() -> None:
    with pytest.raises(RuntimeError, match="phase row ceiling"):
        tasks._validate_split_limits(
            census_record_count=10,
            refresh_population=11,
            http_request_count=2,
            max_rows=10,
            max_calls=100,
        )


def test_api_call_ceiling_uses_measured_http_attempts() -> None:
    with pytest.raises(RuntimeError, match="API-call ceiling"):
        tasks._validate_split_limits(
            census_record_count=10,
            refresh_population=10,
            http_request_count=101,
            max_rows=10,
            max_calls=100,
        )


def test_terminal_finalization_uses_durable_resumed_checkpoint_counts() -> None:
    state = LogicalRunState(
        logical_run_id="logical-1",
        status="running",
        generation=2,
        source_key="bitrix_chat",
        mode="api",
        dump_path=None,
        entity_key=None,
        stop_requested=False,
        stop_reason=None,
        ingest_run_id="ingest-2",
        phase="known_owner_refresh_v1",
        cursor={"last_known_deal_id": "901", "census_epoch": 1},
        committed_count=100,
        duplicate_count=20,
        excluded_count=3,
        retry_count=2,
        checkpointed_at="2026-08-13T00:00:00Z",
    )

    assert tasks._terminal_checkpoint_counts(state) == (100, 20, 3, 2, 125)
