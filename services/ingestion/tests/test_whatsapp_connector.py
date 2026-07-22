"""Regression tests for WhatsApp chat connector identity extraction."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import pytest
from pytest import MonkeyPatch
from src.connectors.whatsapp import connector as whatsapp_module
from src.connectors.whatsapp.connector import WhatsAppChatConnector


@dataclass
class _ContactRow:
    jid: str
    lid_id: str | None
    cus_id: str | None
    phone_number: str | None
    number: str | None
    name: str | None
    pushname: str | None
    short_name: str | None


class _Result(Protocol):
    def __iter__(self) -> object: ...


class _Connection:
    def __init__(self, rows: list[_ContactRow]) -> None:
        self.rows = rows

    def execute(self, stmt: object) -> list[_ContactRow]:
        _ = stmt
        return self.rows


def test_process_whatsapp_bundles_can_fail_on_extraction_error(
    monkeypatch: MonkeyPatch,
) -> None:
    bundle = whatsapp_module._ChatBundle(
        chat_id="chat-1",
        chat_name="Customer",
        session_id="session-1",
        whatsapp_user_id="6599990000@c.us",
        tenant="fundbox",
        msg_text="Customer: hello",
        observed_at="2026-05-07T10:01:00",
        participants=[],
        message_endpoints=[],
        session_phone=None,
    )
    monkeypatch.setattr(whatsapp_module, "run_extraction_batch", lambda texts: [None])

    with pytest.raises(RuntimeError, match="chat-1"):
        list(
            whatsapp_module.process_whatsapp_bundles(
                [bundle],
                fail_on_extraction_error=True,
            )
        )


def test_whatsapp_bundle_batches_yield_before_consuming_all_chats() -> None:
    consumed: list[str] = []

    def bundles() -> Iterator[whatsapp_module._ChatBundle]:
        for chat_id in ("chat-1", "chat-2", "chat-3"):
            consumed.append(chat_id)
            yield whatsapp_module._ChatBundle(
                chat_id=chat_id,
                chat_name="Customer",
                session_id="session-1",
                whatsapp_user_id="6599990000@c.us",
                tenant="fundbox",
                msg_text="hello",
                observed_at="2026-05-07T10:01:00",
                participants=[],
                message_endpoints=[],
                session_phone=None,
            )

    batches = iter(
        whatsapp_module.iter_bundle_batches(
            bundles(),
            max_chars=1_000,
            max_count=2,
        )
    )

    assert [bundle.chat_id for bundle in next(batches)] == ["chat-1", "chat-2"]
    assert consumed == ["chat-1", "chat-2"]


def test_process_whatsapp_bundles_keeps_same_chat_id_sessions_separate(
    monkeypatch: MonkeyPatch,
) -> None:
    bundles = [
        whatsapp_module._ChatBundle(
            chat_id="shared-chat",
            chat_name="Customer",
            session_id=session_id,
            whatsapp_user_id=whatsapp_user_id,
            tenant="fundbox",
            msg_text=text,
            observed_at="2026-05-07T10:01:00",
            participants=[],
            message_endpoints=[],
            session_phone=None,
            source_id_scope=session_id,
        )
        for session_id, whatsapp_user_id, text in (
            ("session-1", "6591111111@c.us", "Alice conversation"),
            ("session-2", "6592222222@c.us", "Bob conversation"),
        )
    ]
    monkeypatch.setattr(
        whatsapp_module,
        "run_extraction_batch",
        lambda texts: [
            {
                "persons": [{"name": "Alice", "phone": "+6581111111"}],
                "transactions": [],
                "summary": "Alice",
                "confidence": 0.9,
            },
            {
                "persons": [{"name": "Bob", "phone": "+6582222222"}],
                "transactions": [],
                "summary": "Bob",
                "confidence": 0.9,
            },
        ],
    )

    records = list(whatsapp_module.process_whatsapp_bundles(bundles))

    assert [record["attributes"]["full_name"] for record in records] == ["Alice", "Bob"]


def test_whatsapp_format_messages_prefers_names_and_speaker_phones() -> None:
    messages: list[dict[str, object]] = [
        {
            "id": "msg-1",
            "from_id": "6599990000@c.us",
            "to_id": "6500000000@c.us",
            "author_id": None,
            "body": "hello",
            "timestamp": datetime(2026, 5, 6, 10, 0),
            "from_me": False,
        },
        {
            "id": "msg-2",
            "from_id": "120363349430463692@g.us",
            "to_id": "6500000000@c.us",
            "author_id": "183330762936572@lid",
            "body": "group reply",
            "timestamp": datetime(2026, 5, 6, 10, 1),
            "from_me": True,
        },
        {
            "id": "msg-3",
            "from_id": "120363349430463692@g.us",
            "to_id": "6500000000@c.us",
            "author_id": None,
            "body": "group notice",
            "timestamp": datetime(2026, 5, 6, 10, 2),
            "from_me": False,
        },
    ]
    participants = [
        whatsapp_module._Participant(
            jid="6599990000@c.us",
            phone="+6599990000",
            name="Ada Lovelace",
            role="participant",
        ),
        whatsapp_module._Participant(
            jid="183330762936572@lid",
            phone="+6588880000",
            name="Babbage Bikes",
            role="participant",
        ),
        whatsapp_module._Participant(
            jid="120363349430463692@g.us",
            phone=None,
            name=None,
            role="chat",
        ),
    ]

    text = whatsapp_module._format_messages(messages, participants, "Loan Group")

    assert text == (
        "[2026-05-06 10:00:00] Ada Lovelace (+6599990000): hello\n"
        "[2026-05-06 10:01:00] [ME] Babbage Bikes (+6588880000): group reply\n"
        "[2026-05-06 10:02:00] Loan Group: group notice"
    )
    assert "@" not in text


def test_whatsapp_fetch_participants_resolves_lid_chat_to_cus_phone() -> None:
    connector = WhatsAppChatConnector()
    conn = _Connection(
        [
            _ContactRow(
                jid="6599990000@c.us",
                lid_id="123456789@lid",
                cus_id="6599990000@c.us",
                phone_number="+6599990000",
                number=None,
                name="Ada Lovelace",
                pushname=None,
                short_name=None,
            )
        ]
    )
    msgs: list[dict[str, object]] = [
        {
            "from_id": "123456789@lid",
            "to_id": "6500000000@c.us",
            "author_id": None,
            "body": "hello",
            "timestamp": datetime(2026, 5, 6),
            "from_me": False,
        }
    ]

    participants = connector._fetch_participants(
        conn,  # type: ignore[arg-type]
        "123456789@lid",
        "6500000000@c.us",
        msgs,
    )

    assert participants[0].jid == "123456789@lid"
    assert participants[0].phone == "+6599990000"
    assert participants[0].name == "Ada Lovelace"


def test_whatsapp_chat_envelope_keeps_agent_identity_raw_only(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(whatsapp_module, "extraction_method_label", lambda: "llm:test")

    bundle = whatsapp_module._ChatBundle(
        chat_id="6599990000@c.us",
        chat_name="Tonni",
        session_id="session-1",
        whatsapp_user_id="6599990000@c.us",
        tenant="fundbox",
        msg_text="[2026-05-07 10:00:00] Tonni: Ada ordered item A\n"
        "[2026-05-07 10:01:00] Ada: My phone is +6591234567",
        observed_at="2026-05-07T10:01:00",
        participants=[
            whatsapp_module._Participant(
                jid="6599990000@c.us",
                phone="+6599990000",
                name="Tonni",
                role="chat",
            ),
            whatsapp_module._Participant(
                jid="6591234567@c.us",
                phone="+6591234567",
                name="Ada Customer",
                role="member",
            ),
        ],
        message_endpoints=[
            {"role": "sender", "jid": "6599990000@c.us", "phone": "+6599990000"},
            {"role": "recipient", "jid": "6591234567@c.us", "phone": "+6591234567"},
        ],
        session_phone="+6599990000",
    )
    extraction = {
        "persons": [{"name": "Ada Customer", "phone": "+6591234567", "email": "ada@example.com"}],
        "transactions": [
            {
                "order_id": "ORD-1",
                "product": "item A",
                "amount": 25.0,
                "currency": "SGD",
                "status": "pending",
                "notes": "Tonni confirmed the order details",
            }
        ],
        "summary": "Ada ordered item A; Tonni confirmed follow-up state.",
        "confidence": 0.95,
    }

    record = whatsapp_module._build_envelope(bundle=bundle, extraction=extraction)

    assert record["attributes"]["full_name"] == "Ada Customer"
    assert {item["value"] for item in record["identifiers"]} == {
        "+6591234567",
        "ada@example.com",
    }
    assert "+6599990000" not in {item["value"] for item in record["identifiers"]}
    assert record["raw_payload"]["participants"][0] == {
        "jid": "6599990000@c.us",
        "phone": "+6599990000",
        "name": "Tonni",
        "role": "chat",
    }
    assert record["raw_payload"]["transactions"][0]["notes"] == (
        "Tonni confirmed the order details"
    )


def test_whatsapp_chat_envelopes_split_possible_people(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(whatsapp_module, "extraction_method_label", lambda: "llm:test")

    bundle = whatsapp_module._ChatBundle(
        chat_id="6599990000@c.us",
        chat_name="Alice chat",
        session_id="session-1",
        whatsapp_user_id="6599990000@c.us",
        tenant="fundbox",
        msg_text="Alice: My phone is +6581234567. My brother Bob is bob@example.com.",
        observed_at="2026-05-07T10:01:00",
        participants=[],
        message_endpoints=[],
        session_phone=None,
        source_id_scope="session-1",
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
        "confidence": 0.9,
    }

    records = whatsapp_module._build_envelopes(bundle=bundle, extraction=extraction)

    assert [record["source_record_id"] for record in records] == [
        "whatsapp-chat-session-1-6599990000@c.us-person-1",
        "whatsapp-chat-session-1-6599990000@c.us-person-2",
    ]
    assert records[0]["attributes"]["full_name"] == "Alice"
    assert {item["value"] for item in records[0]["identifiers"]} == {"+6581234567"}
    assert records[1]["attributes"]["full_name"] == "Bob"
    assert {item["value"] for item in records[1]["identifiers"]} == {"bob@example.com"}
    assert records[1]["raw_payload"]["primary_source_record_id"] == records[0]["source_record_id"]
    assert records[1]["raw_payload"]["relationship_to_primary"] == "brother"
