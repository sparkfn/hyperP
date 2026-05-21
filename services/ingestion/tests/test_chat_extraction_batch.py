"""Regression tests for chat connector LLM extraction batching."""

from __future__ import annotations

import json
from collections.abc import Sequence

from pytest import MonkeyPatch
from src.llm import ChatMessage
from src.llm_prompts import build_extraction_prompt


class _FakeLlmService:
    async def chat_json(self, messages: Sequence[ChatMessage]) -> str:
        _ = messages
        return json.dumps(
            {
                "persons": [{"name": "Ada", "phone": "+6512345678"}],
                "transactions": [],
                "chat_members": [
                    {"name": "Ben", "phone": "+6588880000", "role": "agent", "notes": "Sales rep"}
                ],
                "inquiries": [
                    {
                        "machine_product": "Forklift X",
                        "unit": "Unit 7",
                        "lta_tag": "LTA123",
                        "serial_number": "SN-9",
                        "notes": "Customer asked about availability",
                    }
                ],
                "customer_sentiment": "positive",
                "summary": "Customer / Participants:\nAda asked about Forklift X.",
                "strong_identifiers": [
                    {
                        "type": "phone",
                        "value": "+6512345678",
                        "confidence": 0.95,
                        "notes": "Customer stated phone",
                    }
                ],
                "weak_identifiers": [
                    {
                        "type": "machine_lta_tag",
                        "value": "LTA123",
                        "confidence": 0.7,
                        "notes": "Asked about unit",
                    }
                ],
                "confidence": 0.9,
            }
        )


class _FakeObjectSummaryLlmService:
    async def chat_json(self, messages: Sequence[ChatMessage]) -> str:
        _ = messages
        return json.dumps(
            {
                "persons": [{"name": "Ada", "phone": "+6512345678"}],
                "possible_persons": [],
                "transactions": [],
                "chat_members": [],
                "inquiries": [],
                "customer_sentiment": "neutral",
                "summary": {
                    "Customer / Participants": "Ada asked about Forklift X.",
                    "Identity Evidence": "Ada gave phone +6512345678.",
                    "Uncertainties": "",
                },
                "strong_identifiers": [],
                "weak_identifiers": [],
                "confidence": 0.9,
            }
        )


class _FakeSettings:
    llm_request_delay_seconds = 0.5


def test_chat_extraction_batch_preserves_object_summaries(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.connectors import chat_helpers

    monkeypatch.setattr(chat_helpers, "get_llm_service", lambda: _FakeObjectSummaryLlmService())
    monkeypatch.setattr(chat_helpers, "get_settings", lambda: _FakeSettings())

    results = chat_helpers.run_extraction_batch(["hello"])

    assert len(results) == 1
    assert results[0] is not None
    assert results[0]["summary"] == (
        "Customer / Participants:\nAda asked about Forklift X.\n\n"
        "Identity Evidence:\nAda gave phone +6512345678."
    )


def test_chat_extraction_batch_runs_async_gather_inside_event_loop(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.connectors import chat_helpers

    sleep_calls: list[float] = []

    async def fake_sleep(delay_seconds: float) -> None:
        sleep_calls.append(delay_seconds)

    monkeypatch.setattr(chat_helpers, "get_llm_service", lambda: _FakeLlmService())
    monkeypatch.setattr(chat_helpers, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(chat_helpers.asyncio, "sleep", fake_sleep)

    results = chat_helpers.run_extraction_batch(["hello", "world", "again"])

    assert sleep_calls == [0.5, 1.0]
    assert len(results) == 3
    assert results[0] is not None
    assert results[0]["confidence"] == 0.9
    assert results[0]["persons"][0]["name"] == "Ada"
    assert results[0]["chat_members"][0]["name"] == "Ben"
    assert results[0]["chat_members"][0]["phone"] == "+6588880000"
    assert results[0]["inquiries"][0]["machine_product"] == "Forklift X"
    assert results[0]["inquiries"][0]["unit"] == "Unit 7"
    assert results[0]["inquiries"][0]["lta_tag"] == "LTA123"
    assert results[0]["inquiries"][0]["serial_number"] == "SN-9"
    assert results[0]["customer_sentiment"] == "positive"
    assert results[0]["summary"].startswith("Customer / Participants")
    assert results[0]["strong_identifiers"][0]["type"] == "phone"
    assert results[0]["weak_identifiers"][0]["type"] == "machine_lta_tag"


def test_chat_extraction_prompt_keeps_persons_customer_only() -> None:
    prompt = build_extraction_prompt(
        "[2026-05-07 10:00:00] Tonni: Customer Ada ordered item A\n"
        "[2026-05-07 10:01:00] Ada: My phone is +6512345678"
    )

    assert "customers, clients, prospects" in prompt
    assert "Do not include sales agents" in prompt
    assert "staff" in prompt
    assert "internal users" in prompt
    assert "tenant or business representatives" in prompt
    assert "message senders acting" in prompt
    assert "on behalf of the business" in prompt
    assert "transactions" in prompt
    assert "chat_members" in prompt
    assert "Do not use chat_members as customer identifiers" in prompt
    assert "customer_sentiment" in prompt
    assert "machine_product" in prompt
    assert "lta_tag" in prompt
    assert "serial_number" in prompt
    assert "summary" in prompt
    assert "full conversation" in prompt
    assert "Customer / Participants" in prompt
    assert "Identity Evidence" in prompt
    assert "Products / Machine Units" in prompt
    assert "Orders / Commercial Terms" in prompt
    assert "Timeline / Follow-ups" in prompt
    assert "Uncertainties" in prompt
    assert "strong_identifiers" in prompt
    assert "weak_identifiers" in prompt
    assert "Weak identifiers are evidence" in prompt
    assert "possible_persons" in prompt
    assert "secondary external people" in prompt
    assert "Group identifiers under the possible person they describe" in prompt
    assert "relationship_to_primary" in prompt
    assert "pending KNOWS" in prompt


def test_possible_persons_from_extraction_keeps_grouped_identifiers_separate() -> None:
    from src.connectors.chat_helpers import (
        ExtractionResult,
        identifiers_from_possible_person,
        possible_persons_from_extraction,
    )

    extraction = ExtractionResult(
        persons=[],
        possible_persons=[
            {
                "name": "Alice",
                "phone": "+65 8123 4567",
                "identifiers": [{"type": "phone", "value": "+6581234567", "confidence": 0.95}],
                "weak_identifiers": [{"type": "name", "value": "Alice", "confidence": 0.8}],
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
                "weak_identifiers": [{"type": "name", "value": "Bob", "confidence": 0.8}],
                "role": "secondary_person",
                "relationship_to_primary": "brother",
                "relationship_label": "brother",
                "evidence": "Alice said Bob is her brother",
                "confidence": 0.9,
            },
        ],
        transactions=[],
        chat_members=[],
        inquiries=[],
        strong_identifiers=[],
        weak_identifiers=[],
        summary="Customer / Participants:\nAlice mentioned Bob.",
        customer_sentiment="neutral",
        confidence=0.9,
    )

    people = possible_persons_from_extraction(extraction)
    alice_identifiers = identifiers_from_possible_person(people[0])
    bob_identifiers = identifiers_from_possible_person(people[1])

    assert {item["value"] for item in alice_identifiers} == {"+6581234567"}
    assert {item["value"] for item in bob_identifiers} == {"bob@example.com"}
    assert people[1]["relationship_to_primary"] == "brother"


def test_identifiers_from_extraction_deduplicates_legacy_and_strong_identifiers() -> None:
    from src.connectors.chat_helpers import ExtractionResult, identifiers_from_extraction

    extraction = ExtractionResult(
        persons=[{"name": "Ada", "phone": "+65 8888 9999", "email": "ada@example.com"}],
        transactions=[],
        chat_members=[],
        inquiries=[],
        strong_identifiers=[
            {"type": "phone", "value": "+6588889999", "confidence": 0.95},
            {"type": "email", "value": "ADA@example.com", "confidence": 0.95},
            {"type": "government_id", "value": "S1234567A", "confidence": 0.95},
        ],
        weak_identifiers=[{"type": "name", "value": "Ada", "confidence": 0.8}],
        summary="Customer / Participants:\nAda.",
        customer_sentiment="neutral",
        confidence=0.9,
    )

    identifiers = identifiers_from_extraction(extraction)

    assert sum(1 for item in identifiers if item["type"] == "phone") == 1
    assert any(item["type"] == "email" for item in identifiers)
    assert not any(item["type"] == "government_id" for item in identifiers)
