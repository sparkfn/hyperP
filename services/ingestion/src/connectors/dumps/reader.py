"""Read the subset of SQL dump formats needed by source connectors."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.models import JsonValue

TableColumns = Mapping[str, Sequence[str] | None]


class DumpPathError(ValueError):
    """Raised when a requested dump path is outside the configured dumps root."""


@dataclass(frozen=True)
class DumpRow:
    """Small row wrapper compatible with connector helpers that use attributes."""

    _mapping: Mapping[str, JsonValue]

    def __getattr__(self, name: str) -> JsonValue:
        return self._mapping.get(name)

    def get(self, key: str, default: JsonValue = None) -> JsonValue:
        return self._mapping.get(key, default)

    def as_dict(self) -> dict[str, JsonValue]:
        """Return this row as a plain dictionary."""
        return dict(self._mapping)

    def as_object_dict(self) -> dict[str, object]:
        """Return this row for helpers typed against generic DB row dictionaries."""
        return dict(self._mapping)


@dataclass(frozen=True)
class DumpTables:
    """In-memory dump rows keyed by table name."""

    _tables: Mapping[str, list[DumpRow]]

    def rows(self, table_name: str) -> list[DumpRow]:
        """Return rows for ``table_name`` or an empty list when absent."""
        return list(self._tables.get(_normalize_table_name(table_name), []))


def resolve_dump_path(dump_path: str, dumps_root: str | Path) -> Path:
    """Resolve a user-supplied dump path under the configured dumps root."""
    requested = Path(dump_path)
    if requested.is_absolute():
        raise DumpPathError("dump_path must be relative to DUMPS_ROOT")
    if any(part == ".." for part in requested.parts):
        raise DumpPathError("dump_path must not contain path traversal")

    root = Path(dumps_root).resolve()
    resolved = (root / requested).resolve()
    if root != resolved and root not in resolved.parents:
        raise DumpPathError("dump_path resolved outside DUMPS_ROOT")
    if not resolved.is_file():
        raise DumpPathError(f"dump file does not exist: {dump_path}")
    return resolved


def load_dump_tables(dump_path: str | Path, table_columns: TableColumns) -> DumpTables:
    """Load selected tables from a MySQL/MariaDB or PostgreSQL SQL dump."""
    wanted = {_normalize_table_name(name): columns for name, columns in table_columns.items()}
    inferred_columns: dict[str, list[str]] = {}
    loaded: dict[str, list[DumpRow]] = {name: [] for name in wanted}
    lines = Path(dump_path).read_text(encoding="utf-8", errors="replace").splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("CREATE TABLE "):
            statement_lines = [lines[index]]
            while not statement_lines[-1].rstrip().endswith(";") and index + 1 < len(lines):
                index += 1
                statement_lines.append(lines[index])
            _parse_mysql_create_table("\n".join(statement_lines), wanted, inferred_columns)
        elif stripped.startswith("INSERT INTO "):
            statement_lines = [lines[index]]
            while not statement_lines[-1].rstrip().endswith(";") and index + 1 < len(lines):
                index += 1
                statement_lines.append(lines[index])
            _parse_mysql_insert("\n".join(statement_lines), wanted, inferred_columns, loaded)
        elif stripped.startswith("COPY ") and " FROM stdin;" in stripped:
            index = _parse_postgres_copy(lines, index, wanted, loaded)
        index += 1
    return DumpTables(loaded)


def iter_dump_rows(
    dump_path: str | Path,
    table_name: str,
    columns: Sequence[str] | None,
) -> Iterator[DumpRow]:
    """Stream one selected table without materialising the complete dump."""
    normalized = _normalize_table_name(table_name)
    wanted: TableColumns = {normalized: columns}
    inferred_columns: dict[str, list[str]] = {}
    with Path(dump_path).open(encoding="utf-8", errors="replace") as dump_file:
        lines = iter(dump_file)
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("CREATE TABLE "):
                statement = _read_statement(line, lines)
                _parse_mysql_create_table(statement, wanted, inferred_columns)
            elif stripped.startswith("INSERT INTO "):
                yield from _iter_mysql_insert_stream(
                    line,
                    lines,
                    wanted,
                    inferred_columns,
                    normalized,
                )
            elif stripped.startswith("COPY ") and " FROM stdin;" in stripped:
                yield from _iter_postgres_copy_rows(line, lines, normalized)


def _read_statement(first_line: str, lines: Iterator[str]) -> str:
    statement_lines = [first_line.rstrip("\n")]
    while not statement_lines[-1].rstrip().endswith(";"):
        try:
            statement_lines.append(next(lines).rstrip("\n"))
        except StopIteration:
            break
    return "\n".join(statement_lines)


def _normalize_table_name(raw: str) -> str:
    name = raw.strip().strip('`"')
    if "." in name:
        name = name.rsplit(".", 1)[1]
    return name.strip('`"')


def _parse_mysql_create_table(
    statement: str,
    wanted: Mapping[str, Sequence[str] | None],
    inferred_columns: dict[str, list[str]],
) -> None:
    prefix = "CREATE TABLE "
    rest = statement[len(prefix) :].lstrip()
    if rest.startswith("IF NOT EXISTS "):
        rest = rest[len("IF NOT EXISTS ") :].lstrip()
    table_name, after_table = _read_mysql_identifier(rest)
    normalized = _normalize_table_name(table_name)
    if normalized not in wanted or wanted[normalized] is not None:
        return
    start = after_table.find("(")
    end = after_table.rfind(")")
    if start < 0 or end < start:
        return
    columns: list[str] = []
    for raw_line in after_table[start + 1 : end].splitlines():
        stripped = raw_line.strip().rstrip(",")
        if not stripped.startswith("`"):
            continue
        column_name, _ = _read_mysql_identifier(stripped)
        columns.append(_normalize_table_name(column_name))
    inferred_columns[normalized] = columns


def _parse_mysql_insert(
    statement: str,
    wanted: Mapping[str, Sequence[str] | None],
    inferred_columns: Mapping[str, Sequence[str]],
    loaded: dict[str, list[DumpRow]],
) -> None:
    normalized = _mysql_insert_table_name(statement)
    if normalized not in loaded:
        return
    loaded[normalized].extend(
        _iter_mysql_insert_rows(statement, wanted, inferred_columns, normalized)
    )


def _mysql_insert_table_name(statement: str) -> str:
    prefix = "INSERT INTO "
    rest = statement[len(prefix) :].lstrip()
    table_name, _ = _read_mysql_identifier(rest)
    return _normalize_table_name(table_name)


def _iter_mysql_insert_rows(
    statement: str,
    wanted: Mapping[str, Sequence[str] | None],
    inferred_columns: Mapping[str, Sequence[str]],
    expected_table: str,
) -> Iterator[DumpRow]:
    prefix = "INSERT INTO "
    rest = statement[len(prefix) :].lstrip()
    table_name, after_table = _read_mysql_identifier(rest)
    normalized = _normalize_table_name(table_name)
    if normalized != expected_table or normalized not in wanted:
        return
    columns, after_columns = _read_optional_column_list(after_table.lstrip())
    configured_columns = wanted[normalized]
    fallback_columns = inferred_columns.get(normalized, [])
    final_columns = columns if columns else list(configured_columns or fallback_columns)
    if not final_columns:
        raise ValueError(f"columns required for MySQL dump table {normalized}")
    values_index = after_columns.upper().find("VALUES")
    if values_index < 0:
        return
    values_text = after_columns[values_index + len("VALUES") :].strip().rstrip(";")
    for tuple_values in _iter_mysql_tuples(values_text):
        row = _row_from_columns(final_columns, tuple_values)
        yield DumpRow(row)


def _iter_mysql_insert_stream(
    first_line: str,
    lines: Iterator[str],
    wanted: Mapping[str, Sequence[str] | None],
    inferred_columns: Mapping[str, Sequence[str]],
    expected_table: str,
) -> Iterator[DumpRow]:
    normalized = _mysql_insert_table_name(first_line)
    if normalized != expected_table or normalized not in wanted:
        _consume_statement(first_line, lines)
        return

    header = first_line
    while "VALUES" not in header.upper():
        if header.rstrip().endswith(";"):
            return
        try:
            header += next(lines)
        except StopIteration:
            return

    rest = header[len("INSERT INTO ") :].lstrip()
    _table_name, after_table = _read_mysql_identifier(rest)
    columns, after_columns = _read_optional_column_list(after_table.lstrip())
    configured_columns = wanted[normalized]
    fallback_columns = inferred_columns.get(normalized, [])
    final_columns = columns if columns else list(configured_columns or fallback_columns)
    if not final_columns:
        raise ValueError(f"columns required for MySQL dump table {normalized}")
    values_index = after_columns.upper().find("VALUES")
    if values_index < 0:
        return
    first_values = after_columns[values_index + len("VALUES") :]
    for tuple_values in _iter_mysql_tuple_chunks(
        _statement_value_chunks(first_values, header, lines)
    ):
        yield DumpRow(_row_from_columns(final_columns, tuple_values))


def _consume_statement(first_line: str, lines: Iterator[str]) -> None:
    if first_line.rstrip().endswith(";"):
        return
    for line in lines:
        if line.rstrip().endswith(";"):
            return


def _statement_value_chunks(
    first_values: str,
    header: str,
    lines: Iterator[str],
) -> Iterator[str]:
    yield first_values
    if header.rstrip().endswith(";"):
        return
    for line in lines:
        yield line
        if line.rstrip().endswith(";"):
            return


def _read_mysql_identifier(text: str) -> tuple[str, str]:
    if text.startswith("`"):
        end = text.index("`", 1)
        return text[1:end], text[end + 1 :]
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _read_optional_column_list(text: str) -> tuple[list[str], str]:
    if not text.startswith("("):
        return [], text
    close = text.index(")")
    raw_columns = text[1:close]
    return [_normalize_table_name(part) for part in raw_columns.split(",")], text[close + 1 :]


def _parse_mysql_tuples(values_text: str) -> list[list[JsonValue]]:
    return list(_iter_mysql_tuples(values_text))


def _iter_mysql_tuples(values_text: str) -> Iterator[list[JsonValue]]:
    yield from _iter_mysql_tuple_chunks((values_text,))


def _iter_mysql_tuple_chunks(chunks: Iterator[str] | tuple[str, ...]) -> Iterator[list[JsonValue]]:
    current_row: list[JsonValue] = []
    current_chars: list[str] = []
    in_string = False
    escaping = False
    in_tuple = False
    token_was_quoted = False

    for chunk in chunks:
        for char in chunk:
            if in_string:
                if escaping:
                    current_chars.append(_decode_mysql_escape(char))
                    escaping = False
                elif char == "\\":
                    escaping = True
                elif char == "'":
                    in_string = False
                else:
                    current_chars.append(char)
                continue

            if char == "'" and in_tuple:
                in_string = True
                token_was_quoted = True
                continue
            if char == "(":
                in_tuple = True
                current_row = []
                current_chars = []
                token_was_quoted = False
                continue
            if char == "," and in_tuple:
                current_row.append(_coerce_mysql_token("".join(current_chars), token_was_quoted))
                current_chars = []
                token_was_quoted = False
                continue
            if char == ")" and in_tuple:
                current_row.append(_coerce_mysql_token("".join(current_chars), token_was_quoted))
                yield current_row
                in_tuple = False
                current_chars = []
                token_was_quoted = False
                continue
            if in_tuple:
                current_chars.append(char)


def _decode_mysql_escape(char: str) -> str:
    escapes: dict[str, str] = {
        "0": "\0",
        "b": "\b",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "Z": "\x1a",
    }
    return escapes.get(char, char)


def _coerce_mysql_token(token: str, was_quoted: bool) -> JsonValue:
    if was_quoted:
        return token
    stripped = token.strip()
    if stripped.upper() == "NULL":
        return None
    if stripped in {""}:
        return ""
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        return stripped


def _parse_postgres_copy(
    lines: list[str],
    copy_index: int,
    wanted: Mapping[str, Sequence[str] | None],
    loaded: dict[str, list[DumpRow]],
) -> int:
    header = lines[copy_index].strip()
    table_start = len("COPY ")
    columns_start = header.index("(")
    columns_end = header.index(") FROM stdin;")
    table_name = _normalize_table_name(header[table_start:columns_start].strip())
    columns = [
        _normalize_table_name(part) for part in header[columns_start + 1 : columns_end].split(",")
    ]
    index = copy_index + 1
    while index < len(lines) and lines[index] != r"\.":
        if table_name in wanted:
            values = [
                _coerce_postgres_value(value) for value in _split_postgres_copy_line(lines[index])
            ]
            loaded[table_name].append(DumpRow(_row_from_columns(columns, values)))
        index += 1
    return index


def _iter_postgres_copy_rows(
    header_line: str,
    lines: Iterator[str],
    expected_table: str,
) -> Iterator[DumpRow]:
    header = header_line.strip()
    table_start = len("COPY ")
    columns_start = header.index("(")
    columns_end = header.index(") FROM stdin;")
    table_name = _normalize_table_name(header[table_start:columns_start].strip())
    columns = [
        _normalize_table_name(part) for part in header[columns_start + 1 : columns_end].split(",")
    ]
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if line == r"\.":
            return
        if table_name != expected_table:
            continue
        values = [_coerce_postgres_value(value) for value in _split_postgres_copy_line(line)]
        yield DumpRow(_row_from_columns(columns, values))


def _split_postgres_copy_line(line: str) -> list[str]:
    values: list[str] = []
    current_chars: list[str] = []
    escaping = False
    for char in line:
        if escaping:
            current_chars.append(_decode_postgres_escape(char))
            escaping = False
        elif char == "\\":
            escaping = True
        elif char == "\t":
            values.append("".join(current_chars))
            current_chars = []
        else:
            current_chars.append(char)
    values.append("".join(current_chars))
    return values


def _decode_postgres_escape(char: str) -> str:
    escapes: dict[str, str] = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
    return escapes.get(char, char)


def _coerce_postgres_value(value: str) -> JsonValue:
    if value == "N":
        return None
    if value == "t":
        return True
    if value == "f":
        return False
    return value


def _row_from_columns(columns: Sequence[str], values: Sequence[JsonValue]) -> dict[str, JsonValue]:
    return {columns[index]: value for index, value in enumerate(values) if index < len(columns)}
