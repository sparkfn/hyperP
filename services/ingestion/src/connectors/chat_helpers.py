"""Shared helpers for chat connectors."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import TypedDict

from src.config import get_settings
from src.connectors.fundbox.builders import IdentifierBag, to_iso
from src.llm import ChatMessage, get_llm_service
from src.llm_prompts import EXTRACTION_SYSTEM, build_extraction_prompt
from src.models import JsonValue, QualityFlag
from src.normalizers.email import normalize_email
from src.normalizers.phone import normalize_phone

logger = logging.getLogger(__name__)


class ExtractedPerson(TypedDict, total=False):
    name: str | None
    phone: str | None
    email: str | None
    address: str | None
    nric: str | None
    notes: str | None


class ExtractedTransaction(TypedDict, total=False):
    order_id: str | None
    product: str | None
    amount: float | None
    currency: str
    status: str | None
    notes: str | None


class ExtractionResult(TypedDict):
    persons: list[ExtractedPerson]
    transactions: list[ExtractedTransaction]
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
            if not isinstance(persons_raw, list):
                results.append(None)
                continue
            transactions_raw = parsed.get("transactions")
            confidence_raw = parsed.get("confidence")
            results.append(
                ExtractionResult(
                    persons=[p for p in persons_raw if isinstance(p, dict)],
                    transactions=(
                        [tx for tx in transactions_raw if isinstance(tx, dict)]
                        if isinstance(transactions_raw, list)
                        else []
                    ),
                    confidence=(
                        float(confidence_raw) if isinstance(confidence_raw, int | float) else 0.0
                    ),
                )
            )
        except json.JSONDecodeError:
            logger.warning("LLM returned non-JSON: %s", raw[:200])
            results.append(None)
    return results


def identifiers_from_extraction(extraction: ExtractionResult) -> list[dict[str, JsonValue]]:
    identifiers = IdentifierBag()
    for person in extraction["persons"]:
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
    return identifiers.items


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
        if isinstance(ts, datetime):
            iso_value = to_iso(ts)
            if iso_value is not None:
                return iso_value
    fallback = to_iso(datetime.utcnow())
    assert fallback is not None
    return fallback
