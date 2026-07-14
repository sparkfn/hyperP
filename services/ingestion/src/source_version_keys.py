"""Collision-safe encoding for immutable SourceRecord version keys."""

from __future__ import annotations


def _component(value: str) -> str:
    return f"{len(value)}:{value}"


def encode_source_version_key(
    source_system: str,
    source_record_id: str,
    source_record_version: str,
    *,
    duplicate_discriminator: str | None = None,
) -> str:
    """Encode source identity, version, and optional legacy duplicate injectively."""
    discriminator = duplicate_discriminator or ""
    return "sv1:" + "".join(
        _component(value)
        for value in (
            source_system,
            source_record_id,
            source_record_version,
            discriminator,
        )
    )
