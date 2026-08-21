"""Contract and operator tests for the Person CRM deal-count projection."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

import pytest
from neo4j import ManagedTransaction
from src import crm_deal_count_control as control
from src.graph.client import Neo4jClient
from src.graph.crm_deal_count import CrmDealCountInvariant, _index_online
from src.graph.queries.crm_deal_count import (
    BACKFILL_CRM_DEAL_COUNTS_BATCH,
    CRM_DEAL_COUNT_INDEX_CYPHER,
    CRM_DEAL_COUNT_INDEX_NAME,
    CRM_DEAL_COUNT_INVARIANT_COUNTS,
    RECOMPUTE_PERSON_CRM_DEAL_COUNTS,
    RECOMPUTE_SOURCE_PERSON_CRM_DEAL_COUNTS,
)
from src.graph.queries.persons import CREATE_PERSON
from src.graph.schema_init import BASE_LIFECYCLE_CONSTRAINTS, _find_init_cypher, _split_statements


def test_projection_queries_preserve_the_prior_authoritative_count_contract() -> None:
    for query in (
        RECOMPUTE_PERSON_CRM_DEAL_COUNTS,
        RECOMPUTE_SOURCE_PERSON_CRM_DEAL_COUNTS,
        BACKFILL_CRM_DEAL_COUNTS_BATCH,
        CRM_DEAL_COUNT_INVARIANT_COUNTS,
    ):
        assert "record_type: 'crm_deal'" in query
        assert "source_key: 'bitrix_chat'" in query
        assert "history_family IS NULL OR deal.history_family = 'activity'" in query
        assert "deal.lifecycle_status = 'active'" in query
        assert "deal.lifecycle_status IS NULL AND deal.is_latest = true" in query
        assert "count(DISTINCT deal)" in query


def test_projection_writers_lock_people_in_sorted_order_before_counting() -> None:
    for query in (
        RECOMPUTE_PERSON_CRM_DEAL_COUNTS,
        RECOMPUTE_SOURCE_PERSON_CRM_DEAL_COUNTS,
        BACKFILL_CRM_DEAL_COUNTS_BATCH,
    ):
        order_position = query.index("ORDER BY person.person_id")
        lock_position = query.index("SET person.crm_deal_count_lock_version")
        count_position = query.index("count(DISTINCT deal)")
        assert order_position < lock_position < count_position


def test_new_people_start_with_zero_and_schema_declares_index_once() -> None:
    assert "crm_deal_count: 0" in CREATE_PERSON
    statements = [
        *_split_statements(_find_init_cypher().read_text(encoding="utf-8")),
        *BASE_LIFECYCLE_CONSTRAINTS,
    ]
    normalized = [" ".join(statement.split()) for statement in statements]
    expected = " ".join(CRM_DEAL_COUNT_INDEX_CYPHER.split())
    assert normalized.count(expected) == 1


class _IndexResult:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row


class _IndexTx:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def run(self, query: str, **params: object) -> _IndexResult:
        assert "SHOW INDEXES" in query
        assert params == {"index_name": CRM_DEAL_COUNT_INDEX_NAME}
        return _IndexResult(self._row)


class _IndexClient:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._tx = _IndexTx(row)

    def execute_read(self, work: Callable[[ManagedTransaction], bool]) -> bool:
        return work(cast(ManagedTransaction, self._tx))


def test_index_check_requires_exact_online_range_metadata() -> None:
    expected = {
        "type": "RANGE",
        "entityType": "NODE",
        "labelsOrTypes": ["Person"],
        "properties": ["crm_deal_count"],
        "state": "ONLINE",
        "failureMessage": "",
    }
    assert _index_online(cast(Neo4jClient, _IndexClient(expected))) is True
    assert (
        _index_online(cast(Neo4jClient, _IndexClient({**expected, "properties": ["updated_at"]})))
        is False
    )


class _ControlClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_control_check_fails_closed_on_projection_drift(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _ControlClient()
    monkeypatch.setattr(control, "get_settings", lambda: object())
    monkeypatch.setattr(control, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(
        control,
        "inspect_crm_deal_count_invariant",
        lambda _client: CrmDealCountInvariant(1, 2, False),
    )

    assert control.run(["check"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "command": "check",
        "drifted_person_count": 2,
        "index_online": False,
        "invalid_person_count": 1,
        "status": "invariant_failed",
    }
    assert client.closed is True


def test_control_backfill_reports_updates_after_verification(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _ControlClient()
    monkeypatch.setattr(control, "get_settings", lambda: object())
    monkeypatch.setattr(control, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(
        control,
        "repair_crm_deal_counts",
        lambda _client, *, batch_size: 7,
    )
    monkeypatch.setattr(
        control,
        "inspect_crm_deal_count_invariant",
        lambda _client: CrmDealCountInvariant(0, 0, True),
    )

    assert control.run(["backfill", "--batch-size", "25"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "command": "backfill",
        "drifted_person_count": 0,
        "index_online": True,
        "invalid_person_count": 0,
        "status": "ok",
        "updated_person_count": 7,
    }
    assert client.closed is True


def test_control_operational_failure_is_generic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(control, "get_settings", lambda: (_ for _ in ()).throw(RuntimeError()))

    assert control.run(["check"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "operational_error"}
    assert "RuntimeError" in captured.err
