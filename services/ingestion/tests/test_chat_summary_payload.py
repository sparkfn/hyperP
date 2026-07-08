"""Regression tests for chat summary persistence."""

from __future__ import annotations

import json
from typing import cast

from neo4j import ManagedTransaction
from src.models import EngineType, MatchDecision, MatchResult, RecordType, SourceRecordEnvelope
from src.pipeline_writes import persist_source_record


class _Result:
    def single(self) -> dict[str, str]:
        return {"source_record_pk": "sr-1"}


class _Tx:
    def __init__(self) -> None:
        self.params: dict[str, object] | None = None

    def run(self, query: str, **kwargs: object) -> _Result:
        self.params = kwargs
        return _Result()


def test_persist_source_record_includes_chat_summary_in_normalized_payload() -> None:
    tx = _Tx()
    envelope = SourceRecordEnvelope(
        source_system="whatsapp_chat",
        source_record_id="whatsapp-chat-1",
        record_type=RecordType.CONVERSATION,
        observed_at="2026-05-06T00:00:00",
        record_hash="hash-1",
        raw_payload={
            "summary": " Customer asks about delivery. ",
            "customer_sentiment": "frustrated",
            "chat_members": [
                {"name": "Ben", "phone": "+6588880000", "role": "agent", "notes": "Sales"}
            ],
            "inquiries": [
                {
                    "vehicle_product": "Forklift X",
                    "unit": "Unit 7",
                    "lta_tag": "LTA123",
                    "serial_number": "SN-9",
                    "notes": "Asked for availability",
                }
            ],
        },
        extraction_confidence=0.9,
        extraction_method="llm:qwen-max",
    )
    match_result = MatchResult(
        decision=MatchDecision.NO_MATCH,
        confidence=0.0,
        reasons=[],
        engine_type=EngineType.HEURISTIC,
    )

    persist_source_record(
        cast(ManagedTransaction, tx),
        envelope=envelope,
        identifiers=[],
        addresses=[],
        attributes=[],
        match_result=match_result,
        is_new_person=False,
        ingest_run_id=None,
    )

    assert tx.params is not None
    payload_raw = tx.params["normalized_payload"]
    assert isinstance(payload_raw, str)
    payload = json.loads(payload_raw)
    assert payload["summary"] == "Customer asks about delivery."
    assert payload["customer_sentiment"] == "frustrated"
    assert payload["chat_members"] == [
        {"name": "Ben", "phone": "+6588880000", "role": "agent", "notes": "Sales"}
    ]
    assert payload["inquiries"] == [
        {
            "vehicle_product": "Forklift X",
            "unit": "Unit 7",
            "lta_tag": "LTA123",
            "serial_number": "SN-9",
            "notes": "Asked for availability",
        }
    ]
