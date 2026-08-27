"""Canonical non-secret identifiers for registered source instances."""

from __future__ import annotations

import re

_SOURCE_INSTANCE_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")

# All pre-source-instance records are deterministically assigned this namespace.
# It is deliberately a valid public slug rather than an endpoint, URL, or tenant name.
LEGACY_DEFAULT_SOURCE_INSTANCE_ID = "legacy-default"
# Control identity is separate from SourceRecord provenance. The compatibility
# value is identical so old task payloads and lock names remain unchanged.
LEGACY_DEFAULT_CONTROL_INSTANCE_ID = LEGACY_DEFAULT_SOURCE_INSTANCE_ID


def canonical_source_instance_id(
    value: str,
    *,
    field_name: str = "source_instance_id",
    allow_legacy_default: bool = False,
) -> str:
    """Validate one bounded config-safe source-instance slug.

    IDs deliberately exclude URL punctuation and whitespace so credential-bearing
    webhook URLs cannot be copied into graph provenance by accident.
    """
    if (
        value == LEGACY_DEFAULT_SOURCE_INSTANCE_ID and not allow_legacy_default
    ) or _SOURCE_INSTANCE_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must be a canonical non-secret slug of 1 to 64 characters "
            "and must not use a reserved namespace"
        )
    return value


def effective_source_instance_id(value: str | None) -> str:
    """Return an explicit instance ID, preserving legacy streams in one namespace."""
    if value is None or value == LEGACY_DEFAULT_SOURCE_INSTANCE_ID:
        return LEGACY_DEFAULT_SOURCE_INSTANCE_ID
    return canonical_source_instance_id(value)


def effective_control_instance_id(value: str | None) -> str:
    """Resolve omitted control identity without changing legacy callers."""
    if value is None or value == LEGACY_DEFAULT_CONTROL_INSTANCE_ID:
        return LEGACY_DEFAULT_CONTROL_INSTANCE_ID
    return canonical_source_instance_id(value, field_name="control_instance_id")


def scope_control_identity(base: str, control_instance_id: str) -> str:
    """Scope future control identifiers while preserving every legacy string."""
    if not base:
        raise ValueError("base control identity must be non-empty")
    canonical = effective_control_instance_id(control_instance_id)
    if canonical == LEGACY_DEFAULT_CONTROL_INSTANCE_ID:
        return base
    return f"ci1:{len(canonical)}:{canonical}:{base}"
