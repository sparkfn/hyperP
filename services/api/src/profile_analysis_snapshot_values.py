"""Reviewed scalar values permitted in redacted profile-analysis snapshots."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date
from re import IGNORECASE, compile, fullmatch

_EMAIL_PATTERN = compile(r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w-])")
_NRIC_PATTERN = compile(r"(?<![A-Za-z0-9])[STFGM]\d{7}[A-Z](?![A-Za-z0-9])", IGNORECASE)
_POSTAL_PATTERN = compile(r"(?<!\d)\d{6}(?!\d)")
_VEHICLE_PLATE_PATTERN = compile(
    r"(?<![A-Za-z0-9])[A-Z]{1,3}\s?\d{1,4}[A-Z](?![A-Za-z0-9])",
    IGNORECASE,
)
_ADDRESS_PATTERN = compile(
    r"\b\d+[A-Za-z]?(?:\s+[A-Za-z][A-Za-z.'-]*){1,5}\s+"
    r"(?:street|st|road|rd|avenue|ave|lane|ln|drive|dr|boulevard|blvd|way)\b",
    IGNORECASE,
)
_PHONE_CANDIDATE_PATTERN = compile(r"(?<!\w)\+?\d(?:[\d\s().-]{6,}\d)(?!\w)")
_ISO_DATE_PATTERN = compile(r"\b\d{4}-\d{2}-\d{2}\b")


def contains_direct_identifier_pattern(value: str) -> bool:
    """Conservatively detect direct identifiers in untrusted copied text."""
    if any(
        pattern.search(value) is not None
        for pattern in (
            _EMAIL_PATTERN,
            _NRIC_PATTERN,
            _POSTAL_PATTERN,
            _VEHICLE_PLATE_PATTERN,
            _ADDRESS_PATTERN,
        )
    ):
        return True
    without_dates = _ISO_DATE_PATTERN.sub(" ", value)
    return any(
        sum(character.isdigit() for character in match.group()) >= 8
        for match in _PHONE_CANDIDATE_PATTERN.finditer(without_dates)
    )


@dataclass(frozen=True, slots=True)
class SafeSnapshotLabel:
    """Explicitly reviewed structured label allowed to cross the LLM boundary."""

    value: str

    def __post_init__(self) -> None:
        value = self.value
        unsafe = (
            not isinstance(value, str)
            or not value
            or len(value) > 160
            or value != value.strip()
            or len(value.splitlines()) != 1
            or "<" in value
            or ">" in value
            or any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
            or contains_direct_identifier_pattern(value)
        )
        if unsafe:
            raise ValueError("safe snapshot label must be trimmed, single-line plain text")


@dataclass(frozen=True, slots=True)
class CurrencyCode:
    """Reviewed three-letter uppercase ASCII currency code."""

    value: str

    def __post_init__(self) -> None:
        value = self.value
        if not (
            isinstance(value, str)
            and len(value) == 3
            and value.isascii()
            and value.isalpha()
            and value.isupper()
        ):
            raise ValueError("currency code must be exactly three uppercase ASCII letters")


@dataclass(frozen=True, slots=True)
class SnapshotDate:
    """Canonical date-only value permitted in a redacted snapshot."""

    value: str

    def __post_init__(self) -> None:
        value = self.value
        try:
            parsed = date.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        if (
            parsed is None
            or fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None
            or parsed.isoformat() != value
        ):
            raise ValueError("snapshot date must be canonical ISO YYYY-MM-DD")
