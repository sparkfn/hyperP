"""Scripted-query tests for the sales evidence repository (issue #125)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from src.graph.queries.sales_prediction import (
    SALES_PREDICTION_DEAL_VERSIONS_FOR_PARENTS,
    SALES_PREDICTION_RELEASE,
    SALES_PREDICTION_STAGE_EVENTS_PAGE,
)
from src.sales_prediction.models import SalesEvidence
from src.sales_prediction.repository import (
    _PAGE_SIZE,
    Neo4jSalesEvidenceRepository,
    SalesParameter,
    SalesRow,
)

MAPPING = "crm-stage-map-2026-08-18-v1"
POLICY = "crm-stage-lifecycle-policy-2026-08-18-v1"


class _Record:
    def __init__(self, values: SalesRow) -> None:
        self._values = values

    def keys(self) -> list[str]:
        return list(self._values)

    def values(self) -> list[object]:
        return list(self._values.values())


class _Result:
    def __init__(self, rows: list[_Record]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[_Record]:
        return iter(self._rows)


class _Session:
    def __init__(self, client: _Client) -> None:
        self._client = client

    def run(self, query: str, parameters: dict[str, SalesParameter]) -> _Result:
        self._client.calls.append((query, dict(parameters)))
        queue = self._client.responses.get(query)
        if queue is None or not queue:
            raise AssertionError("unexpected sales evidence repository query")
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return _Result(response)


class _Client:
    def __init__(self, responses: dict[str, list[list[_Record] | Exception]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, SalesParameter]]] = []

    @contextmanager
    def session(self) -> Iterator[_Session]:
        yield _Session(self)


def _release_row(**overrides: object) -> _Record:
    values: SalesRow = {
        "enabled": True,
        "mapping_version": MAPPING,
        "policy_version": POLICY,
        "accepted_at": "2026-08-18T10:00:00Z",
        "boundary_bound": True,
        "reconciliation_bound": True,
        "mapping_bound": True,
        "projection_count": 12,
        "distinct_projection_count": 12,
        "invalid_projection_timestamp_count": 0,
        "restated_event_count": 0,
        "wrong_mapping_count": 0,
        "wrong_policy_count": 0,
        "max_event_at": "2026-07-20T00:00:00Z",
        "max_available_at": "2026-08-01T00:00:00Z",
    }
    values.update(overrides)
    return _Record(values)


def _stage_row(identity: str, deal: str, state: str = "open") -> _Record:
    return _Record(
        {
            "event_identity": identity,
            "parent_source_system": "bitrix_chat",
            "parent_source_record_id": deal,
            "mapped_state": state,
            "category_id": "5",
            "stage_id": "C5:NEW",
            "source_semantic": "S",
            "event_at": "2026-01-10T08:00:00Z",
            "available_at": "2026-01-10T08:05:00Z",
            "authority_head_version": 1,
        }
    )


def _deal_row(deal: str, raw_payload: str = '{"amount": "10"}') -> _Record:
    return _Record(
        {
            "parent_source_system": "bitrix_chat",
            "parent_source_record_id": deal,
            "version_key": "4:abc:1",
            "source_record_version": 1,
            "entity_key": "eko",
            "observed_at": "2026-01-05T00:00:00Z",
            "ingested_at": "2026-01-05T00:01:00Z",
            "activated_at": "2026-01-05T00:01:00Z",
            "superseded_at": None,
            "rejected_at": None,
            "link_failed_at": None,
            "raw_payload": raw_payload,
            "lifecycle_status": "active",
            "linked_person_ids": ["p1"],
            "active_person_ids": ["p1"],
            "linked_person_count": 1,
            "active_person_count": 1,
            "latest_linked_at": "2026-01-06T00:00:00Z",
        }
    )


def _happy_responses() -> dict[str, list[list[_Record] | Exception]]:
    return {
        SALES_PREDICTION_RELEASE: [
            [_release_row()],
            [_release_row()],
        ],
        SALES_PREDICTION_STAGE_EVENTS_PAGE: [
            [_stage_row("evt-1", "deal-1"), _stage_row("evt-2", "deal-1", "won")],
        ],
        SALES_PREDICTION_DEAL_VERSIONS_FOR_PARENTS: [
            [_deal_row("deal-1")],
        ],
    }


def _load(client: _Client) -> SalesEvidence:
    repository = Neo4jSalesEvidenceRepository(client)
    return repository.load_evidence(
        expected_mapping_version=MAPPING, expected_policy_version=POLICY
    )


def test_load_evidence_parses_release_events_and_versions() -> None:
    client = _Client(_happy_responses())
    evidence = _load(client)
    assert evidence.release.mapping_version == MAPPING
    assert evidence.release.source_accounting_complete is True
    assert evidence.release.analytical_release_consistent is True
    assert len(evidence.events) == 2
    assert evidence.events[0].stage_id == "C5:NEW"
    assert evidence.events[1].mapped_state == "won"
    assert len(evidence.versions) == 1
    assert evidence.versions[0].amount_state == "known"


def test_load_evidence_rejects_version_mismatch() -> None:
    responses = _happy_responses()
    responses[SALES_PREDICTION_RELEASE] = [
        [_release_row(mapping_version="crm-stage-map-2025-01-01-v1")],
    ]
    with pytest.raises(ValueError, match="does not match sales dataset inputs"):
        _load(_Client(responses))


def test_load_evidence_rejects_release_change_during_read() -> None:
    responses = _happy_responses()
    responses[SALES_PREDICTION_RELEASE] = [
        [_release_row()],
        [_release_row(restated_event_count=5)],
    ]
    with pytest.raises(ValueError, match="release changed during"):
        _load(_Client(responses))


def test_load_evidence_rejects_deal_version_change_during_read() -> None:
    responses = _happy_responses()
    responses[SALES_PREDICTION_STAGE_EVENTS_PAGE] = [
        [_stage_row("evt-1", "deal-1")],
        [_stage_row("evt-2", "deal-1", "won")],
    ]
    responses[SALES_PREDICTION_DEAL_VERSIONS_FOR_PARENTS] = [
        [_deal_row("deal-1")],
        # deal-1 reappears on the second page; its version row changed content.
        [_deal_row("deal-1", raw_payload='{"amount": "11"}')],
    ]
    with pytest.raises(ValueError, match="deal version changed during"):
        _load(_Client(responses))


def test_load_evidence_paginates_with_strictly_advancing_cursor() -> None:
    page_one = [_stage_row(f"evt-{index:05d}", "deal-1") for index in range(_PAGE_SIZE)]
    page_one.append(_stage_row("evt-Z", "deal-2"))
    responses = _happy_responses()
    responses[SALES_PREDICTION_STAGE_EVENTS_PAGE] = [page_one, []]
    responses[SALES_PREDICTION_DEAL_VERSIONS_FOR_PARENTS] = [[], []]
    client = _Client(responses)
    evidence = _load(client)
    assert len(evidence.events) == _PAGE_SIZE + 1
    page_calls = [call for call in client.calls if call[0] == SALES_PREDICTION_STAGE_EVENTS_PAGE]
    assert len(page_calls) == 2
    assert page_calls[0][1]["after_event_identity"] is None
    assert page_calls[1][1]["after_event_identity"] == "evt-Z"


def test_load_evidence_rejects_non_scalar_row_value() -> None:
    row = _Record(
        {
            "event_identity": "evt-1",
            "parent_source_system": "bitrix_chat",
            "parent_source_record_id": "deal-1",
            "mapped_state": "open",
            "category_id": None,
            "stage_id": None,
            "source_semantic": None,
            "event_at": "2026-01-10T08:00:00Z",
            "available_at": "2026-01-10T08:05:00Z",
            "authority_head_version": {"nested": True},
        }
    )
    responses = _happy_responses()
    responses[SALES_PREDICTION_STAGE_EVENTS_PAGE] = [[row]]
    with pytest.raises(ValueError, match="non-scalar value"):
        _load(_Client(responses))
