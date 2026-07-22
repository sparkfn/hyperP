"""Safe validation for generated profile-analysis plain text."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from src.profile_analysis_snapshot import (
    KnownSensitiveValue,
    ProfileAnalysisSnapshot,
    compact_sensitive_text,
    finite_sensitive_decimal,
    normalize_sensitive_text,
)
from src.profile_analysis_snapshot_values import contains_direct_identifier_pattern

_MAX_OUTPUT_WORDS = 350
_MAX_OUTPUT_CHARACTERS = 12_000
_EVIDENCE_REFERENCE = re.compile(r"\b(?:source|order|vehicle|relationship)-[a-z0-9_-]+\b")
_HTML_ELEMENT = re.compile(r"</?[A-Za-z][^>]*>")
_NUMERIC_TOKEN = re.compile(r"(?<![\w-])[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?![\w-])")
_FORMATTED_IDENTIFIER = re.compile(r"(?<!\w)[+\d](?:[\d\s+()-]|\.(?=\d))*\d(?!\w)")
_IDENTIFIER_CHARACTERS = re.compile(r"[\d\s+().-]+")
_DAY_FIRST_DATE = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?!\d)")


class ProfileAnalysisOutputError(ValueError):
    """Generated text violates the safe output contract."""


class ProfileAnalysisPrivacyOutputError(ProfileAnalysisOutputError):
    """Generated text contains a repository-supplied sensitive value."""


def validate_profile_analysis_output(
    output: str,
    evidence_references: frozenset[str],
    known_sensitive_values: Sequence[KnownSensitiveValue],
) -> str:
    """Return valid bounded plain text without including unsafe data in errors."""
    if not output or output != output.strip():
        raise ProfileAnalysisOutputError("profile analysis output must be trimmed plain text")
    if len(output) > _MAX_OUTPUT_CHARACTERS or len(output.split()) > _MAX_OUTPUT_WORDS:
        raise ProfileAnalysisOutputError("profile analysis output exceeds the size limit")
    if "```" in output or "~~~" in output or _HTML_ELEMENT.search(output) is not None:
        raise ProfileAnalysisOutputError("profile analysis output is not plain text")
    if re.search(r"(?m)^Limitations:", output) is None:
        raise ProfileAnalysisOutputError("profile analysis output lacks limitations")
    referenced = frozenset(_EVIDENCE_REFERENCE.findall(output.casefold()))
    if not referenced.issubset(evidence_references):
        raise ProfileAnalysisOutputError("profile analysis output cites unknown evidence")
    output_without_references = _without_evidence_references(output, referenced)
    if contains_direct_identifier_pattern(output_without_references):
        raise ProfileAnalysisPrivacyOutputError(
            "profile analysis output contains a direct identifier pattern"
        )
    if _contains_sensitive_value(output, known_sensitive_values, referenced):
        raise ProfileAnalysisPrivacyOutputError(
            "profile analysis output contains a known sensitive value"
        )
    return output


def snapshot_evidence_references(snapshot: ProfileAnalysisSnapshot) -> frozenset[str]:
    """Read the allowlist only from typed fields, never dynamic snapshot labels."""
    references = {item.evidence_ref for item in snapshot.sources}
    references.update(item.evidence_ref for item in snapshot.orders)
    references.update(item.evidence_ref for item in snapshot.vehicles)
    references.update(item.evidence_ref for item in snapshot.relationships)
    return frozenset(references)


def _contains_sensitive_value(
    output: str,
    known_sensitive_values: Sequence[KnownSensitiveValue],
    evidence_references: frozenset[str],
) -> bool:
    normalized_output = _without_evidence_references(output, evidence_references)
    numeric_values: set[Decimal] = set()
    identifier_digits: set[str] = set()
    sensitive_dates: set[date] = set()
    compact_text_values: list[str] = []
    long_text_values: list[str] = []
    short_text_values: list[str] = []
    for sensitive in known_sensitive_values:
        if isinstance(sensitive, bool):
            continue
        normalized = normalize_sensitive_text(str(sensitive))
        if not normalized:
            continue
        if isinstance(sensitive, str):
            try:
                sensitive_dates.add(date.fromisoformat(normalized))
            except ValueError:
                pass
            compact_value = compact_sensitive_text(normalized)
            if len(compact_value) >= 4:
                compact_text_values.append(compact_value)
            digits = _identifier_digit_sequence(normalized)
            if digits is not None:
                identifier_digits.add(digits)
            elif len(normalized) >= 4:
                long_text_values.append(normalized)
            else:
                short_text_values.append(normalized)
            continue
        numeric = finite_sensitive_decimal(normalized)
        if numeric is not None:
            numeric_values.add(numeric)
    output_numbers = {
        numeric
        for match in _NUMERIC_TOKEN.finditer(normalized_output)
        if (numeric := finite_sensitive_decimal(match.group())) is not None
    }
    if numeric_values.intersection(output_numbers):
        return True
    output_identifier_digits = {
        "".join(character for character in match.group() if character.isdigit())
        for match in _FORMATTED_IDENTIFIER.finditer(normalized_output)
    }
    if identifier_digits.intersection(output_identifier_digits):
        return True
    if sensitive_dates.intersection(_day_first_dates(normalized_output)):
        return True
    compact_output = compact_sensitive_text(normalized_output)
    if any(value in compact_output for value in compact_text_values):
        return True
    if any(value in normalized_output for value in long_text_values):
        return True
    return any(
        re.search(rf"(?<!\w){re.escape(value)}(?!\w)", normalized_output) is not None
        for value in short_text_values
    )


def _without_evidence_references(
    output: str,
    evidence_references: frozenset[str],
) -> str:
    normalized = normalize_sensitive_text(output)
    for reference in sorted(evidence_references, key=len, reverse=True):
        normalized_reference = normalize_sensitive_text(reference)
        normalized = re.sub(
            rf"(?<!\w){re.escape(normalized_reference)}(?!\w)",
            " ",
            normalized,
        )
    return normalized


def _identifier_digit_sequence(value: str) -> str | None:
    if _IDENTIFIER_CHARACTERS.fullmatch(value) is None:
        return None
    digits = "".join(character for character in value if character.isdigit())
    return digits if len(digits) >= 4 else None


def _day_first_dates(value: str) -> set[date]:
    parsed: set[date] = set()
    for match in _DAY_FIRST_DATE.finditer(value):
        day, month, year = (int(part) for part in match.groups())
        try:
            parsed.add(date(year, month, day))
        except ValueError:
            continue
    return parsed
