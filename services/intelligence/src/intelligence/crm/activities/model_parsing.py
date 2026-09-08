"""Boundary parsing and safe projection coercion for activity archive models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeGuard

from intelligence.crm.activities.models import (
    ArchiveRecord,
    ArchiveRequest,
    BoundaryEntry,
    ParentReference,
    PersonReference,
    RecordKind,
    SealedBoundary,
    _id,
    _text,
)

_REQUEST_KEYS = frozenset(
    {
        "snapshot_id",
        "source_instance_id",
        "source_key",
        "page_size",
        "max_rows",
        "max_pages",
        "database_identity",
        "selection_contract_version",
        "max_references_per_record",
    }
)


def parse_request(value: Mapping[str, object]) -> ArchiveRequest:
    if set(value) != _REQUEST_KEYS:
        raise ValueError("archive request has unexpected fields")
    return ArchiveRequest(
        _required(value, "snapshot_id"),
        _required(value, "source_instance_id"),
        _required(value, "source_key"),
        _integer(value, "page_size"),
        _integer(value, "max_rows"),
        _integer(value, "max_pages"),
        _required(value, "database_identity"),
        _required(value, "selection_contract_version"),
        _integer(value, "max_references_per_record"),
    )


def parse_boundary(value: Mapping[str, object]) -> SealedBoundary:
    if (
        set(value) != {"schema_version", "request", "entries", "digest"}
        or value.get("schema_version") != "crm-activities-boundary-v1"
    ):
        raise ValueError("boundary evidence is malformed")
    request_value = _mapping(value.get("request"), "boundary request")
    entries_value = _items(value.get("entries"), "boundary entries")
    entries: list[BoundaryEntry] = []
    for item in entries_value:
        entry = _mapping(item, "boundary entry")
        if set(entry) != {
            "source_record_pk",
            "record_type",
            "record_digest",
            "reference_fingerprint",
        }:
            raise ValueError("boundary entry has unexpected fields")
        entries.append(
            BoundaryEntry(
                _required(entry, "source_record_pk"),
                _record_kind(_required(entry, "record_type")),
                _required(entry, "record_digest"),
                _required(entry, "reference_fingerprint"),
            )
        )
    request = parse_request(request_value)
    result = SealedBoundary(request, tuple(entries))
    if value.get("digest") != result.digest:
        raise ValueError("boundary digest is corrupt")
    return result


def record_from_mapping(value: Mapping[str, object]) -> ArchiveRecord:
    return ArchiveRecord(
        _required(value, "source_record_pk"),
        _required(value, "source_record_id"),
        _required(value, "source_record_version"),
        _required(value, "source_version_key"),
        _required(value, "record_hash"),
        _required(value, "source_instance_id"),
        _required(value, "source_key"),
        _record_kind(_required(value, "record_type")),
        _text(value.get("lifecycle_status"), "lifecycle_status"),
        _text(value.get("history_family"), "history_family"),
        _text(value.get("history_kind"), "history_kind"),
        _text(value.get("history_source"), "history_source"),
        _text(value.get("projection_version"), "projection_version"),
        _text(value.get("projection_source"), "projection_source"),
        _text(value.get("event_at"), "event_at"),
        _text(value.get("observed_at"), "observed_at"),
        _text(value.get("available_at"), "available_at"),
        _parent(value.get("stored_parent"), "STORED_PARENT"),
        _parents(value.get("child_parents"), "CHILD_OF"),
        _parents(value.get("details_parents"), "DETAILS_HISTORY_ITEM"),
        _people(value.get("people")),
        _capabilities(value.get("user_capabilities")),
        _text(value.get("ingested_at"), "ingested_at"),
        _text(value.get("link_status"), "link_status"),
    )


def _required(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} is required")
    return result


def _integer(value: Mapping[str, object], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool):
        raise ValueError(f"{key} is required")
    return result


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not _is_string_mapping(value):
        raise ValueError(f"{field} must be a mapping")
    return value


def _is_string_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping) and all(isinstance(key, str) for key in value)


def _items(value: object, field: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list or tuple")
    return tuple(value)


def _record_kind(value: str) -> RecordKind:
    if value == "crm_history":
        return "crm_history"
    if value == "call":
        return "call"
    raise ValueError("unsupported query record type")


def _parent(value: object, relationship: str) -> ParentReference:
    if value is None:
        return ParentReference(None, None, None, None, relationship, None)
    item = _mapping(value, "parent")
    return ParentReference(
        _text(item.get("source_record_pk"), "parent pk"),
        _text(item.get("source_instance_id"), "parent instance"),
        _text(item.get("source_record_id"), "parent id"),
        _text(item.get("record_type"), "parent type"),
        relationship,
        _text(item.get("source_system"), "parent source_system"),
    )


def _parents(value: object, relationship: str) -> tuple[ParentReference, ...]:
    if value is None:
        return ()
    items = _items(value, "parent collection")
    result = tuple(
        sorted(
            (_parent(item, relationship) for item in items if item is not None),
            key=ParentReference.key,
        )
    )
    if len({item.key() for item in result}) != len(result):
        raise ValueError("duplicate parent evidence")
    return result


def _people(value: object) -> tuple[PersonReference, ...]:
    if value is None:
        return ()
    items = _items(value, "people")
    people: list[PersonReference] = []
    for item in items:
        if item is None:
            continue
        person = _mapping(item, "person")
        people.append(
            PersonReference(
                _required(person, "person_id"),
                _text(person.get("status"), "person status"),
                _text(person.get("revision"), "person revision"),
                _person_association(person),
            )
        )
    result = tuple(sorted(people, key=PersonReference.key))
    if len({item.key() for item in result}) != len(result):
        raise ValueError("duplicate Person evidence")
    return result


def _capabilities(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    items = _items(value, "user capabilities")
    capabilities: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError("user capabilities must be strings")
        capabilities.append(item)
    result = tuple(sorted(set(capabilities)))
    for item in result:
        _id(item, "user capability")
    return result


def _person_association(person: Mapping[str, object]) -> str | None:
    association = person.get("association_source_record_pk")
    if association is None:
        association = person.get("source_record_pk")
    return _text(association, "person association pk")
