"""Strict models for the WhatsAdmin HyperP extraction contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.types import JsonValue


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class Pagination(ContractModel):
    has_more: bool
    next_cursor: str | None = None
    prev_cursor: str | None = None
    total: int | None = None


class ResponseMeta(ContractModel):
    timestamp: datetime
    request_id: str
    pagination: Pagination
    snapshot_at: datetime | None = None


class SessionRow(ContractModel):
    id: str
    org_id: str
    org_name: str
    whatsapp_user_id: str
    expected_phone_number: str | None
    updated_at: datetime


class ParticipantRow(ContractModel):
    jid: str
    phone: str | None
    name: str | None
    role: str


class MessageRow(ContractModel):
    from_id: str | None
    to_id: str | None
    author_id: str | None
    body: str
    timestamp: datetime
    from_me: bool


class ChatBundle(ContractModel):
    chat_id: str
    chat_name: str
    session_id: str
    whatsapp_user_id: str
    changed_at: datetime
    participants: list[ParticipantRow]
    messages: list[MessageRow]


class SessionPage(ContractModel):
    success: bool
    data: list[SessionRow]
    meta: ResponseMeta


class ChatPage(ContractModel):
    success: bool
    data: list[ChatBundle]
    meta: ResponseMeta


class SessionQueryRequest(ContractModel):
    """One bounded session page request."""

    limit: int
    cursor: str | None = None

    def to_payload(self) -> dict[str, str | int]:
        return _request_payload(self)


class ChatQueryRequest(ContractModel):
    """One bounded chat page request bound to a requested-as-of snapshot."""

    session_id: str
    limit: int
    changed_since: str | None = None
    snapshot_at: str | None = None
    cursor: str | None = None

    def to_payload(self) -> dict[str, str | int]:
        return _request_payload(self)


class DurableModel(BaseModel):
    """Base for bounded adapter state persisted in graph checkpoint entries."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StoredSession(DurableModel):
    """One queued session of a durable session page."""

    session_id: str
    whatsapp_user_id: str
    expected_phone_number: str | None = None


class StoredParticipant(DurableModel):
    jid: str
    phone: str | None = None
    name: str | None = None
    role: str


class StoredBundle(DurableModel):
    """One durably stored chat version awaiting extraction."""

    chat_id: str
    chat_name: str
    session_id: str
    whatsapp_user_id: str
    tenant: str
    msg_text: str
    observed_at: str
    source_version: str
    participants: list[StoredParticipant]
    message_endpoints: list[JsonValue]
    session_phone: str | None = None
    source_id_scope: str | None = None


class StoredChatPage(DurableModel):
    """One bounded chat page durably stored before any extraction."""

    session_id: str
    whatsapp_user_id: str
    bundles: list[StoredBundle]


class PreparedChat(DurableModel):
    """Bounded prepared envelopes for one extracted chat version."""

    chat_id: str
    source_version: str
    outcome: Literal["extracted"]
    envelopes: list[dict[str, JsonValue]]


def _request_payload(model: ContractModel) -> dict[str, str | int]:
    dump = model.model_dump(by_alias=True, exclude_none=True, mode="json")
    payload: dict[str, str | int] = {}
    for key, value in dump.items():
        if isinstance(value, str | int) and not isinstance(value, bool):
            payload[str(key)] = value
    return payload


CapabilityName = Literal[
    "resumable_session_enumeration",
    "requested_as_of_snapshot_binding",
    "cursor_retention_window",
    "explicit_expiry_and_drift_recovery",
    "bounded_chat_message_continuation",
    "old_message_edit_delete_tombstones",
    "participant_change_tombstones",
    "chat_session_removal_tombstones",
    "removed_versus_unavailable_distinction",
]

#: Every upstream guarantee the bounded adapter requires before activation.
REQUIRED_CAPABILITIES: tuple[CapabilityName, ...] = (
    "resumable_session_enumeration",
    "requested_as_of_snapshot_binding",
    "cursor_retention_window",
    "explicit_expiry_and_drift_recovery",
    "bounded_chat_message_continuation",
    "old_message_edit_delete_tombstones",
    "participant_change_tombstones",
    "chat_session_removal_tombstones",
    "removed_versus_unavailable_distinction",
)


class CapabilityGuarantee(BaseModel):
    """One required upstream behaviour and the deployed evidence for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: CapabilityName
    proven: bool
    evidence: str = ""


class UpstreamCapability(BaseModel):
    """Observed versus required WhatsAdmin extraction capabilities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str
    cursor_retention_days: int
    guarantees: tuple[CapabilityGuarantee, ...]

    def missing(self) -> tuple[str, ...]:
        """Return every required guarantee that deployed evidence has not proven."""
        proven = {item.name for item in self.guarantees if item.proven}
        return tuple(name for name in REQUIRED_CAPABILITIES if name not in proven)
