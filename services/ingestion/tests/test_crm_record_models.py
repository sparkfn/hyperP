"""Contract tests for CRM source-record hierarchy and match-only policy."""

from __future__ import annotations

import pytest
from src.graph.queries.crm_history import (
    ACTIVATE_PENDING_CALLS_FOR_DEAL,
    CREATE_CALL_FROM_HISTORY,
    CREATE_CRM_HISTORY,
    LINK_CONVERSATION_TO_CRM_HISTORY,
    LINK_CRM_HISTORY_TO_EXISTING_CONVERSATIONS,
)
from src.models import RecordType, SourceRecordEnvelope, SourceRecordParentRef
from src.pipeline import _is_match_only_record


def _base_payload(record_type: str) -> dict[str, object]:
    return {
        "source_system": "bitrix_chat",
        "source_record_id": "source-1",
        "record_type": record_type,
        "observed_at": "2026-07-31T00:00:00+00:00",
        "record_hash": "hash",
    }


def _company_payload() -> dict[str, object]:
    payload = _base_payload("crm_company")
    payload["source_instance_id"] = "bitrix-primary"
    payload["raw_payload"] = {
        "company_reference": {"type": "crm_company_id", "value": "303"},
        "reference_metadata": {
            "identity_policy_version": "crm_company_reference_v1",
            "source_instance_id": "bitrix-primary",
            "crm_company_id": "303",
            "person_matching_prohibited": True,
        },
    }
    return payload


def test_crm_company_requires_a_non_person_reference_contract() -> None:
    envelope = SourceRecordEnvelope.model_validate(_company_payload())

    assert envelope.record_type == RecordType.CRM_COMPANY
    assert envelope.source_instance_id == "bitrix-primary"


def test_crm_company_requires_a_source_instance() -> None:
    payload = _company_payload()
    payload.pop("source_instance_id")

    with pytest.raises(ValueError, match="require source_instance_id"):
        SourceRecordEnvelope.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("identifiers", [{"type": "crm_company_id", "value": "303"}]),
        ("addresses", [{"raw": "303 Example Street"}]),
    ],
)
def test_crm_company_rejects_person_evidence(field: str, value: object) -> None:
    payload = _company_payload()
    payload[field] = value

    with pytest.raises(ValueError, match="cannot carry Person evidence"):
        SourceRecordEnvelope.model_validate(payload)


@pytest.mark.parametrize(
    "raw_payload",
    [
        {},
        {
            "company_reference": {"type": "crm_company_id", "value": "303"},
            "reference_metadata": {
                "identity_policy_version": "crm_company_reference_v1",
                "source_instance_id": "bitrix-primary",
                "crm_company_id": "303",
                "person_matching_prohibited": False,
            },
        },
        {
            "company_reference": {"type": "contact_id", "value": "303"},
            "reference_metadata": {
                "identity_policy_version": "crm_company_reference_v1",
                "source_instance_id": "bitrix-primary",
                "crm_company_id": "303",
                "person_matching_prohibited": True,
            },
        },
        {
            "company_reference": {"type": "crm_company_id", "value": ""},
            "reference_metadata": {
                "identity_policy_version": "crm_company_reference_v1",
                "source_instance_id": "bitrix-primary",
                "crm_company_id": "",
                "person_matching_prohibited": True,
            },
        },
        {
            "company_reference": {"type": "crm_company_id", "value": "303"},
            "reference_metadata": {
                "identity_policy_version": "crm_company_reference_v1",
                "source_instance_id": "bitrix-secondary",
                "crm_company_id": "303",
                "person_matching_prohibited": True,
            },
        },
        {
            "company_reference": {"type": "crm_company_id", "value": "303"},
            "reference_metadata": {
                "identity_policy_version": "crm_company_reference_v1",
                "source_instance_id": "bitrix-primary",
                "crm_company_id": "404",
                "person_matching_prohibited": True,
            },
        },
    ],
)
def test_crm_company_rejects_an_invalid_reference_contract(
    raw_payload: dict[str, object],
) -> None:
    payload = _company_payload()
    payload["raw_payload"] = raw_payload

    with pytest.raises(ValueError, match="require a prohibited company reference"):
        SourceRecordEnvelope.model_validate(payload)


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
    assert "entity.entity_key = parent.entity_key" in CREATE_CRM_HISTORY
    assert "CREATE (history)-[:OWNED_BY]->(entity)" in CREATE_CRM_HISTORY
    assert "(deal)-[:OWNED_BY]->(entity:Entity)" in CREATE_CALL_FROM_HISTORY
    assert "entity.entity_key = deal.entity_key" in CREATE_CALL_FROM_HISTORY
    assert "CREATE (call)-[:OWNED_BY]->(entity)" in CREATE_CALL_FROM_HISTORY


def test_accepted_deal_rehomes_pending_calls_across_logical_deal_versions() -> None:
    assert "logical_deal.source_record_id = deal.source_record_id" in (
        ACTIVATE_PENDING_CALLS_FOR_DEAL
    )
    assert "call.lifecycle_status = 'active'" in ACTIVATE_PENDING_CALLS_FOR_DEAL
    assert "CREATE (call)-[:LINKED_TO" in ACTIVATE_PENDING_CALLS_FOR_DEAL


def test_late_call_uses_current_logical_deal_for_person_context() -> None:
    assert "origin_deal.source_record_id" in CREATE_CALL_FROM_HISTORY
    assert "deal.lifecycle_status IN ['active', 'pending_review']" in (CREATE_CALL_FROM_HISTORY)
    assert "WHEN 'active' THEN 0" in CREATE_CALL_FROM_HISTORY


def test_crm_history_can_link_to_one_conversation_for_each_activity() -> None:
    assert "UNWIND $crm_activity_ids AS crm_activity_id" in LINK_CONVERSATION_TO_CRM_HISTORY
    assert "MERGE (history)-[link:LINKED_TO {is_active: true}]->(conversation)" in (
        LINK_CONVERSATION_TO_CRM_HISTORY
    )
    assert "link.activated_at = coalesce(link.activated_at, datetime())" in (
        LINK_CONVERSATION_TO_CRM_HISTORY
    )
    assert "link.retired_at = null" in LINK_CONVERSATION_TO_CRM_HISTORY
    assert "MERGE (conversation)-[:CHILD_OF]" not in LINK_CONVERSATION_TO_CRM_HISTORY


def test_history_links_to_existing_current_bitrix_chat_conversations() -> None:
    assert "is_latest: true" in LINK_CRM_HISTORY_TO_EXISTING_CONVERSATIONS
    assert "bitrix-openlines-chat-" in LINK_CRM_HISTORY_TO_EXISTING_CONVERSATIONS
    assert "MERGE (history)-[link:LINKED_TO {is_active: true}]->(conversation)" in (
        LINK_CRM_HISTORY_TO_EXISTING_CONVERSATIONS
    )
    assert "link.activated_at = coalesce(link.activated_at, datetime())" in (
        LINK_CRM_HISTORY_TO_EXISTING_CONVERSATIONS
    )
    assert "link.retired_at = null" in LINK_CRM_HISTORY_TO_EXISTING_CONVERSATIONS
    assert "link_method: 'crm_activity_id'" in LINK_CRM_HISTORY_TO_EXISTING_CONVERSATIONS


def test_source_record_envelope_preserves_instance_provenance_outside_raw_payload() -> None:
    payload = _base_payload("crm_history")
    payload["source_instance_id"] = "bitrix-primary"
    payload["parent_ref"] = {
        "parent_source_system": "bitrix_chat",
        "parent_source_instance_id": "bitrix-primary",
        "parent_source_record_id": "bitrix-crm-deal-1",
        "parent_record_type": "crm_deal",
    }

    envelope = SourceRecordEnvelope.model_validate(payload)

    assert envelope.source_instance_id == "bitrix-primary"
    assert envelope.parent_ref is not None
    assert envelope.parent_ref.parent_source_instance_id == "bitrix-primary"
    assert "source_instance_id" not in envelope.raw_payload


def test_source_record_envelope_rejects_ambiguous_instance_ids() -> None:
    payload = _base_payload("identity")
    payload["source_instance_id"] = " bitrix-primary "

    with pytest.raises(ValueError, match="canonical non-secret slug"):
        SourceRecordEnvelope.model_validate(payload)


def test_parent_ref_rejects_an_ambiguous_instance_id_independently() -> None:
    with pytest.raises(ValueError, match="canonical non-secret slug"):
        SourceRecordParentRef(
            parent_source_system="bitrix_chat",
            parent_source_instance_id=" bitrix-primary ",
            parent_source_record_id="bitrix-crm-deal-1",
            parent_record_type=RecordType.CRM_DEAL,
        )


def test_instance_aware_child_requires_an_instance_aware_parent() -> None:
    payload = _base_payload("crm_history")
    payload["source_instance_id"] = "bitrix-primary"
    payload["parent_ref"] = {
        "parent_source_system": "bitrix_chat",
        "parent_source_record_id": "bitrix-crm-deal-1",
        "parent_record_type": "crm_deal",
    }

    with pytest.raises(ValueError, match="require parent_source_instance_id"):
        SourceRecordEnvelope.model_validate(payload)


def test_instance_aware_same_source_child_cannot_cross_portals() -> None:
    payload = _base_payload("crm_history")
    payload["source_instance_id"] = "bitrix-primary"
    payload["parent_ref"] = {
        "parent_source_system": "bitrix_chat",
        "parent_source_instance_id": "bitrix-secondary",
        "parent_source_record_id": "bitrix-crm-deal-1",
        "parent_record_type": "crm_deal",
    }

    with pytest.raises(ValueError, match="same-source parent references"):
        SourceRecordEnvelope.model_validate(payload)
