"""Regression tests for post-ingest KNOWS materialization."""

from __future__ import annotations

from typing import Any

from src.graph import queries
from src.pipeline_knows import _link_one_chat_relationship


class _Row:
    def __init__(self, **values: object) -> None:
        self._values = values

    def __getitem__(self, key: str) -> object:
        return self._values[key]


class _Result:
    def __init__(self, row: _Row | None) -> None:
        self._row = row

    def single(self) -> _Row | None:
        return self._row


class _Tx:
    def __init__(self) -> None:
        self.link_params: dict[str, object] | None = None

    def run(self, query: str, **params: Any) -> _Result:
        if query == queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID:
            assert params == {"source_record_id": "bitrix-chat-1-person-1"}
            return _Result(_Row(person_id="person-alice"))
        if query == queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_PK:
            assert params == {"source_record_pk": "pk-bob"}
            return _Result(_Row(person_id="person-bob"))
        if query == queries.LINK_PERSON_KNOWS:
            self.link_params = params
            return _Result(_Row(knows_id="knows-1"))
        raise AssertionError(f"unexpected query: {query}")


def test_chat_relationship_materializer_creates_pending_knows() -> None:
    tx = _Tx()
    raw_payload = {
        "primary_source_record_id": "bitrix-chat-1-person-1",
        "relationship_to_primary": "brother",
        "relationship_label": "brother",
    }

    linked = _link_one_chat_relationship(tx, "pk-bob", "bitrix_chat", raw_payload)

    assert linked is True
    assert tx.link_params == {
        "declarer_person_id": "person-alice",
        "contact_person_id": "person-bob",
        "source_system_key": "bitrix_chat",
        "source_record_pk": "pk-bob",
        "relationship_label": "brother",
        "relationship_category": "family",
        "status": "pending",
        "approved_at": None,
    }
