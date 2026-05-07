"""Regression tests for Bitrix chat fetching."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from pytest import MonkeyPatch
from sqlalchemy.engine import Connection
from src.connectors.bitrix.connector import BitrixChatConnector


@dataclass
class _Category:
    id: int
    name: str


@dataclass
class _Chat:
    id: int
    deal_id: int
    bitrix_chat_id: str
    last_message_at: datetime | None = None
    created_at: datetime | None = None


class _Result(Protocol):
    def fetchmany(self, size: int) -> list[_Chat]: ...


class _ChatResult:
    def __init__(self, rows: list[_Chat]) -> None:
        self._rows = rows
        self._offset = 0

    def fetchmany(self, size: int) -> list[_Chat]:
        rows = self._rows[self._offset : self._offset + size]
        self._offset += size
        return rows


class _Connection:
    def __init__(self, rows: list[_Chat]) -> None:
        self.rows = rows
        self.chat_select_count = 0

    def execute(self, stmt: object) -> object:
        stmt_text = str(stmt)
        if "FROM categories" in stmt_text:
            return [_Category(id=1, name="EkoSG")]
        if "FROM chats" in stmt_text:
            self.chat_select_count += 1
            return _ChatResult(self.rows)
        raise AssertionError(f"unexpected statement: {stmt_text}")

    def scalar(self, stmt: object) -> int:
        _ = stmt
        return len(self.rows)


def test_bitrix_fetch_uses_one_cursor_for_chunked_chat_scan(
    monkeypatch: MonkeyPatch,
) -> None:
    conn = _Connection(
        [
            _Chat(id=1, deal_id=101, bitrix_chat_id="b1"),
            _Chat(id=2, deal_id=102, bitrix_chat_id="b2"),
            _Chat(id=3, deal_id=103, bitrix_chat_id="b3"),
        ]
    )
    connector = BitrixChatConnector()
    connector.chunk_size = 2

    monkeypatch.setattr(
        connector,
        "_load_deal",
        lambda conn, deal_id: {"title": f"deal {deal_id}", "category_id": 1},
    )
    monkeypatch.setattr(connector, "_build_conversation", lambda conn, chat_id, deal: "hello")
    monkeypatch.setattr(connector, "_load_agents", lambda conn, chat_id: [])
    monkeypatch.setattr(
        "src.connectors.bitrix.connector.run_extraction_batch",
        lambda texts: [
            {
                "persons": [{"name": f"person {index}"}],
                "transactions": [],
                "summary": None,
                "confidence": 0.9,
            }
            for index, _ in enumerate(texts)
        ],
    )

    records = list(connector._fetch_chats(cast("Connection", conn)))

    assert conn.chat_select_count == 1
    assert [record["source_record_id"] for record in records] == [
        "bitrix-chat-1",
        "bitrix-chat-2",
        "bitrix-chat-3",
    ]


def test_bitrix_fetch_skips_chats_for_unmapped_crm_categories(
    monkeypatch: MonkeyPatch,
) -> None:
    conn = _Connection([_Chat(id=1, deal_id=101, bitrix_chat_id="b1")])
    connector = BitrixChatConnector()

    monkeypatch.setattr(
        connector,
        "_load_deal",
        lambda conn, deal_id: {"title": f"deal {deal_id}", "category_id": 99},
    )
    monkeypatch.setattr(connector, "_build_conversation", lambda conn, chat_id, deal: "hello")
    monkeypatch.setattr(connector, "_load_agents", lambda conn, chat_id: [])
    monkeypatch.setattr(
        "src.connectors.bitrix.connector.run_extraction_batch",
        lambda texts: [
            {"persons": [], "transactions": [], "summary": None, "confidence": 0.0}
            for _ in texts
        ],
    )

    records = list(connector._fetch_chats(cast("Connection", conn)))

    assert records == []
