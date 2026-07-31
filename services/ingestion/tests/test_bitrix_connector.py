"""Regression tests for Bitrix chat fetching."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from pytest import MonkeyPatch
from sqlalchemy.engine import Connection
from src.connectors.bitrix import connector as connector_module
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
    def __init__(self, rows: list[_Chat], connection: _Connection) -> None:
        self._rows = rows
        self._offset = 0
        self._connection = connection

    def fetchmany(self, size: int) -> list[_Chat]:
        rows = self._rows[self._offset : self._offset + size]
        self._offset += size
        self._connection.fetched_rows += len(rows)
        return rows


class _Connection:
    def __init__(self, rows: list[_Chat]) -> None:
        self.rows = rows
        self.chat_select_count = 0
        self.fetched_rows = 0

    def execute(self, stmt: object) -> object:
        stmt_text = str(stmt)
        if "FROM categories" in stmt_text:
            return [_Category(id=1, name="EkoSG")]
        if "FROM chats" in stmt_text:
            self.chat_select_count += 1
            return _ChatResult(self.rows, self)
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

    monkeypatch.setattr(connector_module, "extraction_method_label", lambda: "llm:test")
    monkeypatch.setattr(
        connector,
        "_load_deal",
        lambda conn, deal_id: {"title": f"deal {deal_id}", "category_id": 1},
    )
    monkeypatch.setattr(connector, "_build_conversation", lambda conn, chat_id, deal: "hello")
    monkeypatch.setattr(connector, "_load_agents", lambda conn, chat_id: [])
    monkeypatch.setattr("src.connectors.bitrix.connector.chat_batch_size", lambda: 20)
    monkeypatch.setattr("src.connectors.bitrix.connector.chat_batch_max_chars", lambda: 1_000_000)
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
        "bitrix-chat-1-person-1",
        "bitrix-chat-2-person-1",
        "bitrix-chat-3-person-1",
    ]


def test_bitrix_fetch_extracts_each_chunk_before_reading_the_next(
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
    fetched_at_extraction: list[int] = []

    monkeypatch.setattr(connector_module, "extraction_method_label", lambda: "llm:test")
    monkeypatch.setattr(
        connector,
        "_load_deal",
        lambda conn, deal_id: {"title": f"deal {deal_id}", "category_id": 1},
    )
    monkeypatch.setattr(connector, "_build_conversation", lambda conn, chat_id, deal: "hello")
    monkeypatch.setattr(connector, "_load_agents", lambda conn, chat_id: [])
    monkeypatch.setattr("src.connectors.bitrix.connector.chat_batch_size", lambda: 20)
    monkeypatch.setattr("src.connectors.bitrix.connector.chat_batch_max_chars", lambda: 1_000_000)

    def extract(texts: list[str]) -> list[dict[str, object]]:
        fetched_at_extraction.append(conn.fetched_rows)
        return [
            {
                "persons": [{"name": f"person {index}"}],
                "transactions": [],
                "summary": None,
                "confidence": 0.9,
            }
            for index, _text in enumerate(texts)
        ]

    monkeypatch.setattr("src.connectors.bitrix.connector.run_extraction_batch", extract)

    list(connector._fetch_chats(cast("Connection", conn)))

    assert fetched_at_extraction == [2, 3]


def test_bitrix_chat_envelope_keeps_agent_identity_raw_only(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(connector_module, "extraction_method_label", lambda: "llm:test")

    connector = BitrixChatConnector()
    bundle = connector_module._ChatBundle(
        chat_id=1,
        deal_id=101,
        bitrix_chat_id="chat-1",
        last_message_at=datetime(2026, 5, 7, 10, 1, 0),
        created_at=datetime(2026, 5, 7, 10, 0, 0),
        category_name="Fundbox",
        entity="fundbox",
        conv_text="Tonni: Ada ordered product A. Ada: My phone is +6591234567.",
        deal={"title": "Ada order", "stage_id": "NEW", "opened": True, "closed": False},
        agents=[connector_module._AgentMember("agent-1", "Tonni", True)],
    )
    extraction = {
        "persons": [{"name": "Ada Customer", "phone": "+6591234567", "email": "ada@example.com"}],
        "transactions": [
            {
                "order_id": "ORD-1",
                "product": "product A",
                "amount": 25.0,
                "currency": "SGD",
                "status": "pending",
                "notes": "Tonni confirmed the order details",
            }
        ],
        "summary": "Ada ordered product A; Tonni confirmed follow-up state.",
        "tone": "positive",
        "purpose": "purchase_intent",
        "outcome": "pending_business",
        "difficulty": "low",
        "confidence": 0.95,
    }

    record = connector._build_envelope(bundle=bundle, extraction=extraction)

    assert record["attributes"]["full_name"] == "Ada Customer"
    assert {item["value"] for item in record["identifiers"]} == {
        "+6591234567",
        "ada@example.com",
    }
    assert "+6599990000" not in {item["value"] for item in record["identifiers"]}
    assert record["raw_payload"]["chat_members"] == [
        {"bitrix_agent_id": "agent-1", "name": "Tonni", "active": True, "role": "agent"}
    ]
    assert record["raw_payload"]["transactions"][0]["notes"] == (
        "Tonni confirmed the order details"
    )
    assert record["raw_payload"]["summary"] == (
        "Ada ordered product A; Tonni confirmed follow-up state."
    )
    assert record["raw_payload"]["tone"] == "positive"
    assert record["raw_payload"]["purpose"] == "purchase_intent"
    assert record["raw_payload"]["outcome"] == "pending_business"
    assert record["raw_payload"]["difficulty"] == "low"

    conn = _Connection([_Chat(id=1, deal_id=101, bitrix_chat_id="b1")])
    connector = BitrixChatConnector()

    monkeypatch.setattr(
        connector,
        "_load_deal",
        lambda conn, deal_id: {"title": f"deal {deal_id}", "category_id": 99},
    )
    monkeypatch.setattr(connector, "_build_conversation", lambda conn, chat_id, deal: "hello")
    monkeypatch.setattr(connector, "_load_agents", lambda conn, chat_id: [])
    monkeypatch.setattr("src.connectors.bitrix.connector.chat_batch_size", lambda: 20)
    monkeypatch.setattr("src.connectors.bitrix.connector.chat_batch_max_chars", lambda: 1_000_000)
    monkeypatch.setattr(
        "src.connectors.bitrix.connector.run_extraction_batch",
        lambda texts: [
            {"persons": [], "transactions": [], "summary": None, "confidence": 0.0} for _ in texts
        ],
    )

    records = list(connector._fetch_chats(cast("Connection", conn)))

    assert records == []


def test_bitrix_chat_envelopes_split_possible_people(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(connector_module, "extraction_method_label", lambda: "llm:test")

    connector = BitrixChatConnector()
    bundle = connector_module._ChatBundle(
        chat_id=1,
        deal_id=101,
        bitrix_chat_id="chat-1",
        last_message_at=datetime(2026, 5, 7, 10, 1, 0),
        created_at=datetime(2026, 5, 7, 10, 0, 0),
        category_name="Fundbox",
        entity="fundbox",
        conv_text="Alice: My phone is +6581234567. My brother Bob is bob@example.com.",
        deal={"title": "Alice order", "stage_id": "NEW", "opened": True, "closed": False},
        agents=[],
    )
    extraction = {
        "persons": [],
        "possible_persons": [
            {
                "name": "Alice",
                "phone": "+6581234567",
                "identifiers": [{"type": "phone", "value": "+6581234567", "confidence": 0.95}],
                "weak_identifiers": [],
                "role": "primary_customer",
                "relationship_to_primary": None,
                "relationship_label": None,
                "evidence": "Alice gave her phone",
                "confidence": 0.95,
            },
            {
                "name": "Bob",
                "email": "bob@example.com",
                "identifiers": [{"type": "email", "value": "bob@example.com", "confidence": 0.9}],
                "weak_identifiers": [],
                "role": "secondary_person",
                "relationship_to_primary": "brother",
                "relationship_label": "brother",
                "evidence": "Alice said Bob is her brother",
                "confidence": 0.9,
            },
        ],
        "transactions": [],
        "chat_members": [],
        "inquiries": [],
        "strong_identifiers": [],
        "weak_identifiers": [],
        "summary": "Alice mentioned Bob.",
        "customer_sentiment": "neutral",
        "tone": "mixed",
        "purpose": "relationship_management",
        "outcome": "no_action_required",
        "difficulty": "low",
        "confidence": 0.9,
    }

    records = connector._build_envelopes(bundle=bundle, extraction=extraction)

    assert [record["source_record_id"] for record in records] == [
        "bitrix-chat-1-person-1",
        "bitrix-chat-1-person-2",
    ]
    assert records[0]["attributes"]["full_name"] == "Alice"
    assert {item["value"] for item in records[0]["identifiers"]} == {"+6581234567"}
    assert records[1]["attributes"]["full_name"] == "Bob"
    assert {item["value"] for item in records[1]["identifiers"]} == {"bob@example.com"}
    assert records[1]["raw_payload"]["primary_source_record_id"] == records[0]["source_record_id"]
    assert records[1]["raw_payload"]["relationship_to_primary"] == "brother"
    for record in records:
        assert record["raw_payload"]["tone"] == "mixed"
        assert record["raw_payload"]["purpose"] == "relationship_management"
        assert record["raw_payload"]["outcome"] == "no_action_required"
        assert record["raw_payload"]["difficulty"] == "low"
