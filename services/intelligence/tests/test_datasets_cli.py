"""Fixed dataset CLI parser and read-only command contracts."""

from __future__ import annotations

import pytest
from intelligence.cli import build_parser
from intelligence.config import RuntimeConfig
from intelligence.datasets import cli
from intelligence.datasets import models as dataset_models
from test_datasets_artifacts import _publish_dataset


def test_parser_covers_list_inspect_verify_and_rejects_bad_build_values() -> None:
    parser = build_parser()
    assert parser.parse_args(("dataset", "list", "--limit", "1")).dataset_command == "list"
    assert (
        parser.parse_args(("dataset", "inspect", "id", "--accepted-run-id", "run")).dataset_command
        == "inspect"
    )
    assert (
        parser.parse_args(("dataset", "verify", "id", "--accepted-run-id", "run")).dataset_command
        == "verify"
    )
    with pytest.raises(SystemExit):
        parser.parse_args(("dataset", "build", "--definition", "bad"))


def test_list_and_inspect_are_read_only_verify_is_default_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    run_id, descriptor = _publish_dataset(tmp_path)
    database = tmp_path / "state" / "state.sqlite3"
    before = database.read_bytes()
    monkeypatch.setattr(
        cli.RuntimeConfig, "from_environment", classmethod(lambda _cls: RuntimeConfig(tmp_path))
    )
    assert cli.main(build_parser().parse_args(("dataset", "list", "--limit", "1"))) == 0
    assert (
        cli.main(
            build_parser().parse_args(
                ("dataset", "inspect", descriptor.dataset_id, "--accepted-run-id", run_id)
            )
        )
        == 0
    )
    with pytest.raises(RuntimeError, match="disabled"):
        cli.main(
            build_parser().parse_args(
                ("dataset", "verify", descriptor.dataset_id, "--accepted-run-id", run_id)
            )
        )
    assert database.read_bytes() == before
    with pytest.raises(ValueError):
        cli.main(build_parser().parse_args(("dataset", "list", "--limit", "101")))


def test_old_provenance_remains_readable_when_current_fingerprint_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    run_id, descriptor = _publish_dataset(tmp_path)
    monkeypatch.setattr(dataset_models, "code_contract_fingerprint", lambda: "0" * 64)
    monkeypatch.setattr(
        cli.RuntimeConfig,
        "from_environment",
        classmethod(lambda _cls: RuntimeConfig(tmp_path)),
    )
    assert cli.main(build_parser().parse_args(("dataset", "list"))) == 0
    assert (
        cli.main(
            build_parser().parse_args(
                ("dataset", "inspect", descriptor.dataset_id, "--accepted-run-id", run_id)
            )
        )
        == 0
    )
    enabled = RuntimeConfig(tmp_path, mutations_enabled=True)
    monkeypatch.setattr(
        cli.RuntimeConfig,
        "from_environment",
        classmethod(lambda _cls: enabled),
    )
    with pytest.raises(ValueError, match="current executable"):
        cli.main(
            build_parser().parse_args(
                ("dataset", "verify", descriptor.dataset_id, "--accepted-run-id", run_id)
            )
        )
