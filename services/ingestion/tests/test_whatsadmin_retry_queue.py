from __future__ import annotations

import pytest
from src.connectors.whatsadmin_api.retry_queue import (
    chat_source_version,
    deserialize_retry_bundle,
    retry_matches_bundle,
    serialize_retry_bundle,
)
from src.connectors.whatsapp.connector import _ChatBundle
from src.models import JsonValue


def _versioned_bundle(
    source_version: str | None,
    observed_at: str = "2026-07-17T05:20:00+00:00",
) -> _ChatBundle:
    bundle = _bundle(observed_at)
    bundle.source_version = source_version
    return bundle


def _bundle(observed_at: str = "2026-07-17T05:20:00+00:00") -> _ChatBundle:
    return _ChatBundle(
        chat_id="chat-1",
        chat_name="Customer",
        session_id="ses_1",
        whatsapp_user_id="6590000000@c.us",
        tenant="eko",
        msg_text="Customer: hello",
        observed_at=observed_at,
        participants=[],
        message_endpoints=[],
        session_phone=None,
        source_id_scope="eko-ses_1",
    )


def _details() -> dict[str, JsonValue]:
    return {
        "entity_key": "eko",
        "session_id": "ses_1",
        "chat_id": "chat-1",
        "observed_at": "2026-07-17T05:20:00+00:00",
        "failure_code": "malformed_response",
        "attempts": 4,
    }


def test_retry_bundle_round_trip_preserves_source_material() -> None:
    bundle = _bundle()

    restored = deserialize_retry_bundle(serialize_retry_bundle(bundle, _details()))

    assert restored == bundle


def test_retry_identity_includes_chat_version_timestamp() -> None:
    first = _bundle()
    second = _bundle("2026-07-18T05:20:00+00:00")
    retry = serialize_retry_bundle(first, _details())

    assert retry_matches_bundle(retry, first) is True
    assert retry_matches_bundle(retry, second) is False


def test_retry_bundle_rejects_invalid_entity() -> None:
    retry = serialize_retry_bundle(_bundle(), _details())
    retry["entity_key"] = "fundbox"

    with pytest.raises(RuntimeError, match="entity key"):
        deserialize_retry_bundle(retry)


def test_retry_bundle_round_trips_the_immutable_content_version() -> None:
    bundle = _versioned_bundle("sha256:version-a")

    retry = serialize_retry_bundle(bundle, _details())
    restored = deserialize_retry_bundle(retry)

    assert retry["source_version"] == "sha256:version-a"
    assert restored.source_version == "sha256:version-a"


def test_retry_identity_uses_the_content_version_over_the_timestamp() -> None:
    versioned = _versioned_bundle("sha256:version-a")
    same_version_later_timestamp = _versioned_bundle(
        "sha256:version-a",
        "2026-07-19T05:20:00+00:00",
    )
    edited_same_timestamp = _versioned_bundle("sha256:version-b")
    retry = serialize_retry_bundle(versioned, _details())

    assert retry_matches_bundle(retry, versioned) is True
    assert retry_matches_bundle(retry, same_version_later_timestamp) is True
    assert retry_matches_bundle(retry, edited_same_timestamp) is False


def test_legacy_entries_without_a_content_version_still_match_on_timestamp() -> None:
    legacy = _bundle()
    retry = serialize_retry_bundle(legacy, _details())
    retry.pop("source_version")

    assert deserialize_retry_bundle(retry).source_version is None
    assert retry_matches_bundle(retry, legacy) is True
    assert retry_matches_bundle(retry, _bundle("2026-07-20T05:20:00+00:00")) is False


def test_content_version_is_bound_to_the_rendered_transcript() -> None:
    from src.connectors.whatsapp.connector import _Participant

    participants = [_Participant("6581111111@c.us", "6581111111", "Alice", "chat")]
    base = chat_source_version(
        chat_id="chat-1",
        chat_name="Customer",
        session_id="ses_1",
        msg_text="Customer: hello",
        participants=participants,
    )

    assert base == chat_source_version(
        chat_id="chat-1",
        chat_name="Customer",
        session_id="ses_1",
        msg_text="Customer: hello",
        participants=list(participants),
    )
    assert base != chat_source_version(
        chat_id="chat-1",
        chat_name="Customer",
        session_id="ses_1",
        msg_text="Customer: hello",
        participants=[],
    )
