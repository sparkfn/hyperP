"""Boundary parsing and safe projection coercion for activity archive models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

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


def parse_request(value: Mapping[str, object]) -> ArchiveRequest:
    return ArchiveRequest(
        _required(value, "snapshot_id"),
        _required(value, "source_instance_id"),
        _required(value, "source_key"),
        _integer(value, "page_size"),
        _integer(value, "max_rows"),
        _integer(value, "max_pages"),
    )


def parse_boundary(value: Mapping[str, object]) -> SealedBoundary:
    request_value = value.get("request")
    entries_value = value.get("entries")
    if not isinstance(request_value, Mapping) or not isinstance(entries_value, list):
        raise ValueError("boundary evidence is malformed")
    entries: list[BoundaryEntry] = []
    for item in entries_value:
        if not isinstance(item, Mapping):
            raise ValueError("boundary entry is malformed")
        record_type = _required(cast(Mapping[str, object], item), "record_type")
        if record_type not in {"crm_history", "call"}:
            raise ValueError("boundary entry record type is malformed")
        entries.append(
            BoundaryEntry(
                _required(cast(Mapping[str, object], item), "source_record_pk"),
                cast(RecordKind, record_type),
                _required(cast(Mapping[str, object], item), "record_digest"),
                _required(cast(Mapping[str, object], item), "reference_fingerprint"),
            )
        )
    request = parse_request(cast(Mapping[str, object], request_value))
    result = SealedBoundary(request, tuple(entries))
    if value.get("digest") != result.digest:
        raise ValueError("boundary digest is corrupt")
    return result


def record_from_mapping(value: Mapping[str, object]) -> ArchiveRecord:
    record_type = _required(value, "record_type")
    if record_type not in {"crm_history", "call"}:
        raise ValueError("unsupported query record type")
    return ArchiveRecord(
        _required(value, "source_record_pk"),
        _required(value, "source_record_id"),
        _required(value, "source_record_version"),
        _required(value, "source_version_key"),
        _required(value, "record_hash"),
        _required(value, "source_instance_id"),
        _required(value, "source_key"),
        cast(RecordKind, record_type),
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
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    return cast(Mapping[str, object], value)


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
    if not isinstance(value, list):
        raise ValueError("parent collection must be a list")
    result = tuple(
        sorted(
            (_parent(item, relationship) for item in value if item is not None),
            key=ParentReference.key,
        )
    )
    if len({item.key() for item in result}) != len(result):
        raise ValueError("duplicate parent evidence")
    return result


def _people(value: object) -> tuple[PersonReference, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("people must be a list")
    people: list[PersonReference] = []
    for item in value:
        if item is None:
            continue
        person = _mapping(item, "person")
        people.append(
            PersonReference(
                _required(person, "person_id"),
                _text(person.get("status"), "person status"),
                _text(person.get("revision"), "person revision"),
                _text(person.get("source_record_pk"), "person association pk"),
            )
        )
    result = tuple(sorted(people, key=PersonReference.key))
    if len({item.key() for item in result}) != len(result):
        raise ValueError("duplicate Person evidence")
    return result


def _capabilities(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("user capabilities must be strings")
    result = tuple(sorted(set(cast(list[str], value))))
    for item in result:
        _id(item, "user capability")
    return result
