from __future__ import annotations

import pytest
from src.connectors.whatsadmin_api.retry_queue import (
    deserialize_retry_bundle,
    retry_matches_bundle,
    serialize_retry_bundle,
)
from src.connectors.whatsapp.connector import _ChatBundle
from src.models import JsonValue


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
