"""Address normalization — regex-based parsing into structured components.

Designed for Singapore addresses as the primary format, with a fallback
partial parse for other formats.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.models import QualityFlag

# Matches common Singapore address patterns:
#   "#05-123 10 Example Street Singapore 123456"
#   "10 Example Street, Singapore 123456"
#   "Blk 10 Example Street #05-123 Singapore 123456"
_SG_ADDRESS_RE = re.compile(
    r"(?:#?(?P<unit>\d{1,3}-\d{1,4})\s+)?"       # optional unit e.g. #05-123
    r"(?:(?:Blk|Block)\s+)?"                        # optional Blk/Block prefix
    r"(?P<street_num>\d+[A-Za-z]?)\s+"              # street number
    r"(?P<street_name>.+?)"                          # street name (non-greedy)
    r"(?:\s*,?\s*(?:Singapore|SG))?"                 # optional city marker
    r"\s+(?P<postal>\d{6})"                          # 6-digit postal code
    r"\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class NormalizedAddress:
    """Structured, normalized address ready for graph storage."""

    unit_number: str | None
    street_number: str
    street_name: str
    building_name: str | None
    city: str
    state_province: str | None
    postal_code: str
    country_code: str
    normalized_full: str


def normalize_address(
    raw: str,
    *,
    default_country: str = "SG",
    default_city: str = "Singapore",
) -> tuple[NormalizedAddress | None, QualityFlag]:
    """Parse a raw address string into structured components.

    Returns ``(NormalizedAddress, quality_flag)``.  If the address cannot be
    parsed at all, returns ``(None, 'invalid_format')``.
    """
    stripped = raw.strip()
    if not stripped:
        return None, QualityFlag.INVALID_FORMAT

    # Detect obvious placeholders
    lowered = stripped.lower()
    if lowered in {"na", "n/a", "-", "unknown", "nil", "none", "test", "tbc", "tba"}:
        return None, QualityFlag.PLACEHOLDER_VALUE

    match = _SG_ADDRESS_RE.match(stripped)
    if not match:
        # Attempt minimal parse: just extract postal code if present
        postal_match = re.search(r"\b(\d{6})\b", stripped)
        if postal_match:
            postal = postal_match.group(1)
            full = re.sub(r"\s+", " ", stripped).strip().lower()
            addr = NormalizedAddress(
                unit_number=None,
                street_number="",
                street_name=full,
                building_name=None,
                city=default_city.lower(),
                state_province=None,
                postal_code=postal,
                country_code=default_country.upper(),
                normalized_full=full,
            )
            return addr, QualityFlag.PARTIAL_PARSE

        return None, QualityFlag.INVALID_FORMAT

    unit = match.group("unit")
    street_num = match.group("street_num").strip().lower()
    street_name = re.sub(r"\s+", " ", match.group("street_name")).strip().lower()
    postal = match.group("postal")

    city = default_city.lower()
    country = default_country.upper()

    parts = [street_num, street_name]
    if unit:
        parts.insert(0, f"#{unit}")
    parts.append(f"{city} {postal}")
    parts.append(country.lower())
    normalized_full = ", ".join(parts)

    addr = NormalizedAddress(
        unit_number=unit,
        street_number=street_num,
        street_name=street_name,
        building_name=None,
        city=city,
        state_province=None,
        postal_code=postal,
        country_code=country,
        normalized_full=normalized_full,
    )
    return addr, QualityFlag.VALID
