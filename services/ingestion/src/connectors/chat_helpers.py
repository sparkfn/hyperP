"""Shared helpers for chat connectors."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TypedDict

from src.config import get_settings
from src.connectors.fundbox.builders import IdentifierBag, to_iso
from src.llm import ChatMessage, get_llm_service
from src.llm_prompts import EXTRACTION_SYSTEM, build_extraction_prompt
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
    machine_product: str | None
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


async def _extract_one(text: str, delay_seconds: float, index: int) -> str:
    if delay_seconds > 0 and index > 0:
        await asyncio.sleep(delay_seconds * index)
    svc = get_llm_service()
    return await svc.chat_json(
        [
            ChatMessage(role="system", content=EXTRACTION_SYSTEM),
            ChatMessage(role="user", content=build_extraction_prompt(text)),
        ],
    )


async def _gather_extractions(texts: list[str]) -> list[str | BaseException]:
    delay_seconds = get_settings().llm_request_delay_seconds
    tasks = [_extract_one(text, delay_seconds, index) for index, text in enumerate(texts)]
    return list(await asyncio.gather(*tasks, return_exceptions=True))


def _summary_value_to_text(value: JsonValue) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text if text else None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            item_text = _summary_value_to_text(item)
            if item_text is not None:
                items.append(item_text)
        return "\n".join(items) if items else None
    return None


def _summary_to_text(value: JsonValue) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text if text else None
    if isinstance(value, dict):
        sections: list[str] = []
        for key, section_value in value.items():
            heading = key.strip()
            section_text = _summary_value_to_text(section_value)
            if heading and section_text is not None:
                sections.append(f"{heading}:\n{section_text}")
        return "\n\n".join(sections) if sections else None
    return None


def run_extraction_batch(texts: list[str]) -> list[ExtractionResult | None]:
    raw_results = asyncio.run(_gather_extractions(texts))

    results: list[ExtractionResult | None] = []
    for raw in raw_results:
        if isinstance(raw, BaseException):
            logger.warning("LLM call failed: %s", raw)
            results.append(None)
            continue
        if not raw:
            results.append(None)
            continue
        try:
            parsed: JsonValue = json.loads(raw)
            if not isinstance(parsed, dict):
                results.append(None)
                continue
            persons_raw = parsed.get("persons")
            possible_persons_raw = parsed.get("possible_persons")
            if not isinstance(persons_raw, list) and not isinstance(possible_persons_raw, list):
                results.append(None)
                continue
            transactions_raw = parsed.get("transactions")
            chat_members_raw = parsed.get("chat_members")
            inquiries_raw = parsed.get("inquiries")
            strong_raw = parsed.get("strong_identifiers")
            weak_raw = parsed.get("weak_identifiers")
            summary_raw = parsed.get("summary")
            sentiment_raw = parsed.get("customer_sentiment")
            confidence_raw = parsed.get("confidence")
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
            transactions: list[ExtractedTransaction] = []
            if isinstance(transactions_raw, list):
                for transaction_raw in transactions_raw:
                    if isinstance(transaction_raw, dict):
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
                        if isinstance(amount, int | float):
                            transaction["amount"] = float(amount)
                        elif amount is None:
                            transaction["amount"] = None
                        transactions.append(transaction)
            chat_members: list[ExtractedChatMember] = []
            if isinstance(chat_members_raw, list):
                for member_raw in chat_members_raw:
                    member = _parse_chat_member(member_raw)
                    if member is not None:
                        chat_members.append(member)
            inquiries: list[ExtractedInquiry] = []
            if isinstance(inquiries_raw, list):
                for inquiry_raw in inquiries_raw:
                    inquiry = _parse_inquiry(inquiry_raw)
                    if inquiry is not None:
                        inquiries.append(inquiry)
            strong_identifiers: list[ExtractedStrongIdentifier] = []
            if isinstance(strong_raw, list):
                for identifier_raw in strong_raw:
                    identifier = _parse_identifier(identifier_raw)
                    if identifier is not None:
                        strong_identifiers.append(identifier)
            weak_identifiers: list[ExtractedWeakIdentifier] = []
            if isinstance(weak_raw, list):
                for identifier_raw in weak_raw:
                    identifier = _parse_identifier(identifier_raw)
                    if identifier is not None:
                        weak_identifiers.append(identifier)
            results.append(
                ExtractionResult(
                    persons=persons,
                    possible_persons=possible_persons,
                    transactions=transactions,
                    chat_members=chat_members,
                    inquiries=inquiries,
                    strong_identifiers=strong_identifiers,
                    weak_identifiers=weak_identifiers,
                    summary=_summary_to_text(summary_raw),
                    customer_sentiment=sentiment_raw if isinstance(sentiment_raw, str) else None,
                    confidence=(
                        float(confidence_raw) if isinstance(confidence_raw, int | float) else 0.0
                    ),
                )
            )
        except json.JSONDecodeError:
            logger.warning("LLM returned non-JSON: %s", raw[:200])
            results.append(None)
    return results


def extraction_method_label() -> str:
    try:
        model = get_settings().llm_default_model
    except Exception:
        model = "Qwen/Qwen2.5-72B-Instruct"
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
        machine_product=_optional_str(inquiry_raw.get("machine_product")),
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
                "machine_product": inquiry.get("machine_product"),
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
