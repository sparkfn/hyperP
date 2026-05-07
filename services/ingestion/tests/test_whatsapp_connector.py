"""Regression tests for WhatsApp chat connector identity extraction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

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
    assert record["raw_payload"]["summary"] == (
        "Ada ordered item A; Tonni confirmed follow-up state."
    )
