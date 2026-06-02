"""Parse chat source-record raw payloads into structured transcripts.

Conversation source records (bitrix_chat, whatsapp_chat) store the chat as a
single transcript string under ``conversation_text`` or ``messages_text``, with
lines shaped ``[YYYY-MM-DD HH:MM:SS] Speaker (+phone): text``. Continuation
lines (no timestamp prefix) belong to the previous message. This module turns
that into a typed ``list[ChatMessage]`` so the frontend can render bubbles
instead of escaped JSON.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic.types import JsonValue

from src.display_format import format_display_datetime
from src.types import ChatMessage

_TRANSCRIPT_KEYS: tuple[str, ...] = ("conversation_text", "messages_text")

# Start of a message: "[2025-09-18 10:50:00] <rest>"
_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*(.*)$")
# BBCode noise from bitrix templates: [B][/B][U][/U][I][/I][S][/S]
_BBCODE_RE = re.compile(r"\[/?(?:B|U|I|S)\]", re.IGNORECASE)
_BR_RE = re.compile(r"\[BR\]", re.IGNORECASE)
# Trailing "(+6591234567)" on a speaker label.
_PHONE_RE = re.compile(r"\((\+?\d[\d\s]*)\)\s*$")


@dataclass
class _Pending:
    timestamp: str
    speaker: str
    phone: str | None
    lines: list[str]


def _clean_text(text: str) -> str:
    text = _BR_RE.sub("\n", text)
    text = _BBCODE_RE.sub("", text)
    return text.strip()


def _split_speaker(head: str) -> tuple[str, str, str | None]:
    """Split "Speaker (+phone): message" into (speaker, message, phone)."""
    if ": " in head:
        speaker_part, _, message = head.partition(": ")
    elif head.endswith(":"):
        speaker_part, message = head[:-1], ""
    else:
        speaker_part, message = head, ""
    phone: str | None = None
    match = _PHONE_RE.search(speaker_part)
    if match is not None:
        phone = match.group(1).replace(" ", "")
        speaker_part = speaker_part[: match.start()].strip()
    return speaker_part.strip(), message, phone


def _role_pairs(raw_payload: dict[str, JsonValue]) -> list[tuple[str, str]]:
    members = raw_payload.get("chat_members")
    pairs: list[tuple[str, str]] = []
    if isinstance(members, list):
        for member in members:
            if not isinstance(member, dict):
                continue
            name = member.get("name")
            role = member.get("role")
            if isinstance(name, str) and name and isinstance(role, str) and role:
                pairs.append((name, role))
    return pairs


def _match_role(speaker: str, pairs: list[tuple[str, str]]) -> str | None:
    target = speaker.lower()
    for name, role in pairs:
        candidate = name.lower()
        if candidate and (candidate in target or target in candidate):
            return role
    return None


def parse_chat_transcript(
    raw_payload: dict[str, JsonValue] | None,
) -> list[ChatMessage] | None:
    """Return a structured transcript, or None when the payload has no chat text."""
    if not raw_payload:
        return None
    transcript_text: str | None = None
    for key in _TRANSCRIPT_KEYS:
        value = raw_payload.get(key)
        if isinstance(value, str) and value.strip():
            transcript_text = value
            break
    if transcript_text is None:
        return None

    pairs = _role_pairs(raw_payload)
    pending: list[_Pending] = []
    for line in transcript_text.split("\n"):
        match = _TS_RE.match(line)
        if match is not None:
            speaker, message, phone = _split_speaker(match.group(2))
            pending.append(_Pending(match.group(1), speaker, phone, [message]))
        elif pending:
            pending[-1].lines.append(line)
        # Lines before the first timestamped line (e.g. a "[Deal] …" header) are skipped.

    messages: list[ChatMessage] = [
        ChatMessage(
            timestamp=item.timestamp,
            timestamp_display=format_display_datetime(item.timestamp),
            speaker=item.speaker,
            phone=item.phone,
            role=_match_role(item.speaker, pairs),
            text=_clean_text("\n".join(item.lines)),
        )
        for item in pending
    ]
    return messages or None
