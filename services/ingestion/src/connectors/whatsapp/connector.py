"""WhatsApp chat connector — ingests conversations from PostgreSQL.

Sessions (one row per WhatsApp account) map to tenants via org_name:
- "Fundbox"      → entity fundbox
- "EkoLife SG"   → entity eko
- "SpeedZone"    → entity speedzone

For each session belonging to a known tenant, all chats and messages are
assembled into a conversation text, run through the LLM extractor to pull
identity and transaction data, and one source record is written per chat.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.engine import Connection

from src.config import get_settings
from src.connectors.base import SourceConnector
from src.connectors.chat_helpers import (
    ExtractionResult,
    chat_batch_max_chars,
    chat_batch_size,
    chat_members_payload,
    extraction_method_label,
    identifiers_from_possible_person,
    inquiries_payload,
    iter_char_batches,
    latest_timestamp,
    person_address_payloads,
    possible_person_payload,
    possible_persons_from_extraction,
    run_extraction_batch,
    strong_identifiers_payload,
    transactions_payload,
    weak_identifiers_for_possible_person,
    weak_identifiers_payload,
)
from src.connectors.whatsapp.db import get_engine
from src.connectors.whatsapp.schema import chats, contacts, messages, orgs, sessions
from src.exclusion_config import ExclusionFile
from src.exclusions import build_exclusion_context, filter_extraction
from src.ingestion_config import get_ingestion_config
from src.models import JsonValue

logger = logging.getLogger(__name__)


@dataclass
class _Participant:
    jid: str
    phone: str | None
    name: str | None
    role: str


@dataclass
class _ChatBundle:
    chat_id: str
    chat_name: str
    session_id: str
    whatsapp_user_id: str
    tenant: str
    msg_text: str
    observed_at: str
    participants: list[_Participant]
    message_endpoints: list[JsonValue]
    session_phone: str | None


#: Map org name → entity_key (from graph bootstrap).
ORG_TO_ENTITY: dict[str, str] = {
    "Fundbox": "fundbox",
    "EkoLife SG": "eko",
    "EkoLife MY": "eko",
    "SpeedZone": "speedzone",
}


class WhatsAppChatConnector(SourceConnector):
    """Yields conversation source records from the WhatsApp PostgreSQL DB."""

    def get_source_key(self) -> str:
        return "whatsapp_chat"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        engine = get_engine()
        with engine.connect() as conn:
            yield from self._fetch(conn)

    def _fetch(self, conn: Connection) -> Iterator[dict[str, JsonValue]]:
        # Load all orgs so we can map session.org_id → org_name.
        org_rows = {row.id: row for row in conn.execute(select(orgs))}
        org_name_by_id: dict[str, str] = {str(k): v.name or "" for k, v in org_rows.items()}

        # Load all sessions belonging to known tenants.
        session_stmt = (
            select(sessions)
            .where(sessions.c.org_id.in_(org_name_by_id.keys()))
            .where(sessions.c.status == "ready")
            .where(sessions.c.whatsapp_user_id.isnot(None))
        )
        session_rows = list(conn.execute(session_stmt))

        logger.info(
            "WhatsApp: %d ready sessions across %d orgs",
            len(session_rows),
            len(org_rows),
        )

        # Phase 1: collect all chat bundles (no LLM calls yet).
        all_bundles: list[_ChatBundle] = []
        for session in session_rows:
            org_name = org_name_by_id.get(str(session.org_id), "")
            if org_name not in ORG_TO_ENTITY:
                continue

            tenant = ORG_TO_ENTITY[org_name]
            whatsapp_uid = session.whatsapp_user_id or ""

            chat_stmt = select(chats).where(chats.c.whatsapp_user_id == whatsapp_uid)
            for chat in conn.execute(chat_stmt):
                msgs = self._fetch_messages(conn, chat.id, whatsapp_uid)
                if not msgs:
                    continue
                chat_name = str(chat.name or "")
                participants = self._fetch_participants(conn, chat.id, whatsapp_uid, msgs)

                all_bundles.append(
                    _ChatBundle(
                        chat_id=str(chat.id),
                        chat_name=chat_name,
                        session_id=str(session.id),
                        whatsapp_user_id=whatsapp_uid,
                        tenant=tenant,
                        msg_text=_format_messages(msgs, participants, chat_name),
                        observed_at=_latest_message_timestamp(msgs),
                        participants=participants,
                        message_endpoints=_message_endpoints(msgs),
                        session_phone=_first_str(
                            getattr(session, "expected_phone_number", None),
                            _phone_from_jid(whatsapp_uid),
                        ),
                    )
                )

        logger.info("Collected %d WhatsApp chats — starting LLM batch phase", len(all_bundles))

        try:
            settings = get_settings()
            company_mobile_numbers = list(settings.company_mobile_numbers)
            company_email_addresses = list(settings.company_email_addresses)
            internal_person_names = list(settings.internal_person_names)
            file_exclusions = get_ingestion_config().exclusions
        except Exception:
            company_mobile_numbers = []
            company_email_addresses = []
            internal_person_names = []
            file_exclusions = ExclusionFile()

        # Phase 2: run LLM in batches (one combined call per char-bounded batch).
        texts = [b.msg_text for b in all_bundles]
        max_chars = chat_batch_max_chars()
        max_count = chat_batch_size()
        extraction_cache: dict[str, ExtractionResult] = {}
        for start, end in iter_char_batches(texts, max_chars, max_count):
            batch = all_bundles[start:end]
            batch_results = run_extraction_batch(texts[start:end])
            logger.info("LLM batch %d-%d/%d done", start, end, len(all_bundles))
            for bundle, result in zip(batch, batch_results, strict=True):
                if result is not None:
                    extraction_cache[bundle.chat_id] = result

        # Phase 3: yield envelopes in the same order as the DB cursor.
        for bundle in all_bundles:
            extraction = extraction_cache.get(bundle.chat_id)
            if extraction is None:
                logger.warning("LLM extraction failed for chat %s", bundle.chat_id)
                continue

            yield from _build_envelopes(
                bundle=bundle,
                extraction=extraction,
                company_mobile_numbers=company_mobile_numbers,
                company_email_addresses=company_email_addresses,
                internal_person_names=internal_person_names,
                file_exclusions=file_exclusions,
            )

    def _fetch_messages(
        self,
        conn: Connection,
        chat_id: str,
        whatsapp_user_id: str,
    ) -> list[dict[str, object]]:
        stmt = (
            select(messages)
            .where(messages.c.chat_id == chat_id)
            .where(messages.c.whatsapp_user_id == whatsapp_user_id)
            .where(messages.c.body.isnot(None))
            .where(messages.c.body != "")
            .order_by(messages.c.timestamp)
        )
        rows = conn.execute(stmt).fetchall()
        return [
            {
                "from_id": r.from_id,
                "to_id": r.to_id,
                "author_id": r.author_id,
                "body": r.body,
                "timestamp": r.timestamp,
                "from_me": bool(r.from_me) if r.from_me else False,
            }
            for r in rows
        ]

    def _fetch_participants(
        self,
        conn: Connection,
        chat_id: str,
        whatsapp_user_id: str,
        msgs: list[dict[str, object]],
    ) -> list[_Participant]:
        jids = _participant_jids(chat_id, msgs)
        if not jids:
            return []
        stmt = select(contacts).where(
            contacts.c.whatsapp_user_id == whatsapp_user_id,
            or_(
                contacts.c.jid.in_(jids),
                contacts.c.lid_id.in_(jids),
                contacts.c.cus_id.in_(jids),
            ),
        )
        rows = list(conn.execute(stmt))
        result: list[_Participant] = []
        seen: set[str] = set()
        for row in rows:
            jid = str(row.jid or "")
            phone = _first_str(row.phone_number, row.number)
            name = _first_str(row.name, row.pushname, row.short_name)
            for candidate in (row.jid, row.lid_id, row.cus_id):
                candidate_jid = str(candidate or "")
                if candidate_jid in jids and candidate_jid not in seen:
                    result.append(
                        _Participant(
                            jid=candidate_jid,
                            phone=phone,
                            name=name,
                            role="chat" if candidate_jid == chat_id else "member",
                        )
                    )
                    seen.add(candidate_jid)
            if jid and jid not in seen and (row.lid_id in jids or row.cus_id in jids):
                result.append(_Participant(jid=jid, phone=phone, name=name, role="member"))
                seen.add(jid)
        for jid in jids:
            if jid not in seen:
                result.append(
                    _Participant(jid=jid, phone=_phone_from_jid(jid), name=None, role="member")
                )
        return result


def _build_envelope(
    *,
    bundle: _ChatBundle,
    extraction: ExtractionResult,
    company_mobile_numbers: list[str] | None = None,
    company_email_addresses: list[str] | None = None,
    internal_person_names: list[str] | None = None,
    file_exclusions: ExclusionFile | None = None,
) -> dict[str, JsonValue] | None:
    envelopes = _build_envelopes(
        bundle=bundle,
        extraction=extraction,
        company_mobile_numbers=company_mobile_numbers,
        company_email_addresses=company_email_addresses,
        internal_person_names=internal_person_names,
        file_exclusions=file_exclusions,
    )
    return envelopes[0] if envelopes else None


def _build_envelopes(
    *,
    bundle: _ChatBundle,
    extraction: ExtractionResult,
    company_mobile_numbers: list[str] | None = None,
    company_email_addresses: list[str] | None = None,
    internal_person_names: list[str] | None = None,
    file_exclusions: ExclusionFile | None = None,
) -> list[dict[str, JsonValue]]:
    from src.connectors.fundbox.builders import build_envelope

    company_phones = [] if company_mobile_numbers is None else list(company_mobile_numbers)
    company_emails = [] if company_email_addresses is None else company_email_addresses
    internal_names = [] if internal_person_names is None else internal_person_names
    if bundle.session_phone:
        company_phones.append(bundle.session_phone)
    for endpoint in bundle.message_endpoints:
        phone = endpoint.get("phone") if isinstance(endpoint, dict) else None
        role = endpoint.get("role") if isinstance(endpoint, dict) else None
        if role == "sender" and isinstance(phone, str):
            company_phones.append(phone)
    filtered = filter_extraction(
        extraction,
        build_exclusion_context(
            company_mobile_numbers=company_phones,
            company_email_addresses=company_emails,
            internal_person_names=internal_names,
            file_exclusions=file_exclusions or ExclusionFile(),
        ),
    )
    if filtered is None:
        return []
    extraction = filtered

    tx_payload = transactions_payload(extraction)
    base_raw_payload: dict[str, JsonValue] = {
        "chat_id": bundle.chat_id,
        "chat_name": bundle.chat_name,
        "session_id": bundle.session_id,
        "whatsapp_user_id": bundle.whatsapp_user_id,
        "tenant": bundle.tenant,
        "messages_text": bundle.msg_text,
        "summary": extraction.get("summary"),
        "customer_sentiment": extraction.get("customer_sentiment"),
        "chat_members": chat_members_payload(extraction),
        "inquiries": inquiries_payload(extraction),
        "strong_identifiers": strong_identifiers_payload(extraction),
        "weak_identifiers": weak_identifiers_payload(extraction),
        "participants": _participants_payload(bundle.participants),
        "message_endpoints": bundle.message_endpoints,
        "transactions": tx_payload,
    }
    conversation_ref: dict[str, JsonValue] = {
        "platform": "whatsapp",
        "whatsapp_user_id": bundle.whatsapp_user_id,
        "chat_id": bundle.chat_id,
        "session_id": bundle.session_id,
        "tenant": bundle.tenant,
    }

    people = possible_persons_from_extraction(extraction)
    envelopes: list[dict[str, JsonValue]] = []
    primary_source_record_id: str | None = None
    for index, person in enumerate(people, start=1):
        source_record_id = f"whatsapp-chat-{bundle.chat_id}-person-{index}"
        if primary_source_record_id is None:
            primary_source_record_id = source_record_id
        attributes: dict[str, JsonValue] = {}
        name = person.get("name")
        if name:
            attributes["full_name"] = name
        raw_payload = dict(base_raw_payload)
        raw_payload.update(
            {
                "possible_person": possible_person_payload(person),
                "possible_person_index": index,
                "primary_source_record_id": primary_source_record_id,
                "relationship_to_primary": person.get("relationship_to_primary"),
                "relationship_label": person.get("relationship_label"),
                "relationship_status": "pending"
                if person.get("relationship_to_primary") or person.get("relationship_label")
                else None,
                "person_weak_identifiers": [
                    {
                        "type": item.get("type"),
                        "value": item.get("value"),
                        "label": item.get("label"),
                        "person_name": item.get("person_name"),
                        "confidence": item.get("confidence"),
                        "notes": item.get("notes"),
                    }
                    for item in weak_identifiers_for_possible_person(person)
                ],
            }
        )
        envelopes.append(
            build_envelope(
                source_record_id=source_record_id,
                observed_at=bundle.observed_at,
                identifiers=identifiers_from_possible_person(person),
                attributes=attributes,
                raw_payload=raw_payload,
                record_type="conversation",
                extraction_confidence=person.get("confidence") or extraction["confidence"],
                extraction_method=extraction_method_label(),
                conversation_ref=conversation_ref,
                addresses=person_address_payloads(person),
            )
        )
    return envelopes


def _participants_payload(participants: list[_Participant]) -> list[JsonValue]:
    return [{"jid": p.jid, "phone": p.phone, "name": p.name, "role": p.role} for p in participants]


def _message_endpoints(msgs: list[dict[str, object]]) -> list[JsonValue]:
    endpoints: list[JsonValue] = []
    seen: set[tuple[str, str]] = set()
    for msg in msgs:
        for role, key in (("sender", "from_id"), ("recipient", "to_id"), ("author", "author_id")):
            jid = str(msg.get(key) or "")
            if not jid or (role, jid) in seen:
                continue
            endpoints.append({"role": role, "jid": jid, "phone": _phone_from_jid(jid)})
            seen.add((role, jid))
    return endpoints


def _participant_jids(chat_id: str, msgs: list[dict[str, object]]) -> set[str]:
    jids = {chat_id}
    for msg in msgs:
        for key in ("from_id", "to_id", "author_id"):
            value = str(msg.get(key) or "")
            if value:
                jids.add(value)
    return jids


def _first_str(*values: object) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _phone_from_jid(jid: str) -> str | None:
    if not jid.endswith("@c.us"):
        return None
    digits = "".join(ch for ch in jid.split("@", 1)[0] if ch.isdigit())
    if not digits:
        return None
    return f"+{digits}"


def _message_sort_key(msg: dict[str, object]) -> tuple[int, str, str]:
    ts = msg.get("timestamp")
    if isinstance(ts, datetime):
        return (0, ts.isoformat(), str(msg.get("id") or ""))
    if isinstance(ts, str) and ts.strip():
        return (0, ts.strip(), str(msg.get("id") or ""))
    return (1, "", str(msg.get("id") or ""))


def _participant_by_jid(participants: list[_Participant]) -> dict[str, _Participant]:
    return {participant.jid: participant for participant in participants}


def _clean_jid(jid: str) -> str:
    return jid.split("@", 1)[0]


def _dialog_speaker(
    jid: str,
    participants_by_jid: dict[str, _Participant],
    chat_name: str | None,
) -> str:
    participant = participants_by_jid.get(jid)
    name = participant.name if participant is not None else None
    phone = participant.phone if participant is not None else _phone_from_jid(jid)
    if participant is not None and participant.role == "chat" and name is None:
        name = chat_name
    if name and phone:
        return f"{name} ({phone})"
    if name:
        return name
    if phone:
        return phone
    return _clean_jid(jid)


def _format_messages(
    msgs: list[dict[str, object]],
    participants: list[_Participant] | None = None,
    chat_name: str | None = None,
) -> str:
    participants_by_jid = _participant_by_jid(participants or [])
    lines: list[str] = []
    for m in sorted(msgs, key=_message_sort_key):
        ts = m.get("timestamp")
        ts_str = ""
        if isinstance(ts, datetime):
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
        elif ts:
            ts_str = str(ts)
        body = str(m.get("body", "")).strip()
        if not body:
            continue
        sender_jid = str(m.get("author_id") or m.get("from_id") or "unknown")
        sender = _dialog_speaker(sender_jid, participants_by_jid, chat_name)
        prefix = "[ME] " if m.get("from_me") else ""
        lines.append(f"[{ts_str}] {prefix}{sender}: {body}")
    return "\n".join(lines)


def _latest_message_timestamp(msgs: list[dict[str, object]]) -> str:
    sorted_messages = sorted(msgs, key=_message_sort_key, reverse=True)
    return latest_timestamp(*(m.get("timestamp") for m in sorted_messages))
