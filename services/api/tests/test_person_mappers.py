from __future__ import annotations

import json

from src.graph.mappers import (
    map_person_identifier,
    map_shared_identifier_candidate,
    map_source_record,
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
