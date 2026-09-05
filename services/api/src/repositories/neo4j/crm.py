"""Neo4j implementation of split CRM deal metrics and live scope resolution."""

from __future__ import annotations

from datetime import UTC, datetime

from src.display_format import format_display_date, format_display_datetime
from src.graph.client import get_session
from src.graph.converters import (
    GraphRecord,
    GraphValue,
    to_int,
    to_iso_or_none,
    to_optional_int,
    to_optional_str,
)
from src.graph.queries.crm import GET_PERSON_BITRIX_DEAL_SCOPE, GET_PERSON_CRM_DEAL_METRICS
from src.types_crm import (
    BitrixDealScope,
    CrmDealEntityBreakdown,
    CrmDealStageCount,
    PersonCrmDealMetrics,
)

from ._utils import record_to_dict


def _rows(value: GraphValue) -> list[GraphRecord]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _display_date(value: str | None) -> str | None:
    return format_display_date(value) if value is not None else None


def _daily_counts(row: GraphRecord, key: str) -> list[int]:
    raw = row.get(key)
    if not isinstance(raw, list):
        return [0] * 30
    counts = [to_int(value) for value in raw]
    return (counts + [0] * 30)[:30]


def _metrics(row: GraphRecord) -> PersonCrmDealMetrics:
    first_deal = to_iso_or_none(row.get("first_deal_at"))
    last_deal = to_iso_or_none(row.get("last_deal_at"))
    last_conversation = to_iso_or_none(row.get("last_conversation_at"))
    last_touch = to_iso_or_none(row.get("last_graph_crm_touch_at"))
    stages = [
        CrmDealStageCount(
            stage_id=to_optional_str(item.get("stage_id")), count=to_int(item.get("count"))
        )
        for item in _rows(row.get("deal_stage_breakdown"))
    ]
    entities = [
        CrmDealEntityBreakdown(
            entity_key=to_optional_str(item.get("entity_key")) or "",
            entity_display_name=to_optional_str(item.get("entity_display_name")),
            deal_count=to_int(item.get("deal_count")),
            conversation_count=to_int(item.get("conversation_count")),
        )
        for item in _rows(row.get("entity_breakdown"))
    ]
    prior_deals = to_int(row.get("prior_30d_deal_count"))
    recent_deals = to_int(row.get("recent_30d_deal_count"))
    prior_conversations = to_int(row.get("prior_30d_conversation_count"))
    recent_conversations = to_int(row.get("recent_30d_conversation_count"))
    return PersonCrmDealMetrics(
        deal_count=to_int(row.get("deal_count")),
        deal_stage_breakdown=stages,
        first_deal_at=first_deal,
        first_deal_at_display=_display_date(first_deal),
        last_deal_at=last_deal,
        last_deal_at_display=_display_date(last_deal),
        conversation_count=to_int(row.get("conversation_count")),
        last_conversation_at=last_conversation,
        last_conversation_at_display=_display_date(last_conversation),
        recent_30d_deal_count=recent_deals,
        recent_30d_conversation_count=recent_conversations,
        recent_30d_daily_deal_counts=_daily_counts(row, "deal_daily_counts"),
        recent_30d_daily_conversation_counts=_daily_counts(row, "conversation_daily_counts"),
        recent_30d_deal_change_pct=None
        if prior_deals == 0
        else round((recent_deals - prior_deals) * 100 / prior_deals),
        recent_30d_conversation_change_pct=None
        if prior_conversations == 0
        else round((recent_conversations - prior_conversations) * 100 / prior_conversations),
        last_graph_crm_touch_at=last_touch,
        last_graph_crm_touch_at_display=format_display_datetime(last_touch) if last_touch else None,
        days_since_last_deal=to_optional_int(row.get("days_since_last_deal")),
        entity_breakdown=entities,
    )


class Neo4jCrmDealMetricsRepository:
    async def get_person_crm_deal_metrics(self, person_id: str) -> PersonCrmDealMetrics | None:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_CRM_DEAL_METRICS,
                person_id=person_id,
                as_of_at=datetime.now(UTC).isoformat(),
            )
            record = await result.single()
        return (
            None
            if record is None
            else _metrics(record_to_dict(record.keys(), list(record.values())))
        )

    async def resolve_bitrix_deal_scope(
        self, person_id: str, source_instance: str, deal_limit: int
    ) -> BitrixDealScope | None:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_BITRIX_DEAL_SCOPE,
                person_id=person_id,
                source_instance=source_instance,
                deal_limit_plus_one=deal_limit + 1,
            )
            record = await result.single()
        if record is None:
            return None
        row = record_to_dict(record.keys(), list(record.values()))
        raw_ids = row.get("deal_ids")
        identifiers: set[str] = set()
        if isinstance(raw_ids, list):
            for value in raw_ids:
                identifier = to_optional_str(value)
                if identifier:
                    identifiers.add(identifier)
        ids = tuple(sorted(identifiers))
        return BitrixDealScope(
            canonical_person_id=to_optional_str(row.get("canonical_person_id")) or person_id,
            deal_ids=ids[:deal_limit],
            resolved_deal_count=len(ids),
            deal_limit_exhausted=len(ids) > deal_limit,
        )
