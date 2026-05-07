"""Regression tests for WhatsApp chat connector identity extraction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

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
    assert participants[0].role == "chat"
