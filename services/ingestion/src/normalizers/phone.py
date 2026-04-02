"""Phone number normalization to E.164 format.

Default region: SG (Singapore).
"""

from __future__ import annotations

import phonenumbers
from phonenumbers import NumberParseException

from src.models import QualityFlag

DEFAULT_REGION = "SG"

_PLACEHOLDER_PATTERNS = frozenset({
    "00000000",
    "11111111",
    "12345678",
    "99999999",
    "0000000000",
})


def normalize_phone(
    raw: str,
    *,
    region: str = DEFAULT_REGION,
) -> tuple[str | None, QualityFlag]:
    """Return ``(normalized_e164, quality_flag)`` for a raw phone string.

    Returns ``(None, 'invalid_format')`` when the input cannot be parsed,
    and ``(normalized, 'placeholder_value')`` for known placeholder numbers.
    """
    stripped = raw.strip()
    if not stripped:
        return None, QualityFlag.INVALID_FORMAT

    # Detect obvious placeholders before parsing
    digits_only = "".join(c for c in stripped if c.isdigit())
    if digits_only in _PLACEHOLDER_PATTERNS:
        return None, QualityFlag.PLACEHOLDER_VALUE

    try:
        parsed = phonenumbers.parse(stripped, region)
    except NumberParseException:
        return None, QualityFlag.INVALID_FORMAT

    if not phonenumbers.is_valid_number(parsed):
        return None, QualityFlag.INVALID_FORMAT

    normalized = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    return normalized, QualityFlag.VALID
