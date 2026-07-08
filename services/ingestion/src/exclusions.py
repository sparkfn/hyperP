"""Shared hard-exclusion helpers for ingestion connectors."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.connectors.chat_helpers import (
    ExtractedPerson,
    ExtractedPossiblePerson,
    ExtractedStrongIdentifier,
    ExtractionResult,
)
from src.exclusion_config import ExclusionFile, VehicleIdentifierExclusion
from src.normalizers.email import normalize_email
from src.normalizers.name import normalize_name
from src.normalizers.phone import normalize_phone
from src.vehicles import (
    VehicleObservation,
    normalize_lta_tag,
    normalize_serial_number,
    normalize_vehicle_product,
)


@dataclass(frozen=True)
class VehicleIdentifierKey:
    """Vehicle identifier key (product name + LTA tag and/or serial number)."""

    vehicle_product: str
    lta_tag: str | None = None
    serial_number: str | None = None


@dataclass(frozen=True)
class ExclusionContext:
    """Normalized identifiers that must not enter profile matching."""

    phones: frozenset[str] = field(default_factory=frozenset)
    emails: frozenset[str] = field(default_factory=frozenset)
    email_domains: frozenset[str] = field(default_factory=frozenset)
    names: frozenset[str] = field(default_factory=frozenset)
    source_ids: frozenset[str] = field(default_factory=frozenset)
    vehicle_identifiers: frozenset[VehicleIdentifierKey] = field(default_factory=frozenset)


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


def normalize_excluded_email_domain(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower().removeprefix("@").rstrip(".")
    if not normalized or "@" in normalized:
        return None
    return normalized


def email_matches_domain(email: str, domain: str) -> bool:
    email_domain = email.rsplit("@", maxsplit=1)[1]
    return email_domain == domain or email_domain.endswith(f".{domain}")


def normalize_excluded_name(value: str | None) -> str | None:
    if not value:
        return None
    normalized, _quality = normalize_name(value)
    return normalized.lower() if normalized is not None else None


def normalized_phone_set(values: list[str]) -> frozenset[str]:
    return frozenset(v for value in values if (v := normalize_excluded_phone(value)) is not None)


def normalized_email_set(values: list[str]) -> frozenset[str]:
    return frozenset(v for value in values if (v := normalize_excluded_email(value)) is not None)


def normalized_email_domain_set(values: list[str]) -> frozenset[str]:
    return frozenset(
        v for value in values if (v := normalize_excluded_email_domain(value)) is not None
    )


def normalized_name_set(values: list[str]) -> frozenset[str]:
    return frozenset(v for value in values if (v := normalize_excluded_name(value)) is not None)


def normalized_vehicle_identifier_set(
    values: list[VehicleIdentifierExclusion],
) -> frozenset[VehicleIdentifierKey]:
    keys: set[VehicleIdentifierKey] = set()
    for value in values:
        vehicle_product = normalize_vehicle_product(value.get("vehicle_product"))
        if vehicle_product is None:
            continue
        lta_tag = normalize_lta_tag(value.get("lta_tag"))
        serial_number = normalize_serial_number(value.get("serial_number"))
        if lta_tag is None and serial_number is None:
            continue
        keys.add(
            VehicleIdentifierKey(
                vehicle_product=vehicle_product,
                lta_tag=lta_tag,
                serial_number=serial_number,
            )
        )
    return frozenset(keys)


def build_exclusion_context(
    *,
    company_mobile_numbers: list[str],
    company_email_addresses: list[str],
    internal_person_names: list[str],
    file_exclusions: ExclusionFile,
) -> ExclusionContext:
    return ExclusionContext(
        phones=normalized_phone_set(company_mobile_numbers + file_exclusions.phones),
        emails=normalized_email_set(company_email_addresses + file_exclusions.emails),
        email_domains=normalized_email_domain_set(file_exclusions.email_domains),
        names=normalized_name_set(internal_person_names + file_exclusions.names),
        source_ids=frozenset(
            value.strip().lower() for value in file_exclusions.source_ids if value.strip()
        ),
        vehicle_identifiers=normalized_vehicle_identifier_set(file_exclusions.vehicle_identifiers),
    )


def is_excluded_phone(value: str | None, context: ExclusionContext) -> bool:
    normalized = normalize_excluded_phone(value)
    return normalized is not None and normalized in context.phones


def is_excluded_email(value: str | None, context: ExclusionContext) -> bool:
    normalized = normalize_excluded_email(value)
    if normalized is None:
        return False
    if normalized in context.emails:
        return True
    return any(email_matches_domain(normalized, domain) for domain in context.email_domains)


def is_excluded_name(value: str | None, context: ExclusionContext) -> bool:
    normalized = normalize_excluded_name(value)
    return normalized is not None and normalized in context.names


def is_excluded_source_id(value: str | None, context: ExclusionContext) -> bool:
    if not value:
        return False
    return value.strip().lower() in context.source_ids


def is_excluded_vehicle_observation(
    observation: VehicleObservation,
    context: ExclusionContext,
) -> bool:
    vehicle_product = normalize_vehicle_product(observation.product)
    if vehicle_product is None:
        return False
    lta_tag = normalize_lta_tag(observation.lta_tag)
    serial_number = normalize_serial_number(observation.serial_number)
    return any(
        excluded.vehicle_product == vehicle_product
        and (
            (excluded.lta_tag is not None and excluded.lta_tag == lta_tag)
            or (excluded.serial_number is not None and excluded.serial_number == serial_number)
        )
        for excluded in context.vehicle_identifiers
    )


def is_excluded_person(person: ExtractedPerson, context: ExclusionContext) -> bool:
    return (
        is_excluded_phone(person.get("phone"), context)
        or is_excluded_email(person.get("email"), context)
        or is_excluded_name(person.get("name"), context)
    )


def is_excluded_possible_person(
    person: ExtractedPossiblePerson,
    context: ExclusionContext,
) -> bool:
    if is_excluded_phone(person.get("phone"), context):
        return True
    if is_excluded_email(person.get("email"), context):
        return True
    if is_excluded_name(person.get("name"), context):
        return True
    return any(
        _is_excluded_strong_identifier(identifier, context)
        for identifier in person.get("identifiers", [])
    )


def _is_excluded_strong_identifier(
    identifier: ExtractedStrongIdentifier,
    context: ExclusionContext,
) -> bool:
    identifier_type = identifier.get("type")
    value = identifier.get("value")
    text_value = value if isinstance(value, str) else None
    if identifier_type == "phone":
        return is_excluded_phone(text_value, context)
    if identifier_type == "email":
        return is_excluded_email(text_value, context)
    if identifier_type == "source_customer_ref":
        return is_excluded_source_id(text_value, context)
    return False


def filter_extraction(
    extraction: ExtractionResult,
    context: ExclusionContext,
) -> ExtractionResult | None:
    persons = [
        person for person in extraction["persons"] if not is_excluded_person(person, context)
    ]
    possible_persons = [
        person
        for person in extraction.get("possible_persons", [])
        if not is_excluded_possible_person(person, context)
    ]
    strong_identifiers = [
        identifier
        for identifier in extraction.get("strong_identifiers", [])
        if not _is_excluded_strong_identifier(identifier, context)
    ]
    if not persons and not possible_persons and not strong_identifiers:
        return None
    filtered = extraction.copy()
    filtered["persons"] = persons
    filtered["possible_persons"] = possible_persons
    filtered["strong_identifiers"] = strong_identifiers
    return filtered
