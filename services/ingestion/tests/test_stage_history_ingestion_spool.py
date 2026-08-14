"""Focused tests for the restricted stage-history ingestion spool."""

from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from src.connectors.bitrix_stage_history.ingestion_spool import (
    MAX_INGESTION_PAGE_ROWS,
    MalformedCapturedRow,
    SealedStageHistoryIngestionSpool,
    StageHistoryIngestionSpool,
    ValidCapturedRow,
)
from src.models import JsonValue

_OBSERVED_AT = datetime(2026, 8, 14, 3, 30, 45, 123456, tzinfo=UTC)


def _valid(history_id: str = "101") -> ValidCapturedRow:
    payload: dict[str, JsonValue] = {
        "ID": history_id,
        "OWNER_ID": "501",
        "CATEGORY_ID": "2",
        "STAGE_ID": "C2:NEW",
        "CREATED_TIME": "2026-08-14T03:30:00Z",
        "nested": {"preserved": [True, None, 3]},
    }
    return ValidCapturedRow(
        raw_payload=payload,
        event_identity=f"stage-event-{history_id}",
        canonical_hash=f"sha256:canonical-{history_id}",
    )


def _malformed() -> MalformedCapturedRow:
    return MalformedCapturedRow(
        raw_payload={"ID": None, "OWNER_ID": ["unexpected"]},
        safe_error_code="missing_history_id",
    )


def test_mixed_pages_seal_and_replay_full_payloads_in_order(tmp_path: Path) -> None:
    directory = tmp_path / "restricted"
    spool = StageHistoryIngestionSpool(directory, artifact_id="artifact-001")

    first = spool.append_page([_valid(), _malformed()], source_observed_at=_OBSERVED_AT)
    second = spool.append_page([_valid("102")], source_observed_at=_OBSERVED_AT)

    assert first.page_sequence == 1
    assert first.row_count == 2
    assert [row.row_sequence for row in first.rows] == [1, 2]
    assert [row.artifact_row_sequence for row in first.rows] == [1, 2]
    assert first.rows[0].row_kind == "valid"
    assert first.rows[0].event_identity == "stage-event-101"
    assert first.rows[0].raw_payload == _valid().raw_payload
    assert first.rows[1].row_kind == "malformed"
    assert first.rows[1].event_identity is None
    assert first.rows[1].canonical_hash is None
    assert first.rows[1].safe_error_code == "missing_history_id"
    assert second.page_sequence == 2
    assert second.rows[0].artifact_row_sequence == 3
    assert first.source_observed_at == "2026-08-14T03:30:45.123456Z"
    assert first.page_digest.startswith("sha256:")
    assert all(row.row_digest.startswith("sha256:") for row in first.rows)
    assert spool.total_bytes > 0

    preparing_path = spool.path
    sealed_path = spool.seal()

    assert not preparing_path.exists()
    assert sealed_path.name == "stage-ingestion-artifact-001.sqlite3"
    assert stat.S_IMODE(sealed_path.stat().st_mode) == 0o400
    with SealedStageHistoryIngestionSpool(sealed_path) as reader:
        replayed = reader.pages()
        assert replayed == (first, second)
        assert reader.page(1) == first
        assert reader.total_bytes == sealed_path.stat().st_size


def test_empty_pages_and_rows_remain_contiguous(tmp_path: Path) -> None:
    spool = StageHistoryIngestionSpool(tmp_path / "restricted", artifact_id="empty-page")

    empty = spool.append_page([], source_observed_at=_OBSERVED_AT)
    populated = spool.append_page([_valid()], source_observed_at=_OBSERVED_AT)
    sealed_path = spool.seal()

    assert empty.page_sequence == 1
    assert empty.rows == ()
    assert populated.page_sequence == 2
    assert populated.rows[0].artifact_row_sequence == 1
    with SealedStageHistoryIngestionSpool(sealed_path) as reader:
        assert reader.pages() == (empty, populated)


def test_page_size_and_timestamp_are_strict(tmp_path: Path) -> None:
    spool = StageHistoryIngestionSpool(tmp_path / "restricted", artifact_id="limits")
    oversized = [_valid(str(index)) for index in range(MAX_INGESTION_PAGE_ROWS + 1)]

    with pytest.raises(ValueError, match="cannot exceed 50"):
        spool.append_page(oversized, source_observed_at=_OBSERVED_AT)
    with pytest.raises(ValueError, match="timezone-aware"):
        spool.append_page([_valid()], source_observed_at=datetime(2026, 8, 14))

    first = spool.append_page([_valid()], source_observed_at=_OBSERVED_AT)
    assert first.page_sequence == 1
    spool.cleanup()


def test_row_metadata_and_json_are_validated_before_storage(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="event_identity"):
        ValidCapturedRow(raw_payload={}, event_identity="", canonical_hash="sha256:value")
    with pytest.raises(ValueError, match="safe_error_code"):
        MalformedCapturedRow(raw_payload={}, safe_error_code="Unsafe Error")
    with pytest.raises(ValueError, match="finite JSON"):
        ValidCapturedRow(
            raw_payload={"invalid": float("nan")},
            event_identity="event",
            canonical_hash="sha256:value",
        )

    spool = StageHistoryIngestionSpool(tmp_path / "restricted", artifact_id="validated")
    spool.cleanup()


def test_digests_are_deterministic_for_the_same_ordered_capture(tmp_path: Path) -> None:
    first = StageHistoryIngestionSpool(tmp_path / "first", artifact_id="first")
    second = StageHistoryIngestionSpool(tmp_path / "second", artifact_id="second")

    first_page = first.append_page([_valid(), _malformed()], source_observed_at=_OBSERVED_AT)
    second_page = second.append_page([_valid(), _malformed()], source_observed_at=_OBSERVED_AT)

    assert first_page.page_digest == second_page.page_digest
    assert [row.row_digest for row in first_page.rows] == [
        row.row_digest for row in second_page.rows
    ]
    first.cleanup()
    second.cleanup()


def test_storage_budget_rejection_precedes_database_mutation(tmp_path: Path) -> None:
    spool = StageHistoryIngestionSpool(tmp_path / "restricted", artifact_id="budgeted")
    first = spool.append_page([_valid()], source_observed_at=_OBSERVED_AT)
    database_bytes = spool.path.read_bytes()

    with pytest.raises(RuntimeError, match="storage budget"):
        spool.append_page(
            [_valid("102")],
            source_observed_at=_OBSERVED_AT,
            max_storage_bytes=spool.total_bytes,
        )

    assert spool.path.read_bytes() == database_bytes
    second = spool.append_page(
        [_valid("102")],
        source_observed_at=_OBSERVED_AT,
        max_storage_bytes=10 * 1024 * 1024,
    )
    assert second.page_sequence == first.page_sequence + 1
    spool.cleanup()


def test_sealed_database_is_read_only_and_writer_is_closed(tmp_path: Path) -> None:
    spool = StageHistoryIngestionSpool(tmp_path / "restricted", artifact_id="immutable")
    spool.append_page([_valid()], source_observed_at=_OBSERVED_AT)
    sealed_path = spool.seal()

    with pytest.raises(RuntimeError, match="closed"):
        spool.append_page([_valid("102")], source_observed_at=_OBSERVED_AT)
    uri = f"file:{sealed_path}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM rows")
    finally:
        connection.close()


def test_immutable_reader_detects_payload_digest_tampering(tmp_path: Path) -> None:
    spool = StageHistoryIngestionSpool(tmp_path / "restricted", artifact_id="tamper")
    spool.append_page([_valid()], source_observed_at=_OBSERVED_AT)
    sealed_path = spool.seal()
    sealed_path.chmod(0o600)
    connection = sqlite3.connect(sealed_path)
    try:
        connection.execute(
            "UPDATE rows SET raw_payload_json = ? WHERE artifact_row_sequence = 1",
            ('{"payload":{"ID":"changed"}}\n',),
        )
        connection.commit()
    finally:
        connection.close()
        sealed_path.chmod(0o400)

    with SealedStageHistoryIngestionSpool(sealed_path) as reader:
        with pytest.raises(RuntimeError, match="row digest"):
            reader.pages()


def test_reader_rejects_writable_or_symlinked_sealed_files(tmp_path: Path) -> None:
    spool = StageHistoryIngestionSpool(tmp_path / "restricted", artifact_id="path-check")
    spool.append_page([_valid()], source_observed_at=_OBSERVED_AT)
    sealed_path = spool.seal()
    sealed_path.chmod(0o600)
    with pytest.raises(ValueError, match="must not be writable"):
        SealedStageHistoryIngestionSpool(sealed_path)
    sealed_path.chmod(0o400)

    link = tmp_path / "spool-link.sqlite3"
    link.symlink_to(sealed_path)
    with pytest.raises(ValueError, match="non-symlink"):
        SealedStageHistoryIngestionSpool(link)


def test_cleanup_removes_preparing_and_sealed_storage(tmp_path: Path) -> None:
    preparing = StageHistoryIngestionSpool(tmp_path / "preparing", artifact_id="cleanup-a")
    preparing.append_page([_valid()], source_observed_at=_OBSERVED_AT)
    preparing_path = preparing.path
    preparing.cleanup()
    assert not preparing_path.exists()

    sealed = StageHistoryIngestionSpool(tmp_path / "sealed", artifact_id="cleanup-b")
    sealed.append_page([_valid()], source_observed_at=_OBSERVED_AT)
    sealed_path = sealed.seal()
    sealed.cleanup()
    assert not sealed_path.exists()
