"""Name normalization — NFC normalize, collapse whitespace, detect placeholders."""

from __future__ import annotations

import re
import unicodedata

from src.models import QualityFlag

_PLACEHOLDER_PATTERNS = frozenset({
    "na",
    "n/a",
    "-",
    "unknown",
    "nil",
    "none",
    "test",
    "tbc",
    "tba",
    "customer",
    "guest",
    "walk-in",
    "walkin",
    "walk in",
    "no name",
})


def normalize_name(raw: str) -> tuple[str | None, QualityFlag]:
    """Return ``(normalized_name, quality_flag)`` for a raw name string.

    Normalization steps:
    1. Unicode NFC normalization
    2. Strip leading/trailing whitespace
    3. Collapse internal whitespace to single spaces
    4. Detect placeholder values
    """
    if not raw or not raw.strip():
        return None, QualityFlag.INVALID_FORMAT

    # NFC normalize
    nfc = unicodedata.normalize("NFC", raw)

    # Strip and collapse whitespace
    collapsed = re.sub(r"\s+", " ", nfc).strip()

    if not collapsed:
        return None, QualityFlag.INVALID_FORMAT

    # Detect placeholders (case-insensitive)
    if collapsed.lower() in _PLACEHOLDER_PATTERNS:
        return None, QualityFlag.PLACEHOLDER_VALUE

    return collapsed, QualityFlag.VALID
