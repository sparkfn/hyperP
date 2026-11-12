"""Mapping helpers turning graph rows into projection rows and API payloads.

The projection must return the API documents verbatim, so these helpers
reproduce the API's ``src/graph/mappers.py`` semantics. The primitive
conversions live in :mod:`src.projection_sync_converters`.
"""

from __future__ import annotations

from src.models import JsonValue
from src.projection_sync_converters import (
    format_display_dob,
    to_bool,
    to_float,
    to_int,
    to_iso_or_empty,
    to_iso_or_none,
    to_iso_utc_z,
    to_optional_float,
    to_optional_str,
    to_str,
    to_utc_datetime,
)
from src.projection_sync_models import (
    AddressSummaryPayload,
    ChildRows,
    IdentityLinkRevisionPayload,
    IdentityLinkRevisionRow,
    ListedPersonPayload,
    PersonAddressRow,
    PersonConnectionRow,
    PersonEntitySummaryPayload,
    PersonIdentifierRow,
    PersonPayload,
    PersonProjectionRow,
    SourceRecordRow,
)


def map_address(value: object) -> AddressSummaryPayload | None:
    """Map a graph address projection to the API ``AddressSummary`` document."""
    address = _as_dict(value)
    if not address.get("address_id"):
        return None
    return AddressSummaryPayload(
        address_id=to_str(address.get("address_id")),
        unit_number=to_optional_str(address.get("unit_number")),
        street_number=to_optional_str(address.get("street_number")),
        street_name=to_optional_str(address.get("street_name")),
        city=to_optional_str(address.get("city")),
        postal_code=to_optional_str(address.get("postal_code")),
        country_code=to_optional_str(address.get("country_code")),
        normalized_full=to_optional_str(address.get("normalized_full")),
    )


def map_person_entity(value: object) -> PersonEntitySummaryPayload:
    """Map a graph entity projection to the API ``PersonEntitySummary`` document."""
    entity = _as_dict(value)
    return PersonEntitySummaryPayload(
        entity_key=to_str(entity.get("entity_key")),
        display_name=to_optional_str(entity.get("display_name")),
        entity_type=to_optional_str(entity.get("entity_type")),
        country_code=to_optional_str(entity.get("country_code")),
        is_active=to_bool(entity.get("is_active", True)),
        source_record_count=to_int(entity.get("source_record_count")),
    )


def build_list_payload(row: dict[str, object]) -> ListedPersonPayload:
    """Build the API ``ListedPerson`` document from a projection list row."""
    person = _as_dict(row.get("person"))
    entities = _as_list(row.get("entities"))
    dob_display, dob_invalid = format_display_dob(to_optional_str(person.get("preferred_dob")))
    return ListedPersonPayload(
        person_id=to_str(person.get("person_id")),
        status=to_str(person.get("status"), "active"),
        is_high_value=to_bool(person.get("is_high_value")),
        is_high_risk=to_bool(person.get("is_high_risk")),
        preferred_full_name=to_optional_str(person.get("preferred_full_name")),
        preferred_phone=to_optional_str(person.get("preferred_phone")),
        preferred_email=to_optional_str(person.get("preferred_email")),
        preferred_dob=to_optional_str(person.get("preferred_dob")),
        preferred_address=map_address(row.get("preferred_address")),
        preferred_nric=to_optional_str(person.get("preferred_nric")),
        preferred_race_ethnicity=to_optional_str(person.get("preferred_race_ethnicity")),
        profile_completeness_score=to_float(person.get("profile_completeness_score")),
        golden_profile_computed_at=to_iso_or_none(person.get("golden_profile_computed_at")),
        golden_profile_version=to_optional_str(person.get("golden_profile_version")),
        source_record_count=to_int(row.get("source_record_count")),
        connection_count=to_int(row.get("connection_count")),
        lifetime_value=None,
        loyalty=None,
        vehicles=None,
        created_at=to_iso_or_empty(person.get("created_at")),
        updated_at=to_iso_or_empty(person.get("updated_at")),
        phone_confidence=to_optional_float(row.get("phone_confidence")),
        entities=[map_person_entity(entity) for entity in entities],
        entity_count=to_int(row.get("entity_count", len(entities))),
        identifier_count=to_int(row.get("identifier_count")),
        possible_match_count=to_int(row.get("possible_match_count")),
        system_match_count=to_int(row.get("system_match_count")),
        order_count=to_int(row.get("order_count")),
        crm_deal_count=to_int(row.get("crm_deal_count")),
        bankruptcy_case_count=to_int(row.get("bankruptcy_case_count")),
        preferred_dob_display=dob_display,
        preferred_dob_invalid=dob_invalid,
    )


def build_detail_payload(row: dict[str, object]) -> PersonPayload:
    """Build the API ``Person`` document from a projection detail row."""
    person = _as_dict(row.get("person"))
    return PersonPayload(
        person_id=to_str(person.get("person_id")),
        status=to_str(person.get("status"), "active"),
        is_high_value=to_bool(person.get("is_high_value")),
        is_high_risk=to_bool(person.get("is_high_risk")),
        preferred_full_name=to_optional_str(person.get("preferred_full_name")),
        preferred_phone=to_optional_str(person.get("preferred_phone")),
        preferred_email=to_optional_str(person.get("preferred_email")),
        preferred_dob=to_optional_str(person.get("preferred_dob")),
        preferred_address=map_address(row.get("preferred_address")),
        preferred_nric=to_optional_str(person.get("preferred_nric")),
        preferred_race_ethnicity=to_optional_str(person.get("preferred_race_ethnicity")),
        profile_completeness_score=to_float(person.get("profile_completeness_score")),
        golden_profile_computed_at=to_iso_or_none(person.get("golden_profile_computed_at")),
        golden_profile_version=to_optional_str(person.get("golden_profile_version")),
        source_record_count=to_int(row.get("source_record_count")),
        connection_count=to_int(row.get("connection_count")),
        lifetime_value=to_optional_float(row.get("lifetime_value")),
        loyalty=None,
        vehicles=None,
        created_at=to_iso_or_empty(person.get("created_at")),
        updated_at=to_iso_or_empty(person.get("updated_at")),
    )


def build_revision_payload(row: dict[str, object]) -> IdentityLinkRevisionPayload:
    """Build the public identity-link revision document from a graph row."""
    return IdentityLinkRevisionPayload(
        event_id=to_str(row.get("event_id")),
        global_revision=to_int(row.get("global_revision")),
        source_system=to_str(row.get("source_system")),
        source_instance_id=to_str(row.get("source_instance_id")),
        source_entity_type=to_str(row.get("source_entity_type")),
        source_entity_id=to_str(row.get("source_entity_id")),
        identity_policy_version=to_str(row.get("identity_policy_version")),
        link_status=to_str(row.get("link_status")),
        hyperp_person_id=to_optional_str(row.get("hyperp_person_id")),
        resolution_kind=to_str(row.get("resolution_kind")),
        resolution_revision=to_int(row.get("resolution_revision")),
        effective_at=to_iso_utc_z(row.get("effective_at")),
        match_decision_id=to_optional_str(row.get("match_decision_id")),
        review_case_id=to_optional_str(row.get("review_case_id")),
        supersedes_event_id=to_optional_str(row.get("supersedes_event_id")),
    )


def build_person_row(
    list_row: dict[str, object],
    list_payload: ListedPersonPayload,
    detail_payload: PersonPayload,
) -> PersonProjectionRow:
    """Build the typed parent row and its two API payloads from graph rows."""
    person = _as_dict(list_row.get("person"))
    return PersonProjectionRow(
        person_id=to_str(person.get("person_id")),
        canonical_id=to_optional_str(list_row.get("canonical_id")),
        status=to_str(person.get("status"), "active"),
        preferred_full_name=to_optional_str(person.get("preferred_full_name")),
        preferred_phone=to_optional_str(person.get("preferred_phone")),
        preferred_email=to_optional_str(person.get("preferred_email")),
        preferred_dob=to_optional_str(person.get("preferred_dob")),
        preferred_nric=to_optional_str(person.get("preferred_nric")),
        preferred_address_id=to_optional_str(list_row.get("preferred_address_id")),
        updated_at=to_utc_datetime(person.get("updated_at")),
        # The column keeps NULL (the API list excludes it); the JSON payloads keep 0.0.
        profile_completeness_score=to_optional_float(person.get("profile_completeness_score")),
        is_high_value=to_bool(person.get("is_high_value")),
        is_high_risk=to_bool(person.get("is_high_risk")),
        source_record_count=to_int(list_row.get("source_record_count")),
        connection_count=to_int(list_row.get("connection_count")),
        entity_count=to_int(list_row.get("entity_count")),
        identifier_count=to_int(list_row.get("identifier_count")),
        possible_match_count=to_int(list_row.get("possible_match_count")),
        system_match_count=to_int(list_row.get("system_match_count")),
        order_count=to_int(list_row.get("order_count")),
        crm_deal_count=to_int(list_row.get("crm_deal_count")),
        bankruptcy_case_count=to_int(list_row.get("bankruptcy_case_count")),
        phone_confidence=to_optional_float(list_row.get("phone_confidence")),
        list_payload=list_payload,
        detail_payload=detail_payload,
    )


def build_child_rows(
    identifiers: list[dict[str, object]],
    addresses: list[dict[str, object]],
    connections: list[dict[str, object]],
    source_records: list[dict[str, object]],
) -> ChildRows:
    """Build the child collection rows for one batch of persons."""
    return ChildRows(
        identifiers=tuple(
            PersonIdentifierRow(
                person_id=to_str(row.get("person_id")),
                identifier_type=to_str(row.get("identifier_type")),
                normalized_value=to_str(row.get("normalized_value")),
                is_active=to_bool(row.get("is_active")),
                is_verified=to_bool(row.get("is_verified")),
                payload=_identifier_payload(row),
            )
            for row in identifiers
        ),
        addresses=tuple(
            PersonAddressRow(
                person_id=to_str(row.get("person_id")),
                address_id=to_str(row.get("address_id")),
                is_active=to_bool(row.get("is_active")),
                unit_number=to_optional_str(row.get("unit_number")),
                street_name=to_optional_str(row.get("street_name")),
                city=to_optional_str(row.get("city")),
                postal_code=to_optional_str(row.get("postal_code")),
                country_code=to_optional_str(row.get("country_code")),
                payload=_address_payload(row),
            )
            for row in addresses
        ),
        connections=tuple(
            PersonConnectionRow(
                person_id=to_str(row.get("person_id")),
                connected_person_id=to_str(row.get("connected_person_id")),
                hops=to_int(row.get("hops"), 1),
                connection_type=to_str(row.get("connection_type")),
                payload=_connection_payload(row),
            )
            for row in connections
        ),
        source_records=tuple(
            SourceRecordRow(
                source_record_pk=to_str(row.get("source_record_pk")),
                person_id=to_str(row.get("person_id")),
                entity_key=to_optional_str(row.get("entity_key")),
                source_key=to_optional_str(row.get("source_key")),
                record_type=to_optional_str(row.get("record_type")),
                lifecycle_status=to_str(row.get("lifecycle_status"), "inactive"),
                is_active=to_bool(row.get("is_active")),
                observed_at=to_utc_datetime(row.get("observed_at")),
                payload=_source_record_payload(row),
            )
            for row in source_records
        ),
    )


def _identifier_payload(row: dict[str, object]) -> dict[str, JsonValue]:
    """Return the payload document of one projected identifier."""
    return {
        "last_confirmed_at": to_iso_or_none(row.get("last_confirmed_at")),
        "source_system_key": to_optional_str(row.get("source_system_key")),
    }


def _address_payload(row: dict[str, object]) -> dict[str, JsonValue]:
    """Return the payload document of one projected address."""
    return {
        "street_number": to_optional_str(row.get("street_number")),
        "normalized_full": to_optional_str(row.get("normalized_full")),
    }


def _connection_payload(row: dict[str, object]) -> dict[str, JsonValue]:
    """Return the payload document of one projected connection."""
    return {}


def _source_record_payload(row: dict[str, object]) -> dict[str, JsonValue]:
    """Return the payload document of one projected source record."""
    return {
        "source_record_id": to_optional_str(row.get("source_record_id")),
        "source_record_version": to_optional_str(row.get("source_record_version")),
        "link_status": to_optional_str(row.get("link_status")),
        "ingested_at": to_iso_or_none(row.get("ingested_at")),
    }


def build_revision_row(row: dict[str, object]) -> IdentityLinkRevisionRow:
    """Build one identity-link revision row from a graph row."""
    return IdentityLinkRevisionRow(
        event_id=to_str(row.get("event_id")),
        global_revision=to_int(row.get("global_revision")),
        link_key=to_str(row.get("link_key")),
        revision=build_revision_payload(row),
    )


def _as_dict(value: object) -> dict[str, object]:
    """Return a graph value as a row mapping, or an empty row."""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _as_list(value: object) -> list[object]:
    """Return a graph value as a list, or an empty list."""
    if isinstance(value, list):
        return list(value)
    return []
