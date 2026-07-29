"""Shared helpers for chat connectors."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypedDict

from src.connectors.fundbox.builders import IdentifierBag, to_iso
from src.ingestion_config import get_ingestion_config
from src.llm import ChatMessage, get_chat_extraction_service, get_chat_summary_service
from src.llm_prompts import (
    EXTRACTION_SYSTEM,
    SUMMARY_SYSTEM,
    build_batch_extraction_prompt,
    build_batch_summary_prompt,
)
from src.models import JsonValue, QualityFlag
from src.normalizers.email import normalize_email
from src.normalizers.phone import normalize_phone

logger = logging.getLogger(__name__)


class ExtractedAddress(TypedDict, total=False):
    raw: str | None
    unit_number: str | None
    street_number: str | None
    street_name: str | None
    building_name: str | None
    city: str | None
    state_province: str | None
    postal_code: str | None
    country_code: str | None


class ExtractedPerson(TypedDict, total=False):
    name: str | None
    phone: str | None
    email: str | None
    address: str | None
    addresses: list[ExtractedAddress]
    nric: str | None
    notes: str | None


class ExtractedStrongIdentifier(TypedDict, total=False):
    type: str
    value: str | None
    label: str | None
    person_name: str | None
    confidence: float | None
    notes: str | None


class ExtractedWeakIdentifier(TypedDict, total=False):
    type: str
    value: str | None
    label: str | None
    person_name: str | None
    confidence: float | None
    notes: str | None


class ExtractedPossiblePerson(TypedDict, total=False):
    name: str | None
    phone: str | None
    email: str | None
    address: str | None
    addresses: list[ExtractedAddress]
    nric: str | None
    notes: str | None
    role: str | None
    relationship_to_primary: str | None
    relationship_label: str | None
    identifiers: list[ExtractedStrongIdentifier]
    weak_identifiers: list[ExtractedWeakIdentifier]
    evidence: str | None
    confidence: float | None


class ExtractedTransaction(TypedDict, total=False):
    order_id: str | None
    product: str | None
    amount: float | None
    currency: str
    status: str | None
    notes: str | None


class ExtractedChatMember(TypedDict, total=False):
    name: str | None
    phone: str | None
    role: str | None
    notes: str | None


class ExtractedInquiry(TypedDict, total=False):
    vehicle_product: str | None
    unit: str | None
    lta_tag: str | None
    serial_number: str | None
    notes: str | None


class ExtractionResult(TypedDict):
    persons: list[ExtractedPerson]
    possible_persons: list[ExtractedPossiblePerson]
    transactions: list[ExtractedTransaction]
    chat_members: list[ExtractedChatMember]
    inquiries: list[ExtractedInquiry]
    strong_identifiers: list[ExtractedStrongIdentifier]
    weak_identifiers: list[ExtractedWeakIdentifier]
    summary: str | None
    customer_sentiment: str | None
    confidence: float


@dataclass(frozen=True)
class ExtractionFailure:
    """Bounded, safe diagnostic for one conversation extraction failure."""

    code: ExtractionFailureCode
    attempts: int


ExtractionFailureCode = Literal[
    "malformed_response",
    "missing_conversation",
    "provider_error",
]


@dataclass(frozen=True)
class ExtractionBatchOutcome:
    """Aligned structured-extraction results and failures for a batch."""

    results: list[ExtractionResult | None]
    failures: list[ExtractionFailure | None]


async def _extract_structured(texts: list[str], max_tokens: int) -> str:
    """ProClaude JSON-mode call for structured identity/transaction extraction."""
    svc = get_chat_extraction_service()
    return await svc.chat_json(
        [
            ChatMessage(role="system", content=EXTRACTION_SYSTEM),
            ChatMessage(role="user", content=build_batch_extraction_prompt(texts)),
        ],
        max_tokens=max_tokens,
    )


async def _summarize_batch(texts: list[str], max_tokens: int) -> str:
    """ProClaude call for narrative per-conversation summaries."""
    svc = get_chat_summary_service()
    return await svc.chat_text(
        [
            ChatMessage(role="system", content=SUMMARY_SYSTEM),
            ChatMessage(role="user", content=build_batch_summary_prompt(texts)),
        ],
        max_tokens=max_tokens,
    )


#: Matches the "=== Summary N ===" markers ProClaude emits between summaries.
_SUMMARY_MARKER = re.compile(r"^[ \t]*===[ \t]*Summary[ \t]+(\d+)[ \t]*===[ \t]*$", re.MULTILINE)


def chat_batch_size() -> int:
    """Max conversations per combined LLM call (safety cap), from the config."""
    return get_ingestion_config().llm.chat_batch_size


def chat_batch_max_chars() -> int:
    """Combined transcript-character budget per combined LLM call, from the config."""
    return get_ingestion_config().llm.chat_batch_max_chars


def iter_char_batches(
    texts: Sequence[str], max_chars: int, max_count: int
) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` slices of ``texts`` bounded by size.

    A slice grows until adding the next text would push the combined character
    length past ``max_chars`` or the count past ``max_count``. A single text
    longer than ``max_chars`` still forms its own slice (never dropped).
    """
    total = len(texts)
    start = 0
    while start < total:
        end = start
        chars = 0
        while end < total:
            text_len = len(texts[end])
            if end > start and (chars + text_len > max_chars or end - start >= max_count):
                break
            chars += text_len
            end += 1
        yield start, end
        start = end


def run_extraction_batch(texts: list[str]) -> list[ExtractionResult | None]:
    """Extract every conversation through ProClaude.

    Returns one ``ExtractionResult | None`` per input, aligned to input order.
    Structured extraction uses ProClaude's JSON mode. Invalid or omitted
    responses receive bounded single-conversation retries before a ``None`` is
    returned at that index.
    Summaries are a best-effort second pass — a failed summary call leaves
    ``summary`` ``None`` but keeps the structured data.
    """
    return run_extraction_batch_detailed(texts).results


def run_extraction_batch_detailed(texts: list[str]) -> ExtractionBatchOutcome:
    """Extract chats while isolating malformed responses to individual chats.

    Transport and retryable HTTP failures are retried by ``LLMService``. This
    layer additionally retries a syntactically invalid or incomplete successful
    response, once per unresolved conversation, so a bad multi-chat response
    cannot discard otherwise processable chats or abort the caller's run.
    """
    if not texts:
        return ExtractionBatchOutcome([], [])
    max_tokens = get_ingestion_config().llm.chat_max_tokens
    retry_attempts = max(get_ingestion_config().llm.chat_extraction_retry_attempts, 0)
    results: list[ExtractionResult | None] = [None] * len(texts)
    failures: list[ExtractionFailure | None] = [None] * len(texts)
    initial_failure_code: ExtractionFailureCode = "missing_conversation"
    try:
        raw = asyncio.run(_extract_structured(texts, max_tokens))
    except Exception as exc:  # noqa: BLE001 - one bad batch must not abort the run
        logger.warning("LLM extraction call failed (%d conversations): %r", len(texts), exc)
        raw = ""
        initial_failure_code = "provider_error"
    else:
        if _indexed_conversations(raw, len(texts)) is None:
            initial_failure_code = "malformed_response"
    initial = _split_batch_extraction(raw, len(texts))
    for index, result in enumerate(initial):
        results[index] = result

    for index, result in enumerate(results):
        if result is not None:
            continue
        retry_result, failure = _retry_single_extraction(
            texts[index], max_tokens, retry_attempts, initial_failure_code
        )
        results[index] = retry_result
        failures[index] = failure
    _attach_summaries(texts, results, max_tokens)
    return ExtractionBatchOutcome(results, failures)


def _retry_single_extraction(
    text: str,
    max_tokens: int,
    retry_attempts: int,
    initial_failure_code: ExtractionFailureCode,
) -> tuple[ExtractionResult | None, ExtractionFailure | None]:
    """Retry a previously unresolved chat as a one-conversation request."""
    last_code: ExtractionFailureCode = initial_failure_code
    for attempt in range(1, retry_attempts + 1):
        try:
            raw = asyncio.run(_extract_structured([text], max_tokens))
        except Exception as exc:  # noqa: BLE001 - keep one failed chat isolated
            logger.warning("LLM extraction retry %d/%d failed: %r", attempt, retry_attempts, exc)
            last_code = "provider_error"
            continue
        parsed = _split_batch_extraction(raw, 1)[0]
        if parsed is not None:
            return parsed, None
        last_code = "malformed_response"
    total_attempts = retry_attempts + 1
    logger.warning("LLM extraction exhausted %d attempts; code=%s", total_attempts, last_code)
    return None, ExtractionFailure(last_code, total_attempts)


def _attach_summaries(
    texts: list[str], results: list[ExtractionResult | None], max_tokens: int
) -> None:
    """Best-effort: fill each result's ``summary`` from a ProClaude summary call."""
    if not any(result is not None for result in results):
        return
    try:
        raw = asyncio.run(_summarize_batch(texts, max_tokens))
    except Exception as exc:  # noqa: BLE001 - summaries are best-effort, never fatal
        logger.warning("LLM summary call failed (%d conversations): %r", len(texts), exc)
        return
    summaries = _split_batch_summaries(raw, len(texts))
    for index, result in enumerate(results):
        summary = summaries[index]
        if result is not None and summary is not None:
            result["summary"] = summary


def _indexed_conversations(raw: str, count: int) -> list[tuple[int, dict[str, JsonValue]]] | None:
    """Parse a ``{"conversations": [...]}`` response into ``(index, entry)`` pairs.

    Tolerates a bare top-level array (no wrapper). Skips entries whose
    ``conversation_index`` is missing, non-integer, or out of range. Returns
    ``None`` only on a JSON decode failure so the caller can log the specifics.
    """
    if not raw:
        return []
    try:
        parsed: JsonValue = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        conversations: JsonValue = parsed.get("conversations")
    elif isinstance(parsed, list):
        conversations = parsed
    else:
        return []
    if not isinstance(conversations, list):
        return []
    pairs: list[tuple[int, dict[str, JsonValue]]] = []
    for entry in conversations:
        if not isinstance(entry, dict):
            continue
        index_raw = entry.get("conversation_index")
        if not isinstance(index_raw, int) or isinstance(index_raw, bool):
            continue
        if 0 <= index_raw < count:
            pairs.append((index_raw, entry))
    return pairs


def _split_batch_extraction(raw: str, count: int) -> list[ExtractionResult | None]:
    """Parse a structured-extraction response, aligned to input order by index."""
    results: list[ExtractionResult | None] = [None] * count
    pairs = _indexed_conversations(raw, count)
    if pairs is None:
        # A leading "{" with a decode error usually means a truncated/invalid
        # response — log the length to make that diagnosable.
        logger.warning("LLM returned non-JSON extraction (len=%d): %s", len(raw), raw[:200])
        return results
    for index, entry in pairs:
        results[index] = _parse_extraction_object(entry)
    return results


def _split_batch_summaries(raw: str, count: int) -> list[str | None]:
    """Parse plain-text ``=== Summary N ===`` blocks into per-index summaries.

    A delimited text protocol avoids unnecessary JSON escaping for prose.
    Splitting on the marker keeps multi-line markdown summaries intact.
    """
    summaries: list[str | None] = [None] * count
    if not raw:
        return summaries
    markers = list(_SUMMARY_MARKER.finditer(raw))
    if not markers:
        # Single-conversation batches often skip the marker — the whole response
        # is that one summary. For multi-conversation batches we can't align, so
        # drop them (best-effort).
        text = raw.strip()
        if count == 1 and text:
            summaries[0] = text
        else:
            logger.warning(
                "LLM summary had no '=== Summary N ===' markers (len=%d): %s", len(raw), raw[:200]
            )
        return summaries
    for position, marker in enumerate(markers):
        index = int(marker.group(1))
        body_start = marker.end()
        body_end = markers[position + 1].start() if position + 1 < len(markers) else len(raw)
        text = raw[body_start:body_end].strip()
        if 0 <= index < count and text:
            summaries[index] = text
    return summaries


def _parse_extraction_object(obj: JsonValue) -> ExtractionResult | None:
    """Parse one conversation's extraction object into an ``ExtractionResult``."""
    if not isinstance(obj, dict):
        return None
    persons_raw = obj.get("persons")
    possible_persons_raw = obj.get("possible_persons")
    if not isinstance(persons_raw, list) and not isinstance(possible_persons_raw, list):
        return None
    persons: list[ExtractedPerson] = []
    if isinstance(persons_raw, list):
        for person_raw in persons_raw:
            person = _parse_person(person_raw)
            if person is not None:
                persons.append(person)
    possible_persons: list[ExtractedPossiblePerson] = []
    if isinstance(possible_persons_raw, list):
        for possible_person_raw in possible_persons_raw:
            possible_person = _parse_possible_person(possible_person_raw)
            if possible_person is not None:
                possible_persons.append(possible_person)
    if not possible_persons:
        possible_persons = [_possible_person_from_legacy(person) for person in persons]
    transactions = _parse_transactions(obj.get("transactions"))
    chat_members: list[ExtractedChatMember] = []
    chat_members_raw = obj.get("chat_members")
    if isinstance(chat_members_raw, list):
        for member_raw in chat_members_raw:
            member = _parse_chat_member(member_raw)
            if member is not None:
                chat_members.append(member)
    inquiries: list[ExtractedInquiry] = []
    inquiries_raw = obj.get("inquiries")
    if isinstance(inquiries_raw, list):
        for inquiry_raw in inquiries_raw:
            inquiry = _parse_inquiry(inquiry_raw)
            if inquiry is not None:
                inquiries.append(inquiry)
    strong_identifiers: list[ExtractedStrongIdentifier] = []
    strong_raw = obj.get("strong_identifiers")
    if isinstance(strong_raw, list):
        for identifier_raw in strong_raw:
            identifier = _parse_identifier(identifier_raw)
            if identifier is not None:
                strong_identifiers.append(identifier)
    weak_identifiers: list[ExtractedWeakIdentifier] = []
    weak_raw = obj.get("weak_identifiers")
    if isinstance(weak_raw, list):
        for identifier_raw in weak_raw:
            identifier = _parse_identifier(identifier_raw)
            if identifier is not None:
                weak_identifiers.append(identifier)
    confidence_raw = obj.get("confidence")
    sentiment_raw = obj.get("customer_sentiment")
    return ExtractionResult(
        persons=persons,
        possible_persons=possible_persons,
        transactions=transactions,
        chat_members=chat_members,
        inquiries=inquiries,
        strong_identifiers=strong_identifiers,
        weak_identifiers=weak_identifiers,
        # Summary comes from the separate ProClaude pass (_attach_summaries),
        # not the structured extraction response.
        summary=None,
        customer_sentiment=sentiment_raw if isinstance(sentiment_raw, str) else None,
        confidence=(float(confidence_raw) if isinstance(confidence_raw, int | float) else 0.0),
    )


def _parse_transactions(raw: JsonValue) -> list[ExtractedTransaction]:
    transactions: list[ExtractedTransaction] = []
    if not isinstance(raw, list):
        return transactions
    for transaction_raw in raw:
        if not isinstance(transaction_raw, dict):
            continue
        transaction: ExtractedTransaction = {}
        order_id = transaction_raw.get("order_id")
        if isinstance(order_id, str) or order_id is None:
            transaction["order_id"] = order_id
        product = transaction_raw.get("product")
        if isinstance(product, str) or product is None:
            transaction["product"] = product
        currency = transaction_raw.get("currency")
        if isinstance(currency, str):
            transaction["currency"] = currency
        status = transaction_raw.get("status")
        if isinstance(status, str) or status is None:
            transaction["status"] = status
        notes = transaction_raw.get("notes")
        if isinstance(notes, str) or notes is None:
            transaction["notes"] = notes
        amount = transaction_raw.get("amount")
        if isinstance(amount, int | float) and not isinstance(amount, bool):
            transaction["amount"] = float(amount)
        elif amount is None:
            transaction["amount"] = None
        transactions.append(transaction)
    return transactions


def extraction_method_label() -> str:
    try:
        model = get_chat_extraction_service().default_model
    except Exception:
        model = "proclaude"
    return f"llm:{model}"


def identifiers_from_extraction(extraction: ExtractionResult) -> list[dict[str, JsonValue]]:
    identifiers = IdentifierBag()
    for person in possible_persons_from_extraction(extraction):
        for identifier in identifiers_from_possible_person(person):
            identifiers.add(
                str(identifier["type"]),
                str(identifier["value"]),
                verified=bool(identifier.get("is_verified", False)),
            )
    return identifiers.items


def possible_persons_from_extraction(extraction: ExtractionResult) -> list[ExtractedPossiblePerson]:
    possible_people = extraction.get("possible_persons", [])
    if possible_people:
        return list(possible_people)
    return [_possible_person_from_legacy(person) for person in extraction.get("persons", [])]


def identifiers_from_possible_person(person: ExtractedPossiblePerson) -> list[dict[str, JsonValue]]:
    identifiers = IdentifierBag()
    phone = person.get("phone")
    if phone:
        normalized_phone, phone_quality = normalize_phone(phone)
        if normalized_phone is not None and phone_quality == QualityFlag.VALID:
            identifiers.add("phone", normalized_phone, verified=False)

    email = person.get("email")
    if email:
        normalized_email, email_quality = normalize_email(email)
        if normalized_email is not None and email_quality == QualityFlag.VALID:
            identifiers.add("email", normalized_email, verified=False)

    for identifier in person.get("identifiers", []):
        identifier_type = identifier.get("type")
        value = identifier.get("value")
        if not value:
            continue
        if identifier_type == "phone":
            normalized_phone, phone_quality = normalize_phone(value)
            if normalized_phone is not None and phone_quality == QualityFlag.VALID:
                identifiers.add("phone", normalized_phone, verified=False)
        elif identifier_type == "email":
            normalized_email, email_quality = normalize_email(value)
            if normalized_email is not None and email_quality == QualityFlag.VALID:
                identifiers.add("email", normalized_email, verified=False)
    return identifiers.items


def weak_identifiers_for_possible_person(
    person: ExtractedPossiblePerson,
) -> list[ExtractedWeakIdentifier]:
    weak_identifiers = list(person.get("weak_identifiers", []))
    name = person.get("name")
    if name:
        weak_identifiers.append(
            ExtractedWeakIdentifier(type="name", value=name, confidence=person.get("confidence"))
        )
    relationship = person.get("relationship_to_primary") or person.get("relationship_label")
    if relationship:
        weak_identifiers.append(
            ExtractedWeakIdentifier(
                type="relationship",
                value=relationship,
                confidence=person.get("confidence"),
                notes=person.get("evidence"),
            )
        )
    return weak_identifiers


def possible_person_payload(person: ExtractedPossiblePerson) -> dict[str, JsonValue]:
    return {
        "name": person.get("name"),
        "phone": person.get("phone"),
        "email": person.get("email"),
        "address": person.get("address"),
        "addresses": [_address_payload(item) for item in person_addresses(person)],
        "nric": person.get("nric"),
        "notes": person.get("notes"),
        "role": person.get("role"),
        "relationship_to_primary": person.get("relationship_to_primary"),
        "relationship_label": person.get("relationship_label"),
        "identifiers": [_identifier_payload(item) for item in person.get("identifiers", [])],
        "weak_identifiers": [
            _identifier_payload(item) for item in person.get("weak_identifiers", [])
        ],
        "evidence": person.get("evidence"),
        "confidence": person.get("confidence"),
    }


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) or value is None:
        return value
    return None


def _optional_float(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _parse_person(raw: object) -> ExtractedPerson | None:
    if not isinstance(raw, dict):
        return None
    person: ExtractedPerson = {}
    name = raw.get("name")
    if isinstance(name, str) or name is None:
        person["name"] = name
    phone = raw.get("phone")
    if isinstance(phone, str) or phone is None:
        person["phone"] = phone
    email = raw.get("email")
    if isinstance(email, str) or email is None:
        person["email"] = email
    address = raw.get("address")
    if isinstance(address, str) or address is None:
        person["address"] = address
    addresses = _parse_addresses(raw.get("addresses"))
    if addresses:
        person["addresses"] = addresses
    nric = raw.get("nric")
    if isinstance(nric, str) or nric is None:
        person["nric"] = nric
    notes = raw.get("notes")
    if isinstance(notes, str) or notes is None:
        person["notes"] = notes
    return person


def _possible_person_from_legacy(person: ExtractedPerson) -> ExtractedPossiblePerson:
    return ExtractedPossiblePerson(
        name=person.get("name"),
        phone=person.get("phone"),
        email=person.get("email"),
        address=person.get("address"),
        addresses=person_addresses(person),
        nric=person.get("nric"),
        notes=person.get("notes"),
        identifiers=[],
        weak_identifiers=[],
    )


def _parse_possible_person(raw: object) -> ExtractedPossiblePerson | None:
    person = _parse_person(raw)
    if person is None or not isinstance(raw, dict):
        return None
    possible_person = _possible_person_from_legacy(person)
    possible_person["role"] = _optional_str(raw.get("role"))
    possible_person["relationship_to_primary"] = _optional_str(raw.get("relationship_to_primary"))
    possible_person["relationship_label"] = _optional_str(raw.get("relationship_label"))
    possible_person["evidence"] = _optional_str(raw.get("evidence"))
    possible_person["confidence"] = _optional_float(raw.get("confidence"))

    identifiers_raw = raw.get("identifiers")
    identifiers: list[ExtractedStrongIdentifier] = []
    if isinstance(identifiers_raw, list):
        for identifier_raw in identifiers_raw:
            identifier = _parse_identifier(identifier_raw)
            if identifier is not None:
                identifiers.append(identifier)
    possible_person["identifiers"] = identifiers

    weak_identifiers_raw = raw.get("weak_identifiers")
    weak_identifiers: list[ExtractedWeakIdentifier] = []
    if isinstance(weak_identifiers_raw, list):
        for identifier_raw in weak_identifiers_raw:
            identifier = _parse_identifier(identifier_raw)
            if identifier is not None:
                weak_identifiers.append(identifier)
    possible_person["weak_identifiers"] = weak_identifiers
    return possible_person


def _parse_address(raw: object) -> ExtractedAddress | None:
    if isinstance(raw, str):
        text = raw.strip()
        return ExtractedAddress(raw=text) if text else None
    if not isinstance(raw, dict):
        return None
    address: ExtractedAddress = {}
    for key in (
        "raw",
        "unit_number",
        "street_number",
        "street_name",
        "building_name",
        "city",
        "state_province",
        "postal_code",
        "country_code",
    ):
        value = raw.get(key)
        if isinstance(value, str) or value is None:
            address[key] = value
    return address if any(value for value in address.values()) else None


def _parse_addresses(raw: object) -> list[ExtractedAddress]:
    if not isinstance(raw, list):
        return []
    addresses: list[ExtractedAddress] = []
    for item in raw:
        address = _parse_address(item)
        if address is not None:
            addresses.append(address)
    return addresses


def person_addresses(person: ExtractedPerson | ExtractedPossiblePerson) -> list[ExtractedAddress]:
    addresses = list(person.get("addresses", []))
    legacy_address = person.get("address")
    if legacy_address:
        legacy = ExtractedAddress(raw=legacy_address)
        if legacy not in addresses:
            addresses.append(legacy)
    return addresses


def person_address_payloads(
    person: ExtractedPerson | ExtractedPossiblePerson,
) -> list[dict[str, JsonValue]]:
    return [_address_payload(address) for address in person_addresses(person)]


def _address_payload(address: ExtractedAddress) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {}
    for key, value in address.items():
        if isinstance(value, str):
            payload[key] = value
        elif value is None:
            payload[key] = None
    return payload


def _parse_identifier(raw: object) -> ExtractedStrongIdentifier | ExtractedWeakIdentifier | None:
    if not isinstance(raw, dict):
        return None
    type_value = _optional_str(raw.get("type"))
    value = _optional_str(raw.get("value"))
    if type_value is None or value is None:
        return None
    return {
        "type": type_value,
        "value": value,
        "label": _optional_str(raw.get("label")),
        "person_name": _optional_str(raw.get("person_name")),
        "confidence": _optional_float(raw.get("confidence")),
        "notes": _optional_str(raw.get("notes")),
    }


def _parse_chat_member(member_raw: object) -> ExtractedChatMember | None:
    if not isinstance(member_raw, dict):
        return None
    return ExtractedChatMember(
        name=_optional_str(member_raw.get("name")),
        phone=_optional_str(member_raw.get("phone")),
        role=_optional_str(member_raw.get("role")),
        notes=_optional_str(member_raw.get("notes")),
    )


def _parse_inquiry(inquiry_raw: object) -> ExtractedInquiry | None:
    if not isinstance(inquiry_raw, dict):
        return None
    return ExtractedInquiry(
        vehicle_product=_optional_str(inquiry_raw.get("vehicle_product")),
        unit=_optional_str(inquiry_raw.get("unit")),
        lta_tag=_optional_str(inquiry_raw.get("lta_tag")),
        serial_number=_optional_str(inquiry_raw.get("serial_number")),
        notes=_optional_str(inquiry_raw.get("notes")),
    )


def chat_members_payload(extraction: ExtractionResult) -> list[JsonValue]:
    payload: list[JsonValue] = []
    for member in extraction.get("chat_members", []):
        payload.append(
            {
                "name": member.get("name"),
                "phone": member.get("phone"),
                "role": member.get("role"),
                "notes": member.get("notes"),
            }
        )
    return payload


def inquiries_payload(extraction: ExtractionResult) -> list[JsonValue]:
    payload: list[JsonValue] = []
    for inquiry in extraction.get("inquiries", []):
        payload.append(
            {
                "vehicle_product": inquiry.get("vehicle_product"),
                "unit": inquiry.get("unit"),
                "lta_tag": inquiry.get("lta_tag"),
                "serial_number": inquiry.get("serial_number"),
                "notes": inquiry.get("notes"),
            }
        )
    return payload


def _identifier_payload(
    identifier: ExtractedStrongIdentifier | ExtractedWeakIdentifier,
) -> dict[str, JsonValue]:
    return {
        "type": identifier.get("type"),
        "value": identifier.get("value"),
        "label": identifier.get("label"),
        "person_name": identifier.get("person_name"),
        "confidence": identifier.get("confidence"),
        "notes": identifier.get("notes"),
    }


def strong_identifiers_payload(extraction: ExtractionResult) -> list[JsonValue]:
    return [_identifier_payload(item) for item in extraction.get("strong_identifiers", [])]


def weak_identifiers_payload(extraction: ExtractionResult) -> list[JsonValue]:
    return [_identifier_payload(item) for item in extraction.get("weak_identifiers", [])]


def transactions_payload(extraction: ExtractionResult) -> list[JsonValue]:
    payload: list[JsonValue] = []
    for tx in extraction.get("transactions", []):
        payload.append(
            {
                "order_id": tx.get("order_id"),
                "product": tx.get("product"),
                "amount": tx.get("amount"),
                "currency": tx.get("currency", "SGD"),
                "status": tx.get("status"),
                "notes": tx.get("notes"),
            }
        )
    return payload


def latest_timestamp(*timestamps: object) -> str:
    for ts in timestamps:
        if isinstance(ts, datetime | str):
            iso_value = to_iso(ts)
            if iso_value is not None:
                return iso_value
    fallback = to_iso(datetime.now(UTC))
    assert fallback is not None
    return fallback
