"""Tests for raw_payload two-pass scrub and normalized_payload free-text scrub."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

from src.models import (
    JsonValue,
    MatchDecision,
    MatchResult,
    RawIdentifier,
    RecordType,
    SourceRecordEnvelope,
    SourceRecordLifecycleStatus,
)
from src.pipeline_bankruptcy import bankruptcy_case_blueprint
from src.pipeline_sales import _serialize_metadata
from src.pipeline_writes import build_normalized_source_payload, persist_source_record
from src.raw_payload import (
    decode_raw_payload,
    scrub_nric_strings,
    serialize_raw_payload,
)


def test_no_raw_payload_json_dumps_in_src_except_stage_history() -> None:
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    src_dir = repo_root / "services" / "ingestion" / "src"
    hits: list[str] = []
    for p in src_dir.rglob("*.py"):
        if p.name == "stage_history_ingestion.py":
            continue
        text = p.read_text(encoding="utf-8")
        if "json.dumps(envelope.raw_payload" in text:
            hits.append(str(p.relative_to(repo_root)))
    assert hits == [], f"Found unscrubbed json.dumps(envelope.raw_payload) in: {hits}"


def test_decode_raw_payload() -> None:
    assert decode_raw_payload('{"key": "val"}') == {"key": "val"}
    assert decode_raw_payload({"key": "val"}) == {"key": "val"}
    assert decode_raw_payload("not a json") is None
    assert decode_raw_payload(123) is None
    assert decode_raw_payload("[]") is None


def test_scrub_nric_strings_and_short_token_preservation() -> None:
    # len < 8 tokens (e.g. 'NA') must NOT be substring replaced, name 'Nancy' stays intact
    collected = frozenset({"NA", "S7012164F"})
    payload: dict[str, JsonValue] = {
        "name": "Nancy",
        "status": "NA",
        "chat": "User NRIC is S7012164F please check",
    }
    scrubbed = cast(dict[str, JsonValue], scrub_nric_strings(payload, collected_tokens=collected))
    assert scrubbed["name"] == "Nancy"
    assert scrubbed["status"] == "NA"
    assert scrubbed["chat"] == "User NRIC is S••••164F please check"


def test_serialize_raw_payload_two_pass_and_nested_sales() -> None:
    envelope = SourceRecordEnvelope(
        source_system="speedzone_phppos:sales",
        source_record_id="order-1",
        record_type=RecordType.SALES,
        observed_at="2026-01-01T00:00:00Z",
        record_hash="hash-123",
        identifiers=[RawIdentifier(type="nric", value="S7012164F")],
        raw_payload={
            "customer_nric": "S7012164F",
            "ic_number": "NA",
            "line_items": [
                {
                    "item_id": 1,
                    "metadata": {
                        "nric": "S8110609F",
                        "note": "free text S8929303J included",
                    },
                }
            ],
            "normal_data": "order_42",
        },
    )

    serialized = serialize_raw_payload(envelope)
    loaded = json.loads(serialized)
    assert loaded["customer_nric"] == "S••••164F"
    assert loaded["ic_number"] == "NA"  # len < 8 preserved
    line_item = loaded["line_items"][0]
    assert line_item["metadata"]["nric"] == "S••••609F"
    assert line_item["metadata"]["note"] == "free text S••••303J included"
    assert loaded["normal_data"] == "order_42"
    assert envelope.record_hash == "hash-123"


def test_pipeline_bankruptcy_blueprint_scrubs_raw_payload() -> None:
    envelope = SourceRecordEnvelope(
        source_system="sgbankruptcy",
        source_record_id="case-1",
        record_type=RecordType.BANKRUPTCY,
        observed_at="2026-01-01T00:00:00Z",
        record_hash="hash-case",
        identifiers=[RawIdentifier(type="nric", value="S7012164F")],
        raw_payload={
            "case": {"id": "case-1", "case_number": "B123"},
            "identification_number": "S7012164F",
            "notes": "Bankrupt S8110609F listed",
        },
    )
    bp = bankruptcy_case_blueprint(envelope)
    assert bp is not None
    loaded_raw = json.loads(str(bp["raw_payload"]))
    assert loaded_raw["identification_number"] == "S••••164F"
    assert loaded_raw["notes"] == "Bankrupt S••••609F listed"


def test_persist_source_record_passes_scrubbed_raw_payload() -> None:
    tx = MagicMock()
    tx.run.return_value = MagicMock(single=lambda: {"source_record_pk": "sr-pk-1"})

    envelope = SourceRecordEnvelope(
        source_system="fundbox",
        source_record_id="rec-1",
        record_type=RecordType.IDENTITY,
        source_record_version="1",
        observed_at="2026-01-01T00:00:00Z",
        record_hash="hash-1",
        identifiers=[RawIdentifier(type="nric", value="S7012164F")],
        raw_payload={"nric": "S7012164F"},
    )

    pk = persist_source_record(
        tx,
        envelope=envelope,
        identifiers=[],
        addresses=[],
        attributes=[],
        match_result=MatchResult(
            decision=MatchDecision.MERGE,
            confidence=1.0,
            reasons=[],
            matched_person_id="p-1",
        ),
        is_new_person=False,
        ingest_run_id=None,
        lifecycle_status=SourceRecordLifecycleStatus.ACTIVE,
        expected_active_source_record_pk=None,
    )
    assert pk == "sr-pk-1"
    assert tx.run.called
    kwargs = tx.run.call_args.kwargs
    raw_payload_str = kwargs["raw_payload"]
    assert "S7012164F" not in raw_payload_str
    assert "S••••164F" in raw_payload_str


def test_normalized_payload_scrubs_conversation_free_text() -> None:
    envelope = SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="rec-chat-1",
        record_type=RecordType.CONVERSATION,
        observed_at="2026-01-01T00:00:00Z",
        record_hash="hash-chat",
        identifiers=[],
        raw_payload={
            "summary": "Customer S7012164F inquired about loan",
            "chat_members": ["Alice", "Bob (S8110609F)"],
            "inquiries": ["NRIC T0123456G dispute"],
        },
    )

    normalized = build_normalized_source_payload(
        envelope=envelope,
        identifiers=[],
        addresses=[],
        attributes=[],
    )

    assert normalized["summary"] == "Customer S••••164F inquired about loan"
    assert normalized["chat_members"] == ["Alice", "Bob (S••••609F)"]
    assert normalized["inquiries"] == ["NRIC T••••456G dispute"]


def test_serialize_metadata_scrubs_nric_strings() -> None:
    serialized = _serialize_metadata(
        {"nric": "S7012164F", "note": "buyer S8110609F", "merchant": "Shop"}
    )
    assert json.loads(serialized) == {
        "nric": "S••••164F",
        "note": "buyer S••••609F",
        "merchant": "Shop",
    }
    assert json.loads(_serialize_metadata(None)) == {}


def test_scrub_nric_strings_masks_noncanonical_token_under_nric_key() -> None:
    scrubbed = cast(dict[str, JsonValue], scrub_nric_strings({"nric": "S7012164"}))
    assert scrubbed == {"nric": "•••••••••"}
