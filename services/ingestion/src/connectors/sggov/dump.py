"""Minimal PostgreSQL dump COPY parser for SG Gov source dumps."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from src.models import JsonValue

CopyRow = dict[str, JsonValue]
CopyTables = dict[str, list[CopyRow]]


def _decode_copy_value(value: str) -> str | None:
    if value == r"\N":
        return None
    return value.replace(r"\n", "\n").replace(r"\t", "\t").replace(r"\r", "\r").replace(r"\\", "\\")


def _parse_copy_header(line: str) -> tuple[str, list[str]] | None:
    prefix = "COPY public."
    suffix = ") FROM stdin;"
    if not line.startswith(prefix) or not line.endswith(suffix):
        return None
    table_part, columns_part = line[len(prefix) : -len(suffix)].split(" (", 1)
    return table_part, [column.strip() for column in columns_part.split(",")]


def parse_copy_tables(path: Path, table_names: set[str]) -> CopyTables:
    tables: CopyTables = {}
    current_table: str | None = None
    current_columns: list[str] = []

    with path.open(encoding="utf-8", errors="replace") as dump_file:
        lines = (raw_line.rstrip("\r\n") for raw_line in dump_file)
        for line in lines:
            header = _parse_copy_header(line)
            if header is not None:
                table, columns = header
                if table in table_names:
                    current_table = table
                    current_columns = columns
                    tables[table] = []
                else:
                    current_table = None
                    current_columns = []
                continue

            if line == r"\.":
                current_table = None
                current_columns = []
                continue

            if current_table is None:
                continue

            tables[current_table].append(_copy_row(line, current_columns))

    return tables


def iter_copy_rows(path: Path, table_name: str) -> Iterator[CopyRow]:
    """Stream rows for one PostgreSQL COPY table."""
    current_columns: list[str] | None = None
    with path.open(encoding="utf-8", errors="replace") as dump_file:
        for raw_line in dump_file:
            line = raw_line.rstrip("\r\n")
            header = _parse_copy_header(line)
            if header is not None:
                table, columns = header
                current_columns = columns if table == table_name else None
                continue
            if line == r"\.":
                current_columns = None
                continue
            if current_columns is not None:
                yield _copy_row(line, current_columns)


def _copy_row(line: str, columns: list[str]) -> CopyRow:
    values = line.split("\t")
    return {
        column: _decode_copy_value(values[index]) if index < len(values) else None
        for index, column in enumerate(columns)
    }
