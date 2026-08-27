"""CLI and strict request boundary tests for the internal standalone CRM census control."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import cast

import pytest
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.graph.standalone_crm_census_types import StandaloneCrmCensusAdmission
from src.ingestion_config import (
    BitrixOpenLinesConfig,
    bitrix_configuration_digest,
    standalone_crm_census_configuration_digest,
)
from src.standalone_crm_census_control import StandaloneCrmCensusControl
from src.standalone_crm_census_control_cli import build_parser, dispatch
from src.standalone_crm_census_models import StandaloneCrmBudgetSnapshot
from src.standalone_crm_census_requests import (
    SourceSyncCensusRequest,
    operator_request_from_json,
    operator_request_json,
    request_from_persisted_json,
)
from src.standalone_crm_census_runtime import StandaloneCrmCensusRuntime
from src.standalone_crm_census_tasks import (
    StandaloneCrmCensusTaskUnavailableError,
    classify_reserved_call_unknown_task,
    register_standalone_crm_census_control,
)


class _Task:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def delay(self, *args: str) -> object:
        self.calls.append(args)
        return type("Result", (), {"id": "queued"})()


def _request() -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        "source",
        "control",
        "occurrence",
        "operator",
        ("contact",),
        "policy",
        "association",
        "digest",
        StandaloneCrmBudgetSnapshot(2, 2, 10.0, 4, 4, 2, 20.0),
    )


def test_operator_request_round_trip_is_strict_and_has_no_captured_authority() -> None:
    encoded = operator_request_json(_request())
    assert operator_request_from_json(encoded) == _request()
    assert request_from_persisted_json(encoded) == _request()
    raw = json.loads(encoded)
    raw["captured_authority"] = {"mapping_head_id": "forbidden"}
    with pytest.raises(ValueError, match="missing, extra, or cross-kind"):
        operator_request_from_json(json.dumps(raw))
    raw = json.loads(encoded)
    raw.pop("selected_kinds")
    with pytest.raises(ValueError, match="missing, extra, or cross-kind"):
        operator_request_from_json(json.dumps(raw))
    raw = json.loads(encoded)
    raw["target_revision_id"] = "cross-kind"
    with pytest.raises(ValueError, match="missing, extra, or cross-kind"):
        operator_request_from_json(json.dumps(raw))


def test_cli_dispatches_only_internal_celery_tasks() -> None:
    tasks = {
        name: _Task()
        for name in (
            "start",
            "status",
            "cancel",
            "resume",
            "reconcile",
            "repair",
            "classify-call-unknown",
        )
    }
    args = build_parser().parse_args(["start", "--request-json", operator_request_json(_request())])
    dispatch(args, tasks=tasks)
    assert tasks["start"].calls == [(operator_request_json(_request()),)]
    args = build_parser().parse_args(
        ["cancel", "census", "--actor", "operator", "--reason", "stop"]
    )
    dispatch(args, tasks=tasks)
    assert tasks["cancel"].calls == [("census", "operator", "stop")]
    args = build_parser().parse_args(["repair", "publication"])
    dispatch(args, tasks=tasks)
    assert tasks["repair"].calls == [("publication",)]
    args = build_parser().parse_args(["classify-call-unknown", "census", "intent"])
    dispatch(args, tasks=tasks)
    assert tasks["classify-call-unknown"].calls == [("census", "intent")]
    for command in ("status", "resume", "reconcile"):
        args = build_parser().parse_args([command, "census"])
        dispatch(args, tasks=tasks)
        assert tasks[command].calls == [("census",)]


def test_standalone_settings_leave_legacy_bitrix_digest_fixed_vector_unchanged() -> None:
    base = BitrixOpenLinesConfig(source_instance_id="portal-a")
    changed = replace(
        base, standalone_crm_identity_enabled=True, standalone_crm_identity_max_calls_per_attempt=17
    )
    assert (
        bitrix_configuration_digest(base, ())
        == "sha256:1d50d93638cf83429c0d04907ca323411f8b3d7f03dcae0fae05b9f8581b20e9"
    )
    assert bitrix_configuration_digest(base, ()) == bitrix_configuration_digest(changed, ())
    assert standalone_crm_census_configuration_digest(
        base
    ) != standalone_crm_census_configuration_digest(changed)


class _UnknownCallControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def classify_reserved_call_unknown(self, census_id: str, *, intent_id: str) -> bool:
        self.calls.append((census_id, intent_id))
        return len(self.calls) == 1


def test_unknown_call_task_is_default_off_then_uses_control_without_operator_fences() -> None:
    register_standalone_crm_census_control(None)
    with pytest.raises(StandaloneCrmCensusTaskUnavailableError):
        classify_reserved_call_unknown_task.run("census", "intent")

    control = _UnknownCallControl()
    register_standalone_crm_census_control(lambda: cast(StandaloneCrmCensusControl, control))
    try:
        assert classify_reserved_call_unknown_task.run("census", "intent") == {
            "census_id": "census",
            "intent_id": "intent",
            "classified": True,
        }
        assert classify_reserved_call_unknown_task.run("census", "intent") == {
            "census_id": "census",
            "intent_id": "intent",
            "classified": False,
        }
        assert control.calls == [("census", "intent"), ("census", "intent")]
    finally:
        register_standalone_crm_census_control(None)


class _UnknownCallRepository:
    def __init__(self) -> None:
        self.admission = StandaloneCrmCensusAdmission(
            "census", "running", "fingerprint", "authority", "source", "control", False
        )
        self.classified: list[tuple[str, str]] = []
        self.runtime: _UnknownCallRuntime | None = None

    def load_admitted_request(
        self, census_id: str
    ) -> tuple[StandaloneCrmCensusAdmission, SourceSyncCensusRequest, object]:
        assert census_id == "census"
        return self.admission, _request(), object()

    def classify_current_reserved_call_unknown(
        self, admission: StandaloneCrmCensusAdmission, *, intent_id: str
    ) -> bool:
        assert self.runtime is not None and self.runtime.revalidations == 0
        assert admission == self.admission
        self.classified.append((admission.census_id, intent_id))
        return True


class _UnknownCallRuntime:
    def __init__(self) -> None:
        self.revalidations = 0

    def revalidate_admitted(self, request: SourceSyncCensusRequest, authority: object) -> None:
        assert request == _request()
        assert authority is not None
        self.revalidations += 1


def test_control_unknown_call_classifies_historical_reservation_without_work_revalidation() -> None:
    repository = _UnknownCallRepository()
    runtime = _UnknownCallRuntime()
    repository.runtime = runtime
    control = StandaloneCrmCensusControl(
        cast(StandaloneCrmCensusRepository, repository),
        cast(StandaloneCrmCensusRuntime, runtime),
    )
    assert control.classify_reserved_call_unknown("census", intent_id="intent")
    assert runtime.revalidations == 0
    assert repository.classified == [("census", "intent")]
