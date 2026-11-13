from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import cast

import pytest
from neo4j import AsyncManagedTransaction
from person_list_summary_doubles import (
    SUMMARY_APPLY_ROW,
    is_summary_apply_query,
    is_summary_capture_query,
)
from src.graph.queries.identity_link_revisions import (
    APPEND_IDENTITY_LINK_REVISIONS,
    GET_RESOLVED_IDENTITY_LINK_HEADS_FOR_PERSON,
)
from src.graph.queries.person_lifecycle import RETIRE_PERSON
from src.graph.queries.person_list_summary import (
    APPLY_PERSON_LIST_SUMMARY_DELTAS,
    CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS,
)
from src.repositories.neo4j.person_lifecycle import _retire_person_tx


class _Result:
    def __init__(self, row: Mapping[str, object] | None) -> None:
        self.row = row

    async def single(self) -> Mapping[str, object] | None:
        return self.row

    def __aiter__(self) -> AsyncIterator[Mapping[str, object]]:
        return self

    async def __anext__(self) -> Mapping[str, object]:
        if self.row is None:
            raise StopAsyncIteration
        row = self.row
        self.row = None
        return row


class _Tx:
    def __init__(self, rows: list[Mapping[str, object] | None]) -> None:
        self.rows = rows
        self.calls: list[str] = []
        self.summary_calls: list[str] = []

    async def run(self, query: str, **_: object) -> _Result:
        if is_summary_capture_query(query) or is_summary_apply_query(query):
            self.summary_calls.append(query)
            return _Result(SUMMARY_APPLY_ROW if is_summary_apply_query(query) else None)
        self.calls.append(query)
        return _Result(self.rows.pop(0) if self.rows else None)


@pytest.mark.asyncio
async def test_retire_person_uses_one_transaction_and_is_idempotent_when_not_active() -> None:
    tx = _Tx(
        [
            {"lifecycle_event_id": "retire-1", "retired_at": "2026-08-26T00:00:00+00:00"},
            {
                "source_system": "bitrix_chat",
                "source_instance_id": "portal-1",
                "source_entity_type": "contact",
                "source_entity_id": "contact-1",
                "identity_policy_version": "crm_contact_identity_v1",
                "match_decision_id": None,
                "review_case_id": None,
            },
            None,
        ]
    )
    retired = await _retire_person_tx(
        cast(AsyncManagedTransaction, tx), "person-1", "request", "system"
    )
    assert retired
    assert tx.calls == [
        RETIRE_PERSON,
        GET_RESOLVED_IDENTITY_LINK_HEADS_FOR_PERSON,
        APPEND_IDENTITY_LINK_REVISIONS,
    ]
    # The maintained summary counter is captured before the retirement and
    # published once after it.
    assert tx.summary_calls == [
        CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS,
        CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS,
        APPLY_PERSON_LIST_SUMMARY_DELTAS,
    ]

    inactive_tx = _Tx([None])
    assert not await _retire_person_tx(
        cast(AsyncManagedTransaction, inactive_tx), "person-1", "request", "system"
    )
    assert inactive_tx.calls == [RETIRE_PERSON]
    assert inactive_tx.summary_calls == [CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS]
