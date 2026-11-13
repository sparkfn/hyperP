"""Tests for NRIC normalization, payload determinism, candidates, and writes."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from src.config import Settings
from src.models import (
    NormalizedIdentifier,
    QualityFlag,
    RawIdentifier,
    RecordType,
    SourceRecordEnvelope,
)
from src.normalizers.nric import nric_digest
from src.pipeline_normalization import normalize_envelope_identifiers
from src.pipeline_writes import (
    build_normalized_source_payload,
    find_candidates,
    upsert_nodes,
)

TEST_HMAC_KEY = "test-nric-hmac-key-v1"
EXPECTED_FERNET_KEY = b"1tX4DnEjO1YRZy0Uz74Lt37KdoBwPzfLjwPNjMrek6E="


def test_normalization_missing_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Missing key => no NRIC identifier emitted, other identifiers still emitted
    test_settings = Settings(
        neo4j_password="pwd",
        nric_hmac_key="",
    )
    monkeypatch.setattr("src.pipeline_normalization.get_settings", lambda: test_settings)

    envelope = SourceRecordEnvelope(
        source_system="fundbox",
        source_record_id="rec-1",
        record_type=RecordType.IDENTITY,
        observed_at="2026-01-01T00:00:00Z",
        record_hash="hash-1",
        identifiers=[
            RawIdentifier(type="nric", value="S1234567D"),
            RawIdentifier(type="phone", value="+6589251818"),
        ],
    )

    identifiers = normalize_envelope_identifiers(envelope)
    types = [i.identifier_type for i in identifiers]
    assert "nric" not in types
    assert "phone" in types

    # No raw or canonical NRIC appears in log output
    assert "S1234567D" not in caplog.text


def test_normalization_with_key_populates_digest_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_settings = Settings(
        neo4j_password="pwd",
        nric_hmac_key=TEST_HMAC_KEY,
        nric_hmac_key_version="v1",
    )
    monkeypatch.setattr("src.pipeline_normalization.get_settings", lambda: test_settings)

    envelope = SourceRecordEnvelope(
        source_system="fundbox",
        source_record_id="rec-1",
        record_type=RecordType.IDENTITY,
        observed_at="2026-01-01T00:00:00Z",
        record_hash="hash-1",
        identifiers=[RawIdentifier(type="nric", value="S1234567D")],
    )

    identifiers = normalize_envelope_identifiers(envelope)
    assert len(identifiers) == 1
    ident = identifiers[0]
    expected_digest = nric_digest("S1234567D", TEST_HMAC_KEY)
    assert ident.identifier_type == "nric"
    assert ident.normalized_value == expected_digest
    assert ident.nric_masked == "S••••567D"
    assert ident.checksum_valid is True
    assert ident.nric_key_version == "v1"
    assert ident.nric_plaintext == "S1234567D"


def test_normalization_invalid_checksum_still_persisted_with_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Records are never dropped due to invalid checksum
    test_settings = Settings(
        neo4j_password="pwd",
        nric_hmac_key=TEST_HMAC_KEY,
        nric_hmac_key_version="v1",
    )
    monkeypatch.setattr("src.pipeline_normalization.get_settings", lambda: test_settings)

    envelope = SourceRecordEnvelope(
        source_system="fundbox",
        source_record_id="rec-1",
        record_type=RecordType.IDENTITY,
        observed_at="2026-01-01T00:00:00Z",
        record_hash="hash-1",
        identifiers=[RawIdentifier(type="nric", value="S7012164A")],
    )

    identifiers = normalize_envelope_identifiers(envelope)
    assert len(identifiers) == 1
    ident = identifiers[0]
    assert ident.quality_flag == QualityFlag.VALID
    assert ident.checksum_valid is False


def test_payload_determinism_and_non_nric_byte_identity() -> None:
    nric_ident = NormalizedIdentifier(
        identifier_type="nric",
        normalized_value="digest-123",
        quality_flag=QualityFlag.VALID,
        nric_masked="S••••567D",
        checksum_valid=True,
        nric_key_version="v1",
        nric_plaintext="S1234567D",
    )
    phone_ident = NormalizedIdentifier(
        identifier_type="phone",
        normalized_value="+6589251818",
        quality_flag=QualityFlag.VALID,
    )
    envelope = SourceRecordEnvelope(
        source_system="fundbox",
        source_record_id="rec-1",
        record_type=RecordType.IDENTITY,
        observed_at="2026-01-01T00:00:00Z",
        record_hash="hash-1",
        identifiers=[],
    )

    p1 = build_normalized_source_payload(
        envelope=envelope,
        identifiers=[nric_ident, phone_ident],
        addresses=[],
        attributes=[],
    )
    p2 = build_normalized_source_payload(
        envelope=envelope,
        identifiers=[nric_ident, phone_ident],
        addresses=[],
        attributes=[],
    )
    # Two builds give byte-identical payload
    assert json.dumps(p1, sort_keys=True) == json.dumps(p2, sort_keys=True)

    # In-memory carrier plaintext is excluded
    serialized = json.dumps(p1)
    assert "S1234567D" not in serialized
    assert "nric_plaintext" not in serialized
    assert "nric_encrypted" not in serialized

    # Non-NRIC identifier dump is byte-identical to before (has only standard keys)
    phone_payload = p1["identifiers"][1]  # type: ignore[index]
    assert set(phone_payload.keys()) == {  # type: ignore[attr-defined]
        "identifier_type",
        "normalized_value",
        "source_instance_id",
        "is_verified",
        "quality_flag",
    }


def test_upsert_nodes_non_nric_and_nric_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_settings = Settings(
        neo4j_password="pwd",
        nric_hmac_key=TEST_HMAC_KEY,
        nric_hmac_key_version="v1",
    )
    monkeypatch.setattr("src.pipeline_writes.get_settings", lambda: test_settings)

    tx = MagicMock()
    phone_ident = NormalizedIdentifier(
        identifier_type="phone",
        normalized_value="+6589251818",
        quality_flag=QualityFlag.VALID,
    )
    nric_ident = NormalizedIdentifier(
        identifier_type="nric",
        normalized_value="digest-123",
        quality_flag=QualityFlag.VALID,
        nric_masked="S••••567D",
        checksum_valid=True,
        nric_key_version="v1",
        nric_plaintext="S1234567D",
    )
    upsert_nodes(tx, [phone_ident, nric_ident], [])

    assert tx.run.called
    kwargs = tx.run.call_args.kwargs
    rows = kwargs["identifiers"]
    assert len(rows) == 2

    # Phone row has NONE of the five NRIC keys
    phone_row = rows[0]
    assert phone_row == {
        "identifier_type": "phone",
        "identifier_scope": "global",
        "source_instance_id": None,
        "normalized_value": "+6589251818",
    }

    # NRIC row has all 5 NRIC keys and ciphertext decrypts to plaintext
    nric_row = rows[1]
    assert nric_row["identifier_type"] == "nric"
    assert nric_row["normalized_value"] == "digest-123"
    assert nric_row["hashed_value"] == "digest-123"
    assert nric_row["nric_masked"] == "S••••567D"
    assert nric_row["checksum_valid"] is True
    assert nric_row["nric_key_version"] == "v1"

    ciphertext = nric_row["nric_encrypted"]
    fernet = Fernet(EXPECTED_FERNET_KEY)
    decrypted = fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    assert decrypted == "S1234567D"


def test_find_candidates_transition_row_and_deduplication() -> None:
    tx = MagicMock()
    # Mock tx.run returning two records with same person_id
    # (e.g. matched both digest and plaintext rows)
    tx.run.return_value = [
        {"input_index": 0, "fanout": 1, "person_ids": ["person-1"]},
        {"input_index": 0, "fanout": 1, "person_ids": ["person-1"]},
    ]
    ident = NormalizedIdentifier(
        identifier_type="nric",
        normalized_value="digest-123",
        quality_flag=QualityFlag.VALID,
        nric_masked="S••••567D",
        checksum_valid=True,
        nric_key_version="v1",
        nric_plaintext="S1234567D",
    )

    candidates = find_candidates(tx, [ident], [])
    assert tx.run.called
    kwargs = tx.run.call_args.kwargs
    rows = kwargs["identifiers"]
    assert len(rows) == 2
    assert rows[0]["normalized_value"] == "digest-123"
    assert rows[1]["normalized_value"] == "S1234567D"

    # Exactly ONE candidate returned (deduplicated)
    assert len(candidates) == 1
    assert candidates[0].person_id == "person-1"
