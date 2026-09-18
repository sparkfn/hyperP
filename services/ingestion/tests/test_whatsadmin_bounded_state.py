"""Bounded WhatsAdmin checkpoint, retry-identity, and durable-entry contracts."""

from __future__ import annotations

import pytest
from src.connectors.whatsadmin_api.bounded_state import (
    WhatsAdminCursor,
    WhatsAdminWindowError,
    bundles_digest,
    credential_fingerprint,
    extraction_replay_id,
    prepared_digest,
    window_from_source_window,
)
from src.connectors.whatsadmin_api.credentials import WhatsAdminEntity
from src.connectors.whatsadmin_api.models import StoredSession
from src.connectors.whatsadmin_api.retry_queue import (
    chat_source_version,
    retry_matches_bundle,
    serialize_retry_bundle,
)
from src.connectors.whatsadmin_api.watermark import (
    bounded_digest,
    bounded_entry_key,
    committed_version_key,
    is_completed_watermark_key,
    session_watermark_key,
)
from src.connectors.whatsapp.connector import _ChatBundle, _Participant

WINDOW = {
    "contract_version": "whatsadmin-hyperp-extraction-v1",
    "entity_key": "eko",
    "window_id": "eko-window-1",
    "completed_lower_bound": "2026-07-10T00:00:00+00:00",
    "requested_as_of_upper_bound": "2026-09-17T06:00:00+00:00",
}


def _cursor(**overrides: object) -> WhatsAdminCursor:
    base = WhatsAdminCursor(
        subphase="sessions",
        contract_version="whatsadmin-hyperp-extraction-v1",
        credential_fingerprint="sha256:cred",
        window_id="eko-window-1",
        lower_bound="2026-07-10T00:00:00+00:00",
        upper_bound="2026-09-17T06:00:00+00:00",
        snapshot_id=None,
        sessions_cursor=None,
        session_queue=(),
        sessions_processed=0,
        chats_cursor=None,
        bundles_digest=None,
        bundle_count=0,
        bundle_index=0,
        prepared_digest=None,
        chat_id=None,
        source_version=None,
        retry_replay_id=None,
        retry_source_record_id=None,
        extract_attempts=0,
        completed_sessions=(),
    )
    from dataclasses import replace

    return replace(base, **overrides)  # type: ignore[arg-type]


def test_cursor_round_trips_every_continuation_field() -> None:
    cursor = _cursor(
        subphase="extract",
        session_queue=(StoredSession(session_id="ses_1", whatsapp_user_id="659@c.us"),),
        sessions_processed=3,
        snapshot_id="2026-09-17T05:30:00+00:00",
        chats_cursor="page-2",
        bundles_digest="digest-1",
        bundle_count=2,
        bundle_index=1,
        retry_replay_id="sha256:replay",
        retry_source_record_id="whatsapp-chat-eko-ses_1-chat-1-person-1",
        extract_attempts=2,
        completed_sessions=("ses_0",),
    )

    parsed = WhatsAdminCursor.from_payload(cursor.to_payload())

    assert parsed == cursor
    assert parsed.current_session() == StoredSession(
        session_id="ses_1",
        whatsapp_user_id="659@c.us",
    )


def test_cursor_rejects_unknown_version_and_malformed_fields() -> None:
    payload = _cursor().to_payload()

    with pytest.raises(ValueError, match="version"):
        WhatsAdminCursor.from_payload({**payload, "cursor_version": 99})
    with pytest.raises(ValueError, match="subphase"):
        WhatsAdminCursor.from_payload({**payload, "subphase": "unknown"})
    with pytest.raises(ValueError, match="bundle_index"):
        WhatsAdminCursor.from_payload({**payload, "bundle_index": -1})
    with pytest.raises(ValueError, match="upper_bound"):
        WhatsAdminCursor.from_payload({**payload, "upper_bound": None})


def test_window_requires_the_exact_immutable_contract() -> None:
    window = window_from_source_window(WINDOW)

    assert window.entity_key == "eko"
    assert window.completed_lower_bound == "2026-07-10T00:00:00+00:00"
    with pytest.raises(WhatsAdminWindowError, match="shape"):
        window_from_source_window({**WINDOW, "extra": 1})
    with pytest.raises(WhatsAdminWindowError, match="entity"):
        window_from_source_window({**WINDOW, "entity_key": "fundbox"})
    with pytest.raises(WhatsAdminWindowError, match="shape"):
        window_from_source_window({"contract_version": "x"})


def test_extraction_replay_identity_is_attempt_independent_and_content_bound() -> None:
    first = extraction_replay_id("eko", "ses_1", "chat-1", "sha256:version-a")
    same = extraction_replay_id("eko", "ses_1", "chat-1", "sha256:version-a")
    changed = extraction_replay_id("eko", "ses_1", "chat-1", "sha256:version-b")
    other_chat = extraction_replay_id("eko", "ses_1", "chat-2", "sha256:version-a")
    other_entity = extraction_replay_id("speedzone", "ses_1", "chat-1", "sha256:version-a")

    assert first == same
    assert len({first, changed, other_chat, other_entity}) == 4
    assert first.startswith("sha256:")


def test_content_version_changes_with_edits_and_participants_not_timestamps() -> None:
    participants = [_Participant("6581111111@c.us", "6581111111", "Alice", "chat")]
    base = chat_source_version(
        chat_id="chat-1",
        chat_name="Alice",
        session_id="ses_1",
        msg_text="Hello",
        participants=participants,
    )
    edited = chat_source_version(
        chat_id="chat-1",
        chat_name="Alice",
        session_id="ses_1",
        msg_text="Hello there",
        participants=participants,
    )
    renamed = chat_source_version(
        chat_id="chat-1",
        chat_name="Alice",
        session_id="ses_1",
        msg_text="Hello",
        participants=[_Participant("6581111111@c.us", "6581111111", "Alice Tan", "chat")],
    )

    assert base != edited
    assert base != renamed
    assert base.startswith("sha256:")


def test_retry_matching_prefers_the_content_version_and_keeps_legacy_entries() -> None:
    versioned = _ChatBundle(
        chat_id="chat-1",
        chat_name="Alice",
        session_id="ses_1",
        whatsapp_user_id="659@c.us",
        tenant="eko",
        msg_text="Hello",
        observed_at="2026-09-17T05:20:00+00:00",
        participants=[],
        message_endpoints=[],
        session_phone=None,
        source_version="sha256:version-a",
    )
    legacy = _ChatBundle(
        chat_id="chat-1",
        chat_name="Alice",
        session_id="ses_1",
        whatsapp_user_id="659@c.us",
        tenant="eko",
        msg_text="Hello",
        observed_at="2026-09-17T05:20:00+00:00",
        participants=[],
        message_endpoints=[],
        session_phone=None,
    )

    serialized = serialize_retry_bundle(versioned, {"failure_code": "malformed_response"})
    assert serialized["source_version"] == "sha256:version-a"
    assert retry_matches_bundle(serialized, versioned) is True
    assert retry_matches_bundle(serialized, _with_version(versioned, "sha256:version-b")) is False
    # A timestamp-only entry still matches its own observed version.
    assert retry_matches_bundle({"chat_id": "chat-1", "observed_at": legacy.observed_at}, legacy)
    assert (
        retry_matches_bundle(
            {"chat_id": "chat-1", "observed_at": "2026-09-17T05:25:00+00:00"},
            legacy,
        )
        is False
    )


def test_durable_keys_are_entity_session_and_generation_scoped() -> None:
    entity: WhatsAdminEntity = "eko"

    assert bounded_entry_key("bundles", entity, "ses_1", "d1").endswith("eko:ses_1:bundles:d1")
    assert committed_version_key(entity, "ses_1", "chat-1").endswith("eko:ses_1:version:chat-1")
    assert session_watermark_key(entity, "ses_1").endswith("eko:ses_1:watermark")
    assert is_completed_watermark_key(session_watermark_key(entity, "ses_1")) is True
    assert is_completed_watermark_key(committed_version_key(entity, "ses_1", "chat-1")) is False
    assert credential_fingerprint("hk_eko_secret").startswith("sha256:")
    assert "hk_eko_secret" not in credential_fingerprint("hk_eko_secret")


def test_entry_and_page_digests_are_stable_and_position_bound() -> None:
    assert bundles_digest("ses_1", "page-2", 3) == bundles_digest("ses_1", "page-2", 3)
    assert bundles_digest("ses_1", "page-2", 3) != bundles_digest("ses_1", "page-3", 3)
    assert prepared_digest("ses_1", "chat-1", "v1") != prepared_digest("ses_1", "chat-1", "v2")
    assert bounded_digest("a", "b") != bounded_digest("ab")


def _with_version(bundle: _ChatBundle, version: str) -> _ChatBundle:
    return _ChatBundle(
        chat_id=bundle.chat_id,
        chat_name=bundle.chat_name,
        session_id=bundle.session_id,
        whatsapp_user_id=bundle.whatsapp_user_id,
        tenant=bundle.tenant,
        msg_text=bundle.msg_text,
        observed_at=bundle.observed_at,
        participants=list(bundle.participants),
        message_endpoints=list(bundle.message_endpoints),
        session_phone=bundle.session_phone,
        source_version=version,
    )
