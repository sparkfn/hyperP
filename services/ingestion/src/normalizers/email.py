"""Email normalization and validation."""

from __future__ import annotations

import re

from src.models import QualityFlag

# Deliberately permissive — catches most real-world addresses.
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}$"
)

_PLACEHOLDER_PATTERNS = frozenset({
    "test@test.com",
    "na@na.com",
    "noreply@noreply.com",
    "unknown@unknown.com",
    "test@example.com",
    "null@null.com",
})


def normalize_email(raw: str) -> tuple[str | None, QualityFlag]:
    """Return ``(normalized_email, quality_flag)`` for a raw email string.

    Normalization: lowercase, strip whitespace.
    """
    stripped = raw.strip().lower()
    if not stripped:
        return None, QualityFlag.INVALID_FORMAT

    if stripped in _PLACEHOLDER_PATTERNS:
        return None, QualityFlag.PLACEHOLDER_VALUE

    if not _EMAIL_RE.match(stripped):
        return None, QualityFlag.INVALID_FORMAT

    return stripped, QualityFlag.VALID
