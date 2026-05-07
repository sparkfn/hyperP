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

from src.connectors.base import SourceConnector
from src.connectors.chat_helpers import (
    ExtractionResult,
    chat_members_payload,
    extraction_method_label,
    identifiers_from_extraction,
    inquiries_payload,
    latest_timestamp,
    run_extraction_batch,
    transactions_payload,
)
from src.connectors.whatsapp.db import get_engine
from src.connectors.whatsapp.schema import chats, contacts, messages, orgs, sessions
from src.models import JsonValue

logger = logging.getLogger(__name__)

# LLM batch size — how many conversations to send in parallel.
LLM_BATCH_SIZE = 20


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
                participants = self._fetch_participants(conn, chat.id, whatsapp_uid, msgs)

                all_bundles.append(
                    _ChatBundle(
                        chat_id=str(chat.id),
                        chat_name=str(chat.name or ""),
                        session_id=str(session.id),
                        whatsapp_user_id=whatsapp_uid,
                        tenant=tenant,
                        msg_text=_format_messages(msgs),
                        observed_at=_latest_message_timestamp(msgs),
                        participants=participants,
                        message_endpoints=_message_endpoints(msgs),
                    )
                )

        logger.info("Collected %d WhatsApp chats — starting LLM batch phase", len(all_bundles))

        # Phase 2: run LLM in batches.
        extraction_cache: dict[str, ExtractionResult] = {}
        for i in range(0, len(all_bundles), LLM_BATCH_SIZE):
            batch = all_bundles[i : i + LLM_BATCH_SIZE]
            batch_results = run_extraction_batch([b.msg_text for b in batch])
            logger.info(
                "LLM batch %d-%d/%d done",
                i,
                min(i + LLM_BATCH_SIZE, len(all_bundles)),
                len(all_bundles),
            )
            for bundle, result in zip(batch, batch_results, strict=True):
                if result is not None:
                    extraction_cache[bundle.chat_id] = result

        # Phase 3: yield envelopes in the same order as the DB cursor.
        for bundle in all_bundles:
            extraction = extraction_cache.get(bundle.chat_id)
            if extraction is None:
                logger.warning("LLM extraction failed for chat %s", bundle.chat_id)
                continue

            yield _build_envelope(bundle=bundle, extraction=extraction)

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
) -> dict[str, JsonValue]:
    from src.connectors.fundbox.builders import build_envelope

    chat_id = bundle.chat_id
    chat_name = bundle.chat_name
    whatsapp_uid = bundle.whatsapp_user_id
    session_id = bundle.session_id
    tenant = bundle.tenant
    msg_text = bundle.msg_text
    observed_at = bundle.observed_at

    identifiers = identifiers_from_extraction(extraction)

    attributes: dict[str, JsonValue] = {}
    if extraction["persons"] and extraction["persons"][0].get("name"):
        attributes["full_name"] = extraction["persons"][0]["name"]

    tx_payload = transactions_payload(extraction)

    raw_payload: dict[str, JsonValue] = {
        "chat_id": chat_id,
        "chat_name": chat_name,
        "session_id": session_id,
        "whatsapp_user_id": whatsapp_uid,
        "tenant": tenant,
        "messages_text": msg_text,
        "summary": extraction.get("summary"),
        "customer_sentiment": extraction.get("customer_sentiment"),
        "chat_members": chat_members_payload(extraction),
        "inquiries": inquiries_payload(extraction),
        "participants": _participants_payload(bundle.participants),
        "message_endpoints": bundle.message_endpoints,
        "transactions": tx_payload,
    }
    conversation_ref: dict[str, JsonValue] = {
        "platform": "whatsapp",
        "whatsapp_user_id": whatsapp_uid,
        "chat_id": chat_id,
        "session_id": session_id,
        "tenant": tenant,
    }
    return build_envelope(
        source_record_id=f"whatsapp-chat-{chat_id}",
        observed_at=observed_at,
        identifiers=identifiers,
        attributes=attributes,
        raw_payload=raw_payload,
        record_type="conversation",
        extraction_confidence=extraction["confidence"],
        extraction_method=extraction_method_label(),
        conversation_ref=conversation_ref,
    )


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


def _format_messages(msgs: list[dict[str, object]]) -> str:
    lines: list[str] = []
    for m in msgs:
        ts = m.get("timestamp")
        ts_str = ""
        if isinstance(ts, datetime):
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
        elif ts:
            ts_str = str(ts)
        body = str(m.get("body", "")).strip()
        if not body:
            continue
        sender = str(m.get("author_id") or m.get("from_id") or "unknown")
        prefix = "[ME] " if m.get("from_me") else ""
        lines.append(f"[{ts_str}] {prefix}{sender}: {body}")
    return "\n".join(lines)


def _latest_message_timestamp(msgs: list[dict[str, object]]) -> str:
    return latest_timestamp(*(m.get("timestamp") for m in reversed(msgs)))
