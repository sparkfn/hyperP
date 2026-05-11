"""Shared hard-exclusion helpers for ingestion connectors."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.connectors.chat_helpers import ExtractedPerson, ExtractionResult
from src.normalizers.email import normalize_email
from src.normalizers.name import normalize_name
from src.normalizers.phone import normalize_phone


@dataclass(frozen=True)
class ExclusionContext:
    """Normalized identifiers that must not enter profile matching."""

    phones: frozenset[str] = field(default_factory=frozenset)
    emails: frozenset[str] = field(default_factory=frozenset)
    names: frozenset[str] = field(default_factory=frozenset)
    source_ids: frozenset[str] = field(default_factory=frozenset)


def normalize_excluded_phone(value: str | None) -> str | None:
    if not value:
        return None
    normalized, _quality = normalize_phone(value)
    return normalized


def normalize_excluded_email(value: str | None) -> str | None:
    if not value:
        return None
    normalized, _quality = normalize_email(value)
    return normalized


def normalize_excluded_name(value: str | None) -> str | None:
    if not value:
        return None
    normalized, _quality = normalize_name(value)
    return normalized.lower() if normalized is not None else None


def normalized_phone_set(values: list[str]) -> frozenset[str]:
    return frozenset(v for value in values if (v := normalize_excluded_phone(value)) is not None)


def normalized_email_set(values: list[str]) -> frozenset[str]:
    return frozenset(v for value in values if (v := normalize_excluded_email(value)) is not None)


def normalized_name_set(values: list[str]) -> frozenset[str]:
    return frozenset(v for value in values if (v := normalize_excluded_name(value)) is not None)


def is_excluded_phone(value: str | None, context: ExclusionContext) -> bool:
    normalized = normalize_excluded_phone(value)
    return normalized is not None and normalized in context.phones


def is_excluded_email(value: str | None, context: ExclusionContext) -> bool:
    normalized = normalize_excluded_email(value)
    return normalized is not None and normalized in context.emails


def is_excluded_name(value: str | None, context: ExclusionContext) -> bool:
    normalized = normalize_excluded_name(value)
    return normalized is not None and normalized in context.names


def is_excluded_source_id(value: str | None, context: ExclusionContext) -> bool:
    if not value:
        return False
    return value.strip().lower() in context.source_ids


def is_excluded_person(person: ExtractedPerson, context: ExclusionContext) -> bool:
    return (
        is_excluded_phone(person.get("phone"), context)
        or is_excluded_email(person.get("email"), context)
        or is_excluded_name(person.get("name"), context)
    )


def filter_extraction(
    extraction: ExtractionResult,
    context: ExclusionContext,
) -> ExtractionResult | None:
    persons = [
        person for person in extraction["persons"] if not is_excluded_person(person, context)
    ]
    if not persons:
        return None
    return ExtractionResult(
        persons=persons,
        transactions=extraction["transactions"],
        summary=extraction["summary"],
        confidence=extraction["confidence"],
    )
