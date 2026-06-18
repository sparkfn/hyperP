from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pytest
from src.connectors.bitrix.connector import (
    BitrixChatConnector,
    _AgentMember,
)
from src.connectors.bitrix.connector import (
    _ChatBundle as BitrixBundle,
)
from src.connectors.chat_helpers import ExtractedPerson, ExtractionResult
from src.connectors.whatsapp.connector import (
    _build_envelope as build_whatsapp_envelope,
)
from src.connectors.whatsapp.connector import (
    _build_envelopes as build_whatsapp_envelopes,
)
from src.connectors.whatsapp.connector import (
    _ChatBundle as WhatsAppBundle,
)
from src.exclusion_config import ExclusionFile
from src.ingestion_config import IngestionConfig


@dataclass
class _TestSettings:
    company_mobile_numbers: list[str] = field(default_factory=list)
    company_email_addresses: list[str] = field(default_factory=list)
    internal_person_names: list[str] = field(default_factory=list)
    ingestion_exclusions_file: str = ""


def _extraction(persons: list[ExtractedPerson]) -> ExtractionResult:
    return ExtractionResult(
        persons=persons,
        possible_persons=[],
        transactions=[],
        chat_members=[],
        inquiries=[],
        strong_identifiers=[],
        weak_identifiers=[],
        summary=None,
        customer_sentiment=None,
        confidence=0.8,
    )


def _bitrix_bundle(*, chat_id: int, agent_names: list[str]) -> BitrixBundle:
    return BitrixBundle(
        chat_id=chat_id,
        deal_id=2,
        bitrix_chat_id=f"chat-{chat_id}",
        category_name="Speedzone",
        last_message_at=datetime(2026, 5, 6),
        created_at=datetime(2026, 5, 6),
        entity="speedzone",
        conv_text="conversation",
        deal=None,
        agents=[
            _AgentMember(bitrix_agent_id=str(index), name=name, active=True)
            for index, name in enumerate(agent_names, start=1)
        ],
    )


def _whatsapp_bundle(*, session_phone: str | None = "+6568505434") -> WhatsAppBundle:
    return WhatsAppBundle(
        chat_id="chat-1",
        chat_name="Customer One",
        session_id="session-1",
        whatsapp_user_id="6568505434@c.us",
        tenant="speedzone",
        msg_text="conversation",
        observed_at="2026-05-06T00:00:00",
        participants=[],
        message_endpoints=[],
        session_phone=session_phone,
    )


def test_bitrix_envelope_filters_agent_extraction() -> None:
    extraction = _extraction(
        [
            {"name": "Agent One", "phone": "+6568505434", "email": None},
            {"name": "Customer One", "phone": "+6588889999", "email": None},
        ]
    )

    envelope = BitrixChatConnector()._build_envelope(
        bundle=_bitrix_bundle(chat_id=1, agent_names=["Agent One"]),
        extraction=extraction,
    )

    assert envelope is not None
    assert envelope["attributes"] == {"full_name": "Customer One"}
    assert envelope["identifiers"] == [
        {"type": "phone", "value": "+6588889999", "is_verified": False}
    ]


def test_bitrix_envelope_skips_when_only_agent_extracted() -> None:
    extraction = _extraction([{"name": "Agent One", "phone": "+6568505434", "email": None}])

    assert (
        BitrixChatConnector()._build_envelope(
            bundle=_bitrix_bundle(chat_id=1, agent_names=["Agent One"]),
            extraction=extraction,
        )
        is None
    )


def test_bitrix_repeated_envelope_builds_do_not_read_exclusion_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_get_ingestion_config() -> IngestionConfig:
        nonlocal calls
        calls += 1
        return IngestionConfig()

    monkeypatch.setattr(
        "src.connectors.bitrix.connector.get_settings",
        lambda: _TestSettings(ingestion_exclusions_file="ignored.json"),
    )
    monkeypatch.setattr(
        "src.connectors.bitrix.connector.get_ingestion_config",
        fake_get_ingestion_config,
    )
    connector = BitrixChatConnector()
    extraction = _extraction([{"name": "Customer One", "phone": "+6588889999", "email": None}])

    connector._build_envelopes(
        bundle=_bitrix_bundle(chat_id=1, agent_names=[]),
        extraction=extraction,
    )
    connector._build_envelopes(
        bundle=_bitrix_bundle(chat_id=2, agent_names=[]),
        extraction=extraction,
    )

    assert calls == 0


def test_bitrix_agent_exclusions_do_not_mutate_shared_file_exclusions() -> None:
    file_exclusions = ExclusionFile(names=["Configured Internal User"])
    connector = BitrixChatConnector()

    connector._build_envelopes(
        bundle=_bitrix_bundle(chat_id=1, agent_names=["Agent One"]),
        extraction=_extraction(
            [
                {"name": "Agent One", "phone": "+6568505434", "email": None},
                {"name": "Customer One", "phone": "+6588889999", "email": None},
            ]
        ),
        file_exclusions=file_exclusions,
    )
    second_envelopes = connector._build_envelopes(
        bundle=_bitrix_bundle(chat_id=2, agent_names=[]),
        extraction=_extraction([{"name": "Agent One", "phone": "+6568505434", "email": None}]),
        file_exclusions=file_exclusions,
    )

    assert file_exclusions.names == ["Configured Internal User"]
    assert second_envelopes[0]["attributes"] == {"full_name": "Agent One"}


def test_whatsapp_envelope_filters_session_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.connectors.whatsapp.connector.get_settings",
        lambda: _TestSettings(),
    )
    extraction = _extraction(
        [
            {"name": "Company", "phone": "+6568505434", "email": None},
            {"name": "Customer One", "phone": "+6588889999", "email": None},
        ]
    )

    envelope = build_whatsapp_envelope(
        bundle=_whatsapp_bundle(),
        extraction=extraction,
    )

    assert envelope is not None
    assert envelope["attributes"] == {"full_name": "Customer One"}
    assert envelope["identifiers"] == [
        {"type": "phone", "value": "+6588889999", "is_verified": False}
    ]


def test_whatsapp_repeated_envelope_builds_do_not_read_exclusion_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_get_ingestion_config() -> IngestionConfig:
        nonlocal calls
        calls += 1
        return IngestionConfig()

    monkeypatch.setattr(
        "src.connectors.whatsapp.connector.get_settings",
        lambda: _TestSettings(ingestion_exclusions_file="ignored.json"),
    )
    monkeypatch.setattr(
        "src.connectors.whatsapp.connector.get_ingestion_config",
        fake_get_ingestion_config,
    )
    extraction = _extraction([{"name": "Customer One", "phone": "+6588889999", "email": None}])

    build_whatsapp_envelopes(bundle=_whatsapp_bundle(), extraction=extraction)
    build_whatsapp_envelopes(bundle=_whatsapp_bundle(session_phone=None), extraction=extraction)

    assert calls == 0
