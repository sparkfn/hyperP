"""Collision-resistance contract for SourceRecord version uniqueness keys."""

from __future__ import annotations

import pytest
from src.source_version_keys import encode_source_version_key


def test_delimiters_in_different_components_cannot_collide() -> None:
    left = encode_source_version_key("a|b", "c", "d")
    right = encode_source_version_key("a", "b|c", "d")

    assert left != right


def test_literal_suffix_shaped_version_cannot_collide_with_duplicate_key() -> None:
    literal = encode_source_version_key("a", "b", "d|legacy-duplicate|pk")
    duplicate = encode_source_version_key("a", "b", "d", duplicate_discriminator="pk")

    assert literal != duplicate


def test_encoder_is_deterministic_and_length_prefixed() -> None:
    first = encode_source_version_key("source", "record", "10")
    second = encode_source_version_key("source", "record", "10")

    assert first == second
    assert first == "sv1:6:source6:record2:100:"


def test_instance_scoped_identity_changes_the_source_version_key() -> None:
    first = encode_source_version_key(
        "bitrix_chat",
        "bitrix-crm-contact-42",
        "1",
        source_instance_id="bitrix-primary",
    )
    second = encode_source_version_key(
        "bitrix_chat",
        "bitrix-crm-contact-42",
        "1",
        source_instance_id="bitrix-secondary",
    )

    assert first != second
    assert first == "sv2:11:bitrix_chat14:bitrix-primary21:bitrix-crm-contact-421:10:"


def test_legacy_source_version_key_remains_byte_stable_without_an_instance() -> None:
    assert encode_source_version_key("source", "record", "10") == "sv1:6:source6:record2:100:"


def test_instance_scoped_key_rejects_noncanonical_instance_ids() -> None:
    with pytest.raises(ValueError, match="canonical non-secret slug"):
        encode_source_version_key("source", "record", "1", source_instance_id=" ")
