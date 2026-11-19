"""Tests for NRIC masking, hashing, graph node sanitization, exact lookup, and survivorship."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from src.graph.mappers import (
    _map_graph_nodes,
    _map_shared_identifiers,
    map_person,
    map_person_identifier,
    map_possible_match_detail,
)
from src.nric import (
    NRIC_PLACEHOLDER,
    canonical_nric,
    decrypt_nric,
    derive_fernet_key,
    format_masked_nric,
    is_nric_digest,
    masked_for,
    nric_digest,
    scrub_nric_text,
)
from src.repositories.neo4j.merge import (
    VALID_LITERAL_GOLDEN_FIELDS,
    _apply_golden_profile_selections_tx,
    _is_valid_golden_profile_selection,
)
from src.repositories.neo4j.person import Neo4jPersonRepository
from src.repositories.neo4j.survivorship import _field_options_tx, _is_valid_custom_value
from src.repositories.protocols.merge import GoldenProfileSelection

TEST_HMAC_KEY = "test-nric-hmac-key-v1"
EXPECTED_DIGESTS: dict[str, str] = {
    "S1234567D": "a43bb81909a911bc898702d96b9cb10a382793e492d8175abf54772f8df60b2a",
    "T1234567J": "afa8d6815558f929318f4b97610b9157b0e3507be7ca0269c5d846ca4c0cf0c2",
    "F1234567N": "c548c0537ec2ed6bea2e86e4c37e855f30e55a006a4b11334a1bc4c9e9f2c8dd",
    "G1234567X": "bfd852f6147955514e70adb30309dccac92b8d1dfabdb65398c866aca09eb7c7",
}
EXPECTED_FERNET_KEY = b"1tX4DnEjO1YRZy0Uz74Lt37KdoBwPzfLjwPNjMrek6E="


def test_shared_cross_package_digest_and_key_derivation_vector() -> None:
    """Assert cross-package test vectors for NRIC HMAC digests and Fernet key derivation."""
    derived_fernet_key = derive_fernet_key(TEST_HMAC_KEY)
    assert derived_fernet_key == EXPECTED_FERNET_KEY

    for canonical, expected_digest in EXPECTED_DIGESTS.items():
        assert nric_digest(canonical, key=TEST_HMAC_KEY) == expected_digest

    fernet = Fernet(derived_fernet_key)
    ciphertext = fernet.encrypt(b"S1234567D").decode("ascii")
    assert decrypt_nric(ciphertext, key=TEST_HMAC_KEY) == "S1234567D"


def test_canonical_and_digest_helpers() -> None:
    assert canonical_nric("  xxS1234567d ") == "S1234567D"
    assert canonical_nric("X1234567D") == "1234567D"
    assert is_nric_digest("a43bb81909a911bc898702d96b9cb10a382793e492d8175abf54772f8df60b2a")
    assert not is_nric_digest("S1234567D")
    assert not is_nric_digest("A" * 64)


def test_format_masked_nric() -> None:
    assert format_masked_nric("S1234567D") == "S••••567D"
    assert format_masked_nric("S••••567D") == "S••••567D"  # idempotent
    assert (
        format_masked_nric("a43bb81909a911bc898702d96b9cb10a382793e492d8175abf54772f8df60b2a")
        == NRIC_PLACEHOLDER
    )
    assert format_masked_nric(None) is None


def test_masked_for_distinguishes_digest_without_mask() -> None:
    digest = "a43bb81909a911bc898702d96b9cb10a382793e492d8175abf54772f8df60b2a"
    assert masked_for("S1234567D") == "S••••567D"
    assert masked_for(digest, "S••••567D") == "S••••567D"
    # C1-shaped digest without nric_masked returns None so golden profile never leaks digest bullets
    assert masked_for(digest, None) is None
    assert masked_for(None) is None


def test_scrub_nric_text() -> None:
    reason = "Audited access for S7012164F and t0123456g verification."
    scrubbed = scrub_nric_text(reason)
    assert "S7012164F" not in scrubbed
    assert "t0123456g" not in scrubbed
    assert "S••••164F" in scrubbed
    assert "T••••456G" in scrubbed


def test_mapper_masking_person_and_identifiers() -> None:
    legacy_person = {
        "person_id": "p-1",
        "status": "active",
        "preferred_nric": "S1234567D",
    }
    mapped_legacy = map_person({"person": legacy_person, "p": legacy_person})
    assert mapped_legacy.preferred_nric == "S••••567D"

    already_masked_person = {
        "person_id": "p-2",
        "status": "active",
        "preferred_nric": "S••••567D",
    }
    mapped_masked = map_person({"person": already_masked_person, "p": already_masked_person})
    assert mapped_masked.preferred_nric == "S••••567D"

    # PersonIdentifier mapping
    legacy_id = {
        "identifier_type": "nric",
        "normalized_value": "S1234567D",
        "is_active": True,
    }
    assert map_person_identifier(legacy_id).normalized_value == "S••••567D"

    digest = "a43bb81909a911bc898702d96b9cb10a382793e492d8175abf54772f8df60b2a"
    keyed_id = {
        "identifier_type": "nric",
        "normalized_value": digest,
        "nric_masked": "S••••567D",
        "is_active": True,
    }
    assert map_person_identifier(keyed_id).normalized_value == "S••••567D"

    c1_id = {
        "identifier_type": "nric",
        "normalized_value": digest,
        "nric_masked": None,
        "is_active": True,
    }
    assert map_person_identifier(c1_id).normalized_value == NRIC_PLACEHOLDER

    # Shared identifiers mapping
    shared = _map_shared_identifiers([legacy_id, keyed_id, c1_id])
    assert shared[0].normalized_value == "S••••567D"
    assert shared[1].normalized_value == "S••••567D"
    assert shared[2].normalized_value == NRIC_PLACEHOLDER

    # Possible match detail mapping
    match_detail = map_possible_match_detail(
        [
            {
                "candidate_person_id": "cand-1",
                "candidate_name": "Alice",
                "identifier_type": "nric",
                "normalized_value": digest,
                "nric_masked": "S••••567D",
                "candidate_source_records": [],
                "current_person_source_records": [],
            }
        ]
    )
    assert match_detail.shared_identifier_groups[0].normalized_value == "S••••567D"


def test_map_graph_nodes_sanitizes_nric_identifier_and_person() -> None:
    digest = "a43bb81909a911bc898702d96b9cb10a382793e492d8175abf54772f8df60b2a"
    raw_nodes = [
        {
            "id": "node-1",
            "label": "Identifier",
            "properties": {
                "identifier_type": "nric",
                "normalized_value": digest,
                "hashed_value": digest,
                "nric_encrypted": "gAAAAABl...ciphertext",
                "nric_masked": "S••••567D",
            },
        },
        {
            "id": "node-2",
            "label": "Identifier",
            "properties": {
                "identifier_type": "nric",
                "normalized_value": "S1234567D",
            },
        },
        {
            "id": "node-3",
            "label": "Identifier",
            "properties": {
                "identifier_type": "nric",
                "normalized_value": digest,
            },
        },
        {
            "id": "node-4",
            "label": "Person",
            "properties": {
                "person_id": "p-1",
                "preferred_nric": "S1234567D",
            },
        },
    ]

    mapped = _map_graph_nodes(raw_nodes)

    # Keyed Identifier node
    props_1 = mapped[0].properties
    assert "nric_encrypted" not in props_1
    assert "hashed_value" not in props_1
    assert props_1["normalized_value"] == "S••••567D"

    # Legacy plaintext Identifier node
    props_2 = mapped[1].properties
    assert props_2["normalized_value"] == "S••••567D"

    # C1-shaped digest without nric_masked
    props_3 = mapped[2].properties
    assert props_3["normalized_value"] == NRIC_PLACEHOLDER

    # Person node
    props_4 = mapped[3].properties
    assert props_4["preferred_nric"] == "S••••567D"


def test_survivorship_custom_nric_override_rejected() -> None:
    assert not _is_valid_custom_value("preferred_nric", "S1234567D")
    assert not _is_valid_custom_value("preferred_nric", "S••••567D")


@pytest.mark.asyncio
async def test_survivorship_field_options_masks_and_aligns_is_current() -> None:
    mock_session = AsyncMock()
    mock_record = {
        "preferred_full_name": "Test User",
        "preferred_dob": "1990-01-01",
        "preferred_phone": "+6591234567",
        "preferred_email": "test@example.com",
        "preferred_nric": "S1234567D",
        "preferred_race_ethnicity": None,
        "preferred_address_id": None,
        "preferred_address_value": None,
        "overrides": "{}",
        "options": [
            {
                "field_name": "preferred_nric",
                "source_kind": "identifier",
                "identifier_type": "nric",
                "value": "S1234567D",
                "nric_masked": None,
                "source_record_pk": "sr-1",
                "source_system": "fundbox",
                "entity_display_name": None,
                "observed_at": "2026-05-01T00:00:00Z",
            }
        ],
    }
    mock_cursor = MagicMock()
    mock_cursor.single = AsyncMock(return_value=mock_record)
    mock_session.run = AsyncMock(return_value=mock_cursor)

    data = await _field_options_tx(mock_session, "p-1")
    assert data is not None
    # Current value is masked
    assert data.preferred_nric == "S••••567D"
    # Option value is masked with the exact same representation
    assert len(data.options) == 1
    assert data.options[0].value == "S••••567D"
    assert data.options[0].value == data.preferred_nric


def test_merge_selection_rejects_literal_nric() -> None:
    assert "preferred_nric" not in VALID_LITERAL_GOLDEN_FIELDS

    literal_selection: GoldenProfileSelection = {
        "field_name": "preferred_nric",
        "source_kind": "literal",
        "selected_value": "S1234567D",
        "source_record_pk": None,
        "identifier_type": None,
    }
    assert not _is_valid_golden_profile_selection(literal_selection)

    identifier_selection: GoldenProfileSelection = {
        "field_name": "preferred_nric",
        "source_kind": "identifier",
        "selected_value": "S1234567D",
        "source_record_pk": "sr-1",
        "identifier_type": "nric",
    }
    assert _is_valid_golden_profile_selection(identifier_selection)


@pytest.mark.asyncio
async def test_merge_selection_stores_masked_nric() -> None:
    mock_tx = AsyncMock()
    selections: list[GoldenProfileSelection] = [
        {
            "field_name": "preferred_nric",
            "source_kind": "identifier",
            "selected_value": "S1234567D",
            "source_record_pk": "sr-1",
            "identifier_type": "nric",
        }
    ]
    await _apply_golden_profile_selections_tx(mock_tx, "p-1", selections)
    mock_tx.run.assert_called_once()
    call_kwargs = mock_tx.run.call_args.kwargs
    assert call_kwargs["person_id"] == "p-1"
    updates = call_kwargs["updates"]
    assert len(updates) == 1
    assert updates[0]["field_name"] == "preferred_nric"
    assert updates[0]["value"] == "S••••567D"


@pytest.mark.asyncio
async def test_exact_lookup_passes_digest_and_canonical_when_keyed() -> None:
    repo = Neo4jPersonRepository()
    mock_session = AsyncMock()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__.return_value = mock_session
    mock_session_ctx.__aexit__.return_value = None

    mock_result = MagicMock()

    async def _empty_gen() -> AsyncIterator[MagicMock]:
        return
        yield

    mock_result.__aiter__ = lambda self: _empty_gen()
    mock_session.run = AsyncMock(return_value=mock_result)

    with (
        patch("src.repositories.neo4j.person.get_session", return_value=mock_session_ctx),
        patch("src.repositories.neo4j.person.config.nric_hmac_key", "test-key"),
    ):
        await repo.search_by_identifier("nric", "S1234567D")
        mock_session.run.assert_called_once()
        passed_kwargs = mock_session.run.call_args.kwargs
        assert passed_kwargs["identifier_type"] == "nric"
        values = passed_kwargs["values"]
        assert len(values) == 2
        # First is the digest, second is canonical
        assert is_nric_digest(values[0])
        assert values[1] == "S1234567D"


@pytest.mark.asyncio
async def test_exact_lookup_passes_only_canonical_when_unkeyed() -> None:
    repo = Neo4jPersonRepository()
    mock_session = AsyncMock()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__.return_value = mock_session
    mock_session_ctx.__aexit__.return_value = None

    mock_result = MagicMock()

    async def _empty_gen() -> AsyncIterator[MagicMock]:
        return
        yield

    mock_result.__aiter__ = lambda self: _empty_gen()
    mock_session.run = AsyncMock(return_value=mock_result)

    with (
        patch("src.repositories.neo4j.person.get_session", return_value=mock_session_ctx),
        patch("src.repositories.neo4j.person.config.nric_hmac_key", ""),
    ):
        await repo.search_by_identifier("nric", "  s1234567d ")
        mock_session.run.assert_called_once()
        passed_kwargs = mock_session.run.call_args.kwargs
        assert passed_kwargs["identifier_type"] == "nric"
        assert passed_kwargs["values"] == ["S1234567D"]
