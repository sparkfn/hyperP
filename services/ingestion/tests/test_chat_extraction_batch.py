"""Regression tests for chat connector LLM extraction batching."""

from __future__ import annotations

import json
from collections.abc import Sequence

from pytest import MonkeyPatch
from src.exclusion_config import ExclusionFile
from src.ingestion_config import IngestionConfig, LlmConfig
from src.llm import ChatMessage
from src.llm_prompts import build_batch_extraction_prompt


def _conversation_object(index: int) -> dict[str, object]:
    """Structured extraction object (no summary — that comes from the summary call)."""
    return {
        "conversation_index": index,
        "persons": [{"name": "Ada", "phone": "+6512345678"}],
        "transactions": [],
        "chat_members": [
            {"name": "Ben", "phone": "+6588880000", "role": "agent", "notes": "Sales rep"}
        ],
        "inquiries": [
            {
                "vehicle_product": "Forklift X",
                "unit": "Unit 7",
                "lta_tag": "LTA123",
                "serial_number": "SN-9",
                "notes": "Customer asked about availability",
            }
        ],
        "customer_sentiment": "positive",
        "tone": "positive",
        "purpose": "product_inquiry",
        "outcome": "pending_business",
        "difficulty": "medium",
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
                "type": "vehicle_lta_tag",
                "value": "LTA123",
                "confidence": 0.7,
                "notes": "Asked about unit",
            }
        ],
        "confidence": 0.9,
    }


class _FakeService:
    """Returns a fixed payload, accepting the chat_json keyword args."""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def chat_json(
        self, messages: Sequence[ChatMessage], *, max_tokens: int | None = None
    ) -> str:
        _ = (messages, max_tokens)
        return self._payload

    async def chat_text(
        self, messages: Sequence[ChatMessage], *, max_tokens: int | None = None
    ) -> str:
        _ = (messages, max_tokens)
        return self._payload


def _fake_ingestion_config() -> IngestionConfig:
    return IngestionConfig(exclusions=ExclusionFile(), llm=LlmConfig())


def _patch_services(monkeypatch: MonkeyPatch, extraction: str, summary: str) -> None:
    from src.connectors import chat_helpers

    monkeypatch.setattr(
        chat_helpers, "get_chat_extraction_service", lambda: _FakeService(extraction)
    )
    monkeypatch.setattr(chat_helpers, "get_chat_summary_service", lambda: _FakeService(summary))
    monkeypatch.setattr(chat_helpers, "get_ingestion_config", _fake_ingestion_config)


def test_chat_summary_attached_from_delimited_text(monkeypatch: MonkeyPatch) -> None:
    from src.connectors import chat_helpers

    extraction = json.dumps({"conversations": [_conversation_object(0)]})
    # Plain-text delimited summary (multi-line markdown), not JSON.
    summary = (
        "=== Summary 0 ===\n"
        "### Customer / Participants\nAda asked about Forklift X.\n\n"
        "### Identity Evidence\nAda gave phone +6512345678."
    )
    _patch_services(monkeypatch, extraction, summary)

    results = chat_helpers.run_extraction_batch(["hello"])

    assert len(results) == 1
    assert results[0] is not None
    assert results[0]["summary"] == (
        "### Customer / Participants\nAda asked about Forklift X.\n\n"
        "### Identity Evidence\nAda gave phone +6512345678."
    )


def test_chat_extraction_batch_splits_by_conversation_index(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.connectors import chat_helpers

    extraction = json.dumps({"conversations": [_conversation_object(0), _conversation_object(2)]})
    summary = (
        "=== Summary 0 ===\nCustomer / Participants:\nAda.\n"
        "=== Summary 2 ===\nCustomer / Participants:\nAda."
    )
    _patch_services(monkeypatch, extraction, summary)

    results = chat_helpers.run_extraction_batch(["hello", "world", "again"])

    assert len(results) == 3
    # Index 1 was omitted by the batch response and recovered by its bounded
    # single-conversation retry.
    assert results[1] is not None
    for index in (0, 2):
        result = results[index]
        assert result is not None
        assert result["confidence"] == 0.9
        assert result["persons"][0]["name"] == "Ada"
        assert result["chat_members"][0]["name"] == "Ben"
        assert result["chat_members"][0]["phone"] == "+6588880000"
        assert result["inquiries"][0]["vehicle_product"] == "Forklift X"
        assert result["inquiries"][0]["unit"] == "Unit 7"
        assert result["inquiries"][0]["lta_tag"] == "LTA123"
        assert result["inquiries"][0]["serial_number"] == "SN-9"
        assert result["customer_sentiment"] == "positive"
        assert result["tone"] == "positive"
        assert result["purpose"] == "product_inquiry"
        assert result["outcome"] == "pending_business"
        assert result["difficulty"] == "medium"
        assert result["summary"].startswith("Customer / Participants")
        assert result["strong_identifiers"][0]["type"] == "phone"
        assert result["weak_identifiers"][0]["type"] == "vehicle_lta_tag"


def test_chat_extraction_batch_invalid_extraction_yields_all_none(
    monkeypatch: MonkeyPatch,
) -> None:
    # A broken structured-extraction response drops the whole batch.
    _patch_services(monkeypatch, "this is not json", "{}")
    from src.connectors import chat_helpers

    results = chat_helpers.run_extraction_batch(["hello", "world"])

    assert results == [None, None]


def test_chat_extraction_retries_only_the_unresolved_conversation(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.connectors import chat_helpers

    calls: list[list[str]] = []

    async def extract(texts: list[str], max_tokens: int) -> str:
        _ = max_tokens
        calls.append(texts)
        if len(texts) == 2:
            return json.dumps({"conversations": [_conversation_object(0)]})
        return json.dumps({"conversations": [_conversation_object(0)]})

    monkeypatch.setattr(chat_helpers, "_extract_structured", extract)
    monkeypatch.setattr(chat_helpers, "_attach_summaries", lambda *_args: None)
    monkeypatch.setattr(chat_helpers, "get_ingestion_config", _fake_ingestion_config)

    outcome = chat_helpers.run_extraction_batch_detailed(["first", "second"])

    assert calls == [["first", "second"], ["second"]]
    assert all(result is not None for result in outcome.results)
    assert outcome.failures == [None, None]


def test_chat_extraction_reports_bounded_malformed_response_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.connectors import chat_helpers

    calls = 0

    async def extract(texts: list[str], max_tokens: int) -> str:
        nonlocal calls
        _ = (texts, max_tokens)
        calls += 1
        return "not json"

    config = IngestionConfig(
        exclusions=ExclusionFile(),
        llm=LlmConfig(chat_extraction_retry_attempts=2),
    )
    monkeypatch.setattr(chat_helpers, "_extract_structured", extract)
    monkeypatch.setattr(chat_helpers, "_attach_summaries", lambda *_args: None)
    monkeypatch.setattr(chat_helpers, "get_ingestion_config", lambda: config)

    outcome = chat_helpers.run_extraction_batch_detailed(["chat"])

    assert calls == 3  # one combined attempt plus two bounded individual retries
    assert outcome.results == [None]
    assert outcome.failures == [chat_helpers.ExtractionFailure("malformed_response", 3)]


def test_chat_extraction_reports_initial_failure_when_retries_disabled(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.connectors import chat_helpers

    async def extract(texts: list[str], max_tokens: int) -> str:
        _ = (texts, max_tokens)
        return "not json"

    config = IngestionConfig(
        exclusions=ExclusionFile(),
        llm=LlmConfig(chat_extraction_retry_attempts=0),
    )
    monkeypatch.setattr(chat_helpers, "_extract_structured", extract)
    monkeypatch.setattr(chat_helpers, "_attach_summaries", lambda *_args: None)
    monkeypatch.setattr(chat_helpers, "get_ingestion_config", lambda: config)

    outcome = chat_helpers.run_extraction_batch_detailed(["chat"])

    assert outcome.failures == [chat_helpers.ExtractionFailure("malformed_response", 1)]


def test_chat_extraction_batch_summary_failure_keeps_extraction(
    monkeypatch: MonkeyPatch,
) -> None:
    # An unusable summary call must not lose the JSON-mode extracted data — summaries
    # are best-effort. A multi-conversation batch with no markers can't align, so
    # summaries drop to None while the extracted data is kept.
    extraction = json.dumps({"conversations": [_conversation_object(0), _conversation_object(1)]})
    _patch_services(monkeypatch, extraction, "unmarked prose, cannot align")
    from src.connectors import chat_helpers

    results = chat_helpers.run_extraction_batch(["hello", "world"])

    assert len(results) == 2
    for result in results:
        assert result is not None
        assert result["persons"][0]["name"] == "Ada"
        assert result["confidence"] == 0.9
        assert result["summary"] is None


def test_extraction_method_fallback_names_proclaude(monkeypatch: MonkeyPatch) -> None:
    from src.connectors import chat_helpers

    monkeypatch.setattr(
        chat_helpers,
        "get_chat_extraction_service",
        lambda: (_ for _ in ()).throw(RuntimeError("configuration unavailable")),
    )

    assert chat_helpers.extraction_method_label() == "llm:proclaude"


def test_split_batch_summaries_handles_json_breaking_prose() -> None:
    from src.connectors.chat_helpers import _split_batch_summaries

    # Body contains newlines, quotes, and braces that would break a JSON string —
    # the delimited protocol keeps them intact.
    raw = (
        '=== Summary 0 ===\n### Customer\nAda said "hi" {note}\nLine two\n'
        "=== Summary 1 ===\nShort summary for one"
    )
    summaries = _split_batch_summaries(raw, 2)

    assert summaries[0] == '### Customer\nAda said "hi" {note}\nLine two'
    assert summaries[1] == "Short summary for one"


def test_split_batch_summaries_out_of_range_and_empty() -> None:
    from src.connectors.chat_helpers import _split_batch_summaries

    # Index 5 is out of range (count=2) → ignored; no markers → all None.
    raw = "=== Summary 5 ===\nignored\n=== Summary 0 ===\nkept"
    assert _split_batch_summaries(raw, 2) == ["kept", None]
    # No markers in a multi-conversation batch → can't align → all None.
    assert _split_batch_summaries("just prose, no markers", 2) == [None, None]
    assert _split_batch_summaries("", 2) == [None, None]


def test_split_batch_summaries_single_conversation_no_marker_fallback() -> None:
    from src.connectors.chat_helpers import _split_batch_summaries

    # A single-conversation batch with no marker uses the whole text as the summary.
    assert _split_batch_summaries("### Customer\nAda asked about a forklift.", 1) == [
        "### Customer\nAda asked about a forklift."
    ]
    # But not when the batch has more than one conversation (can't align).
    assert _split_batch_summaries("unmarked prose", 3) == [None, None, None]


def test_chat_extraction_batch_empty_input_returns_empty(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.connectors import chat_helpers

    monkeypatch.setattr(chat_helpers, "get_ingestion_config", _fake_ingestion_config)

    assert chat_helpers.run_extraction_batch([]) == []


def test_chat_extraction_prompt_keeps_persons_customer_only() -> None:
    prompt = build_batch_extraction_prompt(
        [
            "[2026-05-07 10:00:00] Tonni: Customer Ada ordered item A\n"
            "[2026-05-07 10:01:00] Ada: My phone is +6512345678"
        ]
    )

    assert "customers, clients, prospects" in prompt
    assert "Do not include sales agents" in prompt
    assert "staff" in prompt
    assert '"tone"' in prompt
    assert '"purpose"' in prompt
    assert '"outcome"' in prompt
    assert '"difficulty"' in prompt
    assert "internal users" in prompt
    assert "tenant or business representatives" in prompt
    assert "message senders acting" in prompt
    assert "on behalf of the business" in prompt
    assert "transactions" in prompt
    assert "chat_members" in prompt
    assert "Do not use chat_members as customer identifiers" in prompt
    assert "customer_sentiment" in prompt
    assert "vehicle_product" in prompt
    assert "lta_tag" in prompt
    assert "serial_number" in prompt
    assert "full conversation" in prompt
    assert "strong_identifiers" in prompt
    assert "weak_identifiers" in prompt
    assert "Weak identifiers are evidence" in prompt
    assert "possible_persons" in prompt
    assert "secondary external people" in prompt
    assert "Group identifiers under the possible person they describe" in prompt
    assert "relationship_to_primary" in prompt
    assert "pending KNOWS" in prompt
    # Deal-header customers (CRM metadata) must be extracted even with no message.
    assert "[Deal]" in prompt
    assert "primary_customer" in prompt
    # Summary moved to the dedicated summary prompt — not asked of the extractor.
    assert "summary" not in prompt


def test_chat_classifications_are_canonicalized_and_invalid_values_are_dropped() -> None:
    from src.connectors.chat_helpers import _split_batch_extraction

    extraction = json.dumps(
        {
            "conversations": [
                {
                    **_conversation_object(0),
                    "tone": " Negative ",
                    "purpose": "support request",
                    "outcome": "PARTIALLY-RESOLVED",
                    "difficulty": "High",
                },
                {
                    **_conversation_object(1),
                    "tone": "unknown",
                    "purpose": "unknown",
                    "outcome": "unknown",
                    "difficulty": "unknown",
                },
                {
                    **_conversation_object(2),
                    "tone": "urgent",
                    "purpose": 42,
                    "outcome": "",
                    "difficulty": "very_high",
                },
            ]
        }
    )

    results = _split_batch_extraction(extraction, 3)

    assert results[0] is not None
    assert results[0]["tone"] == "negative"
    assert results[0]["purpose"] == "support_request"
    assert results[0]["outcome"] == "partially_resolved"
    assert results[0]["difficulty"] == "high"
    assert results[1] is not None
    assert results[1]["tone"] == "unknown"
    assert results[1]["purpose"] == "unknown"
    assert results[1]["outcome"] == "unknown"
    assert results[1]["difficulty"] == "unknown"
    assert results[2] is not None
    assert results[2]["tone"] is None
    assert results[2]["purpose"] is None
    assert results[2]["outcome"] is None
    assert results[2]["difficulty"] is None


def test_chat_summary_prompt_uses_plain_text_markers() -> None:
    from src.llm_prompts import build_batch_summary_prompt

    prompt = build_batch_summary_prompt(["[2026-05-07 10:00:00] Ada: hi"])

    # Delimited plain-text protocol, not JSON.
    assert '"=== Summary N ==="' in prompt
    assert "no JSON" in prompt
    assert "Customer / Participants" in prompt
    assert "Identity Evidence" in prompt
    assert "Products / Vehicles" in prompt
    assert "Orders / Commercial Terms" in prompt
    assert "Timeline / Follow-ups" in prompt
    assert "Uncertainties" in prompt
    assert "=== Conversation 0 ===" in prompt


def test_iter_char_batches_packs_by_char_length() -> None:
    from src.connectors.chat_helpers import iter_char_batches

    # Each text is 10 chars; budget 25 -> 2 per batch (2*10=20 ok, 3rd 30 > 25).
    texts = ["x" * 10 for _ in range(5)]
    assert list(iter_char_batches(texts, max_chars=25, max_count=100)) == [
        (0, 2),
        (2, 4),
        (4, 5),
    ]


def test_iter_char_batches_respects_max_count() -> None:
    from src.connectors.chat_helpers import iter_char_batches

    # Tiny texts well under the char budget, but max_count caps the batch at 3.
    texts = ["x" for _ in range(7)]
    assert list(iter_char_batches(texts, max_chars=10_000, max_count=3)) == [
        (0, 3),
        (3, 6),
        (6, 7),
    ]


def test_iter_char_batches_oversized_text_is_its_own_batch() -> None:
    from src.connectors.chat_helpers import iter_char_batches

    # A single text larger than the budget still forms its own batch (never dropped).
    texts = ["small", "x" * 100, "small"]
    assert list(iter_char_batches(texts, max_chars=20, max_count=100)) == [
        (0, 1),
        (1, 2),
        (2, 3),
    ]


def test_iter_char_batches_empty() -> None:
    from src.connectors.chat_helpers import iter_char_batches

    assert list(iter_char_batches([], max_chars=100, max_count=10)) == []


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


def test_possible_persons_preserve_address_arrays() -> None:
    from src.connectors.chat_helpers import (
        ExtractionResult,
        person_addresses,
        possible_persons_from_extraction,
    )

    extraction = ExtractionResult(
        persons=[],
        possible_persons=[
            {
                "name": "Alice",
                "address": "10 Orchard Road Singapore 238863",
                "addresses": [
                    {
                        "raw": "#05-123 10 Orchard Road Singapore 238863",
                        "unit_number": "#05-123",
                        "street_number": "10",
                        "street_name": "Orchard Road",
                        "building_name": "Lucky Plaza",
                        "city": "Singapore",
                        "postal_code": "238863",
                        "country_code": "SG",
                    },
                    {
                        "raw": "20 Second Street Singapore 654321",
                        "postal_code": "654321",
                        "country_code": "SG",
                    },
                ],
                "confidence": 0.95,
            }
        ],
        transactions=[],
        chat_members=[],
        inquiries=[],
        strong_identifiers=[],
        weak_identifiers=[],
        summary="Customer / Participants:\nAlice.",
        customer_sentiment="neutral",
        confidence=0.9,
    )

    people = possible_persons_from_extraction(extraction)
    addresses = person_addresses(people[0])

    assert len(addresses) == 3
    assert addresses[0]["unit_number"] == "#05-123"
    assert addresses[1]["postal_code"] == "654321"
    assert addresses[2]["raw"] == "10 Orchard Road Singapore 238863"


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
