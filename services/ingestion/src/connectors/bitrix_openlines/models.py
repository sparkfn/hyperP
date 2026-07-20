"""Typed values returned by the Bitrix Open Lines REST boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class OpenLineConfig:
    id: str
    line_name: str


@dataclass(frozen=True)
class ChatReference:
    chat_id: int
    changed_at: datetime | None
    discovery: str


@dataclass(frozen=True)
class DialogMetadata:
    chat_id: int
    config_id: str
    connector_id: str


@dataclass(frozen=True)
class OpenLineMessage:
    id: int
    author_id: int
    author_name: str
    text: str
    date: datetime
    is_agent: bool
