"""Static weekly API-ingestion group definitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]


@dataclass(frozen=True)
class ScheduledIngestionSpec:
    """One API ingestion request in a scheduled group."""

    source_key: str
    entity_key: str | None = None


@dataclass(frozen=True)
class ScheduledIngestionGroup:
    """A weekly, source-ordered API ingestion chain."""

    key: str
    weekday: Weekday
    tasks: tuple[ScheduledIngestionSpec, ...]


SCHEDULED_INGESTION_GROUPS: tuple[ScheduledIngestionGroup, ...] = (
    ScheduledIngestionGroup(
        key="fundbox",
        weekday="monday",
        tasks=(
            ScheduledIngestionSpec("fundbox"),
            ScheduledIngestionSpec("fundbox:contacts"),
            ScheduledIngestionSpec("fundbox:sales"),
        ),
    ),
    ScheduledIngestionGroup(
        key="eko",
        weekday="tuesday",
        tasks=(
            ScheduledIngestionSpec("eko_phppos"),
            ScheduledIngestionSpec("eko_phppos:sales"),
            ScheduledIngestionSpec("whatsapp_chat", entity_key="eko"),
        ),
    ),
    ScheduledIngestionGroup(
        key="speedzone",
        weekday="wednesday",
        tasks=(
            ScheduledIngestionSpec("speedzone_phppos"),
            ScheduledIngestionSpec("speedzone_phppos:sales"),
            ScheduledIngestionSpec("whatsapp_chat", entity_key="speedzone"),
        ),
    ),
    ScheduledIngestionGroup(
        key="bitrix_chat",
        weekday="thursday",
        tasks=(ScheduledIngestionSpec("bitrix_chat"),),
    ),
    ScheduledIngestionGroup(
        key="sgbankruptcy",
        weekday="friday",
        tasks=(ScheduledIngestionSpec("sgbankruptcy"),),
    ),
    ScheduledIngestionGroup(
        key="sgrentalflats",
        weekday="saturday",
        tasks=(ScheduledIngestionSpec("sgrentalflats"),),
    ),
)

_GROUPS_BY_KEY = {group.key: group for group in SCHEDULED_INGESTION_GROUPS}


def scheduled_ingestion_group(key: str) -> ScheduledIngestionGroup:
    """Return one configured group or raise a clear error."""
    try:
        return _GROUPS_BY_KEY[key]
    except KeyError as exc:
        raise ValueError(f"Unknown scheduled ingestion group {key!r}") from exc
