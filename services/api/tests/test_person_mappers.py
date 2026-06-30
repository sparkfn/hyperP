from __future__ import annotations

import json

from src.graph.mappers import (
    map_person_identifier,
    map_possible_match_detail,
    map_shared_identifier_candidate,
    map_source_record,
)
from src.graph.mappers_entities import map_listed_person
from src.routes.persons import (
    _connection_display_items,
    _identifier_display_items,
    _source_record_display_items,
)
from src.types import (
    ApiResponse,
    KnowsRelationship,
    PersonConnection,
    PersonIdentifier,
    PopoverDisplayItem,
    PossibleMatchDetail,
    ResponseMeta,
    SharedAddress,
    SharedIdentifier,
    SharedIdentifierGroup,
    SourceRecord,
)


def _source_record(
    source_record_pk: str, source_record_id: str, source_system: str
) -> SourceRecord:
    return SourceRecord(
        source_record_pk=source_record_pk,
        source_system=source_system,
        source_record_id=source_record_id,
        link_status="linked",
        observed_at="2026-05-01T00:00:00Z",
        ingested_at="2026-05-02T00:00:00Z",
    )


def test_map_source_record_includes_entity_and_conversation_payloads() -> None:
    normalized_payload = {
        "summary": "Customer asked about a forklift.",
        "customer_sentiment": "positive",
        "chat_members": [{"name": "Ben", "role": "agent"}],
        "inquiries": [{"machine_product": "Forklift X", "lta_tag": "LTA123"}],
    }
    raw_payload = {
        "conversation_text": "[Deal] Forklift X\nCustomer asked about LTA123",
        "tenant": "speedzone",
    }
    conversation_ref = {"platform": "bitrix", "tenant": "speedzone", "chat_id": "42"}

    record = map_source_record(
        {
            "source_record": {
                "source_record_pk": "sr-pk-1",
                "source_record_id": "bitrix-chat-42",
                "source_record_version": "v1",
                "record_type": "conversation",
                "extraction_confidence": 0.87,
                "extraction_method": "llm:qwen",
                "link_status": "linked",
                "observed_at": "2026-05-07T10:00:00Z",
                "ingested_at": "2026-05-07T10:05:00Z",
                "normalized_payload": json.dumps(normalized_payload),
                "raw_payload": json.dumps(raw_payload),
                "conversation_ref": json.dumps(conversation_ref),
            },
            "source_system": "bitrix_chat",
            "linked_person_id": "person-1",
            "entity_key": "speedzone",
            "entity_display_name": "Speedzone",
        }
    )

    assert record.entity_key == "speedzone"
    assert record.entity_display_name == "Speedzone"
    assert record.extraction_method == "llm:qwen"
    assert record.raw_payload == raw_payload
    assert record.conversation_ref == conversation_ref
    assert record.normalized_payload == normalized_payload


def test_map_person_identifier_includes_source_record_provenance() -> None:
    identifier = map_person_identifier(
        {
            "identifier_type": "phone",
            "normalized_value": "+6599990000",
            "is_active": True,
            "is_verified": False,
            "last_confirmed_at": None,
            "source_system_key": "whatsapp_chat",
            "source_record_pks": ["sr-pk-1", "sr-pk-2"],
            "source_record_ids": ["whatsapp-chat-1", "bitrix-chat-2"],
            "entities": [
                {
                    "entity_key": "speedzone",
                    "display_name": "Speedzone",
                    "entity_type": "company",
                    "country_code": "SG",
                    "is_active": True,
                    "source_record_count": 2,
                }
            ],
            "source_records": [
                {
                    "source_record_pk": "sr-pk-1",
                    "source_system": "whatsapp_chat",
                    "source_record_id": "whatsapp-chat-1",
                    "source_record_version": None,
                    "entity_key": "speedzone",
                    "entity_display_name": "Speedzone",
                    "record_type": "conversation",
                    "extraction_confidence": 0.91,
                    "extraction_method": "llm:qwen",
                    "link_status": "linked",
                    "linked_person_id": "person-1",
                    "observed_at": "2026-05-07T10:00:00Z",
                    "ingested_at": "2026-05-07T10:05:00Z",
                    "conversation_ref": None,
                    "raw_payload": None,
                    "normalized_payload": {
                        "identifiers": [
                            {"identifier_type": "phone", "normalized_value": "+6599990000"}
                        ]
                    },
                }
            ],
        }
    )

    assert identifier.source_record_pks == ["sr-pk-1", "sr-pk-2"]
    assert identifier.source_record_ids == ["whatsapp-chat-1", "bitrix-chat-2"]
    assert identifier.entities[0].entity_key == "speedzone"
    assert identifier.source_records[0].source_record_pk == "sr-pk-1"


def test_map_shared_identifier_candidate_groups_identifiers() -> None:
    candidate = map_shared_identifier_candidate(
        {
            "person_id": "person-2",
            "status": "active",
            "preferred_full_name": "Ana Lim",
            "preferred_phone": "+6599990000",
            "preferred_email": "ana@example.com",
            "preferred_dob": "1990-01-02",
            "profile_completeness_score": 0.73,
            "identifiers": [
                {"identifier_type": "nric", "normalized_value": "S1234567A"},
                {"identifier_type": "phone", "normalized_value": "+6599990000"},
            ],
        }
    )

    assert candidate.person_id == "person-2"
    assert candidate.profile_completeness_score == 0.73
    assert candidate.identifier_strength == "strong"
    assert [identifier.identifier_type for identifier in candidate.identifiers] == ["nric", "phone"]


def test_api_response_accepts_popover_display_items() -> None:
    response: ApiResponse[list[str]] = ApiResponse(
        data=["row"],
        meta=ResponseMeta(request_id="req-1"),
        display_items=[PopoverDisplayItem(primary="Ana Lim", secondary="CRM")],
    )

    assert response.display_items is not None
    assert response.display_items[0].primary == "Ana Lim"
    assert response.display_items[0].secondary == "CRM"


def test_possible_match_detail_groups_candidate_and_current_source_records() -> None:
    detail = PossibleMatchDetail(
        candidate_person_id="person-2",
        candidate_name="Ana Lim",
        shared_identifier_groups=[
            SharedIdentifierGroup(
                identifier_type="phone",
                normalized_value="+6599990000",
                candidate_source_records=[],
                current_person_source_records=[],
            )
        ],
    )

    assert detail.shared_identifier_groups[0].identifier_type == "phone"
    assert detail.shared_identifier_groups[0].candidate_source_records == []
    assert detail.shared_identifier_groups[0].current_person_source_records == []


def test_map_listed_person_includes_possible_match_count() -> None:
    listed = map_listed_person(
        {
            "person": {
                "person_id": "person-1",
                "status": "active",
                "is_high_value": False,
                "is_high_risk": False,
                "preferred_full_name": "Ben Tan",
                "preferred_phone": "+6512345678",
                "preferred_email": "ben@example.com",
                "preferred_dob": "1991-02-03",
                "preferred_nric": None,
                "profile_completeness_score": 0.8,
                "golden_profile_computed_at": None,
                "golden_profile_version": None,
                "created_at": "2026-05-01T00:00:00Z",
                "updated_at": "2026-05-02T00:00:00Z",
            },
            "preferred_address": None,
            "source_record_count": 2,
            "connection_count": 1,
            "phone_confidence": 0.9,
            "entities": [],
            "entity_count": 0,
            "identifier_count": 3,
            "possible_match_count": 4,
            "order_count": 5,
            "bankruptcy_case_count": 0,
        }
    )

    assert listed.identifier_count == 3
    assert listed.possible_match_count == 4


def test_display_item_helpers_format_popover_text() -> None:
    identifiers = [
        PersonIdentifier(
            identifier_type="phone",
            normalized_value="+6599990000",
            is_active=True,
            is_verified=False,
            source_system_key="crm",
        )
    ]
    connections = [
        PersonConnection(
            person_id="person-2",
            status="active",
            preferred_full_name="Ana Lim",
            hops=1,
            shared_identifiers=[
                SharedIdentifier(identifier_type="phone", normalized_value="+6599990000")
            ],
            shared_addresses=[SharedAddress(address_id="addr-1", normalized_full="1 Main St")],
            knows_relationships=[
                KnowsRelationship(
                    relationship_label=None, relationship_category="emergency_contact"
                )
            ],
        )
    ]
    source_records = [_source_record("sr-1", "crm-1", "crm")]

    assert _identifier_display_items(identifiers)[0].primary == "phone: +6599990000"
    assert _identifier_display_items(identifiers)[0].secondary == "active · unverified · crm"
    assert _connection_display_items(connections)[0].primary == "Ana Lim"
    assert _connection_display_items(connections)[0].secondary == (
        "phone:+6599990000 · address:1 Main St · knows:emergency_contact"
    )
    assert _source_record_display_items(source_records)[0].primary == "crm"
    assert _source_record_display_items(source_records)[0].secondary == (
        "crm-1 · identity · 2026-05-02T00:00:00Z"
    )


def test_map_possible_match_detail_groups_source_records() -> None:
    detail = map_possible_match_detail(
        [
            {
                "candidate_person_id": "person-2",
                "candidate_name": "Ana Lim",
                "identifier_type": "phone",
                "normalized_value": "+6599990000",
                "candidate_source_records": [
                    {
                        "source_record_pk": "sr-c",
                        "source_system": "crm",
                        "source_record_id": "crm-c",
                        "link_status": "linked",
                        "observed_at": "2026-05-01T00:00:00Z",
                        "ingested_at": "2026-05-02T00:00:00Z",
                    }
                ],
                "current_person_source_records": [
                    {
                        "source_record_pk": "sr-p",
                        "source_system": "pos",
                        "source_record_id": "pos-p",
                        "link_status": "linked",
                        "observed_at": "2026-05-03T00:00:00Z",
                        "ingested_at": "2026-05-04T00:00:00Z",
                    }
                ],
            }
        ]
    )

    assert isinstance(detail, PossibleMatchDetail)
    assert detail.candidate_person_id == "person-2"
    assert detail.candidate_name == "Ana Lim"
    group = detail.shared_identifier_groups[0]
    assert group.identifier_type == "phone"
    assert group.candidate_source_records[0].source_record_pk == "sr-c"
    assert group.current_person_source_records[0].source_record_pk == "sr-p"
