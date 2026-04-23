"""Map raw Neo4j records to Entity domain models."""

from __future__ import annotations

from src.graph.converters import (
    GraphRecord,
    GraphValue,
    to_int,
    to_iso_or_none,
    to_optional_float,
    to_optional_str,
    to_str,
)
from src.graph.mappers import _as_dict, map_person
from src.types import EntityPerson, EntitySummary, ListedPerson, PersonEntitySummary


def map_entity_summary(record: GraphRecord) -> EntitySummary:
    """Map a raw entity record to an EntitySummary."""
    e = _as_dict(record.get("entity"))
    return EntitySummary(
        entity_key=to_str(e.get("entity_key")),
        display_name=to_optional_str(e.get("display_name")),
        entity_type=to_optional_str(e.get("entity_type")),
        country_code=to_optional_str(e.get("country_code")),
        is_active=bool(e.get("is_active", True)),
        person_count=to_int(record.get("person_count")),
        source_record_count=to_int(record.get("source_record_count")),
        last_ingested_at=to_iso_or_none(record.get("last_ingested_at")),
        active_review_cases=to_int(record.get("active_review_cases")),
    )


def map_person_entity(record: GraphRecord) -> PersonEntitySummary:
    """Map an entity record scoped to a single person."""
    e = _as_dict(record.get("entity"))
    return PersonEntitySummary(
        entity_key=to_str(e.get("entity_key")),
        display_name=to_optional_str(e.get("display_name")),
        entity_type=to_optional_str(e.get("entity_type")),
        country_code=to_optional_str(e.get("country_code")),
        is_active=bool(e.get("is_active", True)),
        source_record_count=to_int(record.get("source_record_count")),
    )


def map_entity_person(record: GraphRecord) -> EntityPerson:
    """Map a raw person record (with phone confidence) to an EntityPerson."""
    person = map_person(record)
    return EntityPerson(
        **person.model_dump(),
        phone_confidence=to_optional_float(record.get("phone_confidence")),
    )


def _map_person_entity_dict(raw: GraphValue) -> PersonEntitySummary:
    d = _as_dict(raw)
    return PersonEntitySummary(
        entity_key=to_str(d.get("entity_key")),
        display_name=to_optional_str(d.get("display_name")),
        entity_type=to_optional_str(d.get("entity_type")),
        country_code=to_optional_str(d.get("country_code")),
        is_active=bool(d.get("is_active", True)),
        source_record_count=to_int(d.get("source_record_count")),
    )


def map_listed_person(record: GraphRecord) -> ListedPerson:
    """Map a person record with inline entity list to a ListedPerson."""
    ep = map_entity_person(record)
    raw_entities: GraphValue = record.get("entities")
    entities: list[PersonEntitySummary] = (
        [_map_person_entity_dict(e) for e in raw_entities] if isinstance(raw_entities, list) else []
    )
    return ListedPerson(
        **ep.model_dump(),
        entities=entities,
        entity_count=to_int(record.get("entity_count", len(entities))),
        identifier_count=to_int(record.get("identifier_count")),
    )
