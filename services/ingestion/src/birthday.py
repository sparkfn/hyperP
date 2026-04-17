"""Birthday greeting workflow.

Finds active persons whose ``preferred_dob`` matches today's date and sends a
single WhatsApp message per unique phone number via the external WhatsApp API
client. Invoked by the Celery task in :mod:`src.tasks`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import TYPE_CHECKING

from neo4j import ManagedTransaction
from pydantic import BaseModel

from src.config import Settings, get_settings
from src.external.whatsapp_api import WhatsAppApiClient, WhatsAppApiError
from src.graph.client import Neo4jClient
from src.graph.queries.persons import FIND_BIRTHDAY_PERSONS

if TYPE_CHECKING:
    from neo4j import Record

logger = logging.getLogger(__name__)


class BirthdayRecipient(BaseModel):
    """A person eligible for a birthday message today."""

    person_id: str
    phone: str
    full_name: str | None = None


class BirthdaySendResult(BaseModel):
    """Outcome of a single birthday-message send."""

    chat_id: str
    person_id: str
    success: bool
    error: str | None = None


class BirthdayRunSummary(BaseModel):
    """Aggregate result of a birthday-greeting run."""

    run_date: date
    candidates_found: int
    unique_phones: int
    sent: int
    failed: int
    skipped_no_source: bool = False


def _today_mmdd(today: date) -> str:
    return today.strftime("%m-%d")


def _phone_to_chat_id(phone_e164: str) -> str:
    """Convert an E.164 phone (``+6591234567``) to a WhatsApp chat ID."""
    return f"{phone_e164.lstrip('+')}@c.us"


def _record_to_recipient(record: Record) -> BirthdayRecipient:
    return BirthdayRecipient(
        person_id=str(record["person_id"]),
        phone=str(record["phone"]),
        full_name=record["full_name"] if record["full_name"] is not None else None,
    )


def fetch_birthday_recipients(
    client: Neo4jClient, today: date
) -> list[BirthdayRecipient]:
    """Read all active persons whose preferred DOB matches ``today`` (MM-DD)."""
    mmdd = _today_mmdd(today)

    def work(tx: ManagedTransaction) -> list[BirthdayRecipient]:
        result = tx.run(FIND_BIRTHDAY_PERSONS, mmdd=mmdd)
        return [_record_to_recipient(r) for r in result]

    return client.execute_read(work)


def _dedupe_by_phone(
    recipients: list[BirthdayRecipient],
) -> list[BirthdayRecipient]:
    """Keep the first recipient seen per phone number."""
    seen: set[str] = set()
    out: list[BirthdayRecipient] = []
    for r in recipients:
        if r.phone in seen:
            continue
        seen.add(r.phone)
        out.append(r)
    return out


def _render_message(template: str, recipient: BirthdayRecipient) -> str:
    return template.format(name=recipient.full_name or "there")


async def _send_one(
    wa: WhatsAppApiClient,
    recipient: BirthdayRecipient,
    *,
    template: str,
    session_id: str,
) -> BirthdaySendResult:
    chat_id = _phone_to_chat_id(recipient.phone)
    message = _render_message(template, recipient)
    try:
        await wa.send_message(chat_id, message, session_id=session_id)
    except WhatsAppApiError as exc:
        logger.warning(
            "Birthday send failed for %s (%s): %s",
            recipient.person_id,
            chat_id,
            exc,
        )
        return BirthdaySendResult(
            chat_id=chat_id,
            person_id=recipient.person_id,
            success=False,
            error=str(exc),
        )
    return BirthdaySendResult(
        chat_id=chat_id, person_id=recipient.person_id, success=True
    )


async def _send_all(
    recipients: list[BirthdayRecipient],
    *,
    settings: Settings,
) -> list[BirthdaySendResult]:
    async with WhatsAppApiClient.from_settings(settings) as wa:
        results: list[BirthdaySendResult] = []
        for r in recipients:
            results.append(
                await _send_one(
                    wa,
                    r,
                    template=settings.birthday_message_template,
                    session_id=settings.whatsapp_source_number,
                )
            )
        return results


def run_birthday_greetings(today: date | None = None) -> BirthdayRunSummary:
    """Find today's birthday persons and send each a single WhatsApp message."""
    settings = get_settings()
    run_date = today or date.today()

    if not settings.whatsapp_source_number:
        logger.warning("Birthday task skipped: WHATSAPP_SOURCE_NUMBER is not configured")
        return BirthdayRunSummary(
            run_date=run_date, candidates_found=0, unique_phones=0,
            sent=0, failed=0, skipped_no_source=True,
        )

    graph = Neo4jClient(settings)
    try:
        candidates = fetch_birthday_recipients(graph, run_date)
    finally:
        graph.close()

    unique = _dedupe_by_phone(candidates)
    logger.info(
        "Birthday run %s: %d candidates, %d unique phones",
        run_date.isoformat(), len(candidates), len(unique),
    )

    if not unique:
        return BirthdayRunSummary(
            run_date=run_date, candidates_found=len(candidates),
            unique_phones=0, sent=0, failed=0,
        )

    results = asyncio.run(_send_all(unique, settings=settings))
    sent = sum(1 for r in results if r.success)
    return BirthdayRunSummary(
        run_date=run_date, candidates_found=len(candidates),
        unique_phones=len(unique), sent=sent, failed=len(results) - sent,
    )
