"""Tests for chat transcript parsing of conversation raw payloads."""

from __future__ import annotations

from src.chat_transcript import parse_chat_transcript


def test_bitrix_conversation_text_strips_bbcode() -> None:
    raw = {
        "conversation_text": (
            "[Deal] Yook Peng - Eko Life (Main): Whatsapp\n"
            "[2025-09-18 10:50:00] Template: [B][/B][U][/U]Hey there[BR][BR]welcome"
        ),
        "chat_members": [{"name": "Eko Life (Main)", "phone": "", "role": "sender", "notes": ""}],
    }
    messages = parse_chat_transcript(raw)
    assert messages is not None
    # The "[Deal] …" header line is skipped; one real message remains.
    assert len(messages) == 1
    msg = messages[0]
    assert msg.timestamp == "2025-09-18 10:50:00"
    assert msg.timestamp_display == "18 Sep 2025, 10:50 AM"
    assert msg.speaker == "Template"
    assert "[B]" not in msg.text and "[BR]" not in msg.text
    assert msg.text == "Hey there\n\nwelcome"


def test_whatsapp_messages_text_extracts_phone_role_and_multiline() -> None:
    raw = {
        "messages_text": (
            "[2026-01-27 06:46:04] AH TANG Ullmax Jurong (+6581686686): hello\n"
            "[2026-01-27 07:50:42] Marsya FBX 80895922 (+6580895922): Invoice: 24751\n"
            "HPID: HM000012570"
        ),
        "chat_members": [
            {"name": "AH TANG", "phone": "+6581686686", "role": "customer"},
            {"name": "Marsya", "phone": "+6580895922", "role": "staff"},
        ],
    }
    messages = parse_chat_transcript(raw)
    assert messages is not None
    assert len(messages) == 2

    first = messages[0]
    assert first.speaker == "AH TANG Ullmax Jurong"
    assert first.phone == "+6581686686"
    assert first.role == "customer"
    assert first.text == "hello"

    second = messages[1]
    assert second.phone == "+6580895922"
    assert second.role == "staff"
    # Continuation line (no timestamp) joins the previous message; ": " in body preserved.
    assert second.text == "Invoice: 24751\nHPID: HM000012570"


def test_non_chat_payload_returns_none() -> None:
    assert parse_chat_transcript({"customer_id": 8841, "mobile": "91234567"}) is None


def test_empty_or_missing_payload_returns_none() -> None:
    assert parse_chat_transcript(None) is None
    assert parse_chat_transcript({}) is None
    assert parse_chat_transcript({"conversation_text": "   "}) is None
