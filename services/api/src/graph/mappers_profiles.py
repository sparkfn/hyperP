"""Neo4j record → Profile model mappers."""

from __future__ import annotations

from src.graph.converters import (
    GraphRecord,
    GraphValue,
    to_int,
    to_iso_or_none,
    to_optional_str,
    to_str,
)
from src.graph.mappers import map_person_identifier, map_source_record_list
from src.types import Profile, ProfileDetail, UnlinkedProfile


def _as_dict(value: GraphValue) -> GraphRecord:
    return value if isinstance(value, dict) else {}


def _profile_fields(record: GraphRecord) -> Profile:
    profile = _as_dict(record.get("profile"))
    return Profile(
        profile_id=to_str(profile.get("profile_id")),
        status=to_str(profile.get("status")),
        evidence_version=to_int(profile.get("evidence_version")),
        materialized_at=to_iso_or_none(profile.get("materialized_at")),
        promoted_person_id=to_optional_str(profile.get("promoted_person_id")),
        record_count=to_int(record.get("record_count")),
        identifier_count=to_int(record.get("identifier_count")),
    )


def map_profile(record: GraphRecord) -> Profile:
    """Map one Profile list row."""
    return _profile_fields(record)


def map_unlinked_profile(record: GraphRecord) -> UnlinkedProfile:
    """Map one unlinked-worklist row with its CRM-deal value score."""
    base = _profile_fields(record)
    return UnlinkedProfile(**base.model_dump(), value_score=to_int(record.get("value_score")))


def map_profile_detail(record: GraphRecord) -> ProfileDetail:
    """Map a Profile detail row with member SourceRecords and identifiers."""
    base = _profile_fields(record)
    raw_identifiers = record.get("identifiers")
    identifiers = (
        [
            map_person_identifier(_as_dict(item))
            for item in raw_identifiers
            if isinstance(item, dict)
        ]
        if isinstance(raw_identifiers, list)
        else []
    )
    return ProfileDetail(
        **base.model_dump(),
        source_records=map_source_record_list(record.get("source_records")),
        identifiers=identifiers,
    )
