"""Tests for direct SQL dump reading."""

from __future__ import annotations

from pathlib import Path

import pytest
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
