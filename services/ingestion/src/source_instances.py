"""Canonical non-secret identifiers for registered source instances."""

from __future__ import annotations

import re

_SOURCE_INSTANCE_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?")

# All pre-source-instance records are deterministically assigned this namespace.
# It is deliberately a valid public slug rather than an endpoint, URL, or tenant name.
LEGACY_DEFAULT_SOURCE_INSTANCE_ID = "legacy-default"


def canonical_source_instance_id(value: str, *, field_name: str = "source_instance_id") -> str:
    """Validate one bounded config-safe source-instance slug.

    IDs deliberately exclude URL punctuation and whitespace so credential-bearing
    webhook URLs cannot be copied into graph provenance by accident.
    """
    if _SOURCE_INSTANCE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical non-secret slug of 1 to 64 characters")
    return value


def effective_source_instance_id(value: str | None) -> str:
    """Return an explicit instance ID, preserving legacy streams in one namespace."""
    if value is None:
        return LEGACY_DEFAULT_SOURCE_INSTANCE_ID
    return canonical_source_instance_id(value)
