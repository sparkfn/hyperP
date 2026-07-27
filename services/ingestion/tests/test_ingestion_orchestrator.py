"""Tests for the agent-triggered two-phase all-source ingestion workflow."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from io import StringIO
from typing import cast

import pytest
from _orchestration_test_helpers import manifest_json, manifest_payload
from pydantic.types import JsonValue
from pytest import CaptureFixture, MonkeyPatch
from src import ingestion_orchestrator as orchestrator


def test_manifest_assigns_every_source_to_the_required_phase() -> None:
    manifest = orchestrator.parse_manifest(manifest_json())

    assert [task.source_key for task in manifest.identity] == [
        "fundbox",
        "fundbox:legacy",
        "fundbox:merged",
        "eko_phppos",
        "speedzone_phppos",
        "onediver",
    ]
    assert [task.priority for task in manifest.identity] == [1] * 6
    assert [task.priority for task in manifest.dependent] == [5] * 9


def test_manifest_source_catalog_matches_graph_bootstrap() -> None:
    from src.graph.bootstrap import _SOURCE_SYSTEMS

    registered_sources = {source["source_key"] for source in _SOURCE_SYSTEMS}

    assert orchestrator._ALL_SOURCE_KEYS == registered_sources


def test_manifest_rejects_a_missing_source() -> None:
    payload = manifest_payload()
    dependent = cast(list[JsonValue], payload["dependent"])
    dependent.pop()

    with pytest.raises(orchestrator.ManifestValidationError, match="missing: sgrentalflats"):
        orchestrator.parse_manifest(json.dumps(payload))


def test_manifest_rejects_dump_parent_traversal() -> None:
    payload = manifest_payload()
    identity = cast(list[JsonValue], payload["identity"])
    task = cast(dict[str, JsonValue], identity[1])
    task["dump_path"] = "../fundbox_legacy.sql"

    with pytest.raises(orchestrator.ManifestValidationError, match="parent traversal"):
        orchestrator.parse_manifest(json.dumps(payload))


def test_manifest_rejects_dump_root_as_a_file() -> None:
    payload = manifest_payload()
    identity = cast(list[JsonValue], payload["identity"])
    task = cast(dict[str, JsonValue], identity[1])
    task["dump_path"] = "."

    with pytest.raises(orchestrator.ManifestValidationError, match="relative to DUMPS_ROOT"):
        orchestrator.parse_manifest(json.dumps(payload))


def test_manifest_rejects_unknown_whatsapp_entity() -> None:
    payload = manifest_payload()
    dependent = cast(list[JsonValue], payload["dependent"])
    whatsapp = cast(dict[str, JsonValue], dependent[6])
    whatsapp["entity_key"] = "unknown"

    with pytest.raises(orchestrator.ManifestValidationError, match="must be one of"):
        orchestrator.parse_manifest(json.dumps(payload))


def test_cli_validate_accepts_inline_payload(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["orchestrator", "validate", "--payload", manifest_json()])

    assert orchestrator.main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "dependent_task_count": 9,
        "identity_task_count": 6,
        "status": "valid",
    }


def test_cli_validate_accepts_stdin_payload(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["orchestrator", "validate", "--payload-stdin"])
    monkeypatch.setattr(sys, "stdin", StringIO(manifest_json()))

    assert orchestrator.main() == 0

    assert json.loads(capsys.readouterr().out)["status"] == "valid"


@dataclass
class _FakeResult:
    id: str


class _FakeCelery:
    def __init__(self, _name: str, *, broker: str, backend: None) -> None:
        assert broker == "redis://test/0"
        assert backend is None
        self.calls: list[dict[str, object]] = []

    def send_task(self, name: str, **kwargs: object) -> _FakeResult:
        self.calls.append({"name": name, **kwargs})
        return _FakeResult(id="task-123")


def test_cli_trigger_queues_validated_payload(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    fake_app = _FakeCelery("ignored", broker="redis://test/0", backend=None)
    monkeypatch.setattr(orchestrator, "Celery", lambda *_args, **_kwargs: fake_app)
    monkeypatch.setattr(
        orchestrator,
        "get_settings",
        lambda: type("Settings", (), {"celery_broker_url": "redis://test/0"})(),
    )
    monkeypatch.setattr(sys, "argv", ["orchestrator", "trigger", "--payload", manifest_json()])

    assert orchestrator.main() == 0

    assert (
        fake_app.calls[0]["name"]
        == "src.ingestion_orchestration_tasks.start_orchestrated_ingestion_task"
    )
    assert fake_app.calls[0]["queue"] == "ingestion"
    assert fake_app.calls[0]["priority"] == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "queued"
    assert output["orchestration_id"] == "task-123"
    assert output["celery_task_id"] == "task-123"


def test_cli_does_not_expose_broker_failure_details(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    class FailingCelery:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def send_task(self, _name: str, **_kwargs: object) -> None:
            raise RuntimeError("redis://user:secret@broker.internal/0")

    monkeypatch.setattr(orchestrator, "Celery", FailingCelery)
    monkeypatch.setattr(
        orchestrator,
        "get_settings",
        lambda: type("Settings", (), {"celery_broker_url": "redis://test/0"})(),
    )
    monkeypatch.setattr(sys, "argv", ["orchestrator", "trigger", "--payload", manifest_json()])

    assert orchestrator.main() == 1

    output = capsys.readouterr().out
    assert json.loads(output) == {
        "status": "queue_failed",
        "error": "broker submission failed",
    }
    assert "secret" not in output
