"""Behavioral tests for CRM history/conversation relationship persistence."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from neo4j import ManagedTransaction
from src.graph import queries
from src.graph.client import Neo4jClient
from src.models import RecordType, SourceRecordEnvelope
from src.pipeline_crm import (
    link_conversation_to_crm_history,
    link_crm_history_to_existing_conversations,
)


class _Result:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def single(self) -> dict[str, object]:
        return self._row


class _Tx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> _Result:
        self.calls.append((query, kwargs))
        if query == queries.LINK_CONVERSATION_TO_CRM_HISTORY:
            return _Result({"linked_history_count": 2})
        if query == queries.LINK_CRM_HISTORY_TO_EXISTING_CONVERSATIONS:
            return _Result({"linked_conversation_count": 1})
        raise AssertionError("unexpected query")


class _Session:
    def __init__(self, tx: _Tx) -> None:
        self._tx = tx

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute_write(self, work: Callable[[ManagedTransaction], bool]) -> bool:
        return work(cast(ManagedTransaction, self._tx))


class _Client:
    def __init__(self) -> None:
        self.tx = _Tx()

    def session(self) -> _Session:
        return _Session(self.tx)


def _conversation_envelope(activity_ids: list[object]) -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="bitrix-openlines-chat-77-person-1",
        record_type=RecordType.CONVERSATION,
        observed_at="2026-07-20T08:00:00+00:00",
        record_hash="hash",
        extraction_confidence=0.95,
        extraction_method="llm:test",
        conversation_ref={"platform": "bitrix_openlines"},
        raw_payload={"crm_activity_ids": activity_ids},
    )


def _history_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-history-900",
        record_type=RecordType.CRM_HISTORY,
        observed_at="2026-07-20T08:00:00+00:00",
        record_hash="hash",
        parent_ref={
            "parent_source_system": "bitrix_chat",
            "parent_source_record_id": "bitrix-crm-deal-501",
            "parent_record_type": "crm_deal",
        },
        raw_payload={"crm_activity_id": "900", "bitrix_chat_id_numeric": 77},
    )


def test_conversation_links_every_unique_string_activity_id() -> None:
    client = _Client()

    linked = link_conversation_to_crm_history(
        cast(Neo4jClient, client),
        _conversation_envelope(["900", "900", 901, "", "901"]),
        "conversation-pk",
    )

    assert linked is True
    assert client.tx.calls == [
        (
            queries.LINK_CONVERSATION_TO_CRM_HISTORY,
            {
                "conversation_source_record_pk": "conversation-pk",
                "source_system": "bitrix_chat",
                "crm_activity_ids": ["900", "901"],
            },
        )
    ]


def test_history_links_to_existing_chat_by_provider_chat_id() -> None:
    client = _Client()

    linked = link_crm_history_to_existing_conversations(
        cast(Neo4jClient, client),
        _history_envelope(),
        "history-pk",
    )

    assert linked is True
    assert client.tx.calls == [
        (
            queries.LINK_CRM_HISTORY_TO_EXISTING_CONVERSATIONS,
            {
                "history_source_record_pk": "history-pk",
                "source_system": "bitrix_chat",
                "bitrix_chat_id": 77,
                "crm_activity_id": "900",
            },
        )
    ]


def test_link_helpers_skip_unresolvable_relationships_without_querying() -> None:
    client = _Client()
    conversation = _conversation_envelope([])
    history = _history_envelope()
    history.raw_payload["bitrix_chat_id_numeric"] = None

    assert (
        link_conversation_to_crm_history(cast(Neo4jClient, client), conversation, "conversation-pk")
        is False
    )
    assert (
        link_crm_history_to_existing_conversations(cast(Neo4jClient, client), history, "history-pk")
        is False
    )
    assert client.tx.calls == []
