"""SQLAlchemy Core table declarations for the Bitrix24 MariaDB chat database.

Actual tables discovered by inspecting the live database:
- agents              — agent profiles (name, bitrix_agent_id)
- agent_chat          — which agent is in which chat (agent_id → chat_id)
- categories          — categories / tenants (EkoSG, Speedzone, etc.)
- chats               — WhatsApp chat sessions linked to deals
- deal_stages         — Bitrix deal pipeline stages
- deals               — Bitrix CRM deals (title, stage, category)
- personalize_message_logs — AI-personalized follow-up messages sent per chat
- sent_message_logs   — templated message sends per chat
- templates           — message templates (HTML with {{0}} placeholders)
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, MetaData, String, Table, Text

metadata = MetaData()


agents = Table(
    "agents",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("bitrix_agent_id", BigInteger, unique=True),
    Column("name", String(255)),
    Column("first_name", String(255)),
    Column("last_name", String(255)),
    Column("active", Boolean),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)


agent_chat = Table(
    "agent_chat",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("agent_id", BigInteger, index=True),
    Column("chat_id", BigInteger, index=True),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)


categories = Table(
    "categories",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("bitrix_category_id", String(50)),
    Column("name", String(255)),  # e.g. "EkoSG", "Speedzone", "Dive Shop"
    Column("webhook_url", String(500)),
    Column("send_warmer", Boolean),
    Column("send_webhook", Boolean),
    Column("enable_personalize_message", Boolean),
    Column("quiet_hours_start", Integer),
    Column("quiet_hours_end", Integer),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)


chats = Table(
    "chats",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("deal_id", BigInteger, index=True),
    Column("bitrix_chat_id", String(20)),
    Column("last_message_at", DateTime, index=True),
    Column("last_fetch_at", DateTime),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
    Column("closed_by_agent_at", DateTime),
)


deals = Table(
    "deals",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("bitrix_deal_id", String(10), unique=True),
    Column("title", String(255)),  # deal title e.g. "Mark - Eko Life (Main)"
    Column("type_id", String(50)),
    Column("stage_id", String(50)),
    Column("opened", Boolean),
    Column("closed", Boolean),
    Column("last_activity_date", DateTime),
    Column("category_id", BigInteger, index=True),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)


deal_stages = Table(
    "deal_stages",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("category_id", BigInteger),
    Column("stage_id", String(50)),
    Column("name", String(255)),
    Column("status", String(50)),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)


personalize_message_logs = Table(
    "personalize_message_logs",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("category_id", BigInteger, index=True),
    Column("deal_id", BigInteger, index=True),
    Column("chat_id", BigInteger, index=True),
    Column("template_id", BigInteger),
    Column("client_name", String(255)),  # customer name the AI generated message for
    Column("template_message", Text),  # original template
    Column("llm_message", Text),  # AI-personalized version
    Column("has_enough_context", Boolean),
    Column("message_sent", Text),  # what was actually sent
    Column("sent", Boolean),
    Column("created_at", DateTime),
)


sent_message_logs = Table(
    "sent_message_logs",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("category_id", BigInteger, index=True),
    Column("template_id", BigInteger),
    Column("chat_id", BigInteger, index=True),
    Column("sent_at", DateTime),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)


templates = Table(
    "templates",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("name", String(255)),
    Column("content", Text),  # HTML with [B][/B] etc. and {{0}} placeholders
    Column("send_at_hour", BigInteger),
    Column("category_id", BigInteger),
    Column("disabled", Boolean),
    Column("stage_id", String(50)),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)
