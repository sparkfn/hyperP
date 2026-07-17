"""Strict models for the WhatsAdmin HyperP extraction contract."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


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
