from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
import src.repositories.neo4j.crm as crm_module
from src.graph.converters import GraphRecord, GraphValue
from src.graph.queries.crm import GET_PERSON_CRM_METRICS
from src.repositories.neo4j.crm import Neo4jCrmMetricsRepository
from src.types_crm import (
    CrmActivityKindCount,
    CrmDealStageCount,
    CrmEntityBreakdown,
    PersonCrmMetrics,
)


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
    def __init__(self, record: _Record | None) -> None:
        self.record = record
        self.calls: list[tuple[str, dict[str, GraphValue]]] = []

    async def run(self, query: str, **parameters: GraphValue) -> _Result:
        self.calls.append((query, parameters))
        return _Result(self.record)


def _install_session(monkeypatch: pytest.MonkeyPatch, session: _Session) -> None:
    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[_Session]:
        yield session

    monkeypatch.setattr(crm_module, "get_session", fake_get_session)


def _stage(stage_id: str | None, count: int) -> GraphValue:
    value: GraphRecord = {"count": count}
    if stage_id is not None:
        value["stage_id"] = stage_id
    return value


def _kind(history_kind: str, count: int, last_event_at: str | None) -> GraphValue:
    value: GraphRecord = {"history_kind": history_kind, "count": count}
    if last_event_at is not None:
        value["last_event_at"] = last_event_at
    return value


def _entity(
    entity_key: str,
    display_name: str,
    deals: int,
    activities: int,
    conversations: int,
) -> GraphValue:
    return {
        "entity_key": entity_key,
        "entity_display_name": display_name,
        "deal_count": deals,
        "activity_count": activities,
        "conversation_count": conversations,
    }


def _metrics_record() -> _Record:
    values: GraphRecord = {
        "deal_count": 6,
        "deal_stage_breakdown": [_stage("won", 4), _stage("new", 2)],
        "first_deal_at": "2026-01-02T08:00:00+08:00",
        "last_deal_at": "2026-08-14T09:30:00+08:00",
        "activity_count": 47,
        "call_count": 18,
        "conversation_count": 5,
        "activity_kind_breakdown": [
            _kind("call", 18, "2026-08-14T10:00:00+08:00"),
            _kind("email", 12, "2026-08-12T10:00:00+08:00"),
        ],
        "first_activity_at": "2026-01-05T08:00:00+08:00",
        "last_activity_at": "2026-08-14T10:00:00+08:00",
        "entity_breakdown": [
            _entity("fundbox", "Fundbox", 6, 22, 3),
            _entity("eko", "Eko", 0, 10, 0),
        ],
        "recent_30d_deal_count": 2,
        "recent_30d_activity_count": 8,
        "recent_30d_call_count": 3,
        "recent_30d_conversation_count": 1,
        "last_crm_touch_at": "2026-08-14T10:00:00+08:00",
        "days_since_last_crm_touch": 5,
        "days_since_last_deal": 5,
        "days_since_last_activity": 5,
    }
    return _Record(values)


def test_crm_metrics_query_uses_isolated_record_subqueries() -> None:
    assert "MATCH (p:Person {person_id: $person_id})" in GET_PERSON_CRM_METRICS
    assert GET_PERSON_CRM_METRICS.count("OPTIONAL MATCH (sr:SourceRecord") == 7
    assert "datetime($as_of_at) AS as_of_at" in GET_PERSON_CRM_METRICS
    assert GET_PERSON_CRM_METRICS.count("CALL (person, as_of_at) {") == 4
    assert "duration('P30D')" in GET_PERSON_CRM_METRICS
    assert "sr_timestamp <= as_of_at" in GET_PERSON_CRM_METRICS
    assert "sr.record_type IN ['crm_deal', 'crm_history', 'conversation']" in (
        GET_PERSON_CRM_METRICS
    )


def test_crm_metrics_query_uses_the_deal_stage_projection_not_raw_payload() -> None:
    assert "sr.crm_deal_stage_id" in GET_PERSON_CRM_METRICS
    assert "sr.raw_payload.stage_id" not in GET_PERSON_CRM_METRICS


def test_crm_metrics_query_enforces_reader_authority_boundaries() -> None:
    assert GET_PERSON_CRM_METRICS.count("coalesce(link.is_active, true) = true") == 7
    assert (
        GET_PERSON_CRM_METRICS.count(
            "(sr.history_family IS NULL OR sr.history_family = 'activity')"
        )
        == 7
    )
    assert (
        GET_PERSON_CRM_METRICS.count(
            "(sr.lifecycle_status = 'active' "
            "OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))"
        )
        == 7
    )
    assert (
        GET_PERSON_CRM_METRICS.count(
            "MATCH (sr)-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})"
        )
        == 7
    )


def test_crm_metrics_query_limits_conversations_to_bitrix_open_lines() -> None:
    assert "record_type: 'conversation'" in GET_PERSON_CRM_METRICS
    assert "sr.record_type IN ['crm_deal', 'crm_history', 'conversation']" in (
        GET_PERSON_CRM_METRICS
    )


def test_crm_metrics_query_follows_merged_survivor() -> None:
    assert "OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)" in (GET_PERSON_CRM_METRICS)
    assert "WITH coalesce(canonical, p) AS person" in GET_PERSON_CRM_METRICS


@pytest.mark.anyio
async def test_repository_maps_all_crm_metrics_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(_metrics_record())
    _install_session(monkeypatch, session)
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setattr(crm_module, "_utc_now", lambda: datetime(2026, 8, 20, tzinfo=UTC))

    metrics = await Neo4jCrmMetricsRepository().get_person_crm_metrics("person-1")

    assert metrics == PersonCrmMetrics(
        deal_count=6,
        deal_stage_breakdown=[
            CrmDealStageCount(stage_id="won", count=4),
            CrmDealStageCount(stage_id="new", count=2),
        ],
        first_deal_at="2026-01-02T08:00:00+08:00",
        first_deal_at_display="02 Jan 2026",
        last_deal_at="2026-08-14T09:30:00+08:00",
        last_deal_at_display="14 Aug 2026",
        activity_count=47,
        call_count=18,
        conversation_count=5,
        activity_kind_breakdown=[
            CrmActivityKindCount(
                history_kind="call",
                count=18,
                last_event_at="2026-08-14T10:00:00+08:00",
                last_event_at_display="14 Aug 2026",
            ),
            CrmActivityKindCount(
                history_kind="email",
                count=12,
                last_event_at="2026-08-12T10:00:00+08:00",
                last_event_at_display="12 Aug 2026",
            ),
        ],
        first_activity_at="2026-01-05T08:00:00+08:00",
        first_activity_at_display="05 Jan 2026",
        last_activity_at="2026-08-14T10:00:00+08:00",
        last_activity_at_display="14 Aug 2026",
        entity_breakdown=[
            CrmEntityBreakdown(
                entity_key="fundbox",
                entity_display_name="Fundbox",
                deal_count=6,
                activity_count=22,
                conversation_count=3,
            ),
            CrmEntityBreakdown(
                entity_key="eko",
                entity_display_name="Eko",
                deal_count=0,
                activity_count=10,
                conversation_count=0,
            ),
        ],
        recent_30d_deal_count=2,
        recent_30d_activity_count=8,
        recent_30d_call_count=3,
        recent_30d_conversation_count=1,
        last_crm_touch_at="2026-08-14T10:00:00+08:00",
        last_crm_touch_at_display="14 Aug 2026, 02:00 AM",
        days_since_last_crm_touch=5,
        days_since_last_deal=5,
        days_since_last_activity=5,
    )
    assert session.calls == [
        (
            GET_PERSON_CRM_METRICS,
            {"person_id": "person-1", "as_of_at": "2026-08-20T00:00:00+00:00"},
        )
    ]


@pytest.mark.anyio
async def test_repository_maps_empty_person_to_zero_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: GraphRecord = {
        "deal_count": 0,
        "deal_stage_breakdown": [],
        "first_deal_at": None,
        "last_deal_at": None,
        "activity_count": 0,
        "call_count": 0,
        "conversation_count": 0,
        "activity_kind_breakdown": [],
        "first_activity_at": None,
        "last_activity_at": None,
        "entity_breakdown": [],
    }
    session = _Session(_Record(values))
    _install_session(monkeypatch, session)

    metrics = await Neo4jCrmMetricsRepository().get_person_crm_metrics("person-1")

    assert metrics == PersonCrmMetrics()


@pytest.mark.anyio
async def test_repository_maps_invalid_display_date_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: GraphRecord = {
        "deal_count": 1,
        "deal_stage_breakdown": [],
        "first_deal_at": "not-a-date",
        "last_deal_at": None,
        "activity_count": 0,
        "call_count": 0,
        "conversation_count": 0,
        "activity_kind_breakdown": [],
        "first_activity_at": None,
        "last_activity_at": None,
        "entity_breakdown": [],
    }
    session = _Session(_Record(values))
    _install_session(monkeypatch, session)

    metrics = await Neo4jCrmMetricsRepository().get_person_crm_metrics("person-1")

    assert metrics is not None
    assert metrics.first_deal_at == "not-a-date"
    assert metrics.first_deal_at_display is None


@pytest.mark.anyio
async def test_repository_discards_null_breakdown_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: GraphRecord = {
        "deal_count": 0,
        "deal_stage_breakdown": [None],
        "first_deal_at": None,
        "last_deal_at": None,
        "activity_count": 0,
        "call_count": 0,
        "conversation_count": 0,
        "activity_kind_breakdown": [None],
        "first_activity_at": None,
        "last_activity_at": None,
        "entity_breakdown": [None],
    }
    session = _Session(_Record(values))
    _install_session(monkeypatch, session)

    metrics = await Neo4jCrmMetricsRepository().get_person_crm_metrics("person-1")

    assert metrics == PersonCrmMetrics()


@pytest.mark.anyio
async def test_repository_returns_none_for_missing_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(None)
    _install_session(monkeypatch, session)

    assert await Neo4jCrmMetricsRepository().get_person_crm_metrics("missing") is None


@pytest.mark.anyio
async def test_repository_converts_temporal_bounds_to_iso_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: GraphRecord = {
        "deal_count": 1,
        "deal_stage_breakdown": [],
        "first_deal_at": datetime(2026, 1, 2, 8, 0, tzinfo=UTC),
        "last_deal_at": datetime(2026, 8, 14, 9, 30, tzinfo=UTC),
        "activity_count": 1,
        "call_count": 0,
        "conversation_count": 0,
        "activity_kind_breakdown": [
            {
                "history_kind": "call",
                "count": 1,
                "last_event_at": datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
            }
        ],
        "first_activity_at": datetime(2026, 1, 5, 8, 0, tzinfo=UTC),
        "last_activity_at": datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
        "entity_breakdown": [],
    }
    session = _Session(_Record(values))
    _install_session(monkeypatch, session)

    metrics = await Neo4jCrmMetricsRepository().get_person_crm_metrics("person-1")

    assert metrics is not None
    assert metrics.first_deal_at == "2026-01-02T08:00:00+00:00"
    assert metrics.first_deal_at_display == "02 Jan 2026"
    assert metrics.last_deal_at == "2026-08-14T09:30:00+00:00"
    assert metrics.last_deal_at_display == "14 Aug 2026"
    assert metrics.first_activity_at == "2026-01-05T08:00:00+00:00"
    assert metrics.first_activity_at_display == "05 Jan 2026"
    assert metrics.last_activity_at == "2026-08-14T10:00:00+00:00"
    assert metrics.last_activity_at_display == "14 Aug 2026"
    assert metrics.activity_kind_breakdown[0].last_event_at == ("2026-08-14T10:00:00+00:00")
    assert metrics.activity_kind_breakdown[0].last_event_at_display == "14 Aug 2026"
