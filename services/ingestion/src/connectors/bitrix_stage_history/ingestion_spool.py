"""Restricted SQLite spool for bounded stage-history source capture.

The writer owns one preparing database, appends complete source pages in
strict sequence, and seals by closing and renaming that database.  Sealed
readers always use SQLite's read-only immutable URI mode and revalidate row,
page, ordering, and accounting digests before returning captured payloads.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self, cast

from pydantic import TypeAdapter, ValidationError

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.connectors.bitrix_stage_history.spool import (
    _prepare_restricted_directory,
    spool_storage_bytes,
    spool_storage_paths,
)
from src.models import JsonValue

INGESTION_SPOOL_SCHEMA_VERSION = 1
MAX_INGESTION_PAGE_ROWS = 50
_MAX_SQLITE_SECTOR_SIZE = 65_536
_JOURNAL_PAGE_RECORD_OVERHEAD = 8

_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SAFE_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

type CapturedRowKind = Literal["valid", "malformed"]


@dataclass(frozen=True, slots=True)
class ValidCapturedRow:
    """One qualified source row retaining its complete JSON payload."""

    raw_payload: JsonValue
    event_identity: str
    canonical_hash: str

    def __post_init__(self) -> None:
        _validate_required_text(self.event_identity, "event_identity")
        _validate_required_text(self.canonical_hash, "canonical_hash")
        _encode_payload(self.raw_payload)


@dataclass(frozen=True, slots=True)
class MalformedCapturedRow:
    """One malformed source row with only a safe classification in metadata."""

    raw_payload: JsonValue
    safe_error_code: str

    def __post_init__(self) -> None:
        if _SAFE_ERROR_CODE.fullmatch(self.safe_error_code) is None:
            raise ValueError("safe_error_code must be a bounded lowercase token")
        _encode_payload(self.raw_payload)


type CapturedRowInput = ValidCapturedRow | MalformedCapturedRow


@dataclass(frozen=True, slots=True)
class CapturedIngestionRow:
    """Immutable row returned from a sealed source-free spool."""

    page_sequence: int
    row_sequence: int
    artifact_row_sequence: int
    row_kind: CapturedRowKind
    source_observed_at: str
    row_digest: str
    raw_payload: JsonValue
    event_identity: str | None
    canonical_hash: str | None
    safe_error_code: str | None


@dataclass(frozen=True, slots=True)
class CapturedIngestionPage:
    """Immutable captured page and its ordered rows."""

    page_sequence: int
    source_observed_at: str
    page_digest: str
    rows: tuple[CapturedIngestionRow, ...]

    @property
    def row_count(self) -> int:
        return len(self.rows)


class StageHistoryIngestionSpool:
    """Private preparing spool that can be sealed exactly once."""

    def __init__(self, directory: Path, *, artifact_id: str) -> None:
        _validate_safe_token(artifact_id, "artifact_id")
        _prepare_restricted_directory(directory)
        self.artifact_id = artifact_id
        self.path = directory / f"stage-ingestion-{artifact_id}.preparing.sqlite3"
        self._sealed_path = directory / f"stage-ingestion-{artifact_id}.sqlite3"
        self._connection: sqlite3.Connection | None = None
        self._closed = False
        self._sealed = False
        _create_private_file(self.path)
        try:
            self._connection = sqlite3.connect(self.path)
            self._configure_connection()
            self._create_schema()
        except BaseException:
            self._close_connection()
            _remove_storage(self.path)
            raise

    def __enter__(self) -> Self:
        self._ensure_writable()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _configure_connection(self) -> None:
        connection = self._required_connection()
        connection.execute("PRAGMA foreign_keys = ON")
        journal_mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
        if journal_mode is None or journal_mode[0] != "delete":
            raise RuntimeError("ingestion spool requires SQLite DELETE journal mode")
        connection.execute("PRAGMA synchronous = FULL")

    def _create_schema(self) -> None:
        connection = self._required_connection()
        connection.executescript(
            """
            CREATE TABLE spool_metadata (
              singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
              schema_version INTEGER NOT NULL,
              artifact_id TEXT NOT NULL,
              state TEXT NOT NULL CHECK (state IN ('preparing', 'sealed')),
              page_count INTEGER NOT NULL CHECK (page_count >= 0),
              row_count INTEGER NOT NULL CHECK (row_count >= 0),
              created_at TEXT NOT NULL,
              sealed_at TEXT
            );
            CREATE TABLE pages (
              page_sequence INTEGER PRIMARY KEY CHECK (page_sequence >= 1),
              source_observed_at TEXT NOT NULL,
              row_count INTEGER NOT NULL CHECK (
                row_count >= 0 AND row_count <= 50
              ),
              page_digest TEXT NOT NULL UNIQUE
            );
            CREATE TABLE rows (
              page_sequence INTEGER NOT NULL,
              row_sequence INTEGER NOT NULL CHECK (row_sequence >= 1),
              artifact_row_sequence INTEGER NOT NULL UNIQUE
                CHECK (artifact_row_sequence >= 1),
              row_kind TEXT NOT NULL CHECK (row_kind IN ('valid', 'malformed')),
              source_observed_at TEXT NOT NULL,
              row_digest TEXT NOT NULL UNIQUE,
              raw_payload_json TEXT NOT NULL,
              event_identity TEXT,
              canonical_hash TEXT,
              safe_error_code TEXT,
              PRIMARY KEY (page_sequence, row_sequence),
              FOREIGN KEY (page_sequence) REFERENCES pages(page_sequence),
              CHECK (
                (row_kind = 'valid'
                  AND event_identity IS NOT NULL
                  AND canonical_hash IS NOT NULL
                  AND safe_error_code IS NULL)
                OR
                (row_kind = 'malformed'
                  AND event_identity IS NULL
                  AND canonical_hash IS NULL
                  AND safe_error_code IS NOT NULL)
              )
            );
            CREATE INDEX rows_page_order_idx
              ON rows(page_sequence, row_sequence);
            """
        )
        connection.execute(
            "INSERT INTO spool_metadata("
            "singleton, schema_version, artifact_id, state, page_count, row_count, created_at"
            ") VALUES (1, ?, ?, 'preparing', 0, 0, ?)",
            (
                INGESTION_SPOOL_SCHEMA_VERSION,
                self.artifact_id,
                _utc_timestamp(datetime.now(UTC)),
            ),
        )
        connection.commit()

    def append_page(
        self,
        rows: Sequence[CapturedRowInput],
        *,
        source_observed_at: datetime,
        max_storage_bytes: int | None = None,
        guard: Callable[[], None] | None = None,
    ) -> CapturedIngestionPage:
        """Atomically append one page, assigning strict page and row sequences."""
        self._ensure_writable()
        if len(rows) > MAX_INGESTION_PAGE_ROWS:
            raise ValueError("stage-history source pages cannot exceed 50 rows")
        observed_at = _utc_timestamp(source_observed_at)
        connection = self._required_connection()
        page_count, row_count = _metadata_counts(connection)
        page_sequence = page_count + 1
        captured_rows = tuple(
            _captured_row(
                item,
                page_sequence=page_sequence,
                row_sequence=index,
                artifact_row_sequence=row_count + index,
                source_observed_at=observed_at,
            )
            for index, item in enumerate(rows, start=1)
        )
        page_digest = _page_digest(page_sequence, observed_at, captured_rows)
        if max_storage_bytes is not None:
            if isinstance(max_storage_bytes, bool) or max_storage_bytes < 1:
                raise ValueError("max_storage_bytes must be positive")
            preview_bytes = _preview_append_storage_bytes(
                connection,
                current_storage_bytes=self.total_bytes,
                page_sequence=page_sequence,
                observed_at=observed_at,
                rows=captured_rows,
                page_count=page_count,
                row_count=row_count,
                page_digest=page_digest,
            )
            if preview_bytes > max_storage_bytes:
                raise RuntimeError("ingestion spool page exceeds its storage budget")
        try:
            _run_guard(guard)
            connection.execute("BEGIN IMMEDIATE")
            current_pages, current_rows = _metadata_counts(connection)
            if current_pages != page_count or current_rows != row_count:
                raise RuntimeError("ingestion spool ordering changed during page append")
            _run_guard(guard)
            connection.execute(
                "INSERT INTO pages(page_sequence, source_observed_at, row_count, page_digest) "
                "VALUES (?, ?, ?, ?)",
                (page_sequence, observed_at, len(captured_rows), page_digest),
            )
            _run_guard(guard)
            connection.executemany(
                "INSERT INTO rows("
                "page_sequence, row_sequence, artifact_row_sequence, row_kind, "
                "source_observed_at, row_digest, raw_payload_json, event_identity, "
                "canonical_hash, safe_error_code"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(_row_storage_values(item) for item in captured_rows),
            )
            _run_guard(guard)
            metadata_update = connection.execute(
                "UPDATE spool_metadata SET page_count = ?, row_count = ? "
                "WHERE singleton = 1 AND state = 'preparing' "
                "AND page_count = ? AND row_count = ?",
                (page_sequence, row_count + len(captured_rows), page_count, row_count),
            )
            if metadata_update.rowcount != 1:
                raise RuntimeError("ingestion spool metadata compare-and-set failed")
            _run_guard(guard)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return CapturedIngestionPage(
            page_sequence=page_sequence,
            source_observed_at=observed_at,
            page_digest=page_digest,
            rows=captured_rows,
        )

    @property
    def total_bytes(self) -> int:
        """Return database plus known SQLite sidecar bytes."""
        return spool_storage_bytes(self.path)

    def flush(self) -> None:
        self._ensure_writable()
        self._required_connection().commit()

    def seal(self, *, guard: Callable[[], None] | None = None) -> Path:
        """Durably close and atomically rename the preparing database."""
        self._ensure_writable()
        if self._sealed_path.exists() or self._sealed_path.is_symlink():
            raise FileExistsError("sealed ingestion spool already exists")
        connection = self._required_connection()
        page_count, row_count = _metadata_counts(connection)
        _run_guard(guard)
        metadata_update = connection.execute(
            "UPDATE spool_metadata SET state = 'sealed', sealed_at = ? "
            "WHERE singleton = 1 AND state = 'preparing' "
            "AND page_count = ? AND row_count = ?",
            (_utc_timestamp(datetime.now(UTC)), page_count, row_count),
        )
        if metadata_update.rowcount != 1:
            connection.rollback()
            raise RuntimeError("ingestion spool seal compare-and-set failed")
        _run_guard(guard)
        connection.commit()
        _run_guard(guard)
        self._close_connection()
        _assert_no_sidecars(self.path)
        _run_guard(guard)
        self.path.rename(self._sealed_path)
        _run_guard(guard)
        self._sealed_path.chmod(0o400)
        self.path = self._sealed_path
        self._sealed = True
        self._closed = True
        return self.path

    def close(self) -> None:
        if self._closed:
            return
        self._close_connection()
        self._closed = True

    def cleanup(self) -> None:
        """Close and remove the preparing or sealed database and sidecars."""
        self._close_connection()
        self._closed = True
        _remove_storage(self.path)

    def _ensure_writable(self) -> None:
        if self._closed or self._sealed or self._connection is None:
            raise RuntimeError("stage-history ingestion spool is closed")

    def _required_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise RuntimeError("stage-history ingestion spool is closed")
        return connection

    def _close_connection(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()


class SealedStageHistoryIngestionSpool:
    """Validated immutable reader for a sealed source-free ingestion spool."""

    def __init__(self, path: Path, *, expected_artifact_id: str | None = None) -> None:
        _validate_sealed_path(path)
        if expected_artifact_id is not None:
            _validate_safe_token(expected_artifact_id, "expected_artifact_id")
        self.path = path
        uri = f"file:{path}?mode=ro&immutable=1"
        self._connection: sqlite3.Connection | None = sqlite3.connect(uri, uri=True)
        try:
            self.artifact_id = self._validate_metadata(expected_artifact_id)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Self:
        self._required_connection()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def total_bytes(self) -> int:
        return spool_storage_bytes(self.path)

    def pages(self) -> tuple[CapturedIngestionPage, ...]:
        """Read and verify every page and row in deterministic sequence."""
        connection = self._required_connection()
        page_rows = cast(
            list[tuple[object, ...]],
            connection.execute(
                "SELECT page_sequence, source_observed_at, row_count, page_digest "
                "FROM pages ORDER BY page_sequence"
            ).fetchall(),
        )
        pages: list[CapturedIngestionPage] = []
        expected_artifact_row_sequence = 1
        for expected_page_sequence, raw_page in enumerate(page_rows, start=1):
            page_sequence, observed_at, row_count, stored_digest = _page_values(raw_page)
            if page_sequence != expected_page_sequence:
                raise RuntimeError("sealed ingestion spool pages are not contiguous")
            rows = self._rows_for_page(page_sequence)
            if len(rows) != row_count:
                raise RuntimeError("sealed ingestion spool page row count does not match")
            for expected_row_sequence, row in enumerate(rows, start=1):
                if row.row_sequence != expected_row_sequence:
                    raise RuntimeError("sealed ingestion spool page rows are not contiguous")
                if row.artifact_row_sequence != expected_artifact_row_sequence:
                    raise RuntimeError("sealed ingestion spool artifact rows are not contiguous")
                expected_artifact_row_sequence += 1
            actual_digest = _page_digest(page_sequence, observed_at, rows)
            if stored_digest != actual_digest:
                raise RuntimeError("sealed ingestion spool page digest does not match")
            pages.append(CapturedIngestionPage(page_sequence, observed_at, stored_digest, rows))
        metadata_pages, metadata_rows = _metadata_counts(connection)
        if metadata_pages != len(pages):
            raise RuntimeError("sealed ingestion spool page accounting does not match")
        if metadata_rows != expected_artifact_row_sequence - 1:
            raise RuntimeError("sealed ingestion spool row accounting does not match")
        return tuple(pages)

    def page(self, page_sequence: int) -> CapturedIngestionPage:
        if isinstance(page_sequence, bool) or page_sequence < 1:
            raise ValueError("page_sequence must be positive")
        pages = self.pages()
        if page_sequence > len(pages):
            raise KeyError("sealed ingestion spool page does not exist")
        return pages[page_sequence - 1]

    def close(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()

    def _validate_metadata(self, expected_artifact_id: str | None) -> str:
        row = (
            self._required_connection()
            .execute(
                "SELECT schema_version, artifact_id, state, sealed_at "
                "FROM spool_metadata WHERE singleton = 1"
            )
            .fetchone()
        )
        if row is None or len(row) != 4:
            raise RuntimeError("sealed ingestion spool metadata is missing")
        schema_version, artifact_id, state, sealed_at = tuple(row)
        if schema_version != INGESTION_SPOOL_SCHEMA_VERSION:
            raise RuntimeError("sealed ingestion spool schema version is unsupported")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise RuntimeError("sealed ingestion spool artifact identity is invalid")
        if expected_artifact_id is not None and artifact_id != expected_artifact_id:
            raise RuntimeError("sealed ingestion spool artifact identity does not match")
        if state != "sealed" or not isinstance(sealed_at, str):
            raise RuntimeError("ingestion spool was not sealed")
        _parse_utc_timestamp(sealed_at)
        return artifact_id

    def _rows_for_page(self, page_sequence: int) -> tuple[CapturedIngestionRow, ...]:
        raw_rows = cast(
            list[tuple[object, ...]],
            self._required_connection()
            .execute(
                "SELECT page_sequence, row_sequence, artifact_row_sequence, row_kind, "
                "source_observed_at, row_digest, raw_payload_json, event_identity, "
                "canonical_hash, safe_error_code FROM rows "
                "WHERE page_sequence = ? ORDER BY row_sequence",
                (page_sequence,),
            )
            .fetchall(),
        )
        rows = tuple(_decode_stored_row(item) for item in raw_rows)
        for row in rows:
            expected_digest = _row_digest(
                page_sequence=row.page_sequence,
                row_sequence=row.row_sequence,
                artifact_row_sequence=row.artifact_row_sequence,
                row_kind=row.row_kind,
                source_observed_at=row.source_observed_at,
                raw_payload=row.raw_payload,
                event_identity=row.event_identity,
                canonical_hash=row.canonical_hash,
                safe_error_code=row.safe_error_code,
            )
            if row.row_digest != expected_digest:
                raise RuntimeError("sealed ingestion spool row digest does not match")
        return rows

    def _required_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise RuntimeError("sealed ingestion spool reader is closed")
        return connection


def _captured_row(
    item: CapturedRowInput,
    *,
    page_sequence: int,
    row_sequence: int,
    artifact_row_sequence: int,
    source_observed_at: str,
) -> CapturedIngestionRow:
    raw_payload = _decode_payload(_encode_payload(item.raw_payload))
    if isinstance(item, ValidCapturedRow):
        row_kind: CapturedRowKind = "valid"
        event_identity = item.event_identity
        canonical_hash = item.canonical_hash
        safe_error_code = None
    else:
        row_kind = "malformed"
        event_identity = None
        canonical_hash = None
        safe_error_code = item.safe_error_code
    digest = _row_digest(
        page_sequence=page_sequence,
        row_sequence=row_sequence,
        artifact_row_sequence=artifact_row_sequence,
        row_kind=row_kind,
        source_observed_at=source_observed_at,
        raw_payload=raw_payload,
        event_identity=event_identity,
        canonical_hash=canonical_hash,
        safe_error_code=safe_error_code,
    )
    return CapturedIngestionRow(
        page_sequence=page_sequence,
        row_sequence=row_sequence,
        artifact_row_sequence=artifact_row_sequence,
        row_kind=row_kind,
        source_observed_at=source_observed_at,
        row_digest=digest,
        raw_payload=raw_payload,
        event_identity=event_identity,
        canonical_hash=canonical_hash,
        safe_error_code=safe_error_code,
    )


def _row_storage_values(row: CapturedIngestionRow) -> tuple[object, ...]:
    return (
        row.page_sequence,
        row.row_sequence,
        row.artifact_row_sequence,
        row.row_kind,
        row.source_observed_at,
        row.row_digest,
        _encode_payload(row.raw_payload),
        row.event_identity,
        row.canonical_hash,
        row.safe_error_code,
    )


def _decode_stored_row(values: tuple[object, ...]) -> CapturedIngestionRow:
    if len(values) != 10:
        raise RuntimeError("sealed ingestion spool row shape is invalid")
    (
        page_sequence,
        row_sequence,
        artifact_row_sequence,
        row_kind,
        observed_at,
        row_digest,
        raw_payload_json,
        event_identity,
        canonical_hash,
        safe_error_code,
    ) = values
    if (
        isinstance(page_sequence, bool)
        or not isinstance(page_sequence, int)
        or isinstance(row_sequence, bool)
        or not isinstance(row_sequence, int)
        or isinstance(artifact_row_sequence, bool)
        or not isinstance(artifact_row_sequence, int)
        or row_kind not in {"valid", "malformed"}
        or not isinstance(observed_at, str)
        or not isinstance(row_digest, str)
        or not isinstance(raw_payload_json, str)
    ):
        raise RuntimeError("sealed ingestion spool row values are invalid")
    _parse_utc_timestamp(observed_at)
    payload = _decode_payload(raw_payload_json)
    if row_kind == "valid":
        if (
            not isinstance(event_identity, str)
            or not isinstance(canonical_hash, str)
            or safe_error_code is not None
        ):
            raise RuntimeError("sealed valid ingestion row metadata is invalid")
        kind: CapturedRowKind = "valid"
        error_code: str | None = None
    else:
        if (
            event_identity is not None
            or canonical_hash is not None
            or not isinstance(safe_error_code, str)
        ):
            raise RuntimeError("sealed malformed ingestion row metadata is invalid")
        if _SAFE_ERROR_CODE.fullmatch(safe_error_code) is None:
            raise RuntimeError("sealed malformed ingestion error code is invalid")
        kind = "malformed"
        error_code = safe_error_code
    return CapturedIngestionRow(
        page_sequence=page_sequence,
        row_sequence=row_sequence,
        artifact_row_sequence=artifact_row_sequence,
        row_kind=kind,
        source_observed_at=observed_at,
        row_digest=row_digest,
        raw_payload=payload,
        event_identity=event_identity if isinstance(event_identity, str) else None,
        canonical_hash=canonical_hash if isinstance(canonical_hash, str) else None,
        safe_error_code=error_code,
    )


def _row_digest(
    *,
    page_sequence: int,
    row_sequence: int,
    artifact_row_sequence: int,
    row_kind: CapturedRowKind,
    source_observed_at: str,
    raw_payload: JsonValue,
    event_identity: str | None,
    canonical_hash: str | None,
    safe_error_code: str | None,
) -> str:
    value: dict[str, JsonValue] = {
        "domain": "bitrix-stage-history-ingestion-row-v1",
        "page_sequence": page_sequence,
        "row_sequence": row_sequence,
        "artifact_row_sequence": artifact_row_sequence,
        "row_kind": row_kind,
        "source_observed_at": source_observed_at,
        "raw_payload": raw_payload,
        "event_identity": event_identity,
        "canonical_hash": canonical_hash,
        "safe_error_code": safe_error_code,
    }
    return _sha256(value)


def _page_digest(
    page_sequence: int,
    source_observed_at: str,
    rows: Sequence[CapturedIngestionRow],
) -> str:
    value: dict[str, JsonValue] = {
        "domain": "bitrix-stage-history-ingestion-page-v1",
        "page_sequence": page_sequence,
        "source_observed_at": source_observed_at,
        "row_count": len(rows),
        "row_digests": [row.row_digest for row in rows],
    }
    return _sha256(value)


def _sha256(value: dict[str, JsonValue]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _encode_payload(value: JsonValue) -> str:
    return canonical_json_bytes({"payload": value}).decode("utf-8")


def _decode_payload(encoded: str) -> JsonValue:
    try:
        wrapper = _JSON_VALUE_ADAPTER.validate_json(encoded)
    except ValidationError as exc:
        raise RuntimeError("sealed ingestion spool payload is invalid JSON") from exc
    if not isinstance(wrapper, dict) or set(wrapper) != {"payload"}:
        raise RuntimeError("sealed ingestion spool payload wrapper is invalid")
    return wrapper["payload"]


def _preview_append_storage_bytes(
    connection: sqlite3.Connection,
    *,
    current_storage_bytes: int,
    page_sequence: int,
    observed_at: str,
    rows: tuple[CapturedIngestionRow, ...],
    page_count: int,
    row_count: int,
    page_digest: str,
) -> int:
    preview = sqlite3.connect(":memory:")
    try:
        preview.deserialize(connection.serialize())
        preview.execute(
            "INSERT INTO pages(page_sequence, source_observed_at, row_count, page_digest) "
            "VALUES (?, ?, ?, ?)",
            (page_sequence, observed_at, len(rows), page_digest),
        )
        preview.executemany(
            "INSERT INTO rows("
            "page_sequence, row_sequence, artifact_row_sequence, row_kind, "
            "source_observed_at, row_digest, raw_payload_json, event_identity, "
            "canonical_hash, safe_error_code"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(_row_storage_values(item) for item in rows),
        )
        updated = preview.execute(
            "UPDATE spool_metadata SET page_count = ?, row_count = ? "
            "WHERE singleton = 1 AND state = 'preparing' "
            "AND page_count = ? AND row_count = ?",
            (page_sequence, row_count + len(rows), page_count, row_count),
        )
        if updated.rowcount != 1:
            raise RuntimeError("ingestion spool preview compare-and-set failed")
        preview.commit()
        next_database_bytes = len(preview.serialize())
    finally:
        preview.close()
    page_size_row = connection.execute("PRAGMA page_size").fetchone()
    page_count_row = connection.execute("PRAGMA page_count").fetchone()
    if page_size_row is None or page_count_row is None:
        raise RuntimeError("ingestion spool could not derive its journal bound")
    page_size: object = page_size_row[0]
    database_page_count: object = page_count_row[0]
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or page_size < 512
        or isinstance(database_page_count, bool)
        or not isinstance(database_page_count, int)
        or database_page_count < 1
    ):
        raise RuntimeError("ingestion spool returned invalid SQLite page accounting")
    # DELETE journals contain original pages plus record headers and sector-aligned
    # journal headers. Charging one maximum SQLite sector per original page is a
    # deliberately loose upper bound that also dominates filesystem header padding.
    journal_per_page = page_size + _JOURNAL_PAGE_RECORD_OVERHEAD + _MAX_SQLITE_SECTOR_SIZE
    rollback_journal_bound = max(
        current_storage_bytes,
        database_page_count * journal_per_page + _MAX_SQLITE_SECTOR_SIZE,
    )
    return next_database_bytes + rollback_journal_bound


def _run_guard(guard: Callable[[], None] | None) -> None:
    if guard is not None:
        guard()


def _metadata_counts(connection: sqlite3.Connection) -> tuple[int, int]:
    row = connection.execute(
        "SELECT page_count, row_count FROM spool_metadata "
        "WHERE singleton = 1 AND state IN ('preparing', 'sealed')"
    ).fetchone()
    if row is None or len(row) != 2:
        raise RuntimeError("ingestion spool metadata is missing")
    page_count, row_count = tuple(row)
    if (
        isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or page_count < 0
        or isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 0
    ):
        raise RuntimeError("ingestion spool metadata counts are invalid")
    return page_count, row_count


def _page_values(values: tuple[object, ...]) -> tuple[int, str, int, str]:
    if len(values) != 4:
        raise RuntimeError("sealed ingestion spool page shape is invalid")
    page_sequence, observed_at, row_count, page_digest = values
    if (
        isinstance(page_sequence, bool)
        or not isinstance(page_sequence, int)
        or not isinstance(observed_at, str)
        or isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or not isinstance(page_digest, str)
    ):
        raise RuntimeError("sealed ingestion spool page values are invalid")
    _parse_utc_timestamp(observed_at)
    return page_sequence, observed_at, row_count, page_digest


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("source_observed_at must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("sealed ingestion spool timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise RuntimeError("sealed ingestion spool timestamp must be UTC")
    if _utc_timestamp(parsed) != value:
        raise RuntimeError("sealed ingestion spool timestamp is not canonical")
    return parsed


def _validate_required_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be non-empty")


def _validate_safe_token(value: str, field_name: str) -> None:
    if _SAFE_TOKEN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a safe bounded token")


def _create_private_file(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    os.close(descriptor)


def _validate_sealed_path(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("sealed ingestion spool is unavailable") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise ValueError("sealed ingestion spool must be a regular non-symlink file")
    if stat.S_IMODE(path_stat.st_mode) & 0o222:
        raise ValueError("sealed ingestion spool must not be writable")


def _assert_no_sidecars(path: Path) -> None:
    sidecars = spool_storage_paths(path)[1:]
    if any(candidate.exists() or candidate.is_symlink() for candidate in sidecars):
        raise RuntimeError("ingestion spool cannot seal with SQLite sidecars")


def _remove_storage(path: Path) -> None:
    for candidate in spool_storage_paths(path):
        candidate.unlink(missing_ok=True)
