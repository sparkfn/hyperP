"""CLI safety contracts independent of production command registration."""

from __future__ import annotations

from pathlib import Path

import pytest
from intelligence import cli
from intelligence.artifacts import workspace_layout
from intelligence.cli import _backup_name, build_parser
from intelligence.models import Health, OutputInventory, Run


@pytest.mark.parametrize("value", ("../bundle", "a/bundle", "C:/bundle", "/bundle", "", ".", ".."))
def test_backup_name_rejects_paths(value: str) -> None:
    """A backup command never silently strips traversal from an operator argument."""
    with pytest.raises(ValueError, match="safe relative"):
        _backup_name(value)


def test_backup_name_accepts_single_file_name() -> None:
    """A simple bundle name remains usable inside the fixed backups directory."""
    assert _backup_name("daily-20260905.bundle") == "daily-20260905.bundle"


def test_parser_has_no_shell_or_executable_arguments() -> None:
    """The fixed CLI surface cannot accept caller-provided execution syntax."""
    parser = build_parser()
    run = parser.parse_args(("run", "approved_job"))
    assert run.name == "approved_job"
    assert not any("shell" in action.dest or "exec" in action.dest for action in parser._actions)


def test_status_and_inspect_are_safe_and_operator_useful(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The local CLI exposes controls/evidence without registry metadata or secrets."""
    run = Run("run-1", "approved", "running", 1, 1.0, 1.0, started_at=1.0)

    layout = workspace_layout(tmp_path)
    (layout.manifests / "run-1.json").write_text("{}", encoding="utf-8")

    class FakeState:
        def __init__(self) -> None:
            self.layout = layout

        def active_run(self) -> Run | None:
            return run

        def inspect(self, run_id: str) -> Run | None:
            return run if run_id == "run-1" else None

        def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]:
            assert run_id == "run-1"
            return (OutputInventory("outputs/run-1/result.json", "a" * 64, 2),)

    class FakeRegistry:
        def names(self) -> tuple[str, ...]:
            return ("approved",)

    class FakeConfig:
        mutations_enabled = False

    class FakeRuntime:
        def __init__(self, _config: object) -> None:
            self.config = FakeConfig()
            self.registry = FakeRegistry()
            self.state = FakeState()

        def health(self) -> Health:
            return Health(False, "unresolved orphaned publication requires reconciliation")

        def close(self) -> None:
            return None

    monkeypatch.setattr(cli, "IntelligenceRuntime", FakeRuntime)
    assert cli.main(("status",)) == 0
    status = capsys.readouterr().out
    assert '"mutations_enabled": false' in status
    assert '"health_reason": "unresolved orphaned publication requires reconciliation"' in status
    assert '"commands": ["approved"]' in status
    assert '"active_run": {"command": "approved", "run_id": "run-1", "state": "running"}' in status
    assert cli.main(("inspect", "run-1")) == 0
    inspection = capsys.readouterr().out
    assert '"outputs/run-1/result.json"' in inspection
    assert '"path": "runs/manifests/run-1.json"' in inspection
    assert "secret" not in inspection.lower()


def top_level_success_handler(directory: Path, cancelled: object) -> None:
    """Picklable reviewed test handler for future spawn-supervisor integration tests."""
    if callable(cancelled) and cancelled():
        return
    (directory / "result.json").write_text("{}", encoding="utf-8")


def top_level_uncooperative_handler(directory: Path, cancelled: object) -> None:
    """Picklable intentionally uncooperative handler for timeout supervision tests."""
    del directory, cancelled
    while True:
        continue
