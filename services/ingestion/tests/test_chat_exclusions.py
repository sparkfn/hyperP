from __future__ import annotations

from datetime import datetime

from src.connectors.bitrix.connector import BitrixChatConnector, _AgentMember
from src.connectors.bitrix.connector import _ChatBundle as BitrixBundle
from src.connectors.chat_helpers import ExtractionResult
from src.connectors.whatsapp.connector import _build_envelope as build_whatsapp_envelope
from src.connectors.whatsapp.connector import _ChatBundle as WhatsAppBundle


def test_bitrix_envelope_filters_agent_extraction() -> None:
    bundle = BitrixBundle(
        chat_id=1,
        deal_id=2,
        bitrix_chat_id="chat-1",
        category_name="Speedzone",
        last_message_at=datetime(2026, 5, 6),
        created_at=datetime(2026, 5, 6),
        entity="speedzone",
        conv_text="conversation",
        deal=None,
        agents=[_AgentMember(bitrix_agent_id="99", name="Agent One", active=True)],
    )
    extraction = ExtractionResult(
        persons=[
            {"name": "Agent One", "phone": "+6568505434", "email": None},
            {"name": "Customer One", "phone": "+6588889999", "email": None},
        ],
        transactions=[],
        summary=None,
        confidence=0.8,
    )

    envelope = BitrixChatConnector()._build_envelope(bundle=bundle, extraction=extraction)

    assert envelope is not None
    assert envelope["attributes"] == {"full_name": "Customer One"}
    assert envelope["identifiers"] == [
        {"type": "phone", "value": "+6588889999", "is_verified": False}
    ]


def test_bitrix_envelope_skips_when_only_agent_extracted() -> None:
    bundle = BitrixBundle(
        chat_id=1,
        deal_id=2,
        bitrix_chat_id="chat-1",
        category_name="Speedzone",
        last_message_at=datetime(2026, 5, 6),
        created_at=datetime(2026, 5, 6),
        entity="speedzone",
        conv_text="conversation",
        deal=None,
        agents=[_AgentMember(bitrix_agent_id="99", name="Agent One", active=True)],
    )
    extraction = ExtractionResult(
        persons=[{"name": "Agent One", "phone": "+6568505434", "email": None}],
        transactions=[],
        summary=None,
        confidence=0.8,
    )

    assert BitrixChatConnector()._build_envelope(bundle=bundle, extraction=extraction) is None


def test_whatsapp_envelope_filters_session_phone(monkeypatch: object) -> None:
    monkeypatch.setattr(
        "src.connectors.whatsapp.connector.get_settings",
        lambda: type("Settings", (), {"company_mobile_numbers": []})(),
    )
    bundle = WhatsAppBundle(
        chat_id="chat-1",
        chat_name="Customer One",
        session_id="session-1",
        whatsapp_user_id="6568505434@c.us",
        tenant="speedzone",
        msg_text="conversation",
        observed_at="2026-05-06T00:00:00",
        participants=[],
        message_endpoints=[],
        session_phone="+6568505434",
    )
    extraction = ExtractionResult(
        persons=[
            {"name": "Company", "phone": "+6568505434", "email": None},
            {"name": "Customer One", "phone": "+6588889999", "email": None},
        ],
        transactions=[],
        summary=None,
        confidence=0.8,
    )

    envelope = build_whatsapp_envelope(bundle=bundle, extraction=extraction)

    assert envelope is not None
    assert envelope["attributes"] == {"full_name": "Customer One"}
    assert envelope["identifiers"] == [
        {"type": "phone", "value": "+6588889999", "is_verified": False}
    ]
