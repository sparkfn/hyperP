"""Typed, append-only CRM history contract shared by activity and stage evidence.

Stage traversal remains unsupported.  This module defines the storage contract
only; it does not authorize a connector, checkpoint, SourceRecord, or graph
write for stage-history observations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum

from src.connectors.bitrix_stage_history.models import StageHistoryItem
from src.models import SourceRecordEnvelope

HISTORY_PROJECTION_VERSION = "crm-history-projection-v1"
HISTORY_PROJECTION_SOURCE = "hyperp"


class CrmHistoryFamily(StrEnum):
    """Closed families admitted by CRM-history readers."""

    ACTIVITY = "activity"
    STAGE = "stage"


@dataclass(frozen=True)
class CrmHistoryProperties:
    """Schema-visible history properties persisted on a ``SourceRecord``."""

    history_family: CrmHistoryFamily
    history_kind: str
    history_source: str
    event_category_id: str | None
    event_stage_id: str | None
    event_stage_semantic_id: str | None
    event_at: str | None
    history_projection_version: str
    history_projection_source: str


def generic_activity_properties(envelope: SourceRecordEnvelope) -> CrmHistoryProperties:
    """Return the fixed projection for pre-existing generic CRM activities."""
    return CrmHistoryProperties(
        history_family=CrmHistoryFamily.ACTIVITY,
        history_kind="generic_activity",
        history_source=envelope.source_system,
        event_category_id=None,
        event_stage_id=None,
        event_stage_semantic_id=None,
        event_at=envelope.observed_at,
        history_projection_version=HISTORY_PROJECTION_VERSION,
        history_projection_source=HISTORY_PROJECTION_SOURCE,
    )


def stage_history_properties(item: StageHistoryItem) -> CrmHistoryProperties:
    """Return the immutable typed projection for one stage-history observation."""
    return CrmHistoryProperties(
        history_family=CrmHistoryFamily.STAGE,
        history_kind=item.type_id or "stage_transition",
        history_source="bitrix_chat",
        event_category_id=item.category_id,
        event_stage_id=item.stage_id,
        event_stage_semantic_id=item.stage_semantic_id,
        event_at=item.created_time.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        history_projection_version=HISTORY_PROJECTION_VERSION,
        history_projection_source=HISTORY_PROJECTION_SOURCE,
    )


def activity_reader_predicate(alias: str = "history") -> str:
    """Return the safe mixed-state predicate for legacy and typed activities.

    Unknown/future families are intentionally excluded.  Do not replace this
    with ``<> 'stage'``: that would accidentally admit malformed source data.
    """
    return f"({alias}.history_family IS NULL OR {alias}.history_family = 'activity')"
