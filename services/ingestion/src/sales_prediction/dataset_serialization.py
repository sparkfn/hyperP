"""Deterministic SQLite dataset serialization (issue #125).

One dataset is a single SQLite database with a fixed schema, rows inserted in
``row_id`` order, a fixed page size, and no engine-level nondeterminism
(autoincrement counters, freelist variance). Reproducibility is anchored on
the ``content_digest`` — a SHA-256 over the canonical serialization of every
row in order — with the raw file digest recorded alongside it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path

from src.sales_prediction.models import DatasetDigest, DatasetRow

__all__ = [
    "DatasetDigest",
    "DatasetRow",
    "write_dataset",
    "read_dataset_rows",
    "read_dataset_metadata",
    "content_digest",
]

_PAGE_SIZE = 4096

_META_TABLE = """
CREATE TABLE dataset_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
)
"""

_ROW_TABLE = """
CREATE TABLE snapshots (
  row_id TEXT PRIMARY KEY,
  entity_key TEXT NOT NULL,
  deal_key TEXT NOT NULL,
  as_of_at TEXT NOT NULL,
  month TEXT NOT NULL,
  label INTEGER NOT NULL,
  label_status TEXT NOT NULL,
  label_reason TEXT NOT NULL,
  sufficiency TEXT NOT NULL,
  person_key TEXT,
  stage_id TEXT,
  category_id TEXT,
  source_semantic TEXT,
  deal_age_days REAL NOT NULL,
  days_since_prev_event REAL NOT NULL,
  prior_transition_count INTEGER NOT NULL,
  prior_won_count INTEGER NOT NULL,
  prior_lost_count INTEGER NOT NULL,
  episode_index INTEGER NOT NULL,
  amount_value REAL,
  amount_state TEXT NOT NULL,
  currency_status TEXT NOT NULL,
  currency TEXT,
  amount_known INTEGER NOT NULL,
  amount_nonzero INTEGER NOT NULL,
  assigned_known INTEGER NOT NULL,
  contact_count INTEGER NOT NULL,
  person_linked_at_s INTEGER NOT NULL,
  entity_version_age_days REAL,
  month_sin REAL NOT NULL,
  month_cos REAL NOT NULL,
  missingness_count INTEGER NOT NULL
)
"""

_ROW_COLUMNS = tuple(field.name for field in fields(DatasetRow))


def write_dataset(
    path: Path,
    metadata: Mapping[str, str],
    rows: list[DatasetRow],
) -> DatasetDigest:
    """Write one deterministic SQLite dataset and return its digests."""
    ordered = sorted(rows, key=lambda row: row.row_id)
    identifiers = [row.row_id for row in ordered]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("dataset rows must have unique row IDs")
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"PRAGMA page_size = {_PAGE_SIZE}")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute(_META_TABLE)
        connection.execute(_ROW_TABLE)
        connection.executemany(
            "INSERT INTO dataset_meta (key, value) VALUES (?, ?)",
            sorted((str(key), str(value)) for key, value in metadata.items()),
        )
        connection.executemany(
            "INSERT INTO snapshots ({columns}) VALUES ({marks})".format(
                columns=", ".join(_ROW_COLUMNS),
                marks=", ".join("?" for _ in _ROW_COLUMNS),
            ),
            [tuple(getattr(row, column) for column in _ROW_COLUMNS) for row in ordered],
        )
        connection.commit()
    finally:
        connection.close()
    return DatasetDigest(
        row_count=len(ordered),
        content_digest=content_digest(ordered),
        file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def read_dataset_rows(path: Path) -> list[DatasetRow]:
    """Read dataset rows back in canonical ``row_id`` order."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cursor = connection.execute(
            "SELECT {columns} FROM snapshots ORDER BY row_id".format(
                columns=", ".join(_ROW_COLUMNS)
            )
        )
        typed: list[DatasetRow] = []
        for values in cursor.fetchall():
            raw = dict(zip(_ROW_COLUMNS, values, strict=True))
            typed.append(_coerce_row(raw))
        return typed
    finally:
        connection.close()


def read_dataset_metadata(path: Path) -> dict[str, str]:
    """Read dataset metadata key/value pairs."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cursor = connection.execute("SELECT key, value FROM dataset_meta ORDER BY key")
        return {str(key): str(value) for key, value in cursor.fetchall()}
    finally:
        connection.close()


def content_digest(rows: list[DatasetRow]) -> str:
    """SHA-256 over the canonical serialization of rows in ``row_id`` order."""
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: item.row_id):
        canonical = json.dumps(
            {column: getattr(row, column) for column in _ROW_COLUMNS},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        digest.update(canonical.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _coerce_row(raw: dict[str, object]) -> DatasetRow:
    return DatasetRow(
        row_id=_req_str(raw["row_id"]),
        entity_key=_req_str(raw["entity_key"]),
        deal_key=_req_str(raw["deal_key"]),
        as_of_at=_req_str(raw["as_of_at"]),
        month=_req_str(raw["month"]),
        label=_req_int(raw["label"]),
        label_status=_req_str(raw["label_status"]),
        label_reason=_req_str(raw["label_reason"]),
        sufficiency=_req_str(raw["sufficiency"]),
        person_key=_opt_str(raw["person_key"]),
        stage_id=_opt_str(raw["stage_id"]),
        category_id=_opt_str(raw["category_id"]),
        source_semantic=_opt_str(raw["source_semantic"]),
        deal_age_days=_req_float(raw["deal_age_days"]),
        days_since_prev_event=_req_float(raw["days_since_prev_event"]),
        prior_transition_count=_req_int(raw["prior_transition_count"]),
        prior_won_count=_req_int(raw["prior_won_count"]),
        prior_lost_count=_req_int(raw["prior_lost_count"]),
        episode_index=_req_int(raw["episode_index"]),
        amount_value=_opt_float(raw["amount_value"]),
        amount_state=_req_str(raw["amount_state"]),
        currency_status=_req_str(raw["currency_status"]),
        currency=_opt_str(raw["currency"]),
        amount_known=_req_int(raw["amount_known"]),
        amount_nonzero=_req_int(raw["amount_nonzero"]),
        assigned_known=_req_int(raw["assigned_known"]),
        contact_count=_req_int(raw["contact_count"]),
        person_linked_at_s=_req_int(raw["person_linked_at_s"]),
        entity_version_age_days=_opt_float(raw["entity_version_age_days"]),
        month_sin=_req_float(raw["month_sin"]),
        month_cos=_req_float(raw["month_cos"]),
        missingness_count=_req_int(raw["missingness_count"]),
    )


def _req_str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("dataset row field is not a string")
    return value


def _req_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("dataset row field is not an integer")
    return value


def _req_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("dataset row field is not a number")
    return float(value)


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("dataset row optional field is not a number")
    return float(value)
