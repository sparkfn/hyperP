"""Tests for golden-profile NRIC masking across new, legacy, and C1-shaped rows."""

from __future__ import annotations

from src.golden_profile import _apply_survivorship
from src.golden_profile_active_authority import _derived_fields
from src.normalizers.nric import nric_digest

TEST_KEY = "test-nric-hmac-key-v1"


def test_legacy_survivorship_preferred_nric_masking() -> None:
    digest = nric_digest("S1234567D", TEST_KEY)

    # 1. Keyed new write with nric_masked
    id1 = [
        {
            "identifier_type": "nric",
            "normalized_value": digest,
            "is_verified": True,
            "last_confirmed_at": "2026-01-01T00:00:00Z",
            "nric_masked": "S••••567D",
        }
    ]
    fields1 = _apply_survivorship([], id1, [])
    assert fields1["preferred_nric"] == "S••••567D"

    # 2. Legacy plaintext row without nric_masked
    id2 = [
        {
            "identifier_type": "nric",
            "normalized_value": "S1234567D",
            "is_verified": True,
            "last_confirmed_at": "2026-01-01T00:00:00Z",
            "nric_masked": None,
        }
    ]
    fields2 = _apply_survivorship([], id2, [])
    assert fields2["preferred_nric"] == "S••••567D"

    # 3. C1-shaped row: digest-shaped normalized_value without nric_masked -> None
    # (never leak bullets from digest)
    id3 = [
        {
            "identifier_type": "nric",
            "normalized_value": digest,
            "is_verified": True,
            "last_confirmed_at": "2026-01-01T00:00:00Z",
            "nric_masked": None,
        }
    ]
    fields3 = _apply_survivorship([], id3, [])
    assert fields3["preferred_nric"] is None


def test_active_authority_derived_fields_nric_masking() -> None:
    digest = nric_digest("S1234567D", TEST_KEY)

    # 1. Keyed row with nric_masked
    ident1 = (
        {
            "identifier_type": "nric",
            "normalized_value": digest,
            "is_verified": True,
            "last_confirmed_at": "2026-01-01T00:00:00Z",
            "source_record_pk": "sr-1",
            "nric_masked": "S••••567D",
        },
    )
    fields1 = _derived_fields((), ident1, ())
    assert fields1["preferred_nric"] == "S••••567D"

    # 2. Legacy row
    ident2 = (
        {
            "identifier_type": "nric",
            "normalized_value": "S1234567D",
            "is_verified": True,
            "last_confirmed_at": "2026-01-01T00:00:00Z",
            "source_record_pk": "sr-2",
            "nric_masked": None,
        },
    )
    fields2 = _derived_fields((), ident2, ())
    assert fields2["preferred_nric"] == "S••••567D"

    # 3. C1-shaped row
    ident3 = (
        {
            "identifier_type": "nric",
            "normalized_value": digest,
            "is_verified": True,
            "last_confirmed_at": "2026-01-01T00:00:00Z",
            "source_record_pk": "sr-3",
            "nric_masked": None,
        },
    )
    fields3 = _derived_fields((), ident3, ())
    assert fields3["preferred_nric"] is None
