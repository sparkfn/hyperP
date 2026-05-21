"""Minimal PostgreSQL dump COPY parser for SG Gov source dumps."""

from __future__ import annotations

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

    for line in path.read_text(encoding="utf-8").splitlines():
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

        values = line.split("\t")
        row: CopyRow = {
            column: _decode_copy_value(values[index]) if index < len(values) else None
            for index, column in enumerate(current_columns)
        }
        tables[current_table].append(row)

    return tables
