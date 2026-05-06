"""SQLAlchemy Core table declarations for the WhatsApp PostgreSQL database.

Schema: chrishubert/whatsapp-api (https://github.com/niclase/whatsapp-web-api)
Key tables:
- sessions       — one row per WhatsApp account (linked to org via org_id)
- orgs           — organisation metadata (Fundbox, EkoLife SG, etc.)
- session_users  — maps users → sessions
- contacts       — contact roster per account
- chats          — conversation threads
- messages       — individual messages
- labels         — chat labels (e.g. "invoice", "Need to arrange delivery")
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Index, MetaData, String, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()


sessions = Table(
    "sessions",
    metadata,
    Column("id", String(255), primary_key=True),  # e.g. "fundbox_collections_6568505434"
    Column("org_id", UUID, index=True),
    Column("whatsapp_user_id", String(255), unique=True),  # e.g. "6568505434@c.us"
    Column("status", String(50)),
    Column("expected_phone_number", String(50)),
    Column("phone_validated", Boolean),
    Column("is_primary", Boolean),
    Column("color", String(20)),
    Column("last_ready_at", DateTime),
    Column("last_disconnect_at", DateTime),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)


orgs = Table(
    "orgs",
    metadata,
    Column("id", UUID, primary_key=True),
    Column("name", String(255)),  # e.g. "Fundbox", "EkoLife SG"
    Column("plan", String(50)),
    Column("status", String(50)),
    Column("description", Text),
    Column("owner_user_id", String(255)),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)


session_users = Table(
    "session_users",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", UUID),
    Column("session_id", String(255), index=True),
    Column("created_at", DateTime),
)


contacts = Table(
    "contacts",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("jid", String(255), unique=True),
    Column("number", String(50)),
    Column("name", String(255)),
    Column("pushname", String(255)),
    Column("short_name", String(255)),
    Column("phone_number", String(50)),
    Column("country_code", String(10)),
    Column("about", Text),
    Column("lid_id", String(255)),
    Column("cus_id", String(255)),
    Column("is_me", Boolean),
    Column("is_user", Boolean),
    Column("is_group", Boolean),
    Column("is_wa_contact", Boolean),
    Column("is_business", Boolean),
    Column("is_enterprise", Boolean),
    Column("is_my_contact", Boolean),
    Column("is_blocked", Boolean),
    Column("profile_pic_url", Text),
    Column("last_seen_at", DateTime),
    Column("is_verified", Boolean),
    Column("whatsapp_user_id", String(255), index=True),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)


chats = Table(
    "chats",
    metadata,
    Column("id", String(255), primary_key=True),  # e.g. "6568505434@c.us_6588251000@c.us"
    Column("name", String(255)),
    Column("description", Text),
    Column("is_group", Boolean),
    Column("is_read_only", Boolean),
    Column("archived", Boolean),
    Column("pinned", Boolean),
    Column("is_muted", Boolean),
    Column("unread_count", BigInteger),
    Column("last_message_id", String(255)),
    Column("last_message_at", DateTime),
    Column("whatsapp_user_id", String(255), index=True),  # which session owns this chat
    Column("labels", JSONB),  # labels applied to this chat
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)


messages = Table(
    "messages",
    metadata,
    Column("id", String(255), primary_key=True),
    Column("chat_id", String(255), index=True),
    Column("from_id", String(255)),  # sender JID
    Column("to_id", String(255)),
    Column("author_id", String(255)),  # actual author (for groups)
    Column("body", Text),
    Column("timestamp", DateTime, index=True),
    Column("from_me", Boolean),
    Column("is_forwarded", Boolean),
    Column("forwarding_score", BigInteger),
    Column("is_starred", Boolean),
    Column("is_edited", Boolean),
    Column("is_revoked", Boolean),
    Column("has_media", Boolean),
    Column("has_quoted_msg", Boolean),
    Column("quoted_message_id", String(255)),
    Column("ack", BigInteger),
    Column("whatsapp_user_id", String(255)),  # which session this message is from
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
    Index("ix_messages_chat_timestamp", "chat_id", "timestamp"),
)


labels = Table(
    "labels",
    metadata,
    Column("id", String(255), primary_key=True),
    Column("whatsapp_user_id", String(255), index=True),
    Column("name", String(255)),
    Column("hex_color", String(20)),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)
