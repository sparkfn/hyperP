"""Neo4j implementation of CrmMetricsRepository."""

from __future__ import annotations

from src.display_format import format_display_date
from src.graph.client import get_session
from src.graph.converters import (
    GraphRecord,
    GraphValue,
    to_int,
    to_iso_or_none,
    to_optional_str,
)
from src.graph.queries.crm import GET_PERSON_CRM_METRICS
from src.types_crm import (
    CrmActivityKindCount,
    CrmDealStageCount,
    CrmEntityBreakdown,
    PersonCrmMetrics,
)

from ._utils import record_to_dict


def _as_dicts(rows: GraphValue) -> list[GraphRecord]:
    """Return map rows from a Neo4j list while discarding null sentinels."""
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _to_kind_count(row: GraphRecord) -> CrmActivityKindCount:
    last_event_at = to_iso_or_none(row.get("last_event_at"))
    return CrmActivityKindCount(
        history_kind=to_optional_str(row.get("history_kind")) or "unknown",
        count=to_int(row.get("count")),
        last_event_at=last_event_at,
        last_event_at_display=_display_or_none(last_event_at),
    )


def _to_stage_count(row: GraphRecord) -> CrmDealStageCount:
    return CrmDealStageCount(
        stage_id=to_optional_str(row.get("stage_id")),
        count=to_int(row.get("count")),
    )


def _to_entity_breakdown(row: GraphRecord) -> CrmEntityBreakdown:
    return CrmEntityBreakdown(
        entity_key=to_optional_str(row.get("entity_key")) or "",
        entity_display_name=to_optional_str(row.get("entity_display_name")),
        deal_count=to_int(row.get("deal_count")),
        activity_count=to_int(row.get("activity_count")),
        conversation_count=to_int(row.get("conversation_count")),
    )


def _display_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    return format_display_date(value) or None


def _to_metrics(row: GraphRecord) -> PersonCrmMetrics:
    first_deal_at = to_iso_or_none(row.get("first_deal_at"))
    last_deal_at = to_iso_or_none(row.get("last_deal_at"))
    first_activity_at = to_iso_or_none(row.get("first_activity_at"))
    last_activity_at = to_iso_or_none(row.get("last_activity_at"))
    return PersonCrmMetrics(
        deal_count=to_int(row.get("deal_count")),
        deal_stage_breakdown=[
            _to_stage_count(stage) for stage in _as_dicts(row.get("deal_stage_breakdown"))
        ],
        first_deal_at=first_deal_at,
        first_deal_at_display=_display_or_none(first_deal_at),
        last_deal_at=last_deal_at,
        last_deal_at_display=_display_or_none(last_deal_at),
        activity_count=to_int(row.get("activity_count")),
        call_count=to_int(row.get("call_count")),
        conversation_count=to_int(row.get("conversation_count")),
        activity_kind_breakdown=[
            _to_kind_count(kind) for kind in _as_dicts(row.get("activity_kind_breakdown"))
        ],
        first_activity_at=first_activity_at,
        first_activity_at_display=_display_or_none(first_activity_at),
        last_activity_at=last_activity_at,
        last_activity_at_display=_display_or_none(last_activity_at),
        entity_breakdown=[
            _to_entity_breakdown(entity) for entity in _as_dicts(row.get("entity_breakdown"))
        ],
    )


class Neo4jCrmMetricsRepository:
    async def get_person_crm_metrics(self, person_id: str) -> PersonCrmMetrics | None:
        async with get_session() as session:
            result = await session.run(GET_PERSON_CRM_METRICS, person_id=person_id)
            record = await result.single()
        if record is None:
            return None
        row = record_to_dict(record.keys(), list(record.values()))
        return _to_metrics(row)
