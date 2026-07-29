"""Typed serialization for durable WhatsAdmin extraction retry bundles."""

from __future__ import annotations

from src.connectors.whatsadmin_api.credentials import WhatsAdminEntity
from src.connectors.whatsapp.connector import _ChatBundle, _Participant
from src.models import JsonValue


def bundle_entity_key(bundle: _ChatBundle) -> WhatsAdminEntity:
    """Validate and narrow a shared chat bundle to a WhatsAdmin entity."""
    if bundle.tenant == "eko":
        return "eko"
    if bundle.tenant == "speedzone":
        return "speedzone"
    raise RuntimeError("WhatsAdmin retry bundle has invalid entity key")


def serialize_retry_bundle(
    bundle: _ChatBundle,
    details: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Serialize the minimum source bundle needed for an independent retry."""
    entity_key = bundle_entity_key(bundle)
    return {
        **details,
        "entity_key": entity_key,
        "chat_name": bundle.chat_name,
        "whatsapp_user_id": bundle.whatsapp_user_id,
        "msg_text": bundle.msg_text,
        "participants": [
            {
                "jid": participant.jid,
                "phone": participant.phone,
                "name": participant.name,
                "role": participant.role,
            }
            for participant in bundle.participants
        ],
        "message_endpoints": bundle.message_endpoints,
        "session_phone": bundle.session_phone,
        "source_id_scope": bundle.source_id_scope,
    }


def deserialize_retry_bundle(retry: dict[str, JsonValue]) -> _ChatBundle:
    """Validate a Redis retry payload and rebuild its shared chat bundle."""
    participants = _retry_participants(retry.get("participants"))
    required = (
        "chat_id",
        "chat_name",
        "session_id",
        "whatsapp_user_id",
        "msg_text",
        "observed_at",
        "entity_key",
    )
    if not all(isinstance(retry.get(key), str) for key in required):
        raise RuntimeError("WhatsAdmin extraction retry omitted bundle fields")
    endpoints = retry.get("message_endpoints")
    if not isinstance(endpoints, list):
        raise RuntimeError("WhatsAdmin extraction retry omitted message endpoints")
    retry_entity = _retry_entity(retry["entity_key"])
    session_phone = retry.get("session_phone")
    source_id_scope = retry.get("source_id_scope")
    return _ChatBundle(
        chat_id=str(retry["chat_id"]),
        chat_name=str(retry["chat_name"]),
        session_id=str(retry["session_id"]),
        whatsapp_user_id=str(retry["whatsapp_user_id"]),
        tenant=retry_entity,
        msg_text=str(retry["msg_text"]),
        observed_at=str(retry["observed_at"]),
        participants=participants,
        message_endpoints=endpoints,
        session_phone=session_phone if isinstance(session_phone, str) else None,
        source_id_scope=source_id_scope if isinstance(source_id_scope, str) else None,
    )


def _retry_participants(raw: JsonValue | None) -> list[_Participant]:
    if not isinstance(raw, list):
        raise RuntimeError("WhatsAdmin extraction retry omitted participants")
    participants: list[_Participant] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("WhatsAdmin extraction retry has invalid participant")
        jid = item.get("jid")
        role = item.get("role")
        phone = item.get("phone")
        name = item.get("name")
        if not isinstance(jid, str) or not isinstance(role, str):
            raise RuntimeError("WhatsAdmin extraction retry has invalid participant")
        participants.append(
            _Participant(
                jid,
                phone if isinstance(phone, str) else None,
                name if isinstance(name, str) else None,
                role,
            )
        )
    return participants


def _retry_entity(raw: JsonValue) -> WhatsAdminEntity:
    if raw == "eko":
        return "eko"
    if raw == "speedzone":
        return "speedzone"
    raise RuntimeError("WhatsAdmin extraction retry has invalid entity key")


def retry_matches_bundle(retry: dict[str, JsonValue], bundle: _ChatBundle) -> bool:
    """Return whether a queued entry is the same immutable chat version."""
    return retry.get("chat_id") == bundle.chat_id and retry.get("observed_at") == bundle.observed_at
