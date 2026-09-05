from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
import src.repositories.neo4j.crm as crm_module
from src.graph.converters import GraphRecord, GraphValue
from src.graph.queries.crm import GET_PERSON_BITRIX_DEAL_SCOPE, GET_PERSON_CRM_DEAL_METRICS
from src.repositories.neo4j.crm import Neo4jCrmDealMetricsRepository
from src.types_crm import CrmDealEntityBreakdown, CrmDealStageCount, PersonCrmDealMetrics


class _Record:
    def __init__(self, values: GraphRecord) -> None:
        self._values = values

    def keys(self) -> list[str]:
        return list(self._values)

    def values(self) -> list[GraphValue]:
        return list(self._values.values())


class _Result:
    def __init__(self, record: _Record | None) -> None:
        self._record = record

    async def single(self) -> _Record | None:
        return self._record


class _Session:
    def __init__(self, records: list[_Record | None]) -> None:
        self.records = iter(records)
        self.calls: list[tuple[str, dict[str, GraphValue]]] = []

    async def run(self, query: str, **parameters: GraphValue) -> _Result:
        self.calls.append((query, parameters))
        return _Result(next(self.records))


def _install(monkeypatch: pytest.MonkeyPatch, session: _Session) -> None:
    @asynccontextmanager
    async def fake_session() -> AsyncIterator[_Session]:
        yield session

    monkeypatch.setattr(crm_module, "get_session", fake_session)


def test_deal_query_preserves_graph_authority_and_counts_distinct_records() -> None:
    assert "record_type: 'crm_history'" not in GET_PERSON_CRM_DEAL_METRICS
    assert "record_type: 'call'" not in GET_PERSON_CRM_DEAL_METRICS
    assert GET_PERSON_CRM_DEAL_METRICS.count("collect(DISTINCT sr) AS records") == 2
    assert "collect(DISTINCT sr.observed_at)" not in GET_PERSON_CRM_DEAL_METRICS
    assert "coalesce(record_entity, source_entity)" in GET_PERSON_CRM_DEAL_METRICS
    assert "OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)" in GET_PERSON_CRM_DEAL_METRICS
    for predicate in (
        "coalesce(link.is_active, true) = true",
        "sr.lifecycle_status = 'active'",
        "sr.lifecycle_status IS NULL AND sr.is_latest = true",
        "source_key: 'bitrix_chat'",
    ):
        assert predicate in GET_PERSON_CRM_DEAL_METRICS


def test_scope_is_effective_active_bounded_instance_scoped_and_uses_portal_deal_id() -> None:
    assert "sr.source_instance_id = $source_instance" in GET_PERSON_BITRIX_DEAL_SCOPE
    assert "LIMIT $deal_limit_plus_one" in GET_PERSON_BITRIX_DEAL_SCOPE
    assert "coalesce(link.is_active, true) = true" in GET_PERSON_BITRIX_DEAL_SCOPE
    assert "sr.source_entity_type = 'deal'" in GET_PERSON_BITRIX_DEAL_SCOPE
    assert "sr.source_entity_id IS NOT NULL" in GET_PERSON_BITRIX_DEAL_SCOPE
    assert "RETURN DISTINCT sr.source_entity_id AS deal_id" in GET_PERSON_BITRIX_DEAL_SCOPE
    assert "coalesce(canonical, p) AS person" in GET_PERSON_BITRIX_DEAL_SCOPE


@pytest.mark.anyio
async def test_repository_maps_deal_metrics_without_activity_compatibility_zeros(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row: GraphRecord = {
        "deal_count": 3,
        "deal_stage_breakdown": [{"stage_id": "WON", "count": 2}, None],
        "first_deal_at": datetime(2026, 1, 2, tzinfo=UTC),
        "last_deal_at": "2026-09-01T00:00:00+00:00",
        "conversation_count": 1,
        "last_conversation_at": "2026-09-02T00:00:00+00:00",
        "recent_30d_deal_count": 2,
        "prior_30d_deal_count": 4,
        "recent_30d_conversation_count": 1,
        "prior_30d_conversation_count": 0,
        "deal_daily_counts": [1, 2],
        "conversation_daily_counts": list(range(40)),
        "last_graph_crm_touch_at": "2026-09-02T00:00:00+00:00",
        "days_since_last_deal": 4,
        "entity_breakdown": [
            {
                "entity_key": "fundbox",
                "entity_display_name": "Fundbox",
                "deal_count": 3,
                "conversation_count": 1,
            },
            None,
        ],
    }
    session = _Session([_Record(row)])
    _install(monkeypatch, session)

    metrics = await Neo4jCrmDealMetricsRepository().get_person_crm_deal_metrics("person-1")

    assert metrics == PersonCrmDealMetrics(
        deal_count=3,
        deal_stage_breakdown=[CrmDealStageCount(stage_id="WON", count=2)],
        first_deal_at="2026-01-02T00:00:00+00:00",
        first_deal_at_display="02 Jan 2026",
        last_deal_at="2026-09-01T00:00:00+00:00",
        last_deal_at_display="01 Sep 2026",
        conversation_count=1,
        last_conversation_at="2026-09-02T00:00:00+00:00",
        last_conversation_at_display="02 Sep 2026",
        recent_30d_deal_count=2,
        recent_30d_conversation_count=1,
        recent_30d_daily_deal_counts=[1, 2] + [0] * 28,
        recent_30d_daily_conversation_counts=list(range(30)),
        recent_30d_deal_change_pct=-50,
        recent_30d_conversation_change_pct=None,
        last_graph_crm_touch_at="2026-09-02T00:00:00+00:00",
        last_graph_crm_touch_at_display="02 Sep 2026, 12:00 AM",
        days_since_last_deal=4,
        entity_breakdown=[
            CrmDealEntityBreakdown(
                entity_key="fundbox",
                entity_display_name="Fundbox",
                deal_count=3,
                conversation_count=1,
            )
        ],
    )
    assert session.calls[0][0] == GET_PERSON_CRM_DEAL_METRICS
    assert session.calls[0][1]["person_id"] == "person-1"


@pytest.mark.anyio
async def test_repository_returns_none_when_person_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([None])
    _install(monkeypatch, session)

    assert await Neo4jCrmDealMetricsRepository().get_person_crm_deal_metrics("missing") is None


@pytest.mark.anyio
async def test_scope_dedupes_sorts_caps_and_reports_extra_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row: GraphRecord = {
        "canonical_person_id": "canonical-1",
        "deal_ids": ["20", "10", "10", "30"],
    }
    session = _Session([_Record(row)])
    _install(monkeypatch, session)

    scope = await Neo4jCrmDealMetricsRepository().resolve_bitrix_deal_scope(
        "absorbed-1", "bitrix-primary", 2
    )

    assert scope is not None
    assert scope.canonical_person_id == "canonical-1"
    assert scope.deal_ids == ("10", "20")
    assert scope.resolved_deal_count == 3
    assert scope.deal_limit_exhausted is True
    assert session.calls == [
        (
            GET_PERSON_BITRIX_DEAL_SCOPE,
            {
                "person_id": "absorbed-1",
                "source_instance": "bitrix-primary",
                "deal_limit_plus_one": 3,
            },
        )
    ]
