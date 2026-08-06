from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import src.bitrix_stage_capability as capability_cli
from src.bitrix_stage_capability import (
    _collect_manifests,
    _positive_float,
    _write_json,
    build_parser,
)
from src.connectors.bitrix_stage_history.models import ProbeLimits
from src.connectors.bitrix_stage_history.probe import PassManifest
from src.connectors.bitrix_stage_history.spool import RestrictedSpool


@pytest.mark.parametrize("value", ["nan", "inf", "+inf", "-inf"])
def test_positive_float_rejects_non_finite_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="finite"):
        _positive_float(value)


def test_parser_names_resource_limits_as_per_pass(tmp_path: Path) -> None:
    arguments = [
        "--source-contract-id",
        "123E4567-E89B-12D3-A456-426614174000",
        "--restricted-output-dir",
        str(tmp_path / "restricted"),
        "--max-calls-per-pass",
        "3",
        "--max-rows-per-pass",
        "100",
        "--max-spool-bytes-per-pass",
        "1000000",
        "--max-runtime-seconds-per-pass",
        "5",
    ]

    parsed = build_parser().parse_args(arguments)

    assert parsed.source_contract_id == "123e4567-e89b-12d3-a456-426614174000"
    assert parsed.max_calls_per_pass == 3
    assert parsed.max_rows_per_pass == 100
    assert parsed.max_spool_bytes_per_pass == 1_000_000
    assert parsed.max_runtime_seconds_per_pass == 5


def test_evidence_writer_creates_restricted_file_exclusively(tmp_path: Path) -> None:
    path = tmp_path / "evidence-manifest.json"

    _write_json(path, {"status": "first"})

    assert path.read_text(encoding="utf-8") == '{"status":"first"}\n'
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        _write_json(path, {"status": "second"})
    assert path.read_text(encoding="utf-8") == '{"status":"first"}\n'


def test_evidence_writer_does_not_follow_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("unchanged", encoding="utf-8")
    path = tmp_path / "evidence-manifest.json"
    path.symlink_to(target)

    with pytest.raises(FileExistsError):
        _write_json(path, {"status": "replacement"})

    assert target.read_text(encoding="utf-8") == "unchanged"


def test_manifest_collection_cleans_prior_spools_when_later_pass_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_directory = tmp_path / "restricted"
    output_directory.mkdir(mode=0o700)
    first_spool = RestrictedSpool(output_directory, 1)
    first_manifest = PassManifest(
        traversal_mode="offset",
        calls=1,
        raw_rows=0,
        unique_identity_rows=0,
        duplicate_same_hash_rows=0,
        duplicate_conflict_rows=0,
        pages=1,
        source_total=0,
        source_total_consistent=True,
        source_total_matches_rows=True,
        history_id_ordering=None,
        minimum_history_id=None,
        maximum_history_id=None,
        identity_hash_digest="sha256:empty",
        runtime_seconds=0.1,
        spool_bytes=first_spool.path.stat().st_size,
    )
    calls = 0

    def collect_once_then_fail(
        *_args: object, **_kwargs: object
    ) -> tuple[PassManifest, RestrictedSpool]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return first_manifest, first_spool
        raise RuntimeError("simulated second-pass failure")

    monkeypatch.setattr(capability_cli, "collect_stage_history_pass", collect_once_then_fail)

    with pytest.raises(RuntimeError, match="second-pass"):
        _collect_manifests(
            object(),
            source_contract_id="123e4567-e89b-12d3-a456-426614174000",
            entity_type_id=2,
            filters={},
            limits=ProbeLimits(2, 10, 1_000_000, 5, 2, 2),
            output_directory=output_directory,
            traversal_mode="offset",
        )

    assert not first_spool.path.exists()
