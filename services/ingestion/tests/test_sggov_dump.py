from __future__ import annotations

from pathlib import Path

from src.connectors.sggov.dump import parse_copy_tables


def test_parse_copy_tables_extracts_requested_tables(tmp_path: Path) -> None:
    dump = tmp_path / "sample.sql"
    dump.write_text(
        "COPY public.people (id, name, missing, note) FROM stdin;\n"
        "1\tAlice\t\\N\thello\\nworld\n"
        "2\tBob\tvalue\tplain\n"
        "\\.\n"
        "COPY public.ignored (id) FROM stdin;\n"
        "9\n"
        "\\.\n",
        encoding="utf-8",
    )

    tables = parse_copy_tables(dump, {"people"})

    assert list(tables) == ["people"]
    assert tables["people"] == [
        {"id": "1", "name": "Alice", "missing": None, "note": "hello\nworld"},
        {"id": "2", "name": "Bob", "missing": "value", "note": "plain"},
    ]
