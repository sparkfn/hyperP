"""Tests for direct SQL dump reading."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from src.connectors.dumps import reader
from src.connectors.dumps.reader import DumpPathError, load_dump_tables, resolve_dump_path
from src.connectors.fundbox.builders import to_iso


def test_mysql_multiline_insert_decodes_selected_rows(tmp_path: Path) -> None:
    dump_path = tmp_path / "sample.sql"
    dump_path.write_text(
        """
INSERT INTO `ignored` VALUES (1,'skip');
INSERT INTO `people` VALUES
(1,'Ada\\'s phone',NULL,'2026-05-06 12:34:56'),
(2,'Back\\\\slash',42,'2026-05-07 01:02:03');
""".strip(),
        encoding="utf-8",
    )

    tables = load_dump_tables(
        dump_path,
        {"people": ["id", "name", "optional", "created_at"]},
    )

    assert [row.as_dict() for row in tables.rows("people")] == [
        {
            "id": 1,
            "name": "Ada's phone",
            "optional": None,
            "created_at": "2026-05-06 12:34:56",
        },
        {
            "id": 2,
            "name": "Back\\slash",
            "optional": 42,
            "created_at": "2026-05-07 01:02:03",
        },
    ]


def test_mysql_insert_uses_create_table_columns_when_insert_has_no_columns(
    tmp_path: Path,
) -> None:
    dump_path = tmp_path / "sample.sql"
    dump_path.write_text(
        """
CREATE TABLE `people` (
  `id` bigint NOT NULL,
  `name` varchar(255) DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
);
INSERT INTO `people` VALUES (1,'Ada','2026-05-06 12:34:56');
""".strip(),
        encoding="utf-8",
    )

    tables = load_dump_tables(dump_path, {"people": None})

    assert tables.rows("people")[0].as_dict() == {
        "id": 1,
        "name": "Ada",
        "created_at": "2026-05-06 12:34:56",
    }


def test_postgresql_copy_decodes_selected_rows(tmp_path: Path) -> None:
    dump_path = tmp_path / "whatsapp.sql"
    dump_path.write_text(
        """
COPY public.messages (id, chat_id, body, timestamp, from_me) FROM stdin;
msg-1	chat-1	Hello\\nthere	2026-05-06 10:00:00	f
msg-2	chat-1	\\N	2026-05-06 10:01:00	t
\\.
COPY public.sessions (id, status) FROM stdin;
sess-1	ready
\\.
""".strip(),
        encoding="utf-8",
    )

    tables = load_dump_tables(dump_path, {"messages": None, "sessions": None})

    assert [row.as_dict() for row in tables.rows("messages")] == [
        {
            "id": "msg-1",
            "chat_id": "chat-1",
            "body": "Hello\nthere",
            "timestamp": "2026-05-06 10:00:00",
            "from_me": False,
        },
        {
            "id": "msg-2",
            "chat_id": "chat-1",
            "body": None,
            "timestamp": "2026-05-06 10:01:00",
            "from_me": True,
        },
    ]
    assert tables.rows("sessions")[0].as_dict() == {"id": "sess-1", "status": "ready"}


def test_to_iso_converts_mysql_datetime_strings_to_neo4j_datetime_text() -> None:
    assert to_iso("2026-05-06 12:34:56") == "2026-05-06T12:34:56Z"


def test_resolve_dump_path_rejects_absolute_and_traversal(tmp_path: Path) -> None:
    allowed = tmp_path / "dumps"
    allowed.mkdir()
    valid = allowed / "source.sql"
    valid.write_text("", encoding="utf-8")

    assert resolve_dump_path("source.sql", allowed) == valid.resolve()

    with pytest.raises(DumpPathError):
        resolve_dump_path(str(valid.resolve()), allowed)
    with pytest.raises(DumpPathError):
        resolve_dump_path("../source.sql", allowed)


def test_iter_dump_rows_streams_without_reading_the_whole_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dump_path = tmp_path / "stream.sql"
    dump_path.write_text(
        "INSERT INTO `people` (`id`, `name`) VALUES\n(1,'Ada'),\n(2,'Grace');\n",
        encoding="utf-8",
    )

    def reject_read_text(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("streaming dump reads must not call Path.read_text")

    monkeypatch.setattr(Path, "read_text", reject_read_text)

    rows = list(reader.iter_dump_rows(dump_path, "people", ["id", "name"]))

    assert [row.as_dict() for row in rows] == [
        {"id": 1, "name": "Ada"},
        {"id": 2, "name": "Grace"},
    ]


def test_iter_dump_rows_yields_before_reading_complete_extended_insert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dump_path = tmp_path / "stream.sql"
    dump_path.write_text("placeholder", encoding="utf-8")
    allow_remaining_rows = False

    class GuardedDump:
        def __init__(self) -> None:
            self._lines = iter(
                (
                    "INSERT INTO `people` (`id`, `name`) VALUES\n",
                    "(1,'Ada'),\n",
                    "(2,'Grace'),\n",
                    "(3,'Linus');\n",
                )
            )
            self._line_number = 0

        def __enter__(self) -> GuardedDump:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> GuardedDump:
            return self

        def __next__(self) -> str:
            nonlocal allow_remaining_rows
            self._line_number += 1
            if self._line_number > 2 and not allow_remaining_rows:
                raise AssertionError("reader consumed the complete INSERT before yielding")
            return next(self._lines)

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: GuardedDump())
    rows: Iterator[reader.DumpRow] = reader.iter_dump_rows(
        dump_path,
        "people",
        ["id", "name"],
    )

    assert next(rows).as_dict() == {"id": 1, "name": "Ada"}
    allow_remaining_rows = True
    assert [row.as_dict() for row in rows] == [
        {"id": 2, "name": "Grace"},
        {"id": 3, "name": "Linus"},
    ]
