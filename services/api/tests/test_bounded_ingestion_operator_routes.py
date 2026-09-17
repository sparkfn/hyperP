"""Route and repository contracts for bounded logical-run operator controls."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
import src.celery_client as celery_client
import yaml
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError
from src.auth.deps import require_human_admin
from src.auth.models import AuthUser
from src.celery_client import BoundedLogicalRunRecoveryRequest
from src.error_handlers import register_error_handlers
from src.graph.queries.ingestion_control import (
    GET_BOUNDED_LOGICAL_RUN,
    PAUSE_BOUNDED_LOGICAL_RUN,
    RESUME_BOUNDED_LOGICAL_RUN,
)
from src.repositories.deps import get_ingestion_control_repo
from src.repositories.protocols.ingestion_control import (
    BoundedLogicalRunControlResult,
    BoundedLogicalRunStatusRecord,
    BoundedLogicalRunUsageRecord,
)
from src.routes import ingestion_control as routes
from src.types_ingestion_control import BoundedLogicalRunStatus
from src.types_requests import BoundedRunPauseRequest, BoundedRunResumeRequest


async def _admin_user() -> AuthUser:
    return AuthUser(
        email="admin@example.com",
        google_sub="admin-sub",
        role="admin",
        entity_key=None,
    )


def _status() -> BoundedLogicalRunStatusRecord:
    return BoundedLogicalRunStatusRecord(
        logical_run_id="logical-1",
        source_key="fundbox",
        control_instance_id="control-1",
        entity_key="fundbox-sg",
        status="paused_with_checkpoint",
        pause_reason="manual",
        occurrence_id="weekly-2026-09-17",
        timezone="Asia/Singapore",
        starts_at="2026-09-17T09:00:00+08:00",
        drain_starts_at="2026-09-17T22:45:00+08:00",
        cutoff_at="2026-09-17T23:00:00+08:00",
        next_eligible_at="2026-09-24T09:00:00+08:00",
        usage=BoundedLogicalRunUsageRecord(
            records=12,
            source_requests=3,
            pages=2,
            bytes_read=400,
            extraction_calls=1,
        ),
        phase="delta",
        checkpointed_at="2026-09-17T10:00:00+08:00",
        retry_backlog=2,
        failure_category=None,
    )


@dataclass
class _Repo:
    result: BoundedLogicalRunStatusRecord | None = field(default_factory=_status)
    pause_result: BoundedLogicalRunControlResult = field(
        default_factory=lambda: BoundedLogicalRunControlResult(outcome="updated", status=_status())
    )
    resume_result: BoundedLogicalRunControlResult = field(
        default_factory=lambda: BoundedLogicalRunControlResult(
            outcome="updated",
            status=_status(),
            publish_recovery=True,
        )
    )
    pause_calls: list[tuple[str, str, str, int, str]] = field(default_factory=list)
    resume_calls: list[tuple[str, str, str, int]] = field(default_factory=list)

    async def get_bounded_run(self, logical_run_id: str) -> BoundedLogicalRunStatusRecord | None:
        _ = logical_run_id
        return self.result

    async def pause_bounded_run(
        self,
        logical_run_id: str,
        source_key: str,
        control_instance_id: str,
        reset_generation: int,
        reason: str,
    ) -> BoundedLogicalRunControlResult:
        self.pause_calls.append(
            (logical_run_id, source_key, control_instance_id, reset_generation, reason)
        )
        return self.pause_result

    async def resume_bounded_run(
        self,
        logical_run_id: str,
        source_key: str,
        control_instance_id: str,
        reset_generation: int,
    ) -> BoundedLogicalRunControlResult:
        self.resume_calls.append(
            (logical_run_id, source_key, control_instance_id, reset_generation)
        )
        return self.resume_result


def _client(repo: _Repo) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    register_error_handlers(app)
    app.dependency_overrides[require_human_admin] = _admin_user
    app.dependency_overrides[get_ingestion_control_repo] = lambda: repo
    return TestClient(app)


def test_operator_routes_require_a_human_administrator() -> None:
    for route in routes.router.routes:
        assert isinstance(route, APIRoute)
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        assert require_human_admin in dependency_calls


def test_status_response_has_exact_redacted_contract_fields() -> None:
    assert set(BoundedLogicalRunStatus.model_fields) == {
        "logical_run_id",
        "source_key",
        "control_instance_id",
        "entity_key",
        "status",
        "pause_reason",
        "occurrence_id",
        "timezone",
        "starts_at",
        "drain_starts_at",
        "cutoff_at",
        "next_eligible_at",
        "usage",
        "phase",
        "checkpointed_at",
        "retry_backlog",
        "failure_category",
    }
    assert set(BoundedLogicalRunStatus.model_fields["usage"].annotation.model_fields) == {
        "records",
        "source_requests",
        "pages",
        "bytes_read",
        "extraction_calls",
    }


def test_static_openapi_matches_the_bounded_operator_contract() -> None:
    document = yaml.safe_load(
        Path("docs/profile-unifier-openapi-3.1.yaml").read_text(encoding="utf-8")
    )
    paths = document["paths"]
    expected_operations = {
        "/v1/ingest/logical-runs/{logical_run_id}": ("get", "get_bounded_logical_run"),
        "/v1/ingest/logical-runs/{logical_run_id}/pause": (
            "post",
            "pause_bounded_logical_run",
        ),
        "/v1/ingest/logical-runs/{logical_run_id}/resume": (
            "post",
            "resume_bounded_logical_run",
        ),
    }
    for path, (method, operation_id) in expected_operations.items():
        assert paths[path][method]["operationId"] == operation_id
    status = document["components"]["schemas"]["BoundedLogicalRunStatus"]
    assert set(status["properties"]) == set(BoundedLogicalRunStatus.model_fields)
    assert status["additionalProperties"] is False


def test_get_returns_only_the_redacted_status_projection() -> None:
    response = _client(_Repo()).get("/v1/ingest/logical-runs/logical-1")

    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == set(BoundedLogicalRunStatus.model_fields)
    assert data["usage"] == {
        "records": 12,
        "source_requests": 3,
        "pages": 2,
        "bytes_read": 400,
        "extraction_calls": 1,
    }
    forbidden = {"cursor", "source_boundary", "payload", "replay_token", "fence_token"}
    assert forbidden.isdisjoint(data)


def test_pause_requires_exact_identity_and_returns_updated_status() -> None:
    repo = _Repo()
    response = _client(repo).post(
        "/v1/ingest/logical-runs/logical-1/pause",
        json={
            "source_key": " fundbox ",
            "control_instance_id": " control-1 ",
            "reset_generation": 2,
            "reason": " operator investigation ",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["pause_reason"] == "manual"
    assert repo.pause_calls == [("logical-1", "fundbox", "control-1", 2, "operator investigation")]


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        (BoundedLogicalRunControlResult(outcome="not_found"), 404),
        (BoundedLogicalRunControlResult(outcome="conflict"), 409),
    ],
)
def test_control_actions_keep_identity_mismatch_and_state_conflict_distinct(
    result: BoundedLogicalRunControlResult,
    expected_status: int,
) -> None:
    repo = _Repo(pause_result=result)
    response = _client(repo).post(
        "/v1/ingest/logical-runs/logical-1/pause",
        json={
            "source_key": "fundbox",
            "control_instance_id": "wrong-control",
            "reset_generation": 2,
            "reason": "operator investigation",
        },
    )

    assert response.status_code == expected_status
    expected_code = "not_found" if expected_status == 404 else "bounded_run_state_conflict"
    assert response.json()["error"]["code"] == expected_code


def test_resume_persists_then_attempts_typed_recovery_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[BoundedLogicalRunRecoveryRequest] = []

    def publish(request: BoundedLogicalRunRecoveryRequest) -> bool:
        published.append(request)
        return False

    monkeypatch.setattr(routes, "enqueue_bounded_logical_run_recovery", publish)
    repo = _Repo()
    response = _client(repo).post(
        "/v1/ingest/logical-runs/logical-1/resume",
        json={
            "source_key": "fundbox",
            "control_instance_id": "control-1",
            "reset_generation": 2,
        },
    )

    assert response.status_code == 200
    assert repo.resume_calls == [("logical-1", "fundbox", "control-1", 2)]
    assert published == [
        BoundedLogicalRunRecoveryRequest(
            logical_run_id="logical-1",
            source_key="fundbox",
            control_instance_id="control-1",
            reset_generation=2,
        )
    ]


def test_resume_does_not_publish_when_the_manual_release_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = False

    def publish(_request: BoundedLogicalRunRecoveryRequest) -> bool:
        nonlocal published
        published = True
        return True

    monkeypatch.setattr(routes, "enqueue_bounded_logical_run_recovery", publish)
    repo = _Repo(resume_result=BoundedLogicalRunControlResult(outcome="conflict"))
    response = _client(repo).post(
        "/v1/ingest/logical-runs/logical-1/resume",
        json={
            "source_key": "fundbox",
            "control_instance_id": "control-1",
            "reset_generation": 2,
        },
    )

    assert response.status_code == 409
    assert published is False


@pytest.mark.parametrize(
    "payload",
    [
        {
            "source_key": "fundbox\nother",
            "control_instance_id": "control-1",
            "reset_generation": 1,
        },
        {
            "source_key": "fundbox",
            "control_instance_id": "control-1",
            "reset_generation": 0,
        },
        {
            "source_key": "fundbox",
            "control_instance_id": "control-1",
            "reset_generation": 1,
            "cursor": "not allowed",
        },
    ],
)
def test_resume_request_rejects_unsafe_or_nonpositive_exact_identity(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        BoundedRunResumeRequest.model_validate(payload)


def test_pause_request_rejects_multiline_operator_reason() -> None:
    with pytest.raises(ValidationError, match="bounded single-line text"):
        BoundedRunPauseRequest(
            source_key="fundbox",
            control_instance_id="control-1",
            reset_generation=1,
            reason="operator\nreason",
        )


def test_bounded_control_queries_use_indexed_exact_identity_and_safe_projection() -> None:
    assert "logical_run_id: $logical_run_id" in GET_BOUNDED_LOGICAL_RUN
    assert "bounded_scope_key IS NOT NULL" in GET_BOUNDED_LOGICAL_RUN
    for query in (GET_BOUNDED_LOGICAL_RUN, PAUSE_BOUNDED_LOGICAL_RUN, RESUME_BOUNDED_LOGICAL_RUN):
        assert "raw_payload" not in query
        assert "cursor_json" not in query
        assert "fencing_token" not in query
    for query in (PAUSE_BOUNDED_LOGICAL_RUN, RESUME_BOUNDED_LOGICAL_RUN):
        assert "logical.control_instance_id = $control_instance_id" in query
        assert "logical.reset_generation = $reset_generation" in query


@dataclass
class _RecordingCelery:
    calls: list[tuple[str, tuple[object, ...] | None, dict[str, object], str | None]] = field(
        default_factory=list
    )

    def send_task(
        self,
        name: str,
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
        queue: str | None = None,
    ) -> None:
        self.calls.append((name, args, kwargs or {}, queue))


def test_recovery_publication_contains_only_typed_durable_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _RecordingCelery()
    monkeypatch.setattr(celery_client, "get_celery_app", lambda: producer)

    published = celery_client.enqueue_bounded_logical_run_recovery(
        BoundedLogicalRunRecoveryRequest(
            logical_run_id="logical-1",
            source_key="fundbox",
            control_instance_id="control-1",
            reset_generation=2,
        )
    )

    assert published is True
    assert producer.calls == [
        (
            "src.tasks.recover_bounded_logical_run_task",
            None,
            {
                "logical_run_id": "logical-1",
                "source_key": "fundbox",
                "control_instance_id": "control-1",
                "reset_generation": 2,
            },
            "ingestion",
        )
    ]
