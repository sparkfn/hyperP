"""Tests for NRIC anchor gate, hashing/encryption, and conflict classification."""

from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.fernet import Fernet
from src.graph.bootstrap import SOURCE_NRIC_TRUST
from src.models import NormalizedIdentifier, QualityFlag, RecordType
from src.normalizers.nric import (
    encrypt_nric,
    is_nric_digest,
    mask_nric,
    masked_for,
    nric_digest,
)
from src.nric_anchor import (
    NameDob,
    NricValue,
    alias_link_decision,
    classify_person_nrics,
    nric_anchor_gate,
)

SHARED_TEST_KEY = "test-nric-hmac-key-v1"
EXPECTED_FERNET_KEY = b"1tX4DnEjO1YRZy0Uz74Lt37KdoBwPzfLjwPNjMrek6E="

VECTORS = {
    "S1234567D": "a43bb81909a911bc898702d96b9cb10a382793e492d8175abf54772f8df60b2a",
    "T1234567J": "afa8d6815558f929318f4b97610b9157b0e3507be7ca0269c5d846ca4c0cf0c2",
    "F1234567N": "c548c0537ec2ed6bea2e86e4c37e855f30e55a006a4b11334a1bc4c9e9f2c8dd",
    "G1234567X": "bfd852f6147955514e70adb30309dccac92b8d1dfabdb65398c866aca09eb7c7",
}


def test_shared_test_vectors_hmac_and_fernet() -> None:
    # Test HMAC digests match shared cross-package vectors exactly
    for canonical, expected_digest in VECTORS.items():
        assert nric_digest(canonical, SHARED_TEST_KEY) == expected_digest
        assert is_nric_digest(expected_digest) is True

    # Test Fernet derived key matches shared constant
    key_bytes = SHARED_TEST_KEY.encode("utf-8")
    derived_fernet_key = base64.urlsafe_b64encode(
        hashlib.sha256(b"hyperp-nric-encryption:v1\x00" + key_bytes).digest()
    )
    assert derived_fernet_key == EXPECTED_FERNET_KEY

    # Test Fernet round-trip with derived key
    ciphertext = encrypt_nric("S1234567D", SHARED_TEST_KEY)
    fernet = Fernet(EXPECTED_FERNET_KEY)
    decrypted = fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    assert decrypted == "S1234567D"


def test_hmac_key_sensitivity_and_collision_freedom() -> None:
    d1 = nric_digest("S1234567D", "key-a")
    d2 = nric_digest("S1234567D", "key-b")
    assert d1 != d2

    # Check collision freedom over 100 distinct canonical strings
    fixtures = [f"S{1000000 + i}D" for i in range(100)]
    digests = {nric_digest(f, SHARED_TEST_KEY) for f in fixtures}
    assert len(digests) == 100


def test_nric_masking_rules() -> None:
    assert mask_nric("S1234567D") == "S••••567D"
    assert mask_nric("F1234567N") == "F••••567N"
    assert mask_nric("invalid") == "•••••••••"
    assert mask_nric("") == "•••••••••"

    # masked_for helper
    assert masked_for(None, "S••••567D") is None
    digest = VECTORS["S1234567D"]
    assert masked_for(digest, "S••••567D") == "S••••567D"
    assert masked_for(digest, None) is None
    assert masked_for("S1234567D", None) == "S••••567D"


def test_nric_anchor_gate_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = VECTORS["S1234567D"]
    valid_ident = NormalizedIdentifier(
        identifier_type="nric",
        normalized_value=digest,
        is_verified=True,
        quality_flag=QualityFlag.VALID,
        nric_masked="S••••567D",
        checksum_valid=True,
        nric_key_version="v1",
        nric_plaintext="S1234567D",
    )

    # 1. Not nric
    non_nric = valid_ident.model_copy(update={"identifier_type": "phone"})
    r1 = nric_anchor_gate(
        non_nric, "fundbox", RecordType.IDENTITY, NameDob("Alice", "1990-01-01"), None
    )
    assert r1.promotes_person is False
    assert r1.reason == "not_nric"

    # 2. No key (plaintext / not digest)
    legacy_ident = valid_ident.model_copy(update={"normalized_value": "S1234567D"})
    r2 = nric_anchor_gate(
        legacy_ident, "fundbox", RecordType.IDENTITY, NameDob("Alice", "1990-01-01"), None
    )
    assert r2.promotes_person is False
    assert r2.reason == "no_key"

    # 3. Checksum invalid
    bad_chk = valid_ident.model_copy(update={"checksum_valid": False})
    r3 = nric_anchor_gate(
        bad_chk, "fundbox", RecordType.IDENTITY, NameDob("Alice", "1990-01-01"), None
    )
    assert r3.promotes_person is False
    assert r3.reason == "checksum_invalid"

    # 4. Source not promoting (e.g. match_only source sgbankruptcy)
    r4 = nric_anchor_gate(
        valid_ident, "sgbankruptcy", RecordType.IDENTITY, NameDob("Alice", "1990-01-01"), None
    )
    assert r4.promotes_person is False
    assert r4.reason == "source_not_promoting"

    # 5. Below tier floor (source promotes but tier outside floor)
    monkeypatch.setitem(SOURCE_NRIC_TRUST, "x_source", "tier_9")
    r5_floor = nric_anchor_gate(
        valid_ident, "x_source", RecordType.IDENTITY, NameDob("Alice", "1990-01-01"), None
    )
    assert r5_floor.promotes_person is False
    assert r5_floor.reason == "below_tier_floor"

    # 5b. Unknown source
    r5_unknown = nric_anchor_gate(
        valid_ident, "unknown_source", RecordType.IDENTITY, NameDob("Alice", "1990-01-01"), None
    )
    assert r5_unknown.promotes_person is False
    assert r5_unknown.reason == "source_not_promoting"

    # 6. Conversation record
    r6 = nric_anchor_gate(
        valid_ident, "fundbox", RecordType.CONVERSATION, NameDob("Alice", "1990-01-01"), None
    )
    assert r6.promotes_person is False
    assert r6.reason == "conversation_record"

    # 7. Contradiction: DOB different
    r7_dob = nric_anchor_gate(
        valid_ident,
        "fundbox",
        RecordType.IDENTITY,
        NameDob("Alice Tan", "1990-01-01"),
        NameDob("Alice Tan", "1992-05-10"),
    )
    assert r7_dob.promotes_person is False
    assert r7_dob.reason == "nric_contradiction"

    # 7. Contradiction: Name different
    r7_name = nric_anchor_gate(
        valid_ident,
        "fundbox",
        RecordType.IDENTITY,
        NameDob("Alice Tan", "1990-01-01"),
        NameDob("Bob Lim", "1990-01-01"),
    )
    assert r7_name.promotes_person is False
    assert r7_name.reason == "nric_contradiction"

    # All pass -> anchor
    r_pass = nric_anchor_gate(
        valid_ident,
        "fundbox",
        RecordType.IDENTITY,
        NameDob("Alice Tan", "1990-01-01"),
        NameDob("ALICE TAN", "1990-01-01"),
    )
    assert r_pass.promotes_person is True
    assert r_pass.reason == "anchor"


def test_classify_person_nrics_and_alias_decision() -> None:
    d1 = VECTORS["S1234567D"]
    d2 = VECTORS["T1234567J"]

    assert classify_person_nrics([]) == "none"

    # 1 valid
    assert classify_person_nrics([NricValue(digest=d1, checksum_valid=True)]) == "single"

    # >1 distinct valid -> alias
    assert (
        classify_person_nrics(
            [
                NricValue(digest=d1, checksum_valid=True),
                NricValue(digest=d2, checksum_valid=True),
            ]
        )
        == "alias"
    )

    # >=1 values, none valid -> invalid_only
    assert (
        classify_person_nrics(
            [
                NricValue(digest=d1, checksum_valid=False),
                NricValue(digest=d2, checksum_valid=None),
            ]
        )
        == "invalid_only"
    )

    # alias_link_decision
    link = alias_link_decision(
        "p-1",
        [
            NricValue(digest=d1, checksum_valid=True),
            NricValue(digest=d2, checksum_valid=True),
        ],
    )
    assert link is not None
    assert link.kind == "alias"
    assert link.person_id == "p-1"
    assert link.nric_keys == tuple(sorted([d1, d2]))
