"""Contract tests for CRM source-record hierarchy and match-only policy."""

from __future__ import annotations

import pytest
from src.graph.queries.crm_history import (
    ACTIVATE_PENDING_CALLS_FOR_DEAL,
    CREATE_CALL_FROM_HISTORY,
    CREATE_CRM_HISTORY,
)
from src.models import RecordType, SourceRecordEnvelope
from src.pipeline import _is_match_only_record


def _base_payload(record_type: str) -> dict[str, object]:
    return {
        "source_system": "bitrix_chat",
        "source_record_id": "source-1",
        "record_type": record_type,
        "observed_at": "2026-07-31T00:00:00+00:00",
        "record_hash": "hash",
    }


def test_crm_history_requires_a_logical_deal_parent() -> None:
    with pytest.raises(ValueError, match="require parent_ref"):
        SourceRecordEnvelope.model_validate(_base_payload("crm_history"))


def test_call_requires_a_crm_history_parent() -> None:
    payload = _base_payload("call")
    payload["parent_ref"] = {
        "parent_source_system": "bitrix_chat",
        "parent_source_record_id": "bitrix-crm-deal-1",
        "parent_record_type": "crm_deal",
    }
    with pytest.raises(ValueError, match="must target crm_history"):
        SourceRecordEnvelope.model_validate(payload)


def test_call_requires_a_history_parent_reference() -> None:
    with pytest.raises(ValueError, match="call source records require parent_ref"):
        SourceRecordEnvelope.model_validate(_base_payload("call"))


def test_conversation_and_call_are_match_only_independent_of_source() -> None:
    assert _is_match_only_record("fundbox", RecordType.CONVERSATION)
    assert _is_match_only_record("fundbox", RecordType.CALL)
    assert not _is_match_only_record("fundbox", RecordType.CRM_DEAL)


def test_crm_children_inherit_deal_entity_ownership() -> None:
    assert "(parent)-[:OWNED_BY]->(entity:Entity)" in CREATE_CRM_HISTORY
    assert "CREATE (history)-[:OWNED_BY]->(entity)" in CREATE_CRM_HISTORY
    assert "(deal)-[:OWNED_BY]->(entity:Entity)" in CREATE_CALL_FROM_HISTORY
    assert "CREATE (call)-[:OWNED_BY]->(entity)" in CREATE_CALL_FROM_HISTORY


def test_accepted_deal_rehomes_pending_calls_across_logical_deal_versions() -> None:
    assert "logical_deal.source_record_id = deal.source_record_id" in (
        ACTIVATE_PENDING_CALLS_FOR_DEAL
    )
    assert "call.lifecycle_status = 'active'" in ACTIVATE_PENDING_CALLS_FOR_DEAL
    assert "CREATE (call)-[:LINKED_TO" in ACTIVATE_PENDING_CALLS_FOR_DEAL


def test_late_call_uses_current_logical_deal_for_person_context() -> None:
    assert "origin_deal.source_record_id" in CREATE_CALL_FROM_HISTORY
    assert "deal.lifecycle_status IN ['active', 'pending_review']" in (
        CREATE_CALL_FROM_HISTORY
    )
    assert "WHEN 'active' THEN 0" in CREATE_CALL_FROM_HISTORY
