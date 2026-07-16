"""Collision-resistance contract for SourceRecord version uniqueness keys."""

from __future__ import annotations

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
