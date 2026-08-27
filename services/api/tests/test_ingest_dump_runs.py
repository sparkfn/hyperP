from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from src.graph.queries.ingestion import CREATE_INGEST_RUN
from src.routes.dumps import list_dump_files
from src.types_requests import IngestRunCreateRequest


def test_ingest_run_create_defaults_to_batch_mode() -> None:
    body = IngestRunCreateRequest(run_type="manual")

    assert body.mode == "batch"
    assert body.dump_path is None


def test_ingest_run_create_accepts_api_without_dump_path() -> None:
    body = IngestRunCreateRequest(run_type="sgbankruptcy", mode="api")

    assert body.mode == "api"
    assert body.dump_path is None


def test_ingest_run_create_accepts_backfill_without_dump_path() -> None:
    body = IngestRunCreateRequest(run_type="bitrix_chat", mode="backfill")

    assert body.mode == "backfill"
    assert body.dump_path is None


def test_create_ingest_run_merges_by_source_control_and_idempotency_key() -> None:
    assert "MERGE (ir:IngestRun" in CREATE_INGEST_RUN
    assert "source_key: $source_key" in CREATE_INGEST_RUN
    assert "control_instance_id: 'legacy-default'" in CREATE_INGEST_RUN
    assert "idempotency_key: $idempotency_key" in CREATE_INGEST_RUN
    assert "created AS created" in CREATE_INGEST_RUN


def test_dump_run_requires_dump_path() -> None:
    with pytest.raises(ValidationError, match="dump_path is required"):
        IngestRunCreateRequest(run_type="manual", mode="dump")


def test_dump_run_rejects_absolute_dump_path() -> None:
    with pytest.raises(ValidationError, match="relative to the dumps root"):
        IngestRunCreateRequest(run_type="manual", mode="dump", dump_path="/tmp/file.sql")


def test_dump_run_rejects_windows_absolute_dump_path() -> None:
    with pytest.raises(ValidationError, match="relative to the dumps root"):
        IngestRunCreateRequest(run_type="manual", mode="dump", dump_path="C:\\dump.sql")


def test_dump_run_rejects_parent_traversal() -> None:
    with pytest.raises(ValidationError, match="must not contain parent traversal"):
        IngestRunCreateRequest(run_type="manual", mode="dump", dump_path="../file.sql")

    body = IngestRunCreateRequest(
        run_type="manual",
        mode="dump",
        dump_path="fundbox\\archive\\dump.sql",
    )

    assert body.dump_path == "fundbox/archive/dump.sql"


def test_list_dump_files_returns_recursive_relative_posix_paths(tmp_path: Path) -> None:
    (tmp_path / "fundbox" / "archive").mkdir(parents=True)
    (tmp_path / "fundbox" / "archive" / "dump.sql").write_text("select 1", encoding="utf-8")
    (tmp_path / "root.sql").write_text("select 2", encoding="utf-8")

    files = list_dump_files(tmp_path)

    assert files == ["fundbox/archive/dump.sql", "root.sql"]


def test_list_dump_files_skips_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside.sql"
    outside.write_text("select outside", encoding="utf-8")
    root = tmp_path / "dumps"
    root.mkdir()
    (root / "inside.sql").write_text("select inside", encoding="utf-8")
    symlink = root / "outside.sql"
    try:
        symlink.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    files = list_dump_files(root)

    assert files == ["inside.sql"]


def test_list_dump_files_rejects_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(FileNotFoundError):
        list_dump_files(missing)
