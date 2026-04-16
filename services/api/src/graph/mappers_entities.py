"""Map raw Neo4j records to Entity domain models."""

from __future__ import annotations

from src.graph.converters import GraphRecord, to_int, to_optional_float, to_optional_str, to_str
from src.graph.mappers import _as_dict, map_person
from src.types import EntityPerson, EntitySummary


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
    )


def map_entity_person(record: GraphRecord) -> EntityPerson:
    """Map a raw person record (with phone confidence) to an EntityPerson."""
    person = map_person(record)
    return EntityPerson(
        **person.model_dump(),
        phone_confidence=to_optional_float(record.get("phone_confidence")),
    )
