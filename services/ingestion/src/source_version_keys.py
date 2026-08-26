"""Collision-safe encoding for immutable SourceRecord version keys."""

from __future__ import annotations

from src.source_instances import (
    LEGACY_DEFAULT_SOURCE_INSTANCE_ID,
    canonical_source_instance_id,
)


def _component(value: str) -> str:
    return f"{len(value)}:{value}"


def encode_source_version_key(
    source_system: str,
    source_record_id: str,
    source_record_version: str,
    *,
    source_instance_id: str | None = None,
    duplicate_discriminator: str | None = None,
) -> str:
    """Encode a legacy or instance-scoped source identity and immutable version.

    The legacy ``sv1`` shape is retained byte-for-byte until the graph lifecycle
    migration assigns every existing record its explicit default source instance.
    ``sv2`` is reserved for the new triple-keyed identity and must not be emitted
    by a recurring source until that migration and its graph constraints are live.
    """
    discriminator = duplicate_discriminator or ""
    components: tuple[str, ...]
    if source_instance_id is None:
        components = (
            source_system,
            source_record_id,
            source_record_version,
            discriminator,
        )
        prefix = "sv1:"
    else:
        canonical_source_instance_id(
            source_instance_id,
            allow_legacy_default=(source_instance_id == LEGACY_DEFAULT_SOURCE_INSTANCE_ID),
        )
        components = (
            source_system,
            source_instance_id,
            source_record_id,
            source_record_version,
            discriminator,
        )
        prefix = "sv2:"
    return prefix + "".join(_component(value) for value in components)
