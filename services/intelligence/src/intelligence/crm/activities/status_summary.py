"""Read-only aggregate summaries for CRM activity checkpoint status."""

from __future__ import annotations

from intelligence.crm.activities.models import ArchiveRecord


def parent_summary(records: tuple[ArchiveRecord, ...]) -> dict[str, int]:
    missing_stored = missing_graph = conflicting = resolved = 0
    for record in records:
        stored = record.stored_parent
        if stored.source_record_id is None:
            missing_stored += 1
            continue
        matches = tuple(
            parent
            for parent in record.child_parents
            if parent.source_record_id == stored.source_record_id
            and parent.source_instance_id == stored.source_instance_id
            and parent.record_type == stored.record_type
            and parent.source_system == stored.source_system
        )
        if len(matches) == 1 and len(record.child_parents) == 1:
            resolved += 1
        elif not matches:
            missing_graph += 1
        else:
            conflicting += 1
    return {
        "missing_stored": missing_stored,
        "missing_graph": missing_graph,
        "conflicting": conflicting,
        "resolved": resolved,
    }


def person_summary(records: tuple[ArchiveRecord, ...]) -> dict[str, int]:
    return {
        "missing_or_ambiguous": sum(
            1
            for item in records
            if len(item.people) != 1 or item.malformed_person_association_count > 0
        ),
        "malformed_association_count": sum(
            item.malformed_person_association_count for item in records
        ),
        "missing_revision": sum(
            1
            for item in records
            if (
                len(item.people) == 1
                and item.malformed_person_association_count == 0
                and item.people[0].revision is None
            )
        ),
    }


def duplicate_deliveries(value: dict[str, object] | None) -> int:
    if value is None:
        return 0
    count = value.get("count")
    if (
        set(value) != {"count"}
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
    ):
        raise ValueError("duplicate delivery evidence is invalid")
    return count
