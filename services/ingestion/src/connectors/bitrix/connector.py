"""Bitrix24 chat connector — ingests conversations from MariaDB.

Actual tables:
- chats               — WhatsApp chat sessions linked to deals
- deals               — Bitrix CRM deals (title, stage, category)
- categories          — categories / tenants (EkoSG, Speedzone, etc.)
- personalize_message_logs — AI-personalized follow-up messages per chat
- sent_message_logs   — templated message sends per chat

A "chat" represents a WhatsApp conversation connected to a Bitrix deal.
The deal title and AI-generated messages provide identity and transaction
signal that the LLM extractor parses into structured identifiers/transactions.
The category links the chat to a known tenant (EkoSG, Speedzone, etc.).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from src.config import get_settings
from src.connectors.base import SourceConnector
from src.connectors.bitrix.db import get_engine
from src.connectors.bitrix.schema import (
    agent_chat,
    agents,
    categories,
    chats,
    deals,
    personalize_message_logs,
    sent_message_logs,
    templates,
)
from src.connectors.chat_helpers import (
    ExtractionResult,
    chat_members_payload,
    extraction_method_label,
    identifiers_from_extraction,
    inquiries_payload,
    latest_timestamp,
    run_extraction_batch,
    strong_identifiers_payload,
    transactions_payload,
    weak_identifiers_payload,
)
from src.exclusion_config import load_exclusion_file
from src.exclusions import build_exclusion_context, filter_extraction
from src.models import JsonValue

logger = logging.getLogger(__name__)

BITRIX_SOURCE_KEY = "bitrix_chat"

# LLM batch size — how many conversations to send in parallel.
LLM_BATCH_SIZE = 20

#: Map CRM category name → entity_key.
CATEGORY_TO_ENTITY: dict[str, str] = {
    "EkoSG": "eko",
    "EkoLife SG": "eko",
    "EkoLife MY": "eko",
    "EKO MY": "eko",
    "Speedzone": "speedzone",
}


@dataclass
class _AgentMember:
    bitrix_agent_id: str
    name: str
    active: bool


@dataclass
class _ChatBundle:
    chat_id: int
    deal_id: int
    bitrix_chat_id: str
    last_message_at: datetime | None
    created_at: datetime | None
    category_name: str
    entity: str
    conv_text: str
    deal: dict[str, object] | None
    agents: list[_AgentMember]


def _agents_payload(agents_: list[_AgentMember]) -> list[JsonValue]:
    return [
        {
            "bitrix_agent_id": agent.bitrix_agent_id,
            "name": agent.name,
            "active": agent.active,
            "role": "agent",
        }
        for agent in agents_
    ]


class BitrixChatConnector(SourceConnector):
    """Yields conversation source records from the Bitrix24 MariaDB."""

    chunk_size: int = 200

    def get_source_key(self) -> str:
        return BITRIX_SOURCE_KEY

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        engine = get_engine()
        with engine.connect() as conn:
            yield from self._fetch_chats(conn)

    def _fetch_chats(self, conn: Connection) -> Iterator[dict[str, JsonValue]]:
        cat_stmt = select(categories)
        cat_rows: dict[int, object] = {row.id: row for row in conn.execute(cat_stmt)}

        total = conn.scalar(
            select(func.count()).select_from(chats).where(chats.c.deal_id.isnot(None))
        )
        logger.info("Bitrix: %s chats with deals — fetching deals...", total)

        all_bundles: list[_ChatBundle] = []
        stmt = select(chats).where(chats.c.deal_id.isnot(None)).order_by(chats.c.id)
        chat_result = conn.execute(stmt)
        rows = list(chat_result.fetchmany(self.chunk_size))

        while rows:
            for chat in rows:
                deal = self._load_deal(conn, chat.deal_id)
                cat_id: int | None = None
                if deal:
                    raw_cat_id = deal.get("category_id")
                    if isinstance(raw_cat_id, int):
                        cat_id = raw_cat_id
                    elif isinstance(raw_cat_id, str):
                        cat_id = int(raw_cat_id) if raw_cat_id else None
                category = cat_rows.get(cat_id) if cat_id else None
                if category is None:
                    continue
                cat_name = getattr(category, "name", "") or ""
                entity = CATEGORY_TO_ENTITY.get(cat_name)
                if entity is None:
                    continue
                conv_text = self._build_conversation(conn, chat.id, deal)
                agent_members = self._load_agents(conn, chat.id)

                all_bundles.append(
                    _ChatBundle(
                        chat_id=chat.id,
                        deal_id=chat.deal_id or 0,
                        bitrix_chat_id=getattr(chat, "bitrix_chat_id", None) or "",
                        last_message_at=getattr(chat, "last_message_at", None),
                        created_at=getattr(chat, "created_at", None),
                        category_name=cat_name,
                        entity=entity,
                        conv_text=conv_text,
                        deal=deal,
                        agents=agent_members,
                    )
                )
            rows = list(chat_result.fetchmany(self.chunk_size))

        logger.info("Collected %d Bitrix chats — starting LLM batch phase", len(all_bundles))

        # Phase 2: run LLM in batches.
        extraction_cache: dict[int, ExtractionResult] = {}
        for i in range(0, len(all_bundles), LLM_BATCH_SIZE):
            batch = all_bundles[i : i + LLM_BATCH_SIZE]
            batch_texts = [b.conv_text for b in batch]
            batch_results = run_extraction_batch(batch_texts)
            logger.info(
                "LLM batch %d-%d/%d done",
                i,
                min(i + LLM_BATCH_SIZE, len(all_bundles)),
                len(all_bundles),
            )
            for bundle, result in zip(batch, batch_results, strict=True):
                if result is not None:
                    extraction_cache[bundle.chat_id] = result

        # Phase 3: yield envelopes.
        for bundle in all_bundles:
            extraction = extraction_cache.get(bundle.chat_id)
            if extraction is None:
                logger.warning("LLM extraction failed for chat %s", bundle.chat_id)
                continue
            envelope = self._build_envelope(bundle=bundle, extraction=extraction)
            if envelope is not None:
                yield envelope

    def _load_deal(self, conn: Connection, deal_id: int | None) -> dict[str, object] | None:
        if deal_id is None:
            return None
        row = conn.execute(select(deals).where(deals.c.id == deal_id)).first()
        if row is None:
            return None
        return {
            "id": row.id,
            "bitrix_deal_id": getattr(row, "bitrix_deal_id", None),
            "title": getattr(row, "title", None),
            "stage_id": getattr(row, "stage_id", None),
            "opened": bool(row.opened) if row.opened else False,
            "closed": bool(row.closed) if row.closed else False,
            "category_id": getattr(row, "category_id", None),
        }

    def _load_agents(self, conn: Connection, chat_id: int) -> list[_AgentMember]:
        stmt = (
            select(agents)
            .select_from(agent_chat.join(agents, agent_chat.c.agent_id == agents.c.id))
            .where(agent_chat.c.chat_id == chat_id)
            .order_by(agents.c.id)
        )
        return [
            _AgentMember(
                bitrix_agent_id=str(row.bitrix_agent_id or ""),
                name=str(row.name or ""),
                active=bool(row.active),
            )
            for row in conn.execute(stmt)
        ]

    def _build_conversation(
        self,
        conn: Connection,
        chat_id: int,
        deal: dict[str, object] | None,
    ) -> str:
        lines: list[str] = []
        events: list[tuple[datetime | None, str, int, str]] = []

        if deal:
            title = str(deal.get("title") or "")
            if title:
                lines.append(f"[Deal] {title}")

        p_stmt = select(personalize_message_logs).where(
            personalize_message_logs.c.chat_id == chat_id
        )
        for row in conn.execute(p_stmt):
            ts = ""
            created_at = row.created_at if isinstance(row.created_at, datetime) else None
            if created_at is not None:
                ts = created_at.strftime("%Y-%m-%d %H:%M:%S")
            row_id = int(getattr(row, "id", 0) or 0)
            client_name = str(getattr(row, "client_name", "") or "").strip()
            if client_name:
                events.append((created_at, "personalize", row_id, f"[{ts}] Client: {client_name}"))
            body = str(
                getattr(row, "message_sent", "") or getattr(row, "llm_message", "") or ""
            ).strip()
            if body:
                events.append((created_at, "personalize", row_id, f"[{ts}] Sent: {body}"))

        s_stmt = (
            select(sent_message_logs, templates.c.content.label("template_content"))
            .select_from(
                sent_message_logs.outerjoin(
                    templates,
                    sent_message_logs.c.template_id == templates.c.id,
                )
            )
            .where(sent_message_logs.c.chat_id == chat_id)
        )
        for row in conn.execute(s_stmt):
            ts = ""
            created_at = row.created_at if isinstance(row.created_at, datetime) else None
            if created_at is not None:
                ts = created_at.strftime("%Y-%m-%d %H:%M:%S")
            row_id = int(getattr(row, "id", 0) or 0)
            body = str(getattr(row, "template_content", "") or "").strip()
            if body:
                events.append((created_at, "template", row_id, f"[{ts}] Template: {body}"))

        for _ts, _source, _row_id, line in sorted(
            events,
            key=lambda item: (
                0 if item[0] is not None else 1,
                item[0] or datetime.max,
                item[1],
                item[2],
            ),
        ):
            lines.append(line)

        return "\n".join(lines)

    def _build_envelope(
        self,
        *,
        bundle: _ChatBundle,
        extraction: ExtractionResult,
    ) -> dict[str, JsonValue] | None:
        from src.connectors.fundbox.builders import build_envelope

        agent_names = [agent.name for agent in bundle.agents if agent.name]
        try:
            settings = get_settings()
            company_mobile_numbers = getattr(settings, "company_mobile_numbers", [])
            company_email_addresses = getattr(settings, "company_email_addresses", [])
            internal_person_names = getattr(settings, "internal_person_names", [])
            exclusions_file = getattr(settings, "ingestion_exclusions_file", "")
        except Exception:
            company_mobile_numbers = []
            company_email_addresses = []
            internal_person_names = []
            exclusions_file = ""
        file_exclusions = load_exclusion_file(exclusions_file)
        file_exclusions.names.extend(agent_names)
        filtered = filter_extraction(
            extraction,
            build_exclusion_context(
                company_mobile_numbers=company_mobile_numbers,
                company_email_addresses=company_email_addresses,
                internal_person_names=internal_person_names,
                file_exclusions=file_exclusions,
            ),
        )
        if filtered is None:
            return None
        extraction = filtered

        chat_id = str(bundle.chat_id)
        deal_id = str(bundle.deal_id)
        bitrix_chat_id = bundle.bitrix_chat_id
        observed_at = latest_timestamp(bundle.last_message_at, bundle.created_at)
        entity = bundle.entity
        cat_name = bundle.category_name

        identifiers = identifiers_from_extraction(extraction)

        attributes: dict[str, JsonValue] = {}
        persons = extraction["persons"]
        if persons:
            name = persons[0].get("name")
            if name:
                attributes["full_name"] = name
        if bundle.deal:
            title = str(bundle.deal.get("title") or "")
            if title:
                attributes["deal_title"] = title

        tx_payload = transactions_payload(extraction)

        chat_members = chat_members_payload(extraction) or _agents_payload(bundle.agents)

        d = bundle.deal or {}
        raw_payload: dict[str, JsonValue] = {
            "chat_id": chat_id,
            "deal_id": deal_id,
            "bitrix_chat_id": bitrix_chat_id,
            "category": cat_name,
            "tenant": entity,
            "deal_title": str(d.get("title", "") or ""),
            "deal_stage_id": str(d.get("stage_id", "") or ""),
            "deal_opened": bool(d.get("opened", False)),
            "deal_closed": bool(d.get("closed", False)),
            "conversation_text": bundle.conv_text,
            "summary": extraction.get("summary"),
            "customer_sentiment": extraction.get("customer_sentiment"),
            "chat_members": chat_members,
            "inquiries": inquiries_payload(extraction),
            "strong_identifiers": strong_identifiers_payload(extraction),
            "weak_identifiers": weak_identifiers_payload(extraction),
            "transactions": tx_payload,
        }
        conversation_ref: dict[str, JsonValue] = {
            "platform": "bitrix",
            "chat_id": chat_id,
            "deal_id": deal_id,
            "bitrix_chat_id": bitrix_chat_id,
            "tenant": entity,
        }
        return build_envelope(
            source_record_id=f"bitrix-chat-{chat_id}",
            observed_at=observed_at,
            identifiers=identifiers,
            attributes=attributes,
            raw_payload=raw_payload,
            record_type="conversation",
            extraction_confidence=extraction["confidence"],
            extraction_method=extraction_method_label(),
            conversation_ref=conversation_ref,
        )
